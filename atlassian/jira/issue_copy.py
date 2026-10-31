# coding=utf-8
"""Cross-project issue copying for incident retrospectives.

The flow is split in two phases so the same plan drives both a dry review and
an actual copy:

1. :meth:`IssueCopyMixin.build_issue_copy_checklist` reads the source issue and
   the target project's create metadata, then produces a checklist. Fields the
   target project / issue type does not accept (unknown field or a value absent
   from its ``allowedValues``) are marked ``unsupported`` instead of being sent.
   ``status`` and ``assignee`` are never copied.
2. :meth:`IssueCopyMixin.copy_issue_to_project` creates the target issue either
   from the plain fields dict (``use_builder=False``) or from the per-type
   :class:`~atlassian.models.jira.IssueBuilder` usage (``use_builder=True``),
   then copies attachments, comments (in original order) and issue links.

Re-running the copy is idempotent: the new issue carries an ``issue-copy-source``
property pointing at the source key, already present attachments/comments/links
are skipped and only the missing pieces are added. The source issue is only
read, never modified.

Works with both the legacy Server/Data Center client (also in cloud-compatible
mode) and the versioned :class:`~atlassian.jira.jira_cloud.JiraCloud` client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from typing import Protocol

    class _JiraCopyClient(Protocol):
        """Surface the copy mixin needs; supplied by the legacy Jira or JiraCloud client."""

        api_version: Any
        no_check_headers: Dict[str, str]

        def get_issue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def create_issue(self, *args: Any, **kwargs: Any) -> Any: ...
        def set_issue_property(self, *args: Any, **kwargs: Any) -> Any: ...
        def get_issue_property(self, *args: Any, **kwargs: Any) -> Any: ...
        def jql(self, *args: Any, **kwargs: Any) -> Any: ...
        def enhanced_jql(self, *args: Any, **kwargs: Any) -> Any: ...
        def issue_createmeta_issuetypes(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def issue_createmeta_fieldtypes(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def get_create_issue_meta_issue_types(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def get_create_issue_meta_issue_type_id(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def issue_get_comments(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def get_comments(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def issue_add_comment(self, *args: Any, **kwargs: Any) -> Any: ...
        def add_comment(self, *args: Any, **kwargs: Any) -> Any: ...
        def add_attachment_object(self, *args: Any, **kwargs: Any) -> Any: ...
        def create_issue_link(self, *args: Any, **kwargs: Any) -> Any: ...
        def link_issues(self, *args: Any, **kwargs: Any) -> Any: ...

        def resource_url(self, resource: str, api_root: Optional[str] = ..., api_version: Any = ...) -> str: ...
        def get(self, path: str, **kwargs: Any) -> Any: ...
        def post(self, path: str, **kwargs: Any) -> Any: ...

        # Internal mixin helpers, declared so annotated self sees cross-calls.
        def build_issue_copy_checklist(self, *args: Any, **kwargs: Any) -> "IssueCopyChecklist": ...
        def _build_copy_plan(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def _load_create_metadata(self, *args: Any, **kwargs: Any) -> Any: ...
        def _select_issue_type(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]: ...
        def _find_existing_copy(self, *args: Any, **kwargs: Any) -> Optional[str]: ...
        def _stamp_copy_source(self, *args: Any, **kwargs: Any) -> None: ...
        def _fill_copied_pieces(self, *args: Any, **kwargs: Any) -> None: ...
        def _copy_one_attachment(self, *args: Any, **kwargs: Any) -> None: ...
        def _compat_get_issue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def _compat_create_issue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def _compat_createmeta_issue_types(self, *args: Any, **kwargs: Any) -> Dict[str, Any]: ...
        def _compat_createmeta_fields(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]: ...
        def _fetch_all_comments(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]: ...
        def _compat_add_comment(self, *args: Any, **kwargs: Any) -> None: ...
        def _compat_upload_attachment(self, *args: Any, **kwargs: Any) -> None: ...
        def _compat_create_link(self, *args: Any, **kwargs: Any) -> None: ...

        @property
        def _is_legacy_jira_client(self) -> bool: ...


log = logging.getLogger(__name__)

#: Property stamped on the new issue so a repeated run can find it again.
COPY_PROPERTY_KEY = "issue-copy-source"

#: Policy exclusions: these are intentionally never carried over.
POLICY_SKIP_FIELDS = {"assignee", "status"}

POLICY_SKIP_REASONS = {
    "assignee": "Assignee is not copied by policy",
    "status": "Status is not copied by policy; the new issue starts in its initial status",
}

#: Jira-managed / read-only fields that cannot be set when creating an issue.
READ_ONLY_FIELDS = {
    "resolution",
    "resolutiondate",
    "created",
    "updated",
    "lastViewed",
    "watches",
    "worklog",
    "attachment",
    "comment",
    "issuelinks",
    "subtasks",
    "statuscategorychangedate",
    "progress",
    "aggregateprogress",
    "votes",
    "thumbnail",
    "timespent",
    "timeestimate",
    "aggregatetimeoriginalestimate",
    "aggregatetimeestimate",
    "timeoriginalestimate",
    "workratio",
    "issuerestriction",
}

#: System fields copied verbatim (after stripping entity references) when the
#: target project accepts them. Everything else must use a ``customfield_*`` id.
COPYABLE_SYSTEM_FIELDS = {
    "summary",
    "description",
    "priority",
    "labels",
    "components",
    "fixVersions",
    "versions",
    "duedate",
    "parent",
    "reporter",
    "environment",
    "timetracking",
    "security",
}

#: Display names for standard fields when create metadata is unavailable.
FIELD_LABELS = {
    "summary": "Summary",
    "description": "Description",
    "project": "Project",
    "issuetype": "Issue Type",
    "priority": "Priority",
    "labels": "Labels",
    "components": "Components",
    "fixVersions": "Fix Versions",
    "versions": "Affected Versions",
    "duedate": "Due Date",
    "parent": "Parent",
    "reporter": "Reporter",
    "assignee": "Assignee",
    "status": "Status",
    "environment": "Environment",
    "timetracking": "Time Tracking",
    "security": "Security Level",
}

STATUS_COPY = "copy"
STATUS_UNSUPPORTED = "unsupported"
STATUS_SKIPPED = "skipped"
STATUS_EXISTS = "exists"
STATUS_COPIED = "copied"
STATUS_FAILED = "failed"


@dataclass
class CopyFieldItem:
    """One field row in the copy checklist."""

    field_id: str
    field_name: str
    status: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"field_id": self.field_id, "field_name": self.field_name, "status": self.status, "reason": self.reason}


@dataclass
class CopyCommentItem:
    """One comment row. Comments are copied in the original order."""

    comment_id: str
    author: Optional[str]
    created: Optional[str]
    status: str = STATUS_COPY
    reason: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comment_id": self.comment_id,
            "author": self.author,
            "created": self.created,
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass
class CopyAttachmentItem:
    """One attachment row."""

    attachment_id: str
    filename: str
    size: Optional[int]
    status: str = STATUS_COPY
    reason: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "size": self.size,
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass
class CopyLinkItem:
    """One issue-link row (a business association of the source issue)."""

    link_type: str
    direction: str
    source_issue_key: str
    target_issue_key: str
    status: str = STATUS_COPY
    reason: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "link_type": self.link_type,
            "direction": self.direction,
            "source_issue_key": self.source_issue_key,
            "target_issue_key": self.target_issue_key,
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass
class IssueCopyChecklist:
    """The full copy plan for one source issue -> target project pair."""

    source_key: str
    target_project: str
    target_issue_type_name: Optional[str]
    target_issue_type_id: Optional[str]
    metadata_available: bool
    fields: List[CopyFieldItem] = field(default_factory=list)
    comments: List[CopyCommentItem] = field(default_factory=list)
    attachments: List[CopyAttachmentItem] = field(default_factory=list)
    links: List[CopyLinkItem] = field(default_factory=list)
    required_missing: List[str] = field(default_factory=list)

    @property
    def unsupported_fields(self) -> List[CopyFieldItem]:
        return [item for item in self.fields if item.status == STATUS_UNSUPPORTED]

    @property
    def skipped_fields(self) -> List[CopyFieldItem]:
        return [item for item in self.fields if item.status == STATUS_SKIPPED]

    @property
    def copied_fields(self) -> List[CopyFieldItem]:
        return [item for item in self.fields if item.status in (STATUS_COPY, STATUS_EXISTS, STATUS_COPIED)]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_key": self.source_key,
            "target_project": self.target_project,
            "target_issue_type_name": self.target_issue_type_name,
            "target_issue_type_id": self.target_issue_type_id,
            "metadata_available": self.metadata_available,
            "fields": [item.to_dict() for item in self.fields],
            "comments": [item.to_dict() for item in self.comments],
            "attachments": [item.to_dict() for item in self.attachments],
            "links": [item.to_dict() for item in self.links],
            "required_missing": list(self.required_missing),
        }


@dataclass
class IssueCopyResult:
    """Outcome of a copy run: the new issue details plus the executed checklist."""

    source_key: str
    target_key: Optional[str]
    created: bool
    checklist: IssueCopyChecklist
    issue: Optional[Dict[str, Any]] = None

    @property
    def failed_attachments(self) -> List[CopyAttachmentItem]:
        return [item for item in self.checklist.attachments if item.status == STATUS_FAILED]

    @property
    def failed_comments(self) -> List[CopyCommentItem]:
        return [item for item in self.checklist.comments if item.status == STATUS_FAILED]

    @property
    def failed_links(self) -> List[CopyLinkItem]:
        return [item for item in self.checklist.links if item.status == STATUS_FAILED]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_key": self.source_key,
            "target_key": self.target_key,
            "created": self.created,
            "issue": self.issue,
            "checklist": self.checklist.to_dict(),
        }


# ---------------------------------------------------------------------------
# Pure planning helpers (no HTTP) - unit tested directly.
# ---------------------------------------------------------------------------


def _entity_ref(value: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an entity reference to the minimal shape Jira accepts on create."""
    if "accountId" in value and value.get("accountId"):
        return {"accountId": value["accountId"]}
    if "key" in value and value.get("key"):
        return {"key": value["key"]}
    if "name" in value and value.get("name") is not None:
        return {"name": value["name"]}
    if "id" in value and value.get("id") is not None:
        return {"id": str(value["id"])}
    if "value" in value and value.get("value") is not None:
        return {"value": value["value"]}
    return dict(value)


def _sanitize_system_field(field_id: str, value: Any) -> Any:
    if field_id in ("components", "fixVersions", "versions") and isinstance(value, list):
        return [_entity_ref(item) if isinstance(item, dict) else item for item in value]
    if field_id in ("priority", "parent", "reporter", "security") and isinstance(value, dict):
        return _entity_ref(value)
    return value


def _option_tokens(option: Dict[str, Any]) -> List[str]:
    tokens = []
    for key in ("value", "name", "id", "key", "accountId"):
        if option.get(key) is not None:
            tokens.append(str(option[key]))
    return tokens


def _value_is_allowed(value: Any, allowed_values: List[Dict[str, Any]]) -> bool:
    """Check one create value against the field metadata's allowedValues."""
    allowed = set()
    for option in allowed_values:
        if isinstance(option, dict):
            allowed.update(_option_tokens(option))
        else:
            allowed.add(str(option))

    def matches(single: Any) -> bool:
        if isinstance(single, dict):
            return any(
                str(single[key]) in allowed
                for key in ("value", "name", "id", "key", "accountId")
                if single.get(key) is not None
            )
        return str(single) in allowed

    if isinstance(value, list):
        return all(matches(item) for item in value)
    return matches(value)


def _field_label(field_id: str, accepted_fields: Optional[Dict[str, Dict[str, Any]]]) -> str:
    if accepted_fields and field_id in accepted_fields and accepted_fields[field_id].get("name"):
        return str(accepted_fields[field_id]["name"])
    return FIELD_LABELS.get(field_id, field_id)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def build_field_plan(
    source_fields: Dict[str, Any],
    target_project: str,
    target_issue_type: Dict[str, Any],
    accepted_fields: Optional[Dict[str, Dict[str, Any]]],
    link_key_map: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], List[CopyFieldItem], List[str]]:
    """Plan which source fields are copied, skipped or unsupported.

    :param source_fields: the ``fields`` block of the source Jira issue.
    :param target_project: target project key.
    :param target_issue_type: ``{"id": ..., "name": ...}`` of the target type
        (id optional when create metadata is unavailable).
    :param accepted_fields: field create metadata keyed by field id, or ``None``
        when metadata could not be loaded.
    :param link_key_map: optional source->target issue key map used to redirect
        the ``parent`` reference of copied sub-tasks.
    :return: ``(fields_payload, checklist_items, required_missing)``.
    """
    link_key_map = link_key_map or {}
    items: List[CopyFieldItem] = []
    payload: Dict[str, Any] = {"project": {"key": target_project}}
    if target_issue_type.get("id"):
        payload["issuetype"] = {"id": str(target_issue_type["id"])}
    else:
        payload["issuetype"] = {"name": target_issue_type.get("name")}

    items.append(CopyFieldItem("project", FIELD_LABELS["project"], STATUS_COPY, "Redirected to the target project"))
    items.append(
        CopyFieldItem(
            "issuetype",
            FIELD_LABELS["issuetype"],
            STATUS_COPY,
            "Created with the same issue type in the target project",
        )
    )

    for field_id, raw_value in source_fields.items():
        if field_id in ("project", "issuetype") or _is_empty(raw_value):
            continue
        label = _field_label(field_id, accepted_fields)

        if field_id in POLICY_SKIP_FIELDS:
            items.append(CopyFieldItem(field_id, label, STATUS_SKIPPED, POLICY_SKIP_REASONS[field_id]))
            continue
        if field_id in READ_ONLY_FIELDS:
            items.append(
                CopyFieldItem(
                    field_id, label, STATUS_SKIPPED, "Jira-managed field; it cannot be set when creating an issue"
                )
            )
            continue

        is_custom = field_id.startswith("customfield_")
        if not is_custom and field_id not in COPYABLE_SYSTEM_FIELDS:
            items.append(
                CopyFieldItem(field_id, label, STATUS_SKIPPED, "Unknown system field; not part of the copy whitelist")
            )
            continue

        if accepted_fields is not None and field_id not in accepted_fields:
            items.append(
                CopyFieldItem(
                    field_id, label, STATUS_UNSUPPORTED, "The target project/issue type does not accept this field"
                )
            )
            continue

        value = raw_value

        if field_id == "parent" and isinstance(value, dict):
            parent_key = value.get("key")
            mapped_key = link_key_map.get(parent_key, parent_key) if parent_key else None
            if mapped_key and not mapped_key.startswith(target_project + "-"):
                items.append(
                    CopyFieldItem(
                        field_id,
                        label,
                        STATUS_SKIPPED,
                        f"Parent {mapped_key} belongs to another project; pass it via link_key_map to copy the hierarchy",
                    )
                )
                continue
            value = {"key": mapped_key} if mapped_key else _sanitize_system_field(field_id, value)
        else:
            value = _sanitize_system_field(field_id, raw_value) if not is_custom else raw_value

        if accepted_fields is not None:
            meta = accepted_fields.get(field_id, {})
            allowed = meta.get("allowedValues")
            if allowed and not _value_is_allowed(raw_value, allowed):
                items.append(
                    CopyFieldItem(
                        field_id,
                        label,
                        STATUS_UNSUPPORTED,
                        "The source value is not among the target project's allowed values for this field",
                    )
                )
                continue

        payload[field_id] = value
        items.append(CopyFieldItem(field_id, label, STATUS_COPY))

    required_missing: List[str] = []
    if accepted_fields:
        for field_id, meta in accepted_fields.items():
            if meta.get("required") and field_id not in payload:
                required_missing.append(str(meta.get("name") or field_id))

    return payload, items, required_missing


def build_fields_with_builder(fields_payload: Dict[str, Any], issue_type_name: Optional[str]) -> Dict[str, Any]:
    """Rebuild a plain fields payload with the per-type IssueBuilder usage.

    Registered types (Task, Bug, Story, Epic, Sub-task) use their dedicated
    builder; any other type name uses the generic builder with an explicit
    issue type. The returned fields dict is semantically identical to the
    plain-dict input, so both styles create the same issue.
    """
    from atlassian.models.jira import IssueBuilder, JiraIssue, issue_type_for

    type_name = issue_type_name or ""
    try:
        builder = IssueBuilder(issue_type_for(type_name))
    except ValueError:
        builder = IssueBuilder(JiraIssue).issue_type(name=type_name)

    project = fields_payload.get("project") or {}
    builder.project(project.get("key"), id_=project.get("id"))

    if fields_payload.get("summary") is not None:
        builder.summary(fields_payload["summary"])

    description = fields_payload.get("description")
    if description is not None:
        if isinstance(description, dict):
            builder.description_adf(description)
        else:
            builder.description(description)

    priority = fields_payload.get("priority")
    if isinstance(priority, dict):
        builder.priority(priority.get("name"), id_=priority.get("id"))

    if fields_payload.get("labels"):
        builder.labels(*fields_payload["labels"])

    for component in fields_payload.get("components", []) or []:
        builder.add_component(component.get("name"), id_=component.get("id"))

    for version in fields_payload.get("fixVersions", []) or []:
        builder.add_fix_version(version.get("name"), id_=version.get("id"))

    for version in fields_payload.get("versions", []) or []:
        builder.add_affected_version(version.get("name"), id_=version.get("id"))

    if fields_payload.get("duedate"):
        builder.due_date(fields_payload["duedate"])

    parent = fields_payload.get("parent")
    if isinstance(parent, dict):
        builder.parent(parent.get("key"), id_=parent.get("id"))

    reporter = fields_payload.get("reporter")
    if isinstance(reporter, dict):
        builder.reporter(account_id=reporter.get("accountId"), name=reporter.get("name"))

    handled = {
        "project",
        "issuetype",
        "summary",
        "description",
        "priority",
        "labels",
        "components",
        "fixVersions",
        "versions",
        "duedate",
        "parent",
        "reporter",
    }
    for field_id, value in fields_payload.items():
        if field_id in handled or _is_empty(value):
            continue
        # Generic setter: custom fields (incl. epic link / story points),
        # environment, timetracking and any other accepted field id.
        builder.custom_field(field_id, value)

    return builder.build_dict()


def normalize_comment(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a raw comment object from either REST platform."""
    body = raw.get("body")
    if body is None:
        return None
    author = raw.get("author") or {}
    return {
        "id": str(raw.get("id")),
        "author": author.get("displayName") or author.get("name") or author.get("accountId"),
        "created": raw.get("created"),
        "body": body,
    }


def plan_comments(raw_comments: List[Dict[str, Any]]) -> List[CopyCommentItem]:
    items = []
    for raw in raw_comments:
        comment = normalize_comment(raw)
        if comment is None:
            continue
        items.append(CopyCommentItem(comment["id"], comment["author"], comment["created"]))
    return items


def plan_attachments(raw_attachments: List[Dict[str, Any]]) -> List[CopyAttachmentItem]:
    items = []
    for raw in raw_attachments:
        if raw.get("id") is None:
            continue
        attachment_id = str(raw.get("id"))
        filename = raw.get("filename") or attachment_id
        items.append(CopyAttachmentItem(attachment_id, filename, raw.get("size")))
    return items


def normalize_link(raw: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """Return ``(link_type_name, direction, other_issue_key)`` for a raw link."""
    link_type = (raw.get("type") or {}).get("name")
    if not link_type:
        return None
    if raw.get("outwardIssue"):
        return link_type, "outward", str(raw["outwardIssue"].get("key"))
    if raw.get("inwardIssue"):
        return link_type, "inward", str(raw["inwardIssue"].get("key"))
    return None


def plan_links(raw_links: List[Dict[str, Any]], link_key_map: Optional[Dict[str, str]] = None) -> List[CopyLinkItem]:
    link_key_map = link_key_map or {}
    items: List[CopyLinkItem] = []
    for raw in raw_links:
        normalized = normalize_link(raw)
        if normalized is None:
            continue
        link_type, direction, other_key = normalized
        target_key = link_key_map.get(other_key, other_key)
        items.append(CopyLinkItem(link_type, direction, other_key, target_key))
    return items


def canonical_body(body: Any) -> str:
    if isinstance(body, (dict, list)):
        return json.dumps(body, sort_keys=True, ensure_ascii=False)
    return str(body)


def existing_comment_bodies(raw_comments: List[Dict[str, Any]]) -> set:
    bodies = set()
    for raw in raw_comments:
        comment = normalize_comment(raw)
        if comment is not None:
            bodies.add(canonical_body(comment["body"]))
    return bodies


def link_signature(link_type: str, direction: str, issue_key: str) -> Tuple[str, str, str]:
    return link_type, direction, issue_key


def existing_link_signatures(raw_links: List[Dict[str, Any]]) -> set:
    signatures = set()
    for raw in raw_links:
        normalized = normalize_link(raw)
        if normalized is not None:
            signatures.add(link_signature(*normalized))
    return signatures


def jql_quoted(value: str) -> str:
    """Quote and escape a literal for use inside a JQL string."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Client mixin (Jira Server/DC in any mode and Jira Cloud).
# ---------------------------------------------------------------------------


class IssueCopyMixin:
    """High-level cross-project issue copy operations."""

    # -- public API --------------------------------------------------------

    def build_issue_copy_checklist(
        self: "_JiraCopyClient",
        source_key: str,
        target_project: str,
        target_issue_type: Optional[str] = None,
        link_key_map: Optional[Dict[str, str]] = None,
    ) -> IssueCopyChecklist:
        """Build (but do not execute) the copy checklist.

        :param source_key: issue key to copy from.
        :param target_project: destination project key.
        :param target_issue_type: destination issue type name or id; defaults
            to the source issue type name.
        :param link_key_map: optional source->target key map for parent and
            issue links when a whole set of issues is copied together.
        """
        plan = self._build_copy_plan(source_key, target_project, target_issue_type, link_key_map or {})
        return plan["checklist"]

    def copy_issue_to_project(
        self: "_JiraCopyClient",
        source_key: str,
        target_project: str,
        target_issue_type: Optional[str] = None,
        *,
        use_builder: bool = False,
        target_key: Optional[str] = None,
        link_key_map: Optional[Dict[str, str]] = None,
        include_comments: bool = True,
        include_attachments: bool = True,
        include_links: bool = True,
    ) -> IssueCopyResult:
        """Copy an existing issue into another project.

        Re-running the call (or passing ``target_key`` of the previous run)
        only fills the missing comments, attachments and links; the source
        issue is never modified.

        :param use_builder: build the create payload via the per-type
            :class:`~atlassian.models.jira.IssueBuilder` instead of the plain
            fields dict. Both styles produce the same issue.
        """
        link_key_map = dict(link_key_map or {})
        plan = self._build_copy_plan(source_key, target_project, target_issue_type, link_key_map)
        checklist = plan["checklist"]
        fields_payload = plan["fields_payload"]
        source_attachments = plan["source_attachments"]
        source_comments = plan["source_comments"]

        if not include_comments:
            for item in checklist.comments:
                item.status = STATUS_SKIPPED
                item.reason = "Comment copy disabled"
        if not include_attachments:
            for item in checklist.attachments:
                item.status = STATUS_SKIPPED
                item.reason = "Attachment copy disabled"
        if not include_links:
            for item in checklist.links:
                item.status = STATUS_SKIPPED
                item.reason = "Issue link copy disabled"

        created = False
        if target_key is None:
            target_key = self._find_existing_copy(target_project, fields_payload.get("summary", ""), source_key)

        if target_key is None:
            if use_builder:
                create_payload = build_fields_with_builder(fields_payload, checklist.target_issue_type_name)
            else:
                create_payload = fields_payload
            response = self._compat_create_issue(create_payload) or {}
            target_key = response.get("key") or response.get("id")
            if not target_key:
                raise RuntimeError(f"Jira did not return a key for the new issue in {target_project}")
            created = True
            self._stamp_copy_source(str(target_key), source_key)
        else:
            created = False
            for item in checklist.fields:
                if item.status == STATUS_COPY:
                    item.status = STATUS_EXISTS
            log.info('Resuming existing copy "%s" of "%s"; only missing items will be added', target_key, source_key)
            self._stamp_copy_source(str(target_key), source_key)

        self._fill_copied_pieces(
            target_key=str(target_key),
            checklist=checklist,
            source_comments=source_comments,
            source_attachments=source_attachments,
            include_comments=include_comments,
            include_attachments=include_attachments,
            include_links=include_links,
        )

        issue_details = self._compat_get_issue(str(target_key))
        return IssueCopyResult(
            source_key=source_key,
            target_key=str(target_key),
            created=created,
            issue=issue_details,
            checklist=checklist,
        )

    # -- planning ----------------------------------------------------------

    def _build_copy_plan(
        self: "_JiraCopyClient",
        source_key: str,
        target_project: str,
        target_issue_type: Optional[str],
        link_key_map: Dict[str, str],
    ) -> Dict[str, Any]:
        source = self._compat_get_issue(source_key)
        source_fields = source.get("fields") or {}

        source_type_name = ((source_fields.get("issuetype") or {}).get("name")) if source_fields else None
        selected_type, accepted_fields, metadata_available = self._load_create_metadata(
            target_project, target_issue_type or source_type_name
        )

        fields_payload, field_items, required_missing = build_field_plan(
            source_fields,
            target_project,
            selected_type,
            accepted_fields,
            link_key_map=link_key_map,
        )

        source_comments = self._fetch_all_comments(source_key)
        source_attachments = list(source_fields.get("attachment") or [])
        source_links = list(source_fields.get("issuelinks") or [])

        checklist = IssueCopyChecklist(
            source_key=source_key,
            target_project=target_project,
            target_issue_type_name=selected_type.get("name"),
            target_issue_type_id=selected_type.get("id"),
            metadata_available=metadata_available,
            fields=field_items,
            comments=plan_comments(source_comments),
            attachments=plan_attachments(source_attachments),
            links=plan_links(source_links, link_key_map=link_key_map),
            required_missing=required_missing,
        )
        return {
            "checklist": checklist,
            "fields_payload": fields_payload,
            "source_comments": source_comments,
            "source_attachments": source_attachments,
        }

    def _load_create_metadata(
        self: "_JiraCopyClient", project: str, type_name_or_id: Optional[str]
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Dict[str, Any]]], bool]:
        """Resolve target issue type and accepted fields from create metadata."""
        try:
            raw_types = self._compat_createmeta_issue_types(project)
            types = raw_types.get("values") or raw_types.get("issueTypes") or []
            selected = self._select_issue_type(types, type_name_or_id)
            if selected is None:
                available = ", ".join(str(item.get("name")) for item in types) or "n/a"
                raise ValueError(
                    f"Issue type {type_name_or_id!r} is not available in project {project}. Available: {available}"
                )

            fields_pages = self._compat_createmeta_fields(project, str(selected["id"]))
            accepted: Dict[str, Dict[str, Any]] = {}
            for raw_field in fields_pages:
                field_id = raw_field.get("fieldId") or raw_field.get("id")
                if field_id:
                    accepted[str(field_id)] = raw_field
            return selected, accepted, True
        except ValueError:
            raise
        except Exception as error:  # metadata endpoint may be blocked/removed
            log.warning("Create metadata unavailable for %s/%s: %s", project, type_name_or_id, error)
            selected = {"name": type_name_or_id}
            return selected, None, False

    @staticmethod
    def _select_issue_type(types: List[Dict[str, Any]], name_or_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not name_or_id:
            return None
        for item in types:
            if str(item.get("id")) == str(name_or_id):
                return {"id": str(item.get("id")), "name": item.get("name")}
        for item in types:
            if (item.get("name") or "").lower() == str(name_or_id).lower():
                return {"id": str(item.get("id")), "name": item.get("name")}
        return None

    # -- resume / idempotency ---------------------------------------------

    def _find_existing_copy(
        self: "_JiraCopyClient", target_project: str, summary: str, source_key: str
    ) -> Optional[str]:
        try:
            jql = f"project = {jql_quoted(target_project)} AND summary ~ {jql_quoted(summary)}"
            if self._is_legacy_jira_client:
                response = self.jql(jql, fields="summary", limit=50) or {}
            else:
                response = self.enhanced_jql(jql, fields="summary", limit=50) or {}
            candidates = [issue.get("key") for issue in response.get("issues", []) if issue.get("key")]
        except Exception as error:
            log.warning("Copy lookup search failed, a new issue will be created: %s", error)
            return None

        matches = []
        for key in candidates:
            try:
                prop = self.get_issue_property(key, COPY_PROPERTY_KEY) or {}
            except Exception:
                continue
            value = prop.get("value") if isinstance(prop, dict) else None
            if isinstance(value, dict) and value.get("sourceKey") == source_key:
                matches.append(key)
        if len(matches) > 1:
            raise RuntimeError(f"Multiple existing copies of {source_key} found in {target_project}: {matches}")
        return matches[0] if matches else None

    def _stamp_copy_source(self: "_JiraCopyClient", target_key: str, source_key: str) -> None:
        try:
            self.set_issue_property(target_key, COPY_PROPERTY_KEY, {"sourceKey": source_key})
        except Exception as error:
            # The property is an idempotency aid only; never fail the copy for it.
            log.warning("Could not stamp copy-source property on %s: %s", target_key, error)

    # -- piece-by-piece copy (resume-safe) ---------------------------------

    def _fill_copied_pieces(
        self: "_JiraCopyClient",
        target_key: str,
        checklist: IssueCopyChecklist,
        source_comments: List[Dict[str, Any]],
        source_attachments: List[Dict[str, Any]],
        include_comments: bool,
        include_attachments: bool,
        include_links: bool,
    ) -> None:
        target = self._compat_get_issue(target_key, fields="attachment,issuelinks")
        target_fields = (target or {}).get("fields") or {}
        existing_filenames = {item.get("filename") for item in target_fields.get("attachment") or []}
        existing_links = existing_link_signatures(target_fields.get("issuelinks") or [])

        if include_attachments:
            attachment_meta = {str(item.get("id")): item for item in source_attachments}
            for attachment_item in checklist.attachments:
                if attachment_item.filename in existing_filenames:
                    attachment_item.status = STATUS_EXISTS
                    attachment_item.reason = "Already present on the target issue"
                    continue
                meta = attachment_meta.get(attachment_item.attachment_id)
                try:
                    self._copy_one_attachment(target_key, attachment_item.filename, meta)
                    attachment_item.status = STATUS_COPIED
                except Exception as error:  # keep going; a re-run uploads only what failed
                    attachment_item.status = STATUS_FAILED
                    attachment_item.error = str(error)
                    log.warning("Attachment %s -> %s failed: %s", attachment_item.filename, target_key, error)

        if include_links:
            for link_item in checklist.links:
                signature = link_signature(link_item.link_type, link_item.direction, link_item.target_issue_key)
                if signature in existing_links:
                    link_item.status = STATUS_EXISTS
                    link_item.reason = "Link already present on the target issue"
                    continue
                if link_item.target_issue_key == target_key:
                    link_item.status = STATUS_SKIPPED
                    link_item.reason = "Link would point back to the copied issue itself"
                    continue
                try:
                    self._compat_create_link(link_item.link_type, link_item.direction, link_item.target_issue_key)
                    link_item.status = STATUS_COPIED
                    existing_links.add(signature)
                except Exception as error:
                    link_item.status = STATUS_FAILED
                    link_item.error = str(error)
                    log.warning("Link %s -> %s failed: %s", link_item.target_issue_key, target_key, error)

        if include_comments:
            existing_bodies = existing_comment_bodies(self._fetch_all_comments(target_key))
            # Comments must appear in the original order; append only bodies
            # not already present on the target issue.
            for raw in source_comments:
                comment = normalize_comment(raw)
                if comment is None:
                    continue
                comment_item = next((entry for entry in checklist.comments if entry.comment_id == comment["id"]), None)
                if canonical_body(comment["body"]) in existing_bodies:
                    if comment_item is not None:
                        comment_item.status = STATUS_EXISTS
                        comment_item.reason = "Identical comment already present on the target issue"
                    continue
                try:
                    self._compat_add_comment(target_key, comment["body"])
                    existing_bodies.add(canonical_body(comment["body"]))
                    if comment_item is not None:
                        comment_item.status = STATUS_COPIED
                except Exception as error:
                    if comment_item is not None:
                        comment_item.status = STATUS_FAILED
                        comment_item.error = str(error)
                    log.warning("Comment %s -> %s failed: %s", comment["id"], target_key, error)

    def _copy_one_attachment(
        self: "_JiraCopyClient", target_key: str, filename: str, meta: Optional[Dict[str, Any]]
    ) -> None:
        if not meta or not meta.get("content"):
            raise ValueError("Source attachment metadata has no downloadable content URL")
        content = self.get(
            meta["content"],
            not_json_response=True,
            absolute=True,
            headers={"Accept": "*/*"},
        )
        stream = BytesIO(content)
        self._compat_upload_attachment(target_key, stream, filename)

    # -- platform adapters -------------------------------------------------

    @property
    def _is_legacy_jira_client(self: "_JiraCopyClient") -> bool:
        # The legacy Jira client (Server/DC, incl. cloud-compatible mode)
        # exposes issue_get_comments; the versioned JiraCloud client does not.
        return hasattr(self, "issue_get_comments")

    def _compat_get_issue(self: "_JiraCopyClient", key: str, fields: Optional[str] = None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"update_history": False}
        if fields is not None:
            kwargs["fields"] = fields
        return self.get_issue(key, **kwargs)

    def _compat_create_issue(self: "_JiraCopyClient", fields_payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._is_legacy_jira_client:
            return self.create_issue(fields_payload)
        return self.create_issue(data={"fields": fields_payload})

    def _compat_createmeta_issue_types(self: "_JiraCopyClient", project: str) -> Dict[str, Any]:
        if self._is_legacy_jira_client:
            return self.issue_createmeta_issuetypes(project)
        return self.get_create_issue_meta_issue_types(project)

    def _compat_createmeta_fields(self: "_JiraCopyClient", project: str, issue_type_id: str) -> List[Dict[str, Any]]:
        values: List[Dict[str, Any]] = []
        start = 0
        page_size = 50
        while True:
            if self._is_legacy_jira_client:
                page = self.issue_createmeta_fieldtypes(project, issue_type_id, start=start, limit=page_size)
            else:
                page = self.get_create_issue_meta_issue_type_id(
                    project, issue_type_id, start_at=start, max_results=page_size
                )
            batch = page.get("values") or page.get("fields") or []
            values.extend(batch)
            total = page.get("total")
            if "fields" in page or page.get("isLast") is True:
                break
            if not batch:
                break
            if total is None:
                # Legacy single-page responses carry neither total nor isLast.
                break
            if len(values) >= int(total):
                break
            start += len(batch)
        return values

    def _fetch_all_comments(self: "_JiraCopyClient", issue_key: str) -> List[Dict[str, Any]]:
        if self._is_legacy_jira_client:
            response = self.issue_get_comments(issue_key) or {}
            return list(response.get("comments") or [])

        comments: List[Dict[str, Any]] = []
        start = 0
        page_size = 50
        while True:
            response = self.get_comments(issue_key, start_at=start, max_results=page_size) or {}
            batch = response.get("comments") or []
            comments.extend(batch)
            total = response.get("total")
            if response.get("isLast") is True or not batch:
                break
            if total is not None and len(comments) >= int(total):
                break
            start += len(batch)
        return comments

    def _compat_add_comment(self: "_JiraCopyClient", target_key: str, body: Any) -> None:
        if self._is_legacy_jira_client:
            self.issue_add_comment(target_key, body)
        else:
            self.add_comment(target_key, data={"body": body})

    def _compat_upload_attachment(self: "_JiraCopyClient", target_key: str, stream: BytesIO, filename: str) -> None:
        if self._is_legacy_jira_client:
            self.add_attachment_object(target_key, stream, filename=filename)
            return
        url = self.resource_url(f"issue/{target_key}/attachments", api_root="rest/api", api_version=self.api_version)
        self.post(url, files={"file": (filename, stream)}, headers=self.no_check_headers)

    def _compat_create_link(self: "_JiraCopyClient", link_type: str, direction: str, other_issue_key: str) -> None:
        payload: Dict[str, Any] = {"type": {"name": link_type}}
        if direction == "outward":
            payload["outwardIssue"] = {"key": other_issue_key}
        else:
            payload["inwardIssue"] = {"key": other_issue_key}
        if self._is_legacy_jira_client:
            self.create_issue_link(payload)
        else:
            self.link_issues(data=payload)

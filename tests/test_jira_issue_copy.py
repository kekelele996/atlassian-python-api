# coding=utf-8
"""Tests for cross-project issue copy (incident retrospective cloning).

All tests are mock based: no Jira instance, Docker, MySQL or network access.
"""

from __future__ import annotations

from io import BytesIO
from unittest import mock

import pytest

from atlassian import Jira, JiraCloud
from atlassian.jira.issue_copy import (
    COPY_PROPERTY_KEY,
    IssueCopyMixin,
    build_field_plan,
    build_fields_with_builder,
    canonical_body,
    existing_comment_bodies,
    existing_link_signatures,
    jql_quoted,
    plan_attachments,
    plan_comments,
    plan_links,
)

CLOUD_URL = "https://example.atlassian.net"
SERVER_URL = "https://jira.example.com"

COMMENT_1 = {
    "id": "101",
    "body": "first comment",
    "created": "2026-10-01T10:00:00.000+0000",
    "author": {"displayName": "Ana"},
}
COMMENT_2 = {
    "id": "102",
    "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": []}]},
    "created": "2026-10-02T10:00:00.000+0000",
    "author": {"displayName": "Bob"},
}

SOURCE_ATTACHMENTS = [
    {"id": "200", "filename": "a.log", "size": 10, "content": "https://x/secure/attachment/200/a.log"},
    {"id": "201", "filename": "b.png", "size": 20, "content": "https://x/secure/attachment/201/b.png"},
]

SOURCE_LINKS = [
    {
        "id": "300",
        "type": {"name": "Relates", "inward": "relates", "outward": "relates"},
        "outwardIssue": {"key": "SRC-2"},
    },
    {
        "id": "301",
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "inwardIssue": {"key": "SRC-3"},
    },
]

SOURCE_FIELDS = {
    "project": {"key": "SRC", "id": "100"},
    "issuetype": {"id": "10001", "name": "Task"},
    "summary": "Postmortem: outage 2026-10-30",
    "description": "Root cause details",
    "priority": {"name": "High", "iconUrl": "https://x/icons/high.png"},
    "labels": ["incident"],
    "components": [{"self": "https://x/c/1", "name": "Platform"}],
    "reporter": {"accountId": "r-1", "displayName": "Reporter"},
    "assignee": {"accountId": "a-1", "displayName": "Assignee"},
    "status": {"name": "Closed"},
    "resolution": {"name": "Done"},
    "customfield_10050": [{"value": "P1"}],
    "customfield_10060": "legacy-only-value",
    "attachment": SOURCE_ATTACHMENTS,
    "issuelinks": SOURCE_LINKS,
}

SOURCE_ISSUE = {"id": "10001", "key": "SRC-1", "fields": SOURCE_FIELDS}

CREATE_TYPES = {"values": [{"id": "10101", "name": "Task"}]}
CREATE_FIELDS_META = {
    "isLast": True,
    "total": 8,
    "values": [
        {"fieldId": "summary", "name": "Summary", "required": True},
        {"fieldId": "description", "name": "Description", "required": False},
        {"fieldId": "priority", "name": "Priority", "allowedValues": [{"value": "High"}, {"value": "Low"}]},
        {"fieldId": "labels", "name": "Labels"},
        {"fieldId": "components", "name": "Components", "allowedValues": [{"name": "Platform"}, {"name": "API"}]},
        {"fieldId": "reporter", "name": "Reporter"},
        {"fieldId": "customfield_10050", "name": "Severity", "allowedValues": [{"value": "P1"}, {"value": "P2"}]},
        {"fieldId": "customfield_10070", "name": "Reviewer", "required": True},
    ],
}


# ---------------------------------------------------------------------------
# Pure planning logic
# ---------------------------------------------------------------------------


def test_field_plan_redirects_project_and_type_and_strips_entity_refs():
    accepted = {row["fieldId"]: row for row in CREATE_FIELDS_META["values"]}
    payload, items, required_missing = build_field_plan(SOURCE_FIELDS, "TGT", {"id": "10101", "name": "Task"}, accepted)

    assert payload["project"] == {"key": "TGT"}
    assert payload["issuetype"] == {"id": "10101"}
    assert payload["summary"] == "Postmortem: outage 2026-10-30"
    assert payload["priority"] == {"name": "High"}
    assert payload["components"] == [{"name": "Platform"}]
    assert payload["reporter"] == {"accountId": "r-1"}
    assert payload["labels"] == ["incident"]
    assert payload["customfield_10050"] == [{"value": "P1"}]
    assert "Reviewer" in required_missing

    by_id = {item.field_id: item for item in items}
    assert by_id["status"].status == "skipped"
    assert by_id["assignee"].status == "skipped"
    assert "not copied" in by_id["assignee"].reason and "not copied" in by_id["status"].reason
    assert by_id["resolution"].status == "skipped"
    assert by_id["customfield_10060"].status == "unsupported"
    assert "does not accept" in by_id["customfield_10060"].reason
    # Policy fields never leak into the create payload.
    assert "status" not in payload and "assignee" not in payload


def test_field_plan_flags_values_missing_from_allowed_values():
    fields = dict(SOURCE_FIELDS)
    fields["components"] = [{"name": "Platform"}, {"name": "LegacyTeam"}]
    accepted = {row["fieldId"]: row for row in CREATE_FIELDS_META["values"]}

    payload, items, _ = build_field_plan(fields, "TGT", {"id": "10101", "name": "Task"}, accepted)

    by_id = {item.field_id: item for item in items}
    assert by_id["components"].status == "unsupported"
    assert "allowed values" in by_id["components"].reason
    assert "components" not in payload


def test_field_plan_without_metadata_copies_whitelist_and_custom_fields():
    fields = dict(SOURCE_FIELDS)
    fields["environment"] = "prod"
    fields["mysterySystemField"] = 1

    payload, items, required_missing = build_field_plan(fields, "TGT", {"name": "Task"}, None)

    assert payload["environment"] == "prod"
    assert payload["customfield_10060"] == "legacy-only-value"
    assert "mysterySystemField" not in payload
    by_id = {item.field_id: item for item in items}
    assert by_id["mysterySystemField"].status == "skipped"
    assert required_missing == []


def test_parent_is_skipped_across_projects_and_redirected_with_map():
    fields = dict(SOURCE_FIELDS)
    fields["parent"] = {"key": "SRC-90", "id": "90"}

    payload, items, _ = build_field_plan(fields, "TGT", {"id": "10101", "name": "Sub-task"}, None)
    assert "parent" not in payload
    assert {i.field_id for i in items if i.status == "skipped" and i.field_id == "parent"}

    payload, _, _ = build_field_plan(fields, "TGT", {"id": "10101", "name": "Sub-task"}, None, {"SRC-90": "TGT-90"})
    assert payload["parent"] == {"key": "TGT-90"}

    fields["parent"] = {"key": "TGT-2"}
    payload, _, _ = build_field_plan(fields, "TGT", {"id": "10101", "name": "Sub-task"}, None)
    assert payload["parent"] == {"key": "TGT-2"}


def test_builder_payload_matches_plain_payload_for_known_type():
    accepted = {row["fieldId"]: row for row in CREATE_FIELDS_META["values"]}
    plain, _, _ = build_field_plan(SOURCE_FIELDS, "TGT", {"id": "10101", "name": "Task"}, accepted)

    built = build_fields_with_builder(plain, "Task")

    assert built.pop("issuetype") == {"name": "Task"}
    plain.pop("issuetype")
    assert built == plain


def test_builder_supports_unregistered_type_and_adf_description():
    payload = {
        "project": {"key": "TGT"},
        "issuetype": {"id": "10200"},
        "summary": "Incident",
        "description": {"type": "doc", "version": 1, "content": []},
        "customfield_10050": [{"value": "P1"}],
        "environment": "prod",
    }
    built = build_fields_with_builder(payload, "Incident")
    assert built["issuetype"] == {"name": "Incident"}
    assert built["description"] == payload["description"]
    assert built["customfield_10050"] == payload["customfield_10050"]
    assert built["environment"] == "prod"


def test_comment_link_and_attachment_planning_helpers():
    comments = plan_comments([COMMENT_1, COMMENT_2, {"id": "103"}])
    assert [c.comment_id for c in comments] == ["101", "102"]

    bodies = existing_comment_bodies([COMMENT_1])
    assert canonical_body("first comment") in bodies
    assert canonical_body(COMMENT_2["body"]) not in bodies

    attachments = plan_attachments(SOURCE_ATTACHMENTS)
    assert [a.filename for a in attachments] == ["a.log", "b.png"]

    links = plan_links(SOURCE_LINKS, {"SRC-2": "TGT-2"})
    assert links[0].direction == "outward" and links[0].target_issue_key == "TGT-2"
    assert links[1].direction == "inward" and links[1].target_issue_key == "SRC-3"
    assert ("Blocks", "inward", "SRC-3") in existing_link_signatures(SOURCE_LINKS)

    assert jql_quoted('say "hi"') == '"say \\"hi\\""'


# ---------------------------------------------------------------------------
# Client-side helpers / mock fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_client():
    return JiraCloud(CLOUD_URL)


@pytest.fixture
def server_client():
    return Jira(SERVER_URL, username="user", password="pass", cloud=False)


def _target_state(attachments, links, comments):
    return {
        "id": "90001",
        "key": "TGT-7",
        "fields": {"attachment": attachments, "issuelinks": links, "comment": {"comments": comments}},
    }


def patch_copy(client, *, legacy=False, **state):
    """Patch every HTTP-touching method used by a copy run.

    ``legacy=True`` patches the Server/DC wrapper method names instead of the
    versioned JiraCloud ones.
    """
    target_attachments = state.get("target_attachments") or []
    target_links = state.get("target_links") or []
    target_comments = state.get("target_comments") or []
    search_candidates = state.get("search_candidates") or []
    upload_failure_for = state.get("upload_failure_for")

    def get_issue(key, **kwargs):
        if key == "SRC-1":
            return SOURCE_ISSUE
        return _target_state(target_attachments, target_links, target_comments)

    def get_comments(key, **kwargs):
        if key == "SRC-1":
            return {"comments": [COMMENT_1, COMMENT_2], "total": 2, "isLast": True}
        return {"comments": list(target_comments), "total": len(target_comments), "isLast": True}

    def post_attachment(path, files=None, **kwargs):
        filename = files["file"][0]
        if upload_failure_for and filename in upload_failure_for:
            raise RuntimeError(f"upload failed: {filename}")
        return [{"id": "999", "filename": filename}]

    def get_issue_property(key, property_key):
        raise RuntimeError("404 property not found")

    create_issue = mock.Mock(return_value={"id": "90001", "key": "TGT-7"})
    add_comment = mock.Mock(return_value={"id": "new"})
    create_link = mock.Mock(return_value=None)
    search = mock.Mock(return_value={"issues": search_candidates, "isLast": True})

    common = {
        "get_issue": mock.Mock(side_effect=get_issue),
        "create_issue": create_issue,
        "set_issue_property": mock.Mock(return_value=None),
        "get_issue_property": mock.Mock(side_effect=get_issue_property),
        "post": mock.Mock(side_effect=post_attachment),
        "get": mock.Mock(return_value=b"file-bytes"),
    }
    if legacy:
        common.update(
            {
                "issue_createmeta_issuetypes": mock.Mock(return_value=CREATE_TYPES),
                "issue_createmeta_fieldtypes": mock.Mock(return_value=CREATE_FIELDS_META),
                "issue_get_comments": mock.Mock(side_effect=get_comments),
                "jql": search,
                "issue_add_comment": add_comment,
                "create_issue_link": create_link,
            }
        )

        def legacy_upload(key, stream, filename=None):
            return common["post"](f"rest/api/2/issue/{key}/attachments", files={"file": (filename, stream)})

        common["add_attachment_object"] = mock.Mock(side_effect=legacy_upload)
    else:
        common.update(
            {
                "get_create_issue_meta_issue_types": mock.Mock(return_value=CREATE_TYPES),
                "get_create_issue_meta_issue_type_id": mock.Mock(return_value=CREATE_FIELDS_META),
                "get_comments": mock.Mock(side_effect=get_comments),
                "enhanced_jql": search,
                "add_comment": add_comment,
                "link_issues": create_link,
            }
        )

    patchers = [mock.patch.object(client, name, m) for name, m in common.items()]
    for patcher in patchers:
        patcher.start()

    # Stable aliases the tests assert against on both platforms.
    common["add_comment_mock"] = add_comment
    common["create_link_mock"] = create_link
    common["search_mock"] = search

    def stop():
        for patcher in patchers:
            patcher.stop()

    common["_stop"] = stop
    return common


def patch_cloud_copy(client, **kwargs):
    return patch_copy(client, legacy=False, **kwargs)


def patch_server_copy(client, **kwargs):
    return patch_copy(client, legacy=True, **kwargs)


# ---------------------------------------------------------------------------
# Cloud client behaviour
# ---------------------------------------------------------------------------


def test_mixin_mounted_on_both_clients(cloud_client, server_client):
    assert isinstance(cloud_client, IssueCopyMixin)
    assert isinstance(server_client, IssueCopyMixin)


def test_build_checklist_flags_unsupported_fields_without_writing(cloud_client):
    with (
        mock.patch.object(cloud_client, "get_issue", return_value=SOURCE_ISSUE),
        mock.patch.object(cloud_client, "get_create_issue_meta_issue_types", return_value=CREATE_TYPES),
        mock.patch.object(cloud_client, "get_create_issue_meta_issue_type_id", return_value=CREATE_FIELDS_META),
        mock.patch.object(
            cloud_client,
            "get_comments",
            return_value={"comments": [COMMENT_1, COMMENT_2], "total": 2, "isLast": True},
        ),
    ):
        checklist = cloud_client.build_issue_copy_checklist("SRC-1", "TGT")

    assert checklist.source_key == "SRC-1"
    assert checklist.target_issue_type_name == "Task"
    assert checklist.metadata_available is True
    unsupported = {item.field_id for item in checklist.unsupported_fields}
    assert "customfield_10060" in unsupported
    assert {item.field_id for item in checklist.skipped_fields} >= {"status", "assignee", "resolution"}
    assert [c.comment_id for c in checklist.comments] == ["101", "102"]
    assert [a.filename for a in checklist.attachments] == ["a.log", "b.png"]


def test_cloud_copy_creates_issue_and_copies_pieces_in_order(cloud_client):
    mocks = patch_cloud_copy(cloud_client)
    try:
        result = cloud_client.copy_issue_to_project("SRC-1", "TGT")

        assert result.created is True
        assert result.target_key == "TGT-7"
        assert result.issue["key"] == "TGT-7"

        mocks["create_issue"].assert_called_once()
        payload = mocks["create_issue"].call_args.kwargs["data"]["fields"]
        assert payload["project"] == {"key": "TGT"}
        assert payload["summary"] == SOURCE_FIELDS["summary"]
        assert payload["description"] == SOURCE_FIELDS["description"]
        assert payload["issuetype"] == {"id": "10101"}
        assert "status" not in payload and "assignee" not in payload
        assert "customfield_10060" not in payload

        # Comments copied in source order, ADF body passed through unchanged.
        comment_bodies = [call.kwargs["data"]["body"] for call in mocks["add_comment"].call_args_list]
        assert comment_bodies == ["first comment", COMMENT_2["body"]]

        # Both business links recreated with their direction and type.
        link_payloads = [call.kwargs["data"] for call in mocks["link_issues"].call_args_list]
        assert link_payloads[0] == {"type": {"name": "Relates"}, "outwardIssue": {"key": "SRC-2"}}
        assert link_payloads[1] == {"type": {"name": "Blocks"}, "inwardIssue": {"key": "SRC-3"}}

        upload_paths = [call.args[0] for call in mocks["post"].call_args_list]
        assert upload_paths == [
            "rest/api/3/issue/TGT-7/attachments",
            "rest/api/3/issue/TGT-7/attachments",
        ]
        uploaded = [call.kwargs["files"]["file"][0] for call in mocks["post"].call_args_list]
        assert uploaded == ["a.log", "b.png"]

        mocks["set_issue_property"].assert_called_once_with("TGT-7", COPY_PROPERTY_KEY, {"sourceKey": "SRC-1"})

        checklist = result.checklist
        assert {a.filename: a.status for a in checklist.attachments} == {"a.log": "copied", "b.png": "copied"}
        assert [c.status for c in checklist.comments] == ["copied", "copied"]
        assert [item.status for item in checklist.links] == ["copied", "copied"]

        # The source key must never appear as the target of a mutating call;
        # set_issue_property only stamps the NEW issue with the source key.
        for name, m in mocks.items():
            if name in {
                "get_issue",
                "get_comments",
                "enhanced_jql",
                "get_issue_property",
                "get",
                "get_create_issue_meta_issue_types",
                "get_create_issue_meta_issue_type_id",
                "set_issue_property",
                "_stop",
            }:
                continue
            for call in m.call_args_list:
                assert "SRC-1" not in str(call), f"source issue mutated via {name}: {call}"
    finally:
        mocks["_stop"]()


def test_cloud_copy_with_builder_produces_equivalent_payload(cloud_client):
    mocks = patch_cloud_copy(cloud_client)
    try:
        cloud_client.copy_issue_to_project("SRC-1", "TGT", use_builder=True)

        payload = mocks["create_issue"].call_args.kwargs["data"]["fields"]
        accepted = {row["fieldId"]: row for row in CREATE_FIELDS_META["values"]}
        expected, _, _ = build_field_plan(SOURCE_FIELDS, "TGT", {"id": "10101", "name": "Task"}, accepted)
        assert payload.pop("issuetype") == {"name": "Task"}
        expected.pop("issuetype")
        assert payload == expected
    finally:
        mocks["_stop"]()


def test_cloud_resume_only_adds_missing_pieces(cloud_client):
    existing_state = {
        "target_attachments": [{"id": "900", "filename": "a.log", "size": 10}],
        "target_links": [SOURCE_LINKS[0]],
        "target_comments": [{"id": "9000", "body": "first comment", "author": {"displayName": "Ana"}}],
    }
    mocks = patch_cloud_copy(cloud_client, **existing_state)
    try:
        result = cloud_client.copy_issue_to_project("SRC-1", "TGT", target_key="TGT-7")

        assert result.created is False
        mocks["create_issue"].assert_not_called()
        # Only the missing attachment is uploaded.
        uploaded = [call.kwargs["files"]["file"][0] for call in mocks["post"].call_args_list]
        assert uploaded == ["b.png"]
        # Only the missing comment is appended, order preserved.
        assert [call.kwargs["data"]["body"] for call in mocks["add_comment"].call_args_list] == [COMMENT_2["body"]]
        # Only the missing link is created.
        assert mocks["link_issues"].call_args_list == [
            mock.call(data={"type": {"name": "Blocks"}, "inwardIssue": {"key": "SRC-3"}})
        ]
        statuses = {a.filename: a.status for a in result.checklist.attachments}
        assert statuses == {"a.log": "exists", "b.png": "copied"}
        assert {c.comment_id: c.status for c in result.checklist.comments} == {"101": "exists", "102": "copied"}
        # Field rows reflect that the issue already existed.
        summary_row = next(item for item in result.checklist.fields if item.field_id == "summary")
        assert summary_row.status == "exists"
    finally:
        mocks["_stop"]()


def test_failed_attachment_is_retried_without_duplicates(cloud_client):
    # First run: a.log fails, b.png succeeds.
    mocks1 = patch_cloud_copy(cloud_client, upload_failure_for={"a.log"})
    try:
        result1 = cloud_client.copy_issue_to_project("SRC-1", "TGT", target_key="TGT-7")
    finally:
        mocks1["_stop"]()

    assert {a.filename: a.status for a in result1.checklist.attachments} == {"a.log": "failed", "b.png": "copied"}
    assert len(mocks1["post"].call_args_list) == 2

    # Second run: b.png already present, only a.log is uploaded once.
    mocks2 = patch_cloud_copy(
        cloud_client,
        target_attachments=[{"id": "901", "filename": "b.png", "size": 20}],
        target_comments=[COMMENT_1, COMMENT_2],
        target_links=SOURCE_LINKS,
    )
    try:
        result2 = cloud_client.copy_issue_to_project("SRC-1", "TGT", target_key="TGT-7")
    finally:
        mocks2["_stop"]()

    uploaded_run2 = [call.kwargs["files"]["file"][0] for call in mocks2["post"].call_args_list]
    assert uploaded_run2 == ["a.log"]
    assert {a.filename: a.status for a in result2.checklist.attachments} == {"a.log": "copied", "b.png": "exists"}
    mocks2["add_comment"].assert_not_called()
    mocks2["link_issues"].assert_not_called()


def test_existing_copy_is_auto_discovered_via_property(cloud_client):
    mocks = patch_cloud_copy(
        cloud_client,
        search_candidates=[{"key": "TGT-7"}, {"key": "TGT-8"}],
        target_attachments=SOURCE_ATTACHMENTS,
        target_comments=[COMMENT_1, COMMENT_2],
        target_links=SOURCE_LINKS,
    )
    try:
        # Only TGT-7 carries the marker; TGT-8 answers 404 on the property.
        def get_prop(key, property_key):
            if key == "TGT-7":
                return {"key": property_key, "value": {"sourceKey": "SRC-1"}}
            raise RuntimeError("404")

        mocks["get_issue_property"].side_effect = get_prop
        result = cloud_client.copy_issue_to_project("SRC-1", "TGT")
    finally:
        mocks["_stop"]()

    assert result.created is False
    assert result.target_key == "TGT-7"
    mocks["create_issue"].assert_not_called()


# ---------------------------------------------------------------------------
# Server / Data Center legacy client behaviour
# ---------------------------------------------------------------------------


def test_server_copy_uses_legacy_wrappers(server_client):
    mocks = patch_server_copy(server_client)
    try:
        result = server_client.copy_issue_to_project("SRC-1", "TGT")

        assert result.created is True
        # Plain-fields style: payload passed positionally, no data= wrapper.
        called_args, called_kwargs = mocks["create_issue"].call_args
        assert isinstance(called_args[0], dict)
        assert "data" not in called_kwargs
        payload = called_args[0]
        assert payload["project"] == {"key": "TGT"}
        assert payload["summary"] == SOURCE_FIELDS["summary"]
        assert "assignee" not in payload

        assert mocks["issue_add_comment"].call_args_list[0].args == ("TGT-7", "first comment")
        assert mocks["create_issue_link"].call_args_list[0].args[0] == {
            "type": {"name": "Relates"},
            "outwardIssue": {"key": "SRC-2"},
        }
        upload_calls = mocks["add_attachment_object"].call_args_list
        assert upload_calls[0].args[0] == "TGT-7"
        assert upload_calls[0].kwargs == {"filename": "a.log"}
        assert isinstance(upload_calls[0].args[1], BytesIO)
        assert upload_calls[1].kwargs == {"filename": "b.png"}
        mocks["set_issue_property"].assert_called_once_with("TGT-7", COPY_PROPERTY_KEY, {"sourceKey": "SRC-1"})
    finally:
        mocks["_stop"]()


def test_unknown_target_issue_type_raises(server_client):
    with (
        mock.patch.object(server_client, "get_issue", return_value=SOURCE_ISSUE),
        mock.patch.object(
            server_client, "issue_createmeta_issuetypes", return_value={"values": [{"id": "1", "name": "Story"}]}
        ),
        mock.patch.object(server_client, "issue_get_comments", return_value={"comments": []}),
    ):
        with pytest.raises(ValueError, match="not available in project TGT"):
            server_client.build_issue_copy_checklist("SRC-1", "TGT")

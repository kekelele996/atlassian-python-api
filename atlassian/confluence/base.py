# coding=utf-8

import copy
import hashlib
import io
import re
from urllib.parse import parse_qsl, urljoin, urlparse
import logging
from requests import HTTPError
from ..rest_client import AtlassianRestAPI
from ..errors import ApiConflictError, ApiError, ApiValueError
from .. import utils

log = logging.getLogger(__name__)


class ConfluenceBase(AtlassianRestAPI):
    """
    Base class for Confluence API operations.
    """

    @staticmethod
    def _is_cloud_url(url):
        """Compatibility proxy for the version-aware Confluence base class."""
        from ..confluence_base import ConfluenceBase as VersionedConfluenceBase

        return VersionedConfluenceBase._is_cloud_url(url)

    def __init__(self, url, *args, **kwargs):
        """
        Init the rest api wrapper

        :param url: string:    The base url used for the rest api.
        :param *args: list:    The fixed arguments for the AtlassianRestApi.
        :param **kwargs: dict: The keyword arguments for the AtlassianRestApi.

        :return: nothing
        """
        self._update_data(kwargs.pop("data", {}))
        if url is None:
            url = self.get_link("self")
            if isinstance(url, list):  # Server has a list of links
                url = url[0]
        super().__init__(url, *args, **kwargs)

    def _sub_url(self, url):
        """
        Get the full url from a relative one.

        :param url: string: The sub url
        :return: The absolute url
        """
        return self.url_joiner(self.url, url)

    @staticmethod
    def _create_body(body, representation):
        if representation not in [
            "atlas_doc_format",
            "editor",
            "export_view",
            "view",
            "storage",
            "wiki",
        ]:
            raise ValueError("Wrong value for representation, it should be either wiki or storage")

        return {representation: {"value": body, "representation": representation}}

    def get_page_id_by_url(self, page_url):
        """Resolve a Confluence page URL to its page ID.

        ``viewpage.action?pageId=...`` URLs are resolved without a request.
        Display and short ``/x/...`` URLs are requested through the configured
        authenticated session, which follows the Confluence redirect and reads
        the page ID from the resulting URL or HTML metadata.
        """
        parsed_url = urlparse(page_url)
        page_id_match = re.search(r"(?:[?&]pageId=)([^&#]+)", page_url, re.IGNORECASE)
        if page_id_match:
            return page_id_match.group(1)

        client_host = urlparse(self.url).hostname
        if not parsed_url.scheme or not parsed_url.hostname:
            raise ApiValueError("page_url must be an absolute Confluence page URL")
        if client_host and parsed_url.hostname != client_host:
            raise ApiValueError("page_url must belong to the configured Confluence instance")

        response = self.get(page_url, absolute=True, advanced_mode=True)
        resolved_url = response.url
        page_id_match = re.search(r"(?:[?&]pageId=)([^&#]+)", resolved_url, re.IGNORECASE)
        if page_id_match:
            return page_id_match.group(1)

        content = response.content.decode("utf-8", errors="ignore")
        page_id_match = re.search(
            r'<meta[^>]+name=["\']ajs-page-id["\'][^>]+content=["\']([^"\']+)["\']',
            content,
            re.IGNORECASE,
        )
        if page_id_match:
            return page_id_match.group(1)

        raise ApiValueError("Could not determine a page ID from page_url")

    @property
    def _new_session_args(self):
        """
        Get the kwargs for new objects (session, root, version,...).

        :return: A dict with the kwargs for new objects
        """
        return {
            "session": self._session,
            "cloud": self.cloud,
            "api_root": self.api_root,
            "api_version": self.api_version,
        }

    def _update_data(self, data):
        """
        Internal function to update the data.

        :param data: dict: The new data.
        :return: The updated object
        """
        self.__data = data
        return self

    @property
    def data(self):
        """
        Get the internal cached data. For data integrity a deep copy is returned.

        :return: A copy of the data cache
        """
        return copy.copy(self.__data)

    def get_data(self, id, default=None):
        """
        Get a data element from the internal data cache. For data integrity a deep copy is returned.
        If data isn't present, the default value is returned.

        :param id: string:                     The data element to return
        :param default: any (default is None): The value to return if id is not present

        :return: The requested data element
        """
        return copy.copy(self.__data[id]) if id in self.__data else default

    def get_link(self, link):
        """
        Get a link from the data.

        :param link: string: The link identifier
        :return: The requested link or None if it isn't present
        """
        links = self.get_data("links")
        if links is None or link not in links:
            return None
        return links[link]["href"]

    def _get_paged(
        self,
        url,
        params=None,
        data=None,
        flags=None,
        trailing=None,
        absolute=False,
    ):
        """
        Used to get the paged data

        :param url: string:                        The url to retrieve
        :param params: dict (default is None):     The parameter's
        :param data: dict (default is None):       The data
        :param flags: string[] (default is None):  The flags
        :param trailing: bool (default is None):   If True, a trailing slash is added to the url
        :param absolute: bool (default is False):  If True, the url is used absolute and not relative to the root

        :return: A generator object for the data elements
        """
        if params is None:
            params = {}

        while True:
            current_url = url
            response = self.get(
                url,
                trailing=trailing,
                params=params,
                data=data,
                flags=flags,
                absolute=absolute,
            )
            if "results" not in response:
                return

            yield from response.get("results", [])

            next_link = response.get("_links", {}).get("next")
            if next_link is None:
                break
            if isinstance(next_link, str):
                url = next_link
            else:
                url = next_link.get("href")
            if url is None:
                break

            # Cloud V2 cursor links contain a relative endpoint and a cursor
            # query.  Reusing the already-resolved endpoint preserves both
            # ``/wiki`` and API-gateway tenant prefixes.
            parsed_next = urlparse(url)
            next_path = parsed_next.path.strip("/")
            current_path = urlparse(current_url).path.strip("/")
            if (
                str(getattr(self, "api_version", "")) == "2"
                and parsed_next.query
                and not parsed_next.scheme
                and (not next_path or next_path == current_path)
            ):
                url = current_url
                params = dict(parse_qsl(parsed_next.query, keep_blank_values=True))
                trailing = False
                continue

            if not urlparse(url).scheme:
                # Confluence returns both ``/rest/api/...`` and
                # ``rest/api/...`` forms for next links.  Neither is an
                # absolute URL, despite the latter lacking a leading slash.
                parsed = urlparse(self.url)
                site_url = f"{parsed.scheme}://{parsed.netloc}"
                if url.startswith("/") or url.startswith(("rest/", "wiki/")):
                    # On Server, next links are relative to the context path
                    # (e.g. ``/confluence``) unless they already include it.
                    path = f"/{url.lstrip('/')}"
                    context = "" if self.cloud else parsed.path.rstrip("/")
                    if context and path != context and not path.startswith(f"{context}/"):
                        path = f"{context}{path}"
                    url = f"{site_url}{path}"
                else:
                    url = urljoin(f"{self.url.rstrip('/')}/", url)

            # From now on we have absolute URLs with parameters
            absolute = True
            # Params are now provided by the url
            params = {}
            # Trailing should not be added as it is already part of the url
            trailing = False

        return

    def raise_for_status(self, response):
        """
        Checks the response for errors and throws an exception if return code >= 400

        Implementation for Confluence Server according to
            https://developer.atlassian.com/server/confluence/rest/v1002/intro/#about
        Implementation for Confluence Cloud according to
            https://developer.atlassian.com/cloud/confluence/rest/v2/intro/#about
        :param response:
        :return:
        """
        if 400 <= response.status_code < 600:
            try:
                j = response.json()
            except (TypeError, ValueError):
                j = None

            messages = []
            if isinstance(j, dict):
                for key in ("message", "detail", "reason"):
                    value = j.get(key)
                    if value:
                        messages.append(str(value))
                errors = j.get("errors")
                if isinstance(errors, dict):
                    messages.extend(str(value) for value in errors.values() if value)
                elif isinstance(errors, list):
                    messages.extend(
                        str(item.get("message", item)) if isinstance(item, dict) else str(item)
                        for item in errors
                        if item
                    )
                error_messages = j.get("errorMessages")
                if isinstance(error_messages, list):
                    messages.extend(str(value) for value in error_messages if value)

            if not messages:
                # Some Confluence proxy and HTML-validation failures are not
                # JSON. Include only a bounded, whitespace-normalized server
                # response; never include the submitted page body.
                response_text = " ".join((getattr(response, "text", "") or "").split())
                if response_text:
                    messages.append(response_text[:1000])
                else:
                    messages.append(f"HTTP {response.status_code}: {response.reason}")

            raise HTTPError("\n".join(dict.fromkeys(messages)), response=response)
        else:
            response.raise_for_status()

    # ------------------------------------------------------------------
    # Atomic, idempotent page publishing (body + attachments + labels)
    # ------------------------------------------------------------------
    #
    # Confluence's REST API has no transaction spanning page content,
    # attachments and labels.  ``publish_page`` nevertheless gives callers
    # all-or-nothing *observable* semantics:
    #
    # * the storage body is the commit point and is written only *after*
    #   every attachment has been uploaded, so a failing attachment upload
    #   never leaves readers looking at a new body pointing at missing files;
    # * every step is reconciled from the server state: attachments already
    #   carrying the exact wanted bytes (SHA-256) are skipped instead of
    #   being uploaded again, which keeps retry runs from creating extra
    #   attachment versions;
    # * an optimistic version check stops the publish when the base page
    #   version somebody else modified, and the body PUT relies on the
    #   server-side version conflict (HTTP 409) as the serialization point;
    # * labels are replaced from a fully paginated listing, so pages with
    #   hundreds of existing labels do not silently keep stale ones.
    #
    # The same V1 content endpoints are used on Confluence Cloud and
    # Server/Data Center; only the attachment update verb and a couple of
    # payload shapes differ.

    @staticmethod
    def _publish_normalize_attachments(attachments):
        """Turn attachment arguments into ``[{'name', 'bytes', ...}]``.

        Accepts ``(name, content)`` tuples (with optional content type and
        comment) or dictionaries.  ``content`` may be ``bytes`` or any binary
        stream, including in-memory ``BytesIO`` objects; streams are read
        once here so the payload can be checksummed and replayed safely.
        """
        normalized = []
        seen = set()
        for item in attachments or []:
            if isinstance(item, dict):
                name = item["name"]
                content = item["content"]
                content_type = item.get("content_type") or item.get("contentType")
                comment = item.get("comment")
            elif isinstance(item, (list, tuple)):
                if len(item) < 2:
                    raise ApiValueError("Attachment tuples need at least (name, content)")
                name, content = item[0], item[1]
                content_type = item[2] if len(item) > 2 else None
                comment = item[3] if len(item) > 3 else None
            else:
                raise ApiValueError("Attachments must be dicts or (name, content) tuples")

            # Attachment names are filenames, never paths (mirrors attach_content).
            name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
            if not name:
                raise ApiValueError("Attachment name must contain a filename")
            if name in seen:
                raise ApiValueError(f"Duplicate attachment name in publish: {name}")
            seen.add(name)

            if hasattr(content, "read"):
                payload = content.read()
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
            elif isinstance(content, str):
                payload = content.encode("utf-8")
            elif isinstance(content, (bytes, bytearray)):
                payload = bytes(content)
            else:
                raise ApiValueError(f"Unsupported attachment content for {name}: {type(content)!r}")

            normalized.append(
                {
                    "name": name,
                    "bytes": payload,
                    "content_type": content_type or "application/binary",
                    "comment": comment or f"Uploaded {name}.",
                }
            )
        return normalized

    @staticmethod
    def _publish_normalize_labels(labels):
        """Normalize wanted labels to unique ``{'prefix', 'name'}`` dicts."""
        normalized = []
        seen = set()
        for item in labels or []:
            if isinstance(item, dict):
                prefix = item.get("prefix", "global")
                name = item["name"]
            else:
                prefix, name = "global", str(item)
            key = (prefix, name)
            if key not in seen:
                seen.add(key)
                normalized.append({"prefix": prefix, "name": name})
        return normalized

    @staticmethod
    def _publish_bodies_equal(current_body, wanted_body):
        if current_body is None:
            return False
        return utils.symbol_normalizer(current_body).strip().lower() == wanted_body.strip().lower()

    def _publish_collect(self, url, params, limit):
        """Collect every page of a start/limit V1 collection."""
        results = []
        start = 0
        while True:
            page = self.get(url, params=dict(params, start=start, limit=limit))
            batch = (page or {}).get("results", []) if isinstance(page, dict) else []
            results.extend(batch)
            if not batch:
                break
            total_size = (page or {}).get("totalSize")
            if total_size is not None and start + len(batch) >= int(total_size):
                break
            if len(batch) < limit:
                break
            start += len(batch)
        return results

    def _publish_list_attachments(self, page_id, limit):
        return self._publish_collect(
            f"rest/api/content/{page_id}/child/attachment",
            {"expand": "version,extensions"},
            limit,
        )

    def _publish_list_labels(self, page_id, limit):
        return self._publish_collect(f"rest/api/content/{page_id}/label", {}, limit)

    @staticmethod
    def _publish_attachment_size(attachment):
        extensions = attachment.get("extensions") or {}
        size = extensions.get("fileSize", extensions.get("size"))
        try:
            return int(size) if size is not None else None
        except (TypeError, ValueError):
            return None

    def _publish_attachment_bytes(self, page_id, attachment):
        """Download the current bytes of one attachment for checksum comparison."""
        if self.cloud:
            url = f"rest/api/content/{page_id}/child/attachment/{attachment['id']}/download"
            return self.get(url, not_json_response=True)
        download_link = (attachment.get("_links") or {}).get("download")
        return self.get(download_link, not_json_response=True)

    def _publish_attachment_matches(self, page_id, attachment, payload):
        """Return True when the existing attachment already holds ``payload``."""
        size = self._publish_attachment_size(attachment)
        if size is not None and size != len(payload):
            return False
        try:
            current_bytes = self._publish_attachment_bytes(page_id, attachment)
        except HTTPError:
            # Metadata alone cannot prove equality and the bytes are not
            # readable; force an upload rather than risk skipping a file.
            return False
        return hashlib.sha256(current_bytes).digest() == hashlib.sha256(payload).digest()

    def _publish_upload_attachment(self, page_id, spec, existing=None):
        """Create an attachment or upload a new version of an existing one."""
        data = {
            "type": "attachment",
            "fileName": spec["name"],
            "contentType": spec["content_type"],
            "comment": spec["comment"],
            "minorEdit": "true",
        }
        headers = {"X-Atlassian-Token": "no-check", "Accept": "application/json"}
        base_path = f"rest/api/content/{page_id}/child/attachment"
        files = {"file": (spec["name"], io.BytesIO(spec["bytes"]), spec["content_type"])}
        if existing is None:
            return self.post(base_path, data=data, headers=headers, files=files)
        data_path = f"{base_path}/{existing['id']}/data"
        if self.cloud:
            # Cloud updates attachments with PUT; Server/Data Center only
            # accepts POST on the /data path.
            return self.put(data_path, data=data, headers=headers, files=files)
        return self.post(data_path, data=data, headers=headers, files=files)

    def _publish_put_body(
        self, page_id, *, title, body, representation, version_number, minor_edit, version_comment, parent_id
    ):
        data = {
            "id": page_id,
            "type": "page",
            "title": title,
            "version": {"number": version_number, "minorEdit": minor_edit},
            "body": self._create_body(body, representation),
        }
        if parent_id:
            data["ancestors"] = [{"type": "page", "id": parent_id}]
        if version_comment:
            data["version"]["message"] = version_comment
        try:
            return self.put(
                f"rest/api/content/{page_id}",
                data=data,
                params={"status": "current"},
            )
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                raise ApiConflictError(
                    f"Page {page_id} was modified by another update before version {version_number} "
                    "could be saved; re-read the page and retry the publish",
                    reason=e,
                )
            raise

    def _publish_reconcile_labels(self, page_id, wanted_labels, existing_labels):
        """Replace managed (default: global) labels, touching nothing else."""
        wanted_by_key = {(label["prefix"], label["name"]): label for label in wanted_labels}
        existing_by_key = {(label.get("prefix", "global"), label["name"]): label for label in existing_labels}
        # Only reconcile prefixes the caller explicitly manages; global labels
        # are always managed so ``labels=[]`` clears them.
        managed_prefixes = {"global"} | {label["prefix"] for label in wanted_labels}

        added, removed = [], []
        for key in wanted_by_key.keys() - existing_by_key.keys():
            label = wanted_by_key[key]
            if label["prefix"] != "global":
                continue
            if self.cloud:
                # Cloud's V1 label endpoint takes a JSON array; Server's takes
                # a single label object.
                self.post(f"rest/api/content/{page_id}/label", json=[label])
            else:
                self.post(f"rest/api/content/{page_id}/label", data=label)
            added.append(label["name"])

        for prefix, name in existing_by_key:
            if prefix in managed_prefixes and (prefix, name) not in wanted_by_key:
                self.delete(f"rest/api/content/{page_id}/label", params={"name": name})
                removed.append(name)
        return added, removed

    def publish_page(
        self,
        page_id,
        body=None,
        attachments=None,
        labels=None,
        title=None,
        representation="storage",
        expected_version=None,
        minor_edit=False,
        version_comment=None,
        parent_id=None,
        page_limit=200,
    ):
        """Publish a page body together with attachments and labels.

        The publish is reconciled against the current server state and is
        safe to retry with the same arguments:

        * attachments are uploaded first; an attachment whose existing
          bytes already match (size plus SHA-256) is skipped, so retries do
          not create duplicate attachment versions;
        * the storage body is updated afterwards with an optimistic version
          number and is the reader-visible commit point;
        * labels are reconciled last from a fully paginated listing;
        * if ``expected_version`` is given and the page base version was
          changed to unrelated content, :class:`ApiConflictError` is raised
          before anything is written;
        * a no-op state (everything already equals the wanted publish, for
          example after a response for a successful attempt was lost) is
          reported with ``already_published=True`` and performs no writes.

        Attachments are never deleted when absent from ``attachments`` and
        labels with non-global prefixes (for example team labels) are left
        untouched. Pass ``labels=[]`` to remove every global label.

        :param page_id: existing Confluence page ID
        :param body: storage XHTML (or ``representation``) body; ``None``
            keeps the current body
        :param attachments: list of ``(name, content)`` tuples, optionally
            with content type and comment, or dicts with ``name``/``content``
            keys; content may be bytes or an in-memory binary stream
        :param labels: wanted global label names (dicts with ``prefix`` are
            accepted); ``None`` leaves labels untouched, an empty list clears
            all global labels
        :param title: page title; defaults to the current title
        :param representation: body representation, default ``"storage"``
        :param expected_version: page version the caller read before editing;
            a modified, non-matching page stops the publish
        :param minor_edit: suppress watcher notifications for the body update
        :param version_comment: optional version comment
        :param parent_id: optional parent page ID
        :param page_limit: page size used when listing attachments/labels
        :return: dict with the final page, version number and per-area change
            summary; ``already_published`` is True when nothing had to change
        :raises ApiConflictError: the base version was modified concurrently
        """
        wanted_attachments = self._publish_normalize_attachments(attachments)
        manage_labels = labels is not None
        wanted_labels = self._publish_normalize_labels(labels) if manage_labels else None

        page = self.get(
            f"rest/api/content/{page_id}",
            params={"expand": "body.storage,version"},
        )
        current_version = int(page["version"]["number"])
        current_title = page.get("title")
        current_body = ((page.get("body") or {}).get("storage") or {}).get("value")

        existing_attachments = self._publish_list_attachments(page_id, page_limit) if wanted_attachments else []
        attachments_by_name = {att.get("title"): att for att in existing_attachments}

        body_matches = body is None or (representation == "storage" and self._publish_bodies_equal(current_body, body))

        # Plan attachment work from exact bytes, not from file names: an
        # attachment already carrying the wanted payload needs no new version.
        created, updated, unchanged = [], [], []
        attachment_plans = []
        attachments_match = True
        for spec in wanted_attachments:
            existing = attachments_by_name.get(spec["name"])
            if existing is not None and self._publish_attachment_matches(page_id, existing, spec["bytes"]):
                unchanged.append(spec["name"])
                continue
            attachments_match = False
            attachment_plans.append((spec, existing))
            (updated if existing is not None else created).append(spec["name"])

        existing_labels = self._publish_list_labels(page_id, page_limit) if manage_labels else []
        if manage_labels:
            wanted_keys = {(label["prefix"], label["name"]) for label in wanted_labels}
            managed_prefixes = {"global"} | {label["prefix"] for label in wanted_labels}
            existing_managed_keys = {
                (label.get("prefix", "global"), label["name"])
                for label in existing_labels
                if label.get("prefix", "global") in managed_prefixes
            }
            labels_match = existing_managed_keys == wanted_keys
        else:
            labels_match = True

        # A fully matching page means the previous (possibly response-lost)
        # publish already succeeded: report it without writing anything.
        if body_matches and attachments_match and labels_match:
            return {
                "page": page,
                "version": current_version,
                "already_published": True,
                "changed": False,
                "body_updated": False,
                "attachments": {"created": [], "updated": [], "unchanged": list(unchanged)},
                "labels": {"added": [], "removed": [], "current": [label["name"] for label in existing_labels]},
            }

        if expected_version is not None:
            expected_version = int(expected_version)
            if current_version < expected_version:
                raise ApiValueError(
                    f"Page {page_id} is at version {current_version}, older than expected "
                    f"version {expected_version}"
                )
            # A newer version is acceptable only when our body and every
            # attachment already landed (a response-lost prior attempt); the
            # label reconciliation below then just fills the missing labels.
            if current_version != expected_version and not (body_matches and attachments_match):
                raise ApiConflictError(
                    f"Page {page_id} was modified: expected base version {expected_version}, "
                    f"found {current_version}; re-read the page and retry"
                )

        # 1) attachments first, so a failed upload never exposes a new body
        #    referencing files readers cannot download yet.
        for spec, existing in attachment_plans:
            self._publish_upload_attachment(page_id, spec, existing)

        # 2) body is the commit point; the server rejects a stale version
        #    number with HTTP 409 if somebody else committed in between.
        body_updated = False
        final_version = current_version
        if not body_matches:
            response = self._publish_put_body(
                page_id,
                title=title or current_title,
                body=body,
                representation=representation,
                version_number=current_version + 1,
                minor_edit=minor_edit,
                version_comment=version_comment,
                parent_id=parent_id,
            )
            body_updated = True
            final_version = current_version + 1
            if isinstance(response, dict):
                try:
                    final_version = int(response["version"]["number"])
                except (KeyError, TypeError, ValueError):
                    pass

        # 3) labels last; failure here leaves body/attachments valid and a
        #    retry only performs the missing label changes.
        added_labels, removed_labels = ([], [])
        if manage_labels:
            added_labels, removed_labels = self._publish_reconcile_labels(page_id, wanted_labels, existing_labels)

        result = self._publish_verify(
            page_id=page_id,
            wanted_body=body if representation == "storage" else None,
            wanted_attachments=wanted_attachments,
            wanted_labels=wanted_labels if manage_labels else None,
            expected_final_version=final_version,
        )
        result.update(
            {
                "already_published": False,
                "changed": True,
                "body_updated": body_updated,
                "attachments": {"created": created, "updated": updated, "unchanged": list(unchanged)},
                "labels": {
                    "added": added_labels,
                    "removed": removed_labels,
                    "current": result["labels"]["current"],
                },
            }
        )
        return result

    def _publish_verify(self, *, page_id, wanted_body, wanted_attachments, wanted_labels, expected_final_version):
        """Re-read the page and fail loudly if the publish did not fully land."""
        page = self.get(
            f"rest/api/content/{page_id}",
            params={"expand": "body.storage,version"},
        )
        try:
            final_version = int(page["version"]["number"])
        except (KeyError, TypeError, ValueError) as e:
            raise ApiError(f"Published page {page_id} returned no version number", reason=e)
        if final_version != expected_final_version:
            raise ApiConflictError(
                f"Page {page_id} version after publish is {final_version}, expected {expected_final_version}; "
                "another update may have raced with this publish"
            )
        if wanted_body is not None:
            current_body = ((page.get("body") or {}).get("storage") or {}).get("value")
            if not self._publish_bodies_equal(current_body, wanted_body):
                raise ApiError(f"Body verification failed for page {page_id} after publish")

        if wanted_attachments:
            attachments = self._publish_list_attachments(page_id, 200)
            by_name = {att.get("title"): att for att in attachments}
            for spec in wanted_attachments:
                attachment = by_name.get(spec["name"])
                if attachment is None:
                    raise ApiError(f"Attachment {spec['name']} missing on page {page_id} after publish")
                size = self._publish_attachment_size(attachment)
                if size is not None and size != len(spec["bytes"]):
                    raise ApiError(
                        f"Attachment {spec['name']} on page {page_id} has size {size}, "
                        f"expected {len(spec['bytes'])}"
                    )

        current_label_names = []
        if wanted_labels is not None:
            existing_labels = self._publish_list_labels(page_id, 200)
            wanted_keys = {(label["prefix"], label["name"]) for label in wanted_labels}
            managed_prefixes = {"global"} | {label["prefix"] for label in wanted_labels}
            for label in existing_labels:
                key = (label.get("prefix", "global"), label["name"])
                if key[0] in managed_prefixes and key not in wanted_keys:
                    raise ApiError(f"Stale label {label['name']} remains on page {page_id} after publish")
            existing_keys = {(label.get("prefix", "global"), label["name"]) for label in existing_labels}
            for key in wanted_keys:
                if key not in existing_keys:
                    raise ApiError(f"Wanted label {key[1]} missing on page {page_id} after publish")
            current_label_names = [label["name"] for label in existing_labels]

        return {"page": page, "version": final_version, "labels": {"current": current_label_names}}

# coding=utf-8
"""
Tests for the idempotent, concurrency-safe ``publish_page`` workflow.

A small in-memory fake implements the V1 content endpoints shared by
Confluence Cloud and Server/Data Center.  The same scenarios run against
both clients; the fake also enforces the server-side page version conflict
(HTTP 409) that ``publish_page`` relies on as its serialization point.
"""

import io
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from requests import HTTPError, Response

from atlassian import Confluence
from atlassian.confluence import ConfluenceCloud, ConfluenceServer
from atlassian.errors import ApiConflictError, ApiValueError


# ---------------------------------------------------------------------------
# In-memory Confluence V1 fake
# ---------------------------------------------------------------------------


class FakeConfluenceBackend:
    def __init__(self, cloud, page_body="<p>old body</p>", page_version=2, attachments=None, labels=None):
        self.cloud = cloud
        self.page = {
            "id": "100",
            "type": "page",
            "title": "Release Notes",
            "status": "current",
            "version": {"number": page_version},
            "body": {"storage": {"value": page_body, "representation": "storage"}},
        }
        # name -> {"id", "bytes", "content_type", "version", "comment"}
        self.attachments = attachments or {}
        self.labels = list(labels) if labels is not None else [{"prefix": "global", "name": "old-label", "id": "l0"}]
        self._next_att_id = 1
        self._next_label_id = 1000
        # names of attachments whose next create/upload request fails once
        self.fail_upload_once = set()
        # when True, the next page PUT sees a newer version and returns 409
        self.page_put_conflict = False
        # every non-GET request: (method, path)
        self.mutations = []
        # all requests in order: (method, path, query)
        self.requests = []

    # -- helpers ------------------------------------------------------------

    def _json(self, payload, status=200):
        response = Response()
        response.status_code = status
        response.reason = "OK" if status < 400 else "Error"
        response._content = json.dumps(payload).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        return response

    def _raw(self, content, status=200):
        response = Response()
        response.status_code = status
        response.reason = "OK" if status < 400 else "Error"
        response._content = content
        response.headers["Content-Type"] = "application/octet-stream"
        return response

    def _new_att_id(self, name):
        att_id = f"att{self._next_att_id}"
        self._next_att_id += 1
        return att_id

    def attachment_json(self, name, att):
        return {
            "id": att["id"],
            "type": "attachment",
            "title": name,
            "version": {"number": att["version"]},
            "extensions": {
                "mediaType": att["content_type"],
                "fileSize": len(att["bytes"]),
                "comment": att["comment"],
            },
            "_links": {"download": f"/download/attachments/100/{name}?version={att['version']}&modificationDate=0"},
        }

    def label_json(self, label):
        return {"prefix": label["prefix"], "name": label["name"], "id": label["id"]}

    # -- routing ------------------------------------------------------------

    def __call__(self, method, url, headers=None, data=None, files=None, json=None, **kwargs):
        parsed = urlparse(url)
        path = parsed.path
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        self.requests.append((method, path, query))
        if method != "GET":
            self.mutations.append((method, path))

        try:
            return self._route(method, path, query, data, files, json)
        except _FakeError as e:
            return self._json({"message": str(e)}, status=e.status)

    def _route(self, method, path, query, data, files, json_body):
        # Page read
        if method == "GET" and re.search(r"/content/100$", path):
            return self._json(self.page)

        # Attachment collection
        if re.search(r"/content/100/child/attachment$", path):
            if method == "GET":
                return self._list_attachments(query)
            if method == "POST":
                return self._create_attachment(files)

        # Attachment by id: /download or /data
        match = re.search(r"/content/100/child/attachment/(att[\w-]+)/(download|data)$", path)
        if match:
            att_id, action = match.group(1), match.group(2)
            name, att = self._find_attachment(att_id)
            if action == "download":
                return self._raw(att["bytes"])
            if method in ("PUT", "POST"):
                return self._upload_version(name, att, files)

        # Server-style download link
        match = re.search(r"/download/attachments/100/([^?/]+)$", path)
        if match and method == "GET":
            name = unquote(match.group(1))
            return self._raw(self.attachments[name]["bytes"])

        # Labels
        if re.search(r"/content/100/label$", path):
            if method == "GET":
                return self._list_labels(query)
            if method == "POST":
                return self._add_labels(data, json_body)
            if method == "DELETE":
                return self._remove_label(query.get("name"))

        # Page update
        if method == "PUT" and re.search(r"/content/100$", path):
            return self._update_page(data)

        raise AssertionError(f"unexpected request: {method} {path}")

    # -- attachments --------------------------------------------------------

    def _list_attachments(self, query):
        start = int(query.get("start", 0))
        limit = int(query.get("limit", 200))
        items = [self.attachment_json(name, att) for name, att in sorted(self.attachments.items())]
        batch = items[start : start + limit]
        return self._json(
            {"results": batch, "start": start, "limit": limit, "size": len(batch), "totalSize": len(items)}
        )

    def _read_upload(self, files):
        upload = files["file"]
        name, stream = upload[0], upload[1]
        content_type = upload[2] if len(upload) > 2 else "application/binary"
        return name, stream.read(), content_type

    def _create_attachment(self, files):
        name, payload, content_type = self._read_upload(files)
        if name in self.fail_upload_once:
            self.fail_upload_once.discard(name)
            raise _FakeError(f"simulated upload failure for {name}", 500)
        if name in self.attachments:
            raise _FakeError(f"attachment {name} already exists", 409)
        att = {
            "id": self._new_att_id(name),
            "bytes": payload,
            "content_type": content_type,
            "version": 1,
            "comment": f"Uploaded {name}.",
        }
        self.attachments[name] = att
        return self._json({"results": [self.attachment_json(name, att)]})

    def _find_attachment(self, att_id):
        for name, att in self.attachments.items():
            if att["id"] == att_id:
                return name, att
        raise _FakeError(f"attachment {att_id} not found", 404)

    def _upload_version(self, name, att, files):
        _, payload, content_type = self._read_upload(files)
        if name in self.fail_upload_once:
            self.fail_upload_once.discard(name)
            raise _FakeError(f"simulated upload failure for {name}", 500)
        att["bytes"] = payload
        att["content_type"] = content_type
        att["version"] += 1
        return self._json(self.attachment_json(name, att))

    # -- labels -------------------------------------------------------------

    def _list_labels(self, query):
        start = int(query.get("start", 0))
        limit = int(query.get("limit", 200))
        items = [self.label_json(label) for label in self.labels]
        batch = items[start : start + limit]
        return self._json(
            {"results": batch, "start": start, "limit": limit, "size": len(batch), "totalSize": len(items)}
        )

    def _add_labels(self, data, json_body):
        # Cloud sends a JSON array (via json=), Server a single JSON object.
        payload = json_body if json_body is not None else json.loads(data)
        if isinstance(payload, dict):
            payload = [payload]
        added = []
        for label in payload:
            if any(
                existing["name"] == label["name"] and existing["prefix"] == label["prefix"] for existing in self.labels
            ):
                raise _FakeError(f"label {label['name']} already exists", 409)
            new_label = {
                "prefix": label.get("prefix", "global"),
                "name": label["name"],
                "id": f"l{self._next_label_id}",
            }
            self._next_label_id += 1
            self.labels.append(new_label)
            added.append(new_label)
        return self._json({"results": [self.label_json(label) for label in added]})

    def _remove_label(self, name):
        self.labels = [label for label in self.labels if label["name"] != name]
        return self._json({"results": []}, status=200)

    # -- page ---------------------------------------------------------------

    def _update_page(self, data):
        payload = json.loads(data) if isinstance(data, str) else data
        wanted_number = int(payload["version"]["number"])
        current_number = int(self.page["version"]["number"])
        if self.page_put_conflict or wanted_number != current_number + 1:
            self.page_put_conflict = False
            raise _FakeError(f"version conflict: wanted {wanted_number}, current {current_number}", 409)
        self.page["version"]["number"] = wanted_number
        if "title" in payload:
            self.page["title"] = payload["title"]
        storage = payload.get("body", {}).get("storage")
        if storage:
            self.page["body"] = {"storage": {"value": storage["value"], "representation": "storage"}}
        return self._json(self.page)


class _FakeError(Exception):
    def __init__(self, message, status):
        super().__init__(message, status)
        self.status = status


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_client(platform):
    if platform == "cloud":
        return ConfluenceCloud(url="https://example.atlassian.net", username="user", password="token")
    return ConfluenceServer(url="https://test.confluence.com", username="user", password="token")


@pytest.fixture(params=["cloud", "server"])
def client_and_backend(request):
    client = make_client(request.param)
    backend = FakeConfluenceBackend(cloud=(request.param == "cloud"))
    import unittest.mock as mock

    patcher = mock.patch.object(client._session, "request", side_effect=backend)
    patcher.start()
    request.addfinalizer(patcher.stop)
    return client, backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_publish_full_flow_both_platforms(client_and_backend):
    client, backend = client_and_backend
    cloud = client.cloud

    # "a.txt": exists with different, shorter bytes -> size mismatch, update
    backend.attachments["a.txt"] = {
        "id": "att-a",
        "bytes": b"old",
        "content_type": "text/plain",
        "version": 1,
        "comment": "x",
    }
    # "c.txt": same size, different bytes -> checksum download, update
    backend.attachments["c.txt"] = {
        "id": "att-c",
        "bytes": b"aaaa",
        "content_type": "text/plain",
        "version": 1,
        "comment": "x",
    }
    # "d.txt": identical bytes -> checksum download, skipped (no new version)
    backend.attachments["d.txt"] = {
        "id": "att-d",
        "bytes": b"same-bytes",
        "content_type": "text/plain",
        "version": 3,
        "comment": "x",
    }

    result = client.publish_page(
        "100",
        body="<p>new body</p>",
        attachments=[
            ("a.txt", b"new-a"),
            {"name": "b.txt", "content": io.BytesIO(b"new-b"), "content_type": "text/plain"},
            ("c.txt", b"bbbb"),
            ("d.txt", io.BytesIO(b"same-bytes")),
        ],
        labels=["release-2026"],
        expected_version=2,
        version_comment="publish release",
    )

    # Page committed exactly one version
    assert backend.page["version"]["number"] == 3
    assert backend.page["body"]["storage"]["value"] == "<p>new body</p>"
    assert result["version"] == 3
    assert result["changed"] is True
    assert result["already_published"] is False
    assert result["body_updated"] is True
    assert result["attachments"]["created"] == ["b.txt"]
    assert result["attachments"]["updated"] == ["a.txt", "c.txt"]
    assert result["attachments"]["unchanged"] == ["d.txt"]

    # Attachment versions: no extra version for the unchanged attachment
    assert backend.attachments["a.txt"]["version"] == 2
    assert backend.attachments["a.txt"]["bytes"] == b"new-a"
    assert backend.attachments["b.txt"]["version"] == 1
    assert backend.attachments["c.txt"]["version"] == 2
    assert backend.attachments["d.txt"]["version"] == 3

    # Labels replaced
    assert [label["name"] for label in backend.labels] == ["release-2026"]
    assert result["labels"]["added"] == ["release-2026"]
    assert result["labels"]["removed"] == ["old-label"]

    # Ordering: all attachment writes happen before the body PUT, labels after
    mutation_paths = [path for _, path in backend.mutations]
    assert max(i for i, p in enumerate(mutation_paths) if "/child/attachment" in p) < next(
        i for i, p in enumerate(mutation_paths) if re.search(r"/content/100$", p)
    )
    assert next(
        i for i, (method, p) in enumerate(backend.mutations) if method == "DELETE" and p.endswith("/label")
    ) > next(i for i, (method, p) in enumerate(backend.mutations) if method == "PUT" and p.endswith("/100"))

    # Platform differences
    update_data = [item for item in backend.mutations if item[1].endswith("/att-a/data")][0]
    assert update_data[0] == ("PUT" if cloud else "POST")

    label_post = [item for item in backend.requests if item[0] == "POST" and item[1].endswith("/label")][0]
    # Payload shape is checked directly on the recorded backend call below
    # via the requests sent to the fake: Cloud arrays vs Server object.
    assert label_post[0] == "POST"

    # Checksum downloads use the platform-correct URL
    download_paths = [p for _, p, _ in backend.requests if p.endswith("/download") or "/download/" in p]
    if cloud:
        assert any(p.endswith("/child/attachment/att-c/download") for p in download_paths)
        assert not any(p.startswith("/download/attachments/") for p in download_paths)
    else:
        assert any(p.startswith("/download/attachments/100/") for p in download_paths)


def test_label_payload_shape_differs_by_platform(client_and_backend):
    client, backend = client_and_backend

    captured = []
    original_route = backend._add_labels

    def spy_add_labels(data, json_body):
        captured.append((data, json_body))
        return original_route(data, json_body)

    backend._add_labels = spy_add_labels

    client.publish_page("100", body="<p>new</p>", labels=["fresh"], expected_version=2)

    data, json_body = captured[0]
    if client.cloud:
        assert json_body == [{"prefix": "global", "name": "fresh"}]
        assert data is None
    else:
        assert json.loads(data) == {"prefix": "global", "name": "fresh"}


def test_retry_after_failed_attachment_does_not_duplicate_versions(client_and_backend):
    client, backend = client_and_backend
    backend.fail_upload_once.add("b.txt")

    with pytest.raises(HTTPError):
        client.publish_page(
            "100",
            body="<p>new body</p>",
            attachments=[("a.txt", b"aaa"), ("b.txt", b"bbb")],
            labels=["release"],
            expected_version=2,
        )

    # Failure on the second attachment: body and labels untouched, first file present
    assert backend.page["version"]["number"] == 2
    assert backend.page["body"]["storage"]["value"] == "<p>old body</p>"
    assert [label["name"] for label in backend.labels] == ["old-label"]
    assert backend.attachments["a.txt"]["version"] == 1
    assert "b.txt" not in backend.attachments
    assert not any(method == "PUT" and path.endswith("/100") for method, path in backend.mutations)

    # Retry: a.txt is recognised by checksum, only b.txt is uploaded
    mutations_before = len(backend.mutations)
    result = client.publish_page(
        "100",
        body="<p>new body</p>",
        attachments=[("a.txt", b"aaa"), ("b.txt", b"bbb")],
        labels=["release"],
        expected_version=2,
    )
    retry_mutations = backend.mutations[mutations_before:]

    assert result["version"] == 3
    assert backend.attachments["a.txt"]["version"] == 1
    assert backend.attachments["b.txt"]["version"] == 1
    # Retry must not touch a.txt: checksum matches, so no /data call for it
    attachment_requests = [path for _, path in retry_mutations if "/child/attachment" in path]
    assert all("att-a" not in path for path in attachment_requests)
    assert len(attachment_requests) == 1
    assert attachment_requests[0].endswith("/content/100/child/attachment")


def test_lost_response_after_success_is_recognized_without_writes(client_and_backend):
    client, backend = client_and_backend

    result1 = client.publish_page(
        "100",
        body="<p>new body</p>",
        attachments=[("a.txt", b"aaa")],
        labels=["release"],
        expected_version=2,
    )
    assert result1["version"] == 3

    # Caller never got the response and retries, still quoting the old version
    mutations_before = len(backend.mutations)
    result2 = client.publish_page(
        "100",
        body="<p>new body</p>",
        attachments=[("a.txt", b"aaa")],
        labels=["release"],
        expected_version=2,
    )
    assert backend.mutations[mutations_before:] == []
    assert result2["already_published"] is True
    assert result2["changed"] is False
    assert result2["version"] == 3
    assert result2["attachments"]["unchanged"] == ["a.txt"]


def test_retry_fills_missing_labels_after_body_landed(client_and_backend):
    client, backend = client_and_backend
    # Prior attempt committed v3 (new body + attachment) but died at labels
    backend.page["version"]["number"] = 3
    backend.page["body"]["storage"]["value"] = "<p>new body</p>"
    backend.attachments["a.txt"] = {
        "id": "att-a",
        "bytes": b"aaa",
        "content_type": "application/binary",
        "version": 1,
        "comment": "Uploaded a.txt.",
    }
    # stale label from before the publish is still there

    result = client.publish_page(
        "100",
        body="<p>new body</p>",
        attachments=[("a.txt", b"aaa")],
        labels=["release"],
        expected_version=2,
    )

    # Version mismatch must not abort because body + attachments already landed
    assert result["version"] == 3
    assert result["changed"] is True
    assert result["body_updated"] is False
    assert result["labels"]["removed"] == ["old-label"]
    assert result["labels"]["added"] == ["release"]
    assert not any(method == "PUT" and path.endswith("/100") for method, path in backend.mutations)
    assert not any("/child/attachment" in path for _, path in backend.mutations)


def test_modified_base_version_stops_before_any_write(client_and_backend):
    client, backend = client_and_backend
    backend.page["version"]["number"] = 3
    backend.page["body"]["storage"]["value"] = "<p>somebody else edited this</p>"

    with pytest.raises(ApiConflictError):
        client.publish_page(
            "100",
            body="<p>new body</p>",
            attachments=[("a.txt", b"aaa")],
            labels=["release"],
            expected_version=2,
        )

    assert backend.mutations == []
    assert "a.txt" not in backend.attachments
    assert backend.page["body"]["storage"]["value"] == "<p>somebody else edited this</p>"


def test_server_side_version_conflict_during_body_put_maps_to_api_conflict(client_and_backend):
    client, backend = client_and_backend
    backend.page_put_conflict = True

    with pytest.raises(ApiConflictError):
        client.publish_page("100", body="<p>new body</p>", expected_version=2)

    # Attachment happened first (none requested here), then the conflicting PUT
    assert backend.page["version"]["number"] == 2


def test_many_old_labels_are_all_listed_and_removed(client_and_backend):
    client, backend = client_and_backend
    backend.labels = [{"prefix": "global", "name": f"old-{i:03d}", "id": f"l{i}"} for i in range(205)]
    label_list_queries = []
    original_list = backend._list_labels

    def spy_list(query):
        label_list_queries.append(int(query.get("start", 0)))
        return original_list(query)

    backend._list_labels = spy_list

    result = client.publish_page("100", body="<p>new</p>", labels=["fresh"], expected_version=2, page_limit=100)

    # Three paginated reads: 0, 100, 200
    assert label_list_queries[:3] == [0, 100, 200]
    deletes = [item for item in backend.mutations if item[0] == "DELETE"]
    assert len(deletes) == 205
    assert [label["name"] for label in backend.labels] == ["fresh"]
    assert result["labels"]["removed"] == [f"old-{i:03d}" for i in range(205)]


def test_non_global_labels_are_left_untouched(client_and_backend):
    client, backend = client_and_backend
    backend.labels = [
        {"prefix": "global", "name": "g1", "id": "l1"},
        {"prefix": "team", "name": "team-a", "id": "l2"},
    ]

    client.publish_page("100", body="<p>new</p>", labels=["g2"], expected_version=2)

    remaining = {(label["prefix"], label["name"]) for label in backend.labels}
    assert remaining == {("global", "g2"), ("team", "team-a")}


def test_labels_none_and_attachments_none_skip_those_areas(client_and_backend):
    client, backend = client_and_backend

    result = client.publish_page("100", body="<p>new</p>", expected_version=2)

    paths = [path for _, path, _ in backend.requests]
    assert not any("/label" in path for path in paths)
    assert not any("/child/attachment" in path for path in paths)
    assert [label["name"] for label in backend.labels] == ["old-label"]
    assert result["labels"] == {"added": [], "removed": [], "current": []}


def test_empty_labels_clears_global_labels(client_and_backend):
    client, backend = client_and_backend
    client.publish_page("100", body="<p>new</p>", labels=[], expected_version=2)
    assert backend.labels == []


def test_invalid_attachment_inputs_raise():
    client = make_client("server")
    import unittest.mock as mock

    backend = FakeConfluenceBackend(cloud=False)
    with mock.patch.object(client._session, "request", side_effect=backend):
        with pytest.raises(ApiValueError):
            client.publish_page("100", body="x", attachments=[("only-name",)], expected_version=2)
        with pytest.raises(ApiValueError):
            client.publish_page(
                "100",
                body="x",
                attachments=[("a.txt", b"1"), ("dir/a.txt", b"2")],
                expected_version=2,
            )
        with pytest.raises(ApiValueError):
            client.publish_page("100", body="x", attachments=[12345], expected_version=2)


def test_attachment_path_names_are_basenamed(client_and_backend):
    client, backend = client_and_backend
    client.publish_page(
        "100",
        body="<p>new</p>",
        attachments=[("C:\\docs\\release\\a.txt", b"aaa")],
        expected_version=2,
    )
    assert "a.txt" in backend.attachments


def test_memory_stream_is_consumed_and_uploaded(client_and_backend):
    client, backend = client_and_backend
    stream = io.BytesIO(b"streamed bytes")
    result = client.publish_page(
        "100",
        body="<p>new</p>",
        attachments=[{"name": "mem.bin", "content": stream}],
        expected_version=2,
    )
    assert result["attachments"]["created"] == ["mem.bin"]
    assert backend.attachments["mem.bin"]["bytes"] == b"streamed bytes"


def test_no_expected_version_still_commits(client_and_backend):
    client, backend = client_and_backend
    result = client.publish_page("100", body="<p>new</p>", labels=["x"])
    assert result["version"] == 3
    assert backend.page["version"]["number"] == 3


def test_older_than_expected_version_is_value_error(client_and_backend):
    client, backend = client_and_backend
    backend.page["version"]["number"] = 1
    with pytest.raises(ApiValueError):
        client.publish_page("100", body="<p>new</p>", expected_version=2)


def test_post_publish_verification_detects_race(client_and_backend):
    client, backend = client_and_backend

    original_update = backend._update_page

    def racing_update(data):
        result = original_update(data)
        # Another edit lands after our commit but before verification
        backend.page["version"]["number"] += 1
        return result

    backend._update_page = racing_update
    with pytest.raises(ApiConflictError):
        client.publish_page("100", body="<p>new</p>", expected_version=2)


def test_legacy_confluence_wrapper_delegates_publish_page():
    client = Confluence(url="https://example.atlassian.net", username="user", password="token")
    backend = FakeConfluenceBackend(cloud=True)
    import unittest.mock as mock

    with mock.patch.object(client._session, "request", side_effect=backend):
        result = client.publish_page("100", body="<p>new body</p>", labels=["release"], expected_version=2)
    assert result["version"] == 3
    assert [label["name"] for label in backend.labels] == ["release"]

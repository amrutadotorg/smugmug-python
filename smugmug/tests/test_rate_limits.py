"""Unit tests for rate-limit handling and retry/async-job behavior (no API access)."""

import hashlib
import logging

import pytest
import requests
from requests_oauthlib import OAuth1Session

from smugmug import RateLimitError, SmugMugClient, SmugMugError
from smugmug.client import (
    _filter_duplicates,
    _log_rate_limit_headers,
    _parse_retry_after,
    _rate_limit_error,
    _wait_after_rate_limit,
)


class FakeResp:
    def __init__(self, status_code, headers, text="", json_data=None):
        self.status_code = status_code
        self.headers = headers
        self.text = text
        self._json = json_data

    def json(self):
        return self._json


class FakeSession(OAuth1Session):
    """Session stub — only ``get``/``post``/``patch``/``delete`` are used by the tests, no auth internals."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict | None = None

    def _next(self):
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]

    def get(self, *args, **kwargs):
        return self._next()

    def post(self, *args, **kwargs):
        self.last_kwargs = kwargs
        return self._next()

    def patch(self, *args, **kwargs):
        return self._next()

    def delete(self, *args, **kwargs):
        return self._next()


class RaisingSession(OAuth1Session):
    """Session stub whose requests always fail with a network error."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def _raise(self, *args, **kwargs):
        self.calls += 1
        raise self.exc

    def get(self, *args, **kwargs):
        return self._raise()

    def post(self, *args, **kwargs):
        return self._raise()


def _fake_retry_state(exception):
    class Outcome:
        def exception(self):
            return exception

    class State:
        outcome = Outcome()
        attempt_number = 2

    return State()


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Retry-After": "30"}, 30.0),
        ({"Retry-After": "0.5"}, 0.5),
        ({"Retry-After": "not-a-number"}, None),
        ({}, None),
    ],
)
def test_parse_retry_after(headers, expected):
    assert _parse_retry_after(FakeResp(429, headers)) == expected


def test_rate_limit_error_carries_retry_after():
    err = _rate_limit_error(
        FakeResp(429, {"Retry-After": "45"}, text="slow down"), "GET /x"
    )
    assert isinstance(err, RateLimitError)
    assert err.retry_after == 45.0
    assert "429" in str(err)


@pytest.mark.parametrize(
    "exception,expected",
    [
        (RateLimitError("rl", retry_after=120.0), 60.0),
        (RateLimitError("rl", retry_after=30.0), 30.0),
        (RateLimitError("rl", retry_after=0.5), 1.0),
        (RateLimitError("rl", retry_after=None), 4.0),
        (ValueError("boom"), 4.0),
    ],
)
def test_wait_after_rate_limit(exception, expected):
    assert _wait_after_rate_limit(_fake_retry_state(exception)) == expected


def test_log_rate_limit_headers_warns_when_low(caplog):
    with caplog.at_level(logging.WARNING, logger="smugmug"):
        _log_rate_limit_headers(FakeResp(200, {"X-RateLimit-Remaining": "5"}))
    assert any("rate limit low: 5" in r.message for r in caplog.records)


def test_log_rate_limit_headers_silent_when_ok(caplog):
    with caplog.at_level(logging.WARNING, logger="smugmug"):
        _log_rate_limit_headers(FakeResp(200, {"X-RateLimit-Remaining": "50"}))
        _log_rate_limit_headers(FakeResp(200, {}))
    assert not caplog.records


def test_get_retries_on_429_honoring_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: slept.append(s))

    session = FakeSession(
        [
            FakeResp(429, {"Retry-After": "1"}, text="rate limited"),
            FakeResp(
                200,
                {"X-RateLimit-Remaining": "50"},
                json_data={"Response": {"ok": True}},
            ),
        ]
    )
    client = SmugMugClient(session, root_node_uri="")

    result = client._get("/api/v2/album/x")

    assert result == {"ok": True}
    assert session.calls == 2
    assert slept == [1.0]


def test_get_raises_rate_limit_error_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession([FakeResp(429, {"Retry-After": "1"}, text="rate limited")])
    client = SmugMugClient(session, root_node_uri="")

    with pytest.raises(RateLimitError):
        client._get("/api/v2/album/x")
    assert session.calls == 5


def test_get_does_not_retry_on_client_errors(monkeypatch):
    """404 is not retryable — one attempt, and the error carries the status code."""
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession([FakeResp(404, {}, text="not found")])
    client = SmugMugClient(session, root_node_uri="")

    with pytest.raises(SmugMugError) as excinfo:
        client._get("/api/v2/album/nonexistent99")
    assert excinfo.value.status_code == 404
    assert session.calls == 1


def test_get_retries_on_server_errors(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession(
        [
            FakeResp(500, {}, text="boom"),
            FakeResp(
                200,
                {"X-RateLimit-Remaining": "50"},
                json_data={"Response": {"ok": True}},
            ),
        ]
    )
    client = SmugMugClient(session, root_node_uri="")

    assert client._get("/api/v2/album/x") == {"ok": True}
    assert session.calls == 2


def _job_client(session):
    return SmugMugClient(session, root_node_uri="")


def test_poll_async_job_returns_true_on_completed(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession(
        [FakeResp(200, {}, json_data={"Response": {"Status": "Completed"}})]
    )
    client = _job_client(session)

    assert client._poll_async_job({"Uri": "/api/v2/job/x"}) is True


def test_poll_async_job_returns_false_on_failed(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession(
        [FakeResp(200, {}, json_data={"Response": {"Status": "Failed"}})]
    )
    client = _job_client(session)

    assert client._poll_async_job({"Uri": "/api/v2/job/x"}) is False


def test_poll_async_job_returns_false_on_timeout(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession(
        [FakeResp(200, {}, json_data={"Response": {"Status": "Pending"}})]
    )
    client = _job_client(session)

    assert (
        client._poll_async_job(
            {"Uri": "/api/v2/job/x"}, poll_interval=0.1, max_wait=0.25
        )
        is False
    )


def test_move_collect_marks_failure_when_job_fails(monkeypatch):
    """all_ok must reflect the async job outcome, not just the POST."""
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    client = _job_client(FakeSession([]))
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"Uri": "/job"})
    monkeypatch.setattr(client, "_poll_async_job", lambda *a, **k: False)

    assert (
        client._move_collect_chunked("/api/v2/album/to", ["/img/1"], "moveimages")
        is False
    )


def test_move_collect_returns_true_when_job_completes(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    client = _job_client(FakeSession([]))
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"Uri": "/job"})
    monkeypatch.setattr(client, "_poll_async_job", lambda *a, **k: True)

    assert (
        client._move_collect_chunked("/api/v2/album/to", ["/img/1"], "collectimages")
        is True
    )


def test_get_node_children_paginates(monkeypatch):
    """Children beyond the first page must not be dropped."""
    session = FakeSession(
        [
            FakeResp(
                200,
                {},
                json_data={
                    "Response": {
                        "Node": [{"Name": "a"}, {"Name": "b"}],
                        "Pages": {"NextPage": "/api/v2/node/x!children"},
                    }
                },
            ),
            FakeResp(
                200,
                {},
                json_data={"Response": {"Node": [{"Name": "c"}]}},
            ),
        ]
    )
    client = SmugMugClient(session, root_node_uri="")

    children = client.get_node_children("/api/v2/node/x", count=2)

    assert [c["Name"] for c in children] == ["a", "b", "c"]
    assert session.calls == 2


def test_get_node_children_single_page(monkeypatch):
    session = FakeSession(
        [
            FakeResp(
                200,
                {},
                json_data={"Response": {"Node": [{"Name": "a"}]}},
            ),
        ]
    )
    client = SmugMugClient(session, root_node_uri="")

    assert [c["Name"] for c in client.get_node_children("/api/v2/node/x")] == ["a"]
    assert session.calls == 1


def test_delete_accepts_204(monkeypatch):
    """SmugMug may answer DELETE with 204 No Content — that is success."""
    session = FakeSession([FakeResp(204, {}, text="")])
    client = SmugMugClient(session, root_node_uri="")

    assert client._delete("/api/v2/album/x") is True


def test_upload_bytes_sends_hex_content_md5():
    """Content-MD5 must match the API's ArchivedMD5 format: 32-char hex."""
    session = FakeSession(
        [
            FakeResp(
                200,
                {},
                json_data={
                    "stat": "ok",
                    "Image": {
                        "URL": "u",
                        "ImageUri": "/api/v2/image/x",
                        "UploadKey": "k",
                    },
                },
            )
        ]
    )
    client = SmugMugClient(session, root_node_uri="")

    result = client._upload_bytes("/api/v2/album/x", "photo.jpg", b"abc")
    assert result is not None

    assert session.last_kwargs is not None
    headers = session.last_kwargs["headers"]
    assert headers["Content-MD5"] == hashlib.md5(b"abc").hexdigest()
    assert len(headers["Content-MD5"]) == 32
    assert result["md5"] == headers["Content-MD5"]


def test_upload_bytes_rejects_control_chars_in_file_name():
    client = SmugMugClient(FakeSession([]), root_node_uri="")

    with pytest.raises(SmugMugError):
        client._upload_bytes("/api/v2/album/x", "bad\r\nname.jpg", b"abc")


def test_get_node_swallows_errors_by_default():
    session = FakeSession([FakeResp(404, {}, text="not found")])
    client = SmugMugClient(session, root_node_uri="")

    assert client.get_node("/api/v2/node/x") is None


def test_get_node_raise_on_error():
    session = FakeSession([FakeResp(404, {}, text="not found")])
    client = SmugMugClient(session, root_node_uri="")

    with pytest.raises(SmugMugError):
        client.get_node("/api/v2/node/x", raise_on_error=True)


UPLOAD_OK = {
    "stat": "ok",
    "Image": {"URL": "u", "ImageUri": "/api/v2/image/x", "UploadKey": "k"},
}


def test_filter_duplicates_skips_by_md5(tmp_path):
    keep = tmp_path / "keep.jpg"
    dup = tmp_path / "dup.jpg"
    keep.write_bytes(b"unique")
    dup.write_bytes(b"same")

    kept, skipped = _filter_duplicates(
        [keep, dup], {hashlib.md5(b"same").hexdigest()}, set()
    )

    assert kept == [keep]
    assert skipped == 1


def test_upload_parallel_uploads_all_files(monkeypatch, tmp_path):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "b.jpg").write_bytes(b"b")

    session = FakeSession([FakeResp(200, {}, json_data=UPLOAD_OK)])
    client = SmugMugClient(session, root_node_uri="")

    uploaded, skipped = client.upload(
        "/api/v2/album/x", tmp_path, dedup=False, max_workers=2
    )

    assert [r["file_name"] for r in uploaded] == ["a.jpg", "b.jpg"]
    assert skipped == 0


def test_upload_dedup_skips_existing_md5(monkeypatch, tmp_path):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)
    (tmp_path / "keep.jpg").write_bytes(b"new")
    dup = tmp_path / "dup.jpg"
    dup.write_bytes(b"existing")

    hashes_resp = {
        "Response": {
            "AlbumImage": [
                {
                    "FileName": "dup.jpg",
                    "ArchivedMD5": hashlib.md5(b"existing").hexdigest(),
                    "Uri": "/api/v2/image/1",
                    "ImageKey": "k",
                }
            ]
        }
    }
    session = FakeSession(
        [
            FakeResp(200, {}, json_data=hashes_resp),
            FakeResp(200, {}, json_data=UPLOAD_OK),
        ]
    )
    client = SmugMugClient(session, root_node_uri="")

    uploaded, skipped = client.upload(
        "/api/v2/album/x", tmp_path, dedup=True, max_workers=1
    )

    assert [r["file_name"] for r in uploaded] == ["keep.jpg"]
    assert skipped == 1


def test_upload_sequential_is_paced(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: slept.append(s))
    (tmp_path / "a.jpg").write_bytes(b"a")

    session = FakeSession([FakeResp(200, {}, json_data=UPLOAD_OK)])
    client = SmugMugClient(session, root_node_uri="")

    client.upload("/api/v2/album/x", tmp_path, dedup=False, max_workers=1)

    assert slept == [0.2]


def test_get_wraps_network_error_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = RaisingSession(requests.exceptions.ConnectionError("boom"))
    client = SmugMugClient(session, root_node_uri="")

    with pytest.raises(SmugMugError):
        client._get("/api/v2/album/x")
    assert session.calls == 5


def test_upload_sequential_survives_network_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)
    (tmp_path / "a.jpg").write_bytes(b"a")

    session = RaisingSession(requests.exceptions.Timeout("slow"))
    client = SmugMugClient(session, root_node_uri="")

    uploaded, skipped = client.upload("/api/v2/album/x", tmp_path, max_workers=1)

    assert uploaded == []
    assert skipped == 0


def test_delete_retries_on_server_errors_then_raises(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession([FakeResp(500, {}, text="boom")])
    client = SmugMugClient(session, root_node_uri="")

    with pytest.raises(SmugMugError):
        client._delete("/api/v2/album/x")
    assert session.calls == 5


def test_delete_album_returns_false_after_5xx_retries_exhausted(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession([FakeResp(500, {}, text="boom")])
    client = SmugMugClient(session, root_node_uri="")

    assert client.delete_album("/api/v2/album/x") is False
    assert session.calls == 5


def test_patch_description_returns_false_after_5xx_retries_exhausted(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession([FakeResp(500, {}, text="boom")])
    client = SmugMugClient(session, root_node_uri="")

    assert client.patch_album_description("/api/v2/node/x", "desc") is False
    assert session.calls == 5


def test_context_manager_closes_session(monkeypatch):
    session = FakeSession([])
    closed = []
    monkeypatch.setattr(session, "close", lambda: closed.append(True))

    with SmugMugClient(session, root_node_uri=""):
        pass

    assert closed == [True]

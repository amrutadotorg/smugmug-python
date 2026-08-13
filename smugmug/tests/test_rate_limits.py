"""Unit tests for rate-limit handling and retry/async-job behavior (no API access)."""

import logging

import pytest
from requests_oauthlib import OAuth1Session

from smugmug import RateLimitError, SmugMugClient, SmugMugError
from smugmug.client import (
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
    """Session stub — only ``get`` is used by the tests, no auth internals."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]


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
            FakeResp(200, {"X-RateLimit-Remaining": "50"}, json_data={"ok": True}),
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
            FakeResp(200, {"X-RateLimit-Remaining": "50"}, json_data={"ok": True}),
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

    assert client._poll_async_job({"Response": {"Uri": "/api/v2/job/x"}}) is True


def test_poll_async_job_returns_false_on_failed(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession(
        [FakeResp(200, {}, json_data={"Response": {"Status": "Failed"}})]
    )
    client = _job_client(session)

    assert client._poll_async_job({"Response": {"Uri": "/api/v2/job/x"}}) is False


def test_poll_async_job_returns_false_on_timeout(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    session = FakeSession(
        [FakeResp(200, {}, json_data={"Response": {"Status": "Pending"}})]
    )
    client = _job_client(session)

    assert (
        client._poll_async_job(
            {"Response": {"Uri": "/api/v2/job/x"}}, poll_interval=0.1, max_wait=0.25
        )
        is False
    )


def test_move_collect_marks_failure_when_job_fails(monkeypatch):
    """all_ok must reflect the async job outcome, not just the POST."""
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    client = _job_client(FakeSession([]))
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"Response": {"Uri": "/job"}})
    monkeypatch.setattr(client, "_poll_async_job", lambda *a, **k: False)

    assert (
        client._move_collect_chunked("/api/v2/album/to", ["/img/1"], "moveimages")
        is False
    )


def test_move_collect_returns_true_when_job_completes(monkeypatch):
    monkeypatch.setattr("smugmug.client.time.sleep", lambda s: None)

    client = _job_client(FakeSession([]))
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"Response": {"Uri": "/job"}})
    monkeypatch.setattr(client, "_poll_async_job", lambda *a, **k: True)

    assert (
        client._move_collect_chunked("/api/v2/album/to", ["/img/1"], "collectimages")
        is True
    )

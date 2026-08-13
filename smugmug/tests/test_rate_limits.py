"""Unit tests for rate-limit handling (no API access)."""

import logging

import pytest
from requests_oauthlib import OAuth1Session

from smugmug import RateLimitError, SmugMugClient
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

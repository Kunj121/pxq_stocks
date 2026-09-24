"""Tests for the Quant Data client's refusal handling. No network.

    python -m pytest research/quantdata/test_client.py -q

The server signals three different things through an HTTP 200, so the body is
the only thing that distinguishes success, a permanent refusal, and "slow down".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import client as C  # noqa: E402

RATE_LIMITED = ('qd: /v1 call failed with status 429: {"detail":"Rate limit '
                'exceeded. Retry in 1 seconds.","properties":{"limit":240,'
                '"retryAfterSeconds":1},"status":429}')


@pytest.fixture
def qd(monkeypatch):
    monkeypatch.setattr(C, "resolve_key", lambda: "key")
    monkeypatch.setattr(C.time, "sleep", lambda _: None)
    return C.QuantData()


def replies(qd, monkeypatch, *bodies):
    seq = list(bodies)
    calls = []

    def _text(tool, arguments):
        calls.append(tool)
        return seq.pop(0)

    monkeypatch.setattr(qd, "_text", _text)
    return calls


def test_a_clean_body_is_returned_as_is(qd, monkeypatch):
    replies(qd, monkeypatch, "ts,ticker\n1,NVDA")
    assert qd.call("qd_x") == "ts,ticker\n1,NVDA"


def test_rate_limit_retries_then_succeeds(qd, monkeypatch):
    """429 arrives as HTTP 200 with the status quoted in the body, so it has to
    be recognised by text. It is a pace, not a rejection."""
    calls = replies(qd, monkeypatch, RATE_LIMITED, RATE_LIMITED, "ok,1")
    assert qd.call("qd_x") == "ok,1"
    assert len(calls) == 3


def test_rate_limit_eventually_gives_up_rather_than_hanging(qd, monkeypatch):
    calls = replies(qd, monkeypatch, *[RATE_LIMITED] * C.RATE_LIMIT_RETRIES)
    with pytest.raises(C.QuantDataError, match="rate limited"):
        qd.call("qd_x")
    assert len(calls) == C.RATE_LIMIT_RETRIES


def test_a_validation_failure_is_not_retried(qd, monkeypatch):
    """Asking again cannot fix a bad argument; retrying would just be slow."""
    calls = replies(qd, monkeypatch, "input validation failed: bad field")
    with pytest.raises(C.QuantDataError, match="validation"):
        qd.call("qd_x")
    assert len(calls) == 1


def test_a_non_429_upstream_error_is_not_retried(qd, monkeypatch):
    calls = replies(qd, monkeypatch, "/v1 call failed with status 400: bad request")
    with pytest.raises(C.QuantDataError, match="400"):
        qd.call("qd_x")
    assert len(calls) == 1


def test_the_servers_own_retry_after_is_honoured(qd, monkeypatch):
    waited = []
    monkeypatch.setattr(C.time, "sleep", waited.append)
    replies(qd, monkeypatch, RATE_LIMITED, "ok")
    qd.call("qd_x")
    assert waited == [1.0]


def test_backoff_grows_when_the_server_suggests_nothing(qd, monkeypatch):
    waited = []
    monkeypatch.setattr(C.time, "sleep", waited.append)
    bare = "/v1 call failed with status 429: slow down"
    replies(qd, monkeypatch, bare, bare, "ok")
    qd.call("qd_x")
    assert waited == [1.0, 2.0]

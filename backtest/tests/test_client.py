"""Client behaviour that does not need a network: retries, embargo handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backtest.client import (
    SIP_EMBARGO,
    AlpacaClient,
    AlpacaError,
    _cap_to_sip_embargo,
    _parse_bound,
)
from backtest.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(api_key="k", secret_key="s", cache_dir=tmp_path, output_dir=tmp_path)


def _client(settings: Settings, handler) -> AlpacaClient:
    """An AlpacaClient wired to an in-memory transport instead of the internet."""
    client = AlpacaClient(settings)
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), headers=settings.auth_headers
    )
    return client


# -- embargo arithmetic ------------------------------------------------------


def test_date_only_end_is_read_as_end_of_day():
    today = datetime.now(timezone.utc).date().isoformat()
    parsed = _parse_bound(today, end_of_day=True)
    assert parsed.hour == 23 and parsed.minute == 59


def test_date_only_start_is_read_as_start_of_day():
    parsed = _parse_bound("2024-01-02", end_of_day=False)
    assert (parsed.hour, parsed.minute) == (0, 0)


def test_cap_returns_the_embargo_edge_for_a_request_ending_today():
    today = datetime.now(timezone.utc).date().isoformat()
    capped = _cap_to_sip_embargo(today, "2024-01-01")
    assert capped is not None
    edge = datetime.fromisoformat(capped)
    assert edge < datetime.now(timezone.utc) - SIP_EMBARGO + timedelta(seconds=5)


def test_cap_gives_up_when_the_whole_window_is_inside_the_embargo():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = now.isoformat()
    assert _cap_to_sip_embargo(end, start) is None


def test_cap_returns_none_when_the_window_already_predates_the_embargo():
    assert _cap_to_sip_embargo("2024-01-05", "2024-01-01") is None


# -- request behaviour -------------------------------------------------------


def test_embargo_403_is_retried_on_sip_not_downgraded(settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if len(seen) == 1:
            return httpx.Response(
                403, json={"message": "subscription does not permit querying recent SIP data"}
            )
        return httpx.Response(200, json={"bars": {"SPY": [{"t": "2024-01-02T05:00:00Z", "c": 1.0}]}})

    today = datetime.now(timezone.utc).date().isoformat()
    with _client(settings, handler) as client:
        bars = client.stock_bars(["SPY"], "1Day", "2024-01-01", today)

    assert len(bars["SPY"]) == 1
    assert [call["feed"] for call in seen] == ["sip", "sip"], "should stay on SIP"
    assert seen[1]["end"] != seen[0]["end"], "end should have been truncated"


def test_entitlement_403_falls_back_to_iex(settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        if seen[-1]["feed"] == "sip":
            return httpx.Response(403, json={"message": "subscription does not permit this feed"})
        return httpx.Response(200, json={"bars": {"SPY": [{"t": "2024-01-02T05:00:00Z", "c": 1.0}]}})

    with _client(settings, handler) as client:
        client.stock_bars(["SPY"], "1Day", "2024-01-01", "2024-01-05")
        assert client._sip_denied
        # The latch means the second call goes straight to IEX.
        client.stock_bars(["SPY"], "1Day", "2024-01-01", "2024-01-05")

    assert [call["feed"] for call in seen] == ["sip", "iex", "iex"]


def test_pagination_follows_the_page_token(settings):
    pages = [
        {"bars": {"SPY": [{"t": "2024-01-02T05:00:00Z", "c": 1.0}]}, "next_page_token": "abc"},
        {"bars": {"SPY": [{"t": "2024-01-03T05:00:00Z", "c": 2.0}]}, "next_page_token": None},
    ]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[len(seen) - 1])

    with _client(settings, handler) as client:
        bars = client.stock_bars(["SPY"], "1Day", "2024-01-01", "2024-01-05")

    assert len(bars["SPY"]) == 2
    assert seen[1]["page_token"] == "abc"


def test_retries_then_succeeds_on_500(settings, monkeypatch):
    monkeypatch.setattr("backtest.client.time.sleep", lambda _: None)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"bars": {"SPY": []}})

    with _client(settings, handler) as client:
        client.stock_bars(["SPY"], "1Day", "2024-01-01", "2024-01-05")
    assert len(attempts) == 3


def test_a_404_is_not_retried(settings):
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, text="nope")

    with _client(settings, handler) as client, pytest.raises(AlpacaError) as exc:
        client.stock_bars(["SPY"], "1Day", "2024-01-01", "2024-01-05")

    assert exc.value.status_code == 404
    assert len(attempts) == 1

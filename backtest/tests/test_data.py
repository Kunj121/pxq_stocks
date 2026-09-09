"""Data-layer tests. The Alpaca client is stubbed — no network access."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtest.config import Settings
from backtest.data import (
    BarPanel,
    TimeFrame,
    _missing_range,
    _union_range,
    load_bars,
)


# -- timeframe ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("1Day", "1Day"), ("15Min", "15Min"), ("1 hour", "1Hour"), ("4Hours", "4Hour")],
)
def test_timeframe_round_trips_to_alpaca_form(text, expected):
    assert TimeFrame.parse(text).alpaca == expected


def test_timeframe_rejects_nonsense():
    with pytest.raises(ValueError, match="Unrecognised timeframe"):
        TimeFrame.parse("daily")


def test_periods_per_year():
    assert TimeFrame.parse("1Day").periods_per_year == 252
    assert TimeFrame.parse("1Week").periods_per_year == 52
    assert TimeFrame.parse("1Min").periods_per_year == 252 * 390
    assert TimeFrame.parse("1Hour").periods_per_year == pytest.approx(252 * 6.5)


# -- panel -------------------------------------------------------------------


ALPACA_PAYLOAD = {
    "AAPL": [
        {"t": "2024-01-02T05:00:00Z", "o": 187.0, "h": 188.0, "l": 183.0, "c": 185.0, "v": 82_000_000, "n": 1_000_000, "vw": 185.8},
        {"t": "2024-01-03T05:00:00Z", "o": 184.0, "h": 185.0, "l": 183.0, "c": 184.0, "v": 58_000_000, "n": 700_000, "vw": 184.1},
    ],
    "MSFT": [
        {"t": "2024-01-02T05:00:00Z", "o": 373.0, "h": 376.0, "l": 366.0, "c": 370.0, "v": 25_000_000, "n": 400_000, "vw": 370.5},
        {"t": "2024-01-03T05:00:00Z", "o": 369.0, "h": 373.0, "l": 368.0, "c": 370.6, "v": 23_000_000, "n": 380_000, "vw": 370.9},
    ],
}


def test_panel_parses_alpaca_payload():
    panel = BarPanel.from_alpaca(ALPACA_PAYLOAD, TimeFrame.parse("1Day"), "sip")
    assert panel.symbols == ["AAPL", "MSFT"]
    assert len(panel) == 2
    assert panel.close.loc[panel.timestamps[0], "AAPL"] == 185.0


def test_panel_index_is_market_time():
    panel = BarPanel.from_alpaca(ALPACA_PAYLOAD, TimeFrame.parse("1Day"), "sip")
    first = panel.timestamps[0]
    assert str(first.tz) == "America/New_York"
    # 05:00Z is midnight ET.
    assert (first.hour, first.date()) == (0, date(2024, 1, 2))


def test_head_until_truncates():
    panel = BarPanel.from_alpaca(ALPACA_PAYLOAD, TimeFrame.parse("1Day"), "sip")
    truncated = panel.head_until(panel.timestamps[0])
    assert len(truncated) == 1


def test_empty_payload_yields_empty_panel():
    panel = BarPanel.from_alpaca({}, TimeFrame.parse("1Day"))
    assert len(panel) == 0
    assert panel.symbols == []


def test_dropna_symbols_filters_sparse_series():
    payload = {
        "AAPL": ALPACA_PAYLOAD["AAPL"],
        "IPO": [ALPACA_PAYLOAD["AAPL"][1]],  # only one of the two bars
    }
    panel = BarPanel.from_alpaca(payload, TimeFrame.parse("1Day"))
    assert panel.dropna_symbols(min_coverage=0.9).symbols == ["AAPL"]


# -- cache range arithmetic --------------------------------------------------


def test_cache_hit_when_fully_covered():
    assert _missing_range((date(2024, 2, 1), date(2024, 3, 1)),
                          (date(2024, 1, 1), date(2024, 4, 1))) is None


def test_missing_range_extends_forward():
    got = _missing_range((date(2024, 1, 1), date(2024, 5, 1)),
                         (date(2024, 1, 1), date(2024, 3, 1)))
    assert got == (date(2024, 3, 2), date(2024, 5, 1))


def test_missing_range_extends_backward():
    got = _missing_range((date(2023, 6, 1), date(2024, 2, 1)),
                         (date(2024, 1, 1), date(2024, 3, 1)))
    assert got == (date(2023, 6, 1), date(2023, 12, 31))


def test_missing_range_refetches_when_holes_on_both_sides():
    wanted = (date(2023, 1, 1), date(2024, 12, 1))
    assert _missing_range(wanted, (date(2024, 1, 1), date(2024, 3, 1))) == wanted


def test_missing_range_refetches_when_disjoint():
    wanted = (date(2020, 1, 1), date(2020, 6, 1))
    assert _missing_range(wanted, (date(2024, 1, 1), date(2024, 3, 1))) == wanted


def test_union_joins_contiguous_windows():
    assert _union_range((date(2024, 1, 1), date(2024, 3, 1)),
                        (date(2024, 3, 2), date(2024, 5, 1))) == (date(2024, 1, 1), date(2024, 5, 1))


def test_union_discards_a_disjoint_old_window():
    new = (date(2024, 6, 1), date(2024, 7, 1))
    assert _union_range((date(2020, 1, 1), date(2020, 2, 1)), new) == new


# -- load_bars against a stub client ----------------------------------------


class StubClient:
    """Stands in for AlpacaClient; records every request it is given."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else ALPACA_PAYLOAD
        self.calls: list[dict] = []

    def stock_bars(self, symbols, timeframe, start, end, **kwargs):
        self.calls.append({"symbols": list(symbols), "start": start, "end": end, **kwargs})
        return {s: self.payload.get(s, []) for s in symbols}

    def crypto_bars(self, symbols, timeframe, start, end, **kwargs):
        return self.stock_bars(symbols, timeframe, start, end, **kwargs)

    def close(self):
        pass


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_key="test", secret_key="test", cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "out",
    )


def test_load_bars_uses_the_client(settings):
    client = StubClient()
    panel = load_bars(
        ["AAPL", "MSFT"], "2024-01-02", "2024-01-03",
        client=client, settings=settings, use_cache=False,
    )
    assert panel.symbols == ["AAPL", "MSFT"]
    assert len(client.calls) == 1


def test_end_date_is_made_inclusive(settings):
    client = StubClient()
    load_bars(["AAPL"], "2024-01-02", "2024-01-03",
              client=client, settings=settings, use_cache=False)
    # Alpaca's end is exclusive, so we push it one day out.
    assert client.calls[0]["end"] == "2024-01-04"


def test_default_adjustment_is_split_and_dividend(settings):
    client = StubClient()
    load_bars(["AAPL"], "2024-01-02", "2024-01-03",
              client=client, settings=settings, use_cache=False)
    assert client.calls[0]["adjustment"] == "all"


def test_no_data_raises_a_clear_error(settings):
    client = StubClient(payload={})
    with pytest.raises(Exception, match="no bars"):
        load_bars(["ZZZZ"], "2024-01-02", "2024-01-03",
                  client=client, settings=settings, use_cache=False)


def test_second_load_hits_the_cache(settings):
    client = StubClient()
    kwargs = dict(client=client, settings=settings, use_cache=True)
    load_bars(["AAPL"], "2024-01-02", "2024-01-03", **kwargs)
    calls_after_first = len(client.calls)
    load_bars(["AAPL"], "2024-01-02", "2024-01-03", **kwargs)
    assert len(client.calls) == calls_after_first, "second load should not re-fetch"


def test_symbols_are_uppercased_and_deduped(settings):
    client = StubClient()
    load_bars(["aapl", "AAPL", " msft "], "2024-01-02", "2024-01-03",
              client=client, settings=settings, use_cache=False)
    assert client.calls[0]["symbols"] == ["AAPL", "MSFT"]


def test_backwards_date_range_raises(settings):
    with pytest.raises(ValueError, match="before start"):
        load_bars(["AAPL"], "2024-03-01", "2024-01-01",
                  client=StubClient(), settings=settings, use_cache=False)


def test_warns_when_start_predates_alpaca_history(settings, caplog):
    client = StubClient()
    with caplog.at_level("WARNING"):
        load_bars(["AAPL"], "2005-01-01", "2024-01-03",
                  client=client, settings=settings, use_cache=False)
    assert "predates Alpaca's equity history" in caplog.text


# -- intraday session filtering ----------------------------------------------

INTRADAY_PAYLOAD = {
    # 04:00, 09:29, 09:30, 15:59, 16:00 and 19:59 ET on 2024-01-02.
    "SPY": [
        {"t": "2024-01-02T09:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 10},
        {"t": "2024-01-02T14:29:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 10},
        {"t": "2024-01-02T14:30:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 10},
        {"t": "2024-01-02T20:59:00Z", "o": 4, "h": 4, "l": 4, "c": 4, "v": 10},
        {"t": "2024-01-02T21:00:00Z", "o": 5, "h": 5, "l": 5, "c": 5, "v": 10},
        {"t": "2024-01-03T00:59:00Z", "o": 6, "h": 6, "l": 6, "c": 6, "v": 10},
    ]
}


HOURLY_PAYLOAD = {
    # 08:00, 09:00, 15:00 and 16:00 ET — the 09:00 bar spans the 09:30 open.
    "SPY": [
        {"t": "2024-01-02T13:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 10},
        {"t": "2024-01-02T14:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 10},
        {"t": "2024-01-02T20:00:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 10},
        {"t": "2024-01-02T21:00:00Z", "o": 4, "h": 4, "l": 4, "c": 4, "v": 10},
    ]
}


def test_hourly_bar_spanning_the_open_is_kept(settings):
    """The 09:00 bar covers 09:30-10:00 and carries the open; dropping it loses
    the single most active stretch of the session."""
    panel = load_bars(
        ["SPY"], "2024-01-02", "2024-01-03", "1Hour",
        client=StubClient(HOURLY_PAYLOAD), settings=settings, use_cache=False,
    )
    times = [t.strftime("%H:%M") for t in panel.timestamps]
    assert times == ["09:00", "15:00"], "09:00 in, 08:00 and 16:00 out"


def test_minute_bars_tile_the_session_exactly(settings):
    """Minute timeframes divide 390 evenly, so overlap and start-time filtering
    agree and no boundary bar sneaks in."""
    bars = []
    for minute in range(0, 24 * 60, 5):
        bars.append({
            "t": f"2024-01-02T{minute // 60:02d}:{minute % 60:02d}:00Z",
            "o": 1, "h": 1, "l": 1, "c": 1, "v": 10,
        })
    panel = load_bars(
        ["SPY"], "2024-01-02", "2024-01-03", "5Min",
        client=StubClient({"SPY": bars}), settings=settings, use_cache=False,
    )
    times = [t.strftime("%H:%M") for t in panel.timestamps]
    assert len(times) == 78, "6.5 hours / 5 minutes"
    assert times[0] == "09:30" and times[-1] == "15:55"


def test_regular_session_keeps_only_market_hours(settings):
    panel = load_bars(
        ["SPY"], "2024-01-02", "2024-01-03", "1Min",
        client=StubClient(INTRADAY_PAYLOAD), settings=settings, use_cache=False,
    )
    times = [t.strftime("%H:%M") for t in panel.timestamps]
    assert times == ["09:30", "15:59"]


def test_extended_session_keeps_the_whole_day(settings):
    panel = load_bars(
        ["SPY"], "2024-01-02", "2024-01-03", "1Min", session="extended",
        client=StubClient(INTRADAY_PAYLOAD), settings=settings, use_cache=False,
    )
    times = [t.strftime("%H:%M") for t in panel.timestamps]
    assert times == ["04:00", "09:29", "09:30", "15:59", "16:00", "19:59"]


def test_session_changes_the_annualisation_factor(settings):
    kwargs = dict(client=StubClient(INTRADAY_PAYLOAD), settings=settings, use_cache=False)
    regular = load_bars(["SPY"], "2024-01-02", "2024-01-03", "1Min", **kwargs)
    extended = load_bars(
        ["SPY"], "2024-01-02", "2024-01-03", "1Min", session="extended", **kwargs
    )
    assert regular.timeframe.periods_per_year == 252 * 390
    assert extended.timeframe.periods_per_year == 252 * 960


def test_session_filter_leaves_daily_bars_alone(settings):
    """Daily bars are stamped midnight ET and must survive the regular filter."""
    panel = load_bars(
        ["AAPL"], "2024-01-02", "2024-01-03", "1Day",
        client=StubClient(), settings=settings, use_cache=False,
    )
    assert len(panel) == 2


def test_switching_session_does_not_refetch(settings):
    """The cache stores the unfiltered superset, so session is a read-time view."""
    client = StubClient(INTRADAY_PAYLOAD)
    kwargs = dict(client=client, settings=settings, use_cache=True)
    load_bars(["SPY"], "2024-01-02", "2024-01-03", "1Min", **kwargs)
    calls = len(client.calls)
    extended = load_bars(
        ["SPY"], "2024-01-02", "2024-01-03", "1Min", session="extended", **kwargs
    )
    assert len(client.calls) == calls, "should be served from cache"
    assert len(extended) == 6, "cache must hold the pre/post-market bars too"


def test_bad_session_name_is_rejected(settings):
    with pytest.raises(ValueError, match="session must be one of"):
        load_bars(["SPY"], "2024-01-02", "2024-01-03", "1Min", session="overnight",
                  client=StubClient(), settings=settings, use_cache=False)

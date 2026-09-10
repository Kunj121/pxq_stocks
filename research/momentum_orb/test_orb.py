"""Unit tests for the ORB entry and stop logic.

The intrabar rules are the fragile part of this engine — a wrong tie-break moved
total return by 1,700 points — so they are pinned here on synthetic bars where
the correct answer is known by construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orb import ORBConfig, simulate_symbol, wilder_atr

DAY = "2023-06-01"


def bars(rows):
    """rows = [(minute_offset_from_0930, o, h, l, c, v), ...]"""
    idx = pd.DatetimeIndex([
        pd.Timestamp(f"{DAY} 09:30", tz="America/New_York") + pd.Timedelta(minutes=m)
        for m, *_ in rows
    ])
    return pd.DataFrame(
        [r[1:] for r in rows], index=idx,
        columns=["open", "high", "low", "close", "volume"],
    )


def atr(value=1.0):
    return pd.Series({pd.Timestamp(DAY).date(): value})


def run(rows, cfg=None, atr_value=1.0):
    cfg = cfg or ORBConfig(opening_minutes=1)
    out = simulate_symbol(bars(rows), atr(atr_value), cfg)
    assert len(out) == 1
    return out.iloc[0]


# --- direction -------------------------------------------------------------

def test_bullish_opening_range_goes_long_only():
    r = run([(0, 100, 101, 99, 100.5, 1000),      # bullish OR
             (1, 100.4, 100.6, 99.0, 99.2, 500),  # breaks the LOW, not the high
             (2, 99.2, 99.3, 99.0, 99.1, 500)])
    assert r.direction == 1
    assert r.exit_reason == "never_triggered"     # a long never triggered; no short taken


def test_bearish_opening_range_goes_short_only():
    r = run([(0, 100, 101, 99, 99.5, 1000),
             (1, 99.6, 101.5, 99.5, 101.4, 500),  # breaks the HIGH
             (2, 101.4, 101.5, 101.3, 101.4, 500)])
    assert r.direction == -1
    assert r.exit_reason == "never_triggered"


def test_doji_is_not_traded():
    r = run([(0, 100, 101, 99, 100, 1000), (1, 100, 102, 98, 101, 500)])
    assert r.direction == 0
    assert r.exit_reason == "doji"


# --- entry pricing ---------------------------------------------------------

def test_entry_fills_at_the_range_high_when_crossed_intrabar():
    r = run([(0, 100, 101, 99, 100.5, 1000),
             (1, 100.5, 101.4, 100.4, 101.3, 500),
             (2, 101.3, 101.4, 101.2, 101.35, 500)])
    assert r.entry_price == pytest.approx(101.0)      # the range high, not the open


def test_entry_fills_at_the_open_when_the_bar_gaps_through():
    r = run([(0, 100, 101, 99, 100.5, 1000),
             (1, 101.8, 102.0, 101.7, 101.9, 500),    # opens above the trigger
             (2, 101.9, 102.0, 101.8, 101.95, 500)])
    assert r.entry_price == pytest.approx(101.8)      # the open, not 101.0


# --- the intrabar tie-break, the part that broke ---------------------------

# Long setups below all share: opening range 100/101/99/100.5 (bullish),
# trigger 101.0, ATR 1.0 -> risk 0.10, stop 100.90.

OR_BULL = (0, 100, 101, 99, 100.5, 1000)


def provably_safe_bar():
    """Entry bar OPENS at 100.0, below the 100.90 stop. Price therefore rose
    from below the stop, through it, up to the 101.0 trigger — so the bar's low
    of 99.5 necessarily printed BEFORE the fill. Not chargeable under any
    policy."""
    return [OR_BULL,
            (1, 100.0, 101.5, 99.5, 101.4, 500),
            (2, 101.4, 101.6, 101.3, 101.5, 500)]


def ambiguous_bar():
    """Entry bar opens at 100.95 — between the stop and the trigger — dips to
    99.5 and closes at 101.4. The dip may have come before or after the
    breakout. OHLC cannot say. This is the only set the bracket disputes."""
    return [OR_BULL,
            (1, 100.95, 101.5, 99.5, 101.4, 500),
            (2, 101.4, 101.6, 101.3, 101.5, 500)]


def provably_stopped_bar():
    """Entry bar closes at 100.0, below the stop. The level was crossed after
    entry and held. Chargeable under every policy."""
    return [OR_BULL,
            (1, 100.95, 101.5, 99.5, 100.0, 500),
            (2, 100.0, 100.1, 99.9, 100.0, 500)]


@pytest.mark.parametrize("policy", ["certain", "pessimistic", "optimistic"])
def test_provably_pre_entry_excursion_is_never_charged(policy):
    r = run(provably_safe_bar(), ORBConfig(opening_minutes=1, same_bar_policy=policy))
    assert r.same_bar_stop                 # the stop level is inside the bar's range
    assert not r.entry_bar_ambiguous       # ...but its timing is not in doubt
    assert r.exit_reason == "eod"
    assert r.pnl_r > 0


@pytest.mark.parametrize("policy", ["certain", "pessimistic", "optimistic"])
def test_provable_stop_is_always_charged(policy):
    r = run(provably_stopped_bar(), ORBConfig(opening_minutes=1, same_bar_policy=policy))
    assert r.exit_reason == "stop"
    assert r.pnl_r == pytest.approx(-1.0)


def test_ambiguous_bar_is_the_only_thing_the_bracket_disputes():
    amb = ambiguous_bar()
    flagged = run(amb, ORBConfig(opening_minutes=1))
    assert flagged.entry_bar_ambiguous

    lower = run(amb, ORBConfig(opening_minutes=1, same_bar_policy="pessimistic"))
    upper = run(amb, ORBConfig(opening_minutes=1, same_bar_policy="optimistic"))
    assert lower.exit_reason == "stop" and lower.pnl_r == pytest.approx(-1.0)
    assert upper.exit_reason == "eod" and upper.pnl_r > 0


def test_gap_fill_makes_the_whole_entry_bar_chargeable():
    """Filled at the open, so nothing in the bar predates the position."""
    r = run([OR_BULL,
             (1, 101.5, 101.6, 101.3, 101.55, 500),   # entry 101.5, stop 101.4
             (2, 101.55, 101.6, 101.5, 101.55, 500)])
    assert r.entry_price == pytest.approx(101.5)
    assert r.exit_reason == "stop"


# --- stop distance and R ---------------------------------------------------

def test_stop_sits_at_ten_percent_of_atr_and_r_is_that_distance():
    r = run([(0, 100, 101, 99, 100.5, 1000),
             (1, 100.5, 101.2, 100.4, 101.1, 500),
             (2, 101.1, 103.0, 101.0, 103.0, 500)], atr_value=2.0)
    assert r.risk_per_share == pytest.approx(0.2)         # 10% of ATR 2.0
    assert r.stop_price == pytest.approx(101.0 - 0.2)
    assert r.pnl_r == pytest.approx((103.0 - 101.0) / 0.2)


def test_short_trade_is_mirrored():
    r = run([(0, 100, 101, 99, 99.5, 1000),
             (1, 99.5, 99.6, 98.5, 98.6, 500),
             (2, 98.6, 98.7, 98.0, 98.0, 500)], atr_value=1.0)
    assert r.direction == -1
    assert r.entry_price == pytest.approx(99.0)           # the range low
    assert r.stop_price == pytest.approx(99.1)
    assert r.pnl_r == pytest.approx((99.0 - 98.0) / 0.1)


# --- causality -------------------------------------------------------------

def test_wilder_atr_matches_a_hand_computed_value():
    h = pd.Series([10, 11, 12, 13.0])
    l = pd.Series([9, 9.5, 10, 11.0])
    c = pd.Series([9.5, 10.5, 11, 12.0])
    a = wilder_atr(h, l, c, period=2)
    # TR = [1.0, 1.5, 2.0, 2.0]. Wilder alpha = 1/period = 1/2, adjust=False:
    #   1.0 -> 0.5(1.5)+0.5(1.0) = 1.25 -> 0.5(2.0)+0.5(1.25) = 1.625
    #       -> 0.5(2.0)+0.5(1.625) = 1.8125
    assert a.notna().sum() == 3
    assert a.iloc[-1] == pytest.approx(1.8125, rel=1e-9)
    # A simple rolling mean would give 2.0 here — the two are not interchangeable.
    assert a.iloc[-1] != pytest.approx(
        pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                  axis=1).max(axis=1).rolling(2).mean().iloc[-1])


def test_day_without_an_0930_bar_is_skipped():
    """A day whose first print is 09:33 has no comparable opening range."""
    out = simulate_symbol(bars([(3, 100, 101, 99, 100.5, 1000),
                                (4, 100.5, 102, 100, 101.5, 500)]),
                          atr(1.0), ORBConfig(opening_minutes=1))
    assert out.empty

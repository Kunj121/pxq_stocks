"""Tests for the daily runner's constraint logic.

The engine itself is tested in ../test_orb.py. What is pinned here is the part
that makes this a *Robinhood proxy* rather than a $94k/4x paper fantasy, plus
the two live-vs-backtest divergences that were found and fixed:

  * the RelVol denominator must use each symbol's own last 14 opening ranges,
    not the last 14 shared calendar dates;
  * the opening range must start at the bell, but need not have every minute.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[2])]

from daily_orb import RobinhoodProfile, build_plan, opening_ranges
from orb import ORBConfig


def sig_row(symbol, or_open, or_close, or_high, or_low, atr,
            rel=2.0, avg_vol=5e6):
    return {"symbol": symbol, "or_open": or_open, "or_close": or_close,
            "or_high": or_high, "or_low": or_low, "atr": atr,
            "rel_volume": rel, "avg_volume": avg_vol, "day_open": or_open,
            "or_volume": 1e6, "bars": 5}


def frame(rows):
    return pd.DataFrame(rows)


# --- direction and filters -------------------------------------------------

def test_bullish_range_is_long_and_bearish_is_short():
    s = frame([sig_row("UP", 100, 101, 102, 99, 2.0),
               sig_row("DN", 100, 99, 101, 98, 2.0)])
    p = build_plan(s, ORBConfig(), RobinhoodProfile()).set_index("symbol")
    assert p.loc["UP", "side"] == "buy" and p.loc["UP", "entry"] == 102
    assert p.loc["DN", "side"] == "sell" and p.loc["DN", "entry"] == 98


def test_doji_is_dropped():
    s = frame([sig_row("FLAT", 100, 100, 101, 99, 2.0)])
    assert build_plan(s, ORBConfig(), RobinhoodProfile()).empty


def test_long_only_profile_drops_shorts():
    s = frame([sig_row("UP", 100, 101, 102, 99, 2.0),
               sig_row("DN", 100, 99, 101, 98, 2.0)])
    p = build_plan(s, ORBConfig(), RobinhoodProfile(allow_short=False))
    assert set(p["symbol"]) == {"UP"}


def test_atr_filter_is_strictly_greater_than_the_threshold():
    """ATR exactly at $0.50 does not qualify; the paper's filter is `>`."""
    at = frame([sig_row("AT", 100, 101, 102, 99, 0.50)])
    over = frame([sig_row("OVER", 100, 101, 102, 99, 0.51)])
    assert build_plan(at, ORBConfig(), RobinhoodProfile()).empty
    assert not build_plan(over, ORBConfig(), RobinhoodProfile()).empty


def test_relvol_below_one_is_excluded():
    s = frame([sig_row("LOW", 100, 101, 102, 99, 2.0, rel=0.8)])
    assert build_plan(s, ORBConfig(), RobinhoodProfile()).empty


# --- the Robinhood constraints, which are the point ------------------------

def test_gross_never_exceeds_one_times_capital():
    """1x means 1x. Sizing 20 names at 1% risk each would gross far more."""
    # ATR must clear $0.50 strictly — the paper's filter is `>`, not `>=`.
    rows = [sig_row(f"S{i}", 50, 51, 52, 49, 0.6) for i in range(20)]
    prof = RobinhoodProfile(capital=10_000, max_leverage=1.0)
    p = build_plan(frame(rows), ORBConfig(), prof)
    assert p["notional"].sum() <= prof.capital * prof.max_leverage + 1e-6


def test_shares_are_whole_numbers():
    rows = [sig_row(f"S{i}", 137.77, 139.13, 140.01, 136.5, 1.3) for i in range(6)]
    p = build_plan(frame(rows), ORBConfig(), RobinhoodProfile())
    assert (p["shares"] == p["shares"].astype(int)).all()
    assert (p["shares"] > 0).all()


def test_expensive_wide_stop_names_quantize_out_at_small_capital():
    """A $660 name with a $2.15 stop cannot be held at all on $10k at 1x once
    the leverage cap scales it down. Dropping it is correct, not a bug — and it
    is why small accounts run a different strategy than the paper's."""
    rows = [sig_row("BIG", 660, 665, 666, 655, 21.5)] + \
           [sig_row(f"S{i}", 20, 20.5, 20.6, 19.8, 0.6) for i in range(19)]
    p = build_plan(frame(rows), ORBConfig(), RobinhoodProfile(capital=10_000))
    assert "BIG" not in set(p["symbol"])
    # ...and it survives when the account is large enough to hold one share
    p2 = build_plan(frame(rows), ORBConfig(), RobinhoodProfile(capital=500_000))
    assert "BIG" in set(p2["symbol"])


def test_stop_is_ten_percent_of_atr_on_the_correct_side():
    s = frame([sig_row("UP", 100, 101, 102, 99, 3.0),
               sig_row("DN", 100, 99, 101, 98, 3.0)])
    p = build_plan(s, ORBConfig(), RobinhoodProfile()).set_index("symbol")
    assert p.loc["UP", "stop"] == pytest.approx(102 - 0.3)   # below a long entry
    assert p.loc["DN", "stop"] == pytest.approx(98 + 0.3)    # above a short entry


def test_position_count_is_capped():
    rows = [sig_row(f"S{i}", 20, 20.5, 20.6, 19.8, 0.6, rel=1 + i / 100)
            for i in range(40)]
    p = build_plan(frame(rows), ORBConfig(), RobinhoodProfile(max_positions=20))
    assert len(p) <= 20


def test_highest_relvol_names_are_the_ones_selected():
    rows = [sig_row(f"S{i}", 20, 20.5, 20.6, 19.8, 0.6, rel=1 + i / 10)
            for i in range(10)]
    p = build_plan(frame(rows), ORBConfig(), RobinhoodProfile(max_positions=3))
    assert set(p["symbol"]) == {"S9", "S8", "S7"}


# --- opening range ---------------------------------------------------------

def _bars(times, symbol="A"):
    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(f"2026-09-10 {t}", tz="America/New_York"), symbol)
         for t in times], names=["timestamp", "symbol"])
    n = len(times)
    return pd.DataFrame({"open": [10.0] * n, "high": [11.0] * n,
                         "low": [9.0] * n, "close": [10.5] * n,
                         "volume": [100.0] * n}, index=idx)


def test_opening_range_flags_a_missing_bell_bar():
    full = opening_ranges(_bars(["09:30", "09:31", "09:32", "09:33", "09:34"]))
    assert bool(full["has_open_bar"].iloc[0]) and int(full["bars"].iloc[0]) == 5

    late = opening_ranges(_bars(["09:32", "09:33", "09:34"]))
    assert not bool(late["has_open_bar"].iloc[0])


def test_opening_range_tolerates_a_gap_after_the_bell():
    """A name with no print at 09:31 still has a valid opening range — the
    backtest accepts it, so the live runner must too."""
    gappy = opening_ranges(_bars(["09:30", "09:32", "09:34"]))
    assert bool(gappy["has_open_bar"].iloc[0])
    assert int(gappy["bars"].iloc[0]) == 3


def test_opening_range_ignores_bars_after_the_window():
    o = opening_ranges(_bars(["09:30", "09:31", "10:15", "15:59"]))
    assert int(o["bars"].iloc[0]) == 2

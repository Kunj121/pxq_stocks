"""Unit tests for the IBS engine.

The dangerous bug in a daily cross-sectional strategy is not an off-by-one in a
loop, it is an off-by-one in *time*: aligning a signal with the return of the bar
that produced it. That prints a huge Sharpe and looks like success. It is pinned
here with a synthetic series whose correct answer is known by construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ibs import (IBSConfig, compute_ibs, hit_probabilities, hold_overlap,
                 minmax_weights, run, threshold_weights)


def frame(data, cols, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(data))
    return pd.DataFrame(data, index=idx, columns=cols)


# --- the indicator ---------------------------------------------------------

def test_ibs_is_zero_at_the_low_and_one_at_the_high():
    h = frame([[10.0]], ["A"]); l = frame([[8.0]], ["A"])
    assert compute_ibs(h, l, frame([[8.0]], ["A"])).iloc[0, 0] == 0.0
    assert compute_ibs(h, l, frame([[10.0]], ["A"])).iloc[0, 0] == 1.0
    assert compute_ibs(h, l, frame([[9.0]], ["A"])).iloc[0, 0] == pytest.approx(0.5)


def test_zero_range_bar_is_nan_not_half():
    """H == L has no meaningful IBS. Filling it with 0.5 would invent a signal
    and make the ETF eligible for the Min-Max ranking."""
    v = compute_ibs(frame([[5.0]], ["A"]), frame([[5.0]], ["A"]),
                    frame([[5.0]], ["A"])).iloc[0, 0]
    assert np.isnan(v)


def test_ibs_is_invariant_to_price_scaling():
    """Dividend/split adjustment scales O/H/L/C by a common factor, so IBS is
    unchanged. This is why the choice of adjustment does not affect the signal."""
    h, l, c = frame([[10.0]], ["A"]), frame([[8.0]], ["A"]), frame([[9.5]], ["A"])
    base = compute_ibs(h, l, c).iloc[0, 0]
    scaled = compute_ibs(h * 0.37, l * 0.37, c * 0.37).iloc[0, 0]
    assert base == pytest.approx(scaled)


def test_multi_day_ibs_uses_the_rolling_range():
    h = frame([[10.0], [12.0]], ["A"]); l = frame([[8.0], [9.0]], ["A"])
    c = frame([[9.0], [10.0]], ["A"])
    two = compute_ibs(h, l, c, window=2)
    assert np.isnan(two.iloc[0, 0])                     # needs 2 days
    assert two.iloc[1, 0] == pytest.approx((10 - 8) / (12 - 8))


# --- cross-sectional selection ---------------------------------------------

def test_minmax_longs_the_lowest_and_shorts_the_highest():
    ibs = frame([[0.1, 0.5, 0.9]], list("ABC"))
    w = minmax_weights(ibs, IBSConfig())
    assert w.iloc[0]["A"] > 0 and w.iloc[0]["C"] < 0
    assert w.iloc[0]["B"] == 0
    assert w.iloc[0].abs().sum() == pytest.approx(1.0)   # gross exposure
    assert w.iloc[0].sum() == pytest.approx(0.0)         # dollar neutral


def test_top_n_takes_n_per_leg():
    ibs = frame([[0.1, 0.2, 0.5, 0.8, 0.9]], list("ABCDE"))
    w = minmax_weights(ibs, IBSConfig(n_positions=2)).iloc[0]
    assert (w > 0).sum() == 2 and (w < 0).sum() == 2
    assert w["A"] > 0 and w["B"] > 0 and w["D"] < 0 and w["E"] < 0
    assert w["C"] == 0


def test_day_with_too_few_etfs_is_skipped():
    ibs = frame([[0.1, np.nan, np.nan]], list("ABC"))
    assert minmax_weights(ibs, IBSConfig()).iloc[0].abs().sum() == 0.0


def test_nan_etfs_are_never_selected():
    """A delisted or not-yet-listed ETF must not win the ranking by being NaN."""
    ibs = frame([[np.nan, 0.4, 0.9]], list("ABC"))
    w = minmax_weights(ibs, IBSConfig()).iloc[0]
    assert w["A"] == 0.0 and w["B"] > 0 and w["C"] < 0


def test_long_only_drops_the_short_leg():
    ibs = frame([[0.1, 0.5, 0.9]], list("ABC"))
    w = minmax_weights(ibs, IBSConfig(long_only=True)).iloc[0]
    assert (w < 0).sum() == 0
    assert w["A"] == pytest.approx(1.0)


def test_threshold_requires_both_sides_crossed():
    both = frame([[0.1, 0.5, 0.9]], list("ABC"))
    assert threshold_weights(both, IBSConfig()).iloc[0].abs().sum() > 0
    one_side = frame([[0.1, 0.5, 0.6]], list("ABC"))   # nothing above 0.8
    assert threshold_weights(one_side, IBSConfig()).iloc[0].abs().sum() == 0


# --- causality, the one that matters ---------------------------------------

def test_signal_earns_the_NEXT_days_return_not_todays():
    """Two ETFs, hand-built. On day 0 A closes on its low (IBS 0) and B on its
    high (IBS 1), so we go long A / short B at the day-0 close. Day 1 then moves
    A up 10% and B down 10%. The strategy's only non-zero return must land on
    day 1 and equal 0.5*10% + 0.5*10% = 10%.

    If the shift is missing, the return lands on day 0 instead — the position
    would have earned the very move that generated its signal.
    """
    cols = ["A", "B"]
    high = frame([[10, 10], [11, 10], [11, 10]], cols)
    low = frame([[9, 9], [11, 9], [11, 9]], cols)
    close = frame([[9.0, 10.0], [11.0, 9.0], [11.0, 9.0]], cols)
    ibs = compute_ibs(high, low, close)
    assert ibs.iloc[0]["A"] == 0.0 and ibs.iloc[0]["B"] == 1.0

    r = run(ibs, close, IBSConfig(borrow_rate_daily=0.0))
    ret = r["returns"]
    day0, day1 = close.index[0], close.index[1]

    # Day 0 has no prior signal, so it yields nothing and is dropped entirely.
    assert day0 not in ret.index

    # The whole move lands on day 1 — the session AFTER the signal.
    a_up = 11.0 / 9.0 - 1.0          # +22.2%
    b_down = 9.0 / 10.0 - 1.0        # -10.0%
    assert ret.loc[day1] == pytest.approx(0.5 * a_up - 0.5 * b_down)


def test_a_pure_lookahead_would_have_been_caught():
    """Sanity on the test itself: mis-aligning the weights by using the same
    day's return produces a different, larger number. If the two were equal the
    causality test above would prove nothing."""
    cols = ["A", "B"]
    high = frame([[10, 10], [11, 10], [11, 10]], cols)
    low = frame([[9, 9], [11, 9], [11, 9]], cols)
    close = frame([[9.0, 10.0], [11.0, 9.0], [11.0, 9.0]], cols)
    ibs = compute_ibs(high, low, close)
    w = minmax_weights(ibs, IBSConfig())
    returns = close.pct_change()
    causal = (w.shift(1) * returns).sum(axis=1, min_count=1)
    leaked = (w * returns).sum(axis=1, min_count=1)
    assert not np.isclose(causal.iloc[1], leaked.iloc[1])


# --- costs and mechanics ---------------------------------------------------

def test_borrow_is_charged_only_on_the_short_leg():
    cols = ["A", "B"]
    high = frame([[10, 10]] * 3, cols); low = frame([[9, 9]] * 3, cols)
    close = frame([[9.0, 10.0], [9.5, 9.5], [9.5, 9.5]], cols)
    ibs = compute_ibs(high, low, close)
    free = run(ibs, close, IBSConfig(borrow_rate_daily=0.0))["returns"]
    paid = run(ibs, close, IBSConfig(borrow_rate_daily=0.001))["returns"]
    # short leg is 0.5 of gross, so the drag is 0.5 * 0.001 per day held
    assert (free.iloc[1] - paid.iloc[1]) == pytest.approx(0.5 * 0.001)

    long_only = run(ibs, close, IBSConfig(long_only=True, borrow_rate_daily=0.001))
    no_borrow = run(ibs, close, IBSConfig(long_only=True, borrow_rate_daily=0.0))
    assert long_only["returns"].iloc[1] == pytest.approx(no_borrow["returns"].iloc[1])


def test_multi_day_hold_keeps_gross_exposure_constant():
    ibs = frame([[0.1, 0.9], [0.9, 0.1], [0.1, 0.9], [0.5, 0.5]], ["A", "B"])
    w = minmax_weights(ibs, IBSConfig())
    held = hold_overlap(w, 3)
    assert held.iloc[2].abs().sum() <= 1.0 + 1e-9
    # opposite signals on consecutive days must partially cancel, not stack
    assert abs(held.iloc[1]["A"]) < abs(w.iloc[1]["A"])


def test_hit_probabilities_count_the_right_direction():
    """Long bucket counts UP days; short bucket counts DOWN days."""
    ibs = frame([[0.1], [0.9], [0.1], [0.9], [0.5]], ["A"])
    close = frame([[100.0], [110.0], [99.0], [90.0], [95.0]], ["A"])
    p = hit_probabilities(ibs, close).loc["A"]
    # IBS 0.1 on rows 0 and 2 -> next-day returns +10% and -9.09% -> 1 of 2 up
    assert p["long_on_ibs_below"] == pytest.approx(0.5)
    assert p["n_long"] == 2
    # IBS 0.9 on rows 1 and 3 -> next-day returns -10% and +5.6% -> 1 of 2 down
    assert p["short_on_ibs_above"] == pytest.approx(0.5)


def test_open_to_open_waits_for_the_next_open():
    """The signal is known only after day t's close, so an open-to-open trade
    cannot start until the open of t+1. Measuring open_t -> open_{t+1} would
    span the move that created the signal — an inverted look-ahead that prints a
    large NEGATIVE Sharpe rather than a small one."""
    cols = ["A", "B"]
    n = 6
    high = frame([[10, 10]] * n, cols)
    low = frame([[9, 9]] * n, cols)
    close = frame([[9.0, 10.0]] * n, cols)      # A always on its low, B on its high
    opens = frame([[9.5, 9.5]] * n, cols)
    ibs = compute_ibs(high, low, close)

    c2c = run(ibs, close, IBSConfig(borrow_rate_daily=0.0))
    o2o = run(ibs, opens, IBSConfig(price_field="open", borrow_rate_daily=0.0))

    # The open-to-open book is held one session later than the close book.
    assert o2o["weights"].notna().any().any()
    first_c2c = c2c["weights"].abs().sum(axis=1).gt(0).idxmax()
    first_o2o = o2o["weights"].abs().sum(axis=1).gt(0).idxmax()
    assert first_o2o > first_c2c


def test_single_etf_threshold_needs_require_both_off():
    """One ETF can never be below 0.2 and above 0.8 on the same day, so the
    paper's basket rule (§4.8) would make a single-ETF threshold never trade."""
    ibs = frame([[0.1], [0.9], [0.5]], ["A"])
    both = threshold_weights(ibs, IBSConfig(), require_both=True)
    assert both.abs().sum().sum() == 0.0
    solo = threshold_weights(ibs, IBSConfig(), require_both=False)
    assert solo.iloc[0]["A"] > 0     # IBS 0.1 -> long
    assert solo.iloc[1]["A"] < 0     # IBS 0.9 -> short
    assert solo.iloc[2]["A"] == 0    # IBS 0.5 -> flat

"""Unit tests for the high-frequency engine.

Two things here are easy to get silently wrong and expensive if you do:

1. **The volatility estimator must not see the return it is testing.** If the
   trailing bipower window includes t, a jump inflates its own denominator and
   hides itself. That is a look-ahead in disguise.
2. **Overnight returns must not enter the panel.** A gap between yesterday's
   close and today's open is not a diffusion increment; leaving it in produces a
   'jump' at 09:30 every single day.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hf import (BARS_PER_DAY, HFConfig, build_weights, decompose, detect_jumps,
                estimate_month, gumbel_threshold, intraday_returns,
                local_volatility, pca_factors, variation_stats)


def session_index(days: int, bars: int = BARS_PER_DAY) -> pd.DatetimeIndex:
    stamps = []
    for d in pd.bdate_range("2024-01-02", periods=days):
        stamps += [d + pd.Timedelta(minutes=30 * i + 570) for i in range(bars)]
    return pd.DatetimeIndex(stamps)


# --- returns ---------------------------------------------------------------

def test_overnight_return_is_excluded():
    idx = session_index(3)
    close = pd.DataFrame({"A": np.linspace(100, 130, len(idx))}, index=idx)
    r = intraday_returns(close)
    first_bars = r.index.normalize() != pd.Series(r.index.normalize(),
                                                  index=r.index).shift(1)
    assert r[first_bars.to_numpy()].isna().all().all()
    # every other bar has a return
    assert r[~first_bars.to_numpy()].notna().all().all()
    assert r.notna().sum().iloc[0] == 3 * (BARS_PER_DAY - 1)


def test_returns_are_log_returns():
    idx = session_index(1)
    px = np.full(len(idx), 100.0); px[1] = 110.0
    r = intraday_returns(pd.DataFrame({"A": px}, index=idx))
    assert r["A"].iloc[1] == pytest.approx(np.log(1.1))


# --- Lee & Mykland ---------------------------------------------------------

def test_gumbel_threshold_grows_with_sample_size():
    """More observations means a higher bar — otherwise the maximum of many
    draws would be flagged as a jump by construction."""
    assert gumbel_threshold(100, 0.05) < gumbel_threshold(10_000, 0.05)
    assert gumbel_threshold(1000, 0.01) > gumbel_threshold(1000, 0.05)


def test_local_volatility_never_sees_the_current_return():
    """The window must end strictly before t. A jump that inflates its own
    denominator masks itself — the classic way this test fails silently."""
    r = pd.Series([0.001] * 60 + [0.5] + [0.001] * 10)
    sigma = local_volatility(r, window=20)
    before = sigma.iloc[60]      # at the jump
    after = sigma.iloc[63]       # a few bars later
    assert np.isfinite(before)
    assert after > before        # the jump only raises vol AFTER it happens


def test_a_large_isolated_return_is_flagged_as_a_jump():
    idx = session_index(40)
    rng = np.random.default_rng(0)
    r = pd.DataFrame({"A": rng.normal(0, 0.001, len(idx))}, index=idx)
    r.iloc[300, 0] = 0.08                       # ~80 sigma
    jumps = detect_jumps(r, HFConfig(lm_window=78))
    assert jumps.iloc[300, 0]
    assert jumps["A"].sum() <= 3                # and almost nothing else


def test_pure_diffusion_produces_few_false_positives():
    """With no jumps present, a 5% test should flag very little."""
    idx = session_index(120)
    rng = np.random.default_rng(7)
    r = pd.DataFrame({f"S{i}": rng.normal(0, 0.002, len(idx)) for i in range(20)},
                     index=idx)
    jumps = detect_jumps(r, HFConfig())
    rate = jumps.to_numpy().sum() / r.notna().to_numpy().sum()
    assert rate < 0.01


def test_decomposition_is_exact():
    idx = session_index(30)
    rng = np.random.default_rng(3)
    r = pd.DataFrame({"A": rng.normal(0, 0.002, len(idx))}, index=idx)
    r.iloc[100, 0] = 0.06
    jumps = detect_jumps(r, HFConfig())
    r_c, r_j = decompose(r, jumps)
    recombined = (r_c.fillna(0) + r_j.fillna(0))
    assert np.allclose(recombined.to_numpy(), r.fillna(0).to_numpy())


def test_rv_equals_cv_plus_jv():
    idx = session_index(45)
    rng = np.random.default_rng(11)
    r = pd.DataFrame({"A": rng.normal(0, 0.002, len(idx)),
                      "B": rng.normal(0, 0.003, len(idx))}, index=idx)
    r.iloc[200, 0] = 0.07
    stats = variation_stats(r, detect_jumps(r, HFConfig()))
    assert np.allclose(stats["rv"], stats["cv"] + stats["jv"])
    assert (stats["jump_share"].dropna() >= 0).all()
    assert (stats["jump_share"].dropna() <= 1).all()


# --- factor structure ------------------------------------------------------

def test_pca_recovers_a_planted_single_factor():
    rng = np.random.default_rng(1)
    T, N = 400, 60
    f = rng.normal(0, 1, T)
    loadings = rng.uniform(0.5, 1.5, N)
    panel = np.outer(loadings, f) + rng.normal(0, 0.05, (N, T))
    factors, load_hat = pca_factors(panel, 1)
    corr = abs(np.corrcoef(factors[0], f)[0, 1])
    assert corr > 0.99
    assert abs(np.corrcoef(load_hat[:, 0], loadings)[0, 1]) > 0.95


def test_zero_factors_requested_returns_empty():
    f, l = pca_factors(np.zeros((10, 20)), 0)
    assert f.shape[0] == 0 and l.shape[1] == 0


def test_alpha_recovers_a_planted_mispricing():
    """Stocks are a factor plus a constant drift. The estimated alpha must line
    up with the planted drift in the cross-section."""
    rng = np.random.default_rng(5)
    idx = session_index(21)
    T, N = len(idx), 40
    f = rng.normal(0, 0.002, T)
    beta = rng.uniform(0.5, 1.5, N)
    planted = np.linspace(-2e-4, 2e-4, N)
    panel = np.outer(beta, f).T + planted + rng.normal(0, 2e-4, (T, N))
    cols = [f"S{i}" for i in range(N)]
    r = pd.DataFrame(panel, index=idx, columns=cols)
    zero = pd.DataFrame(0.0, index=idx, columns=cols)
    est = estimate_month(r, zero, r, HFConfig(k_diffusive=1, k_jump=0))
    assert np.corrcoef(est["alpha"], planted)[0, 1] > 0.9


# --- portfolio -------------------------------------------------------------

def test_weights_are_dollar_neutral_and_vol_scaled():
    idx = session_index(21)
    cols = [f"S{i}" for i in range(30)]
    rng = np.random.default_rng(2)
    r = pd.DataFrame(rng.normal(0, 0.002, (len(idx), 30)), index=idx, columns=cols)
    alpha = pd.Series(rng.normal(0, 1e-4, 30), index=cols)
    cfg = HFConfig(target_vol=0.10)
    w = build_weights(alpha, r, cfg)
    assert abs(w.sum()) < 1e-10                       # dollar neutral
    port = (r * w).sum(axis=1)
    from hf import INTERVALS_PER_YEAR
    realised = port.std(ddof=1) * np.sqrt(INTERVALS_PER_YEAR)
    assert realised == pytest.approx(0.10, rel=0.02)  # scaled as requested


def test_degenerate_alpha_gives_flat_weights():
    idx = session_index(5)
    cols = ["A", "B"]
    r = pd.DataFrame(0.0, index=idx, columns=cols)
    w = build_weights(pd.Series([0.0, 0.0], index=cols), r, HFConfig())
    assert (w == 0).all()

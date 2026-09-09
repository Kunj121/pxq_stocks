"""Metrics are pure functions — check them against hand-computable cases."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest import metrics


def test_total_return_compounds():
    returns = pd.Series([0.1, 0.1])
    assert metrics.total_return(returns) == pytest.approx(0.21)


def test_cagr_of_one_year_of_daily_returns():
    daily = pd.Series([0.001] * 252)
    assert metrics.cagr(daily, 252) == pytest.approx(1.001**252 - 1, rel=1e-9)


def test_volatility_annualises():
    rng = np.random.default_rng(0)
    daily = pd.Series(rng.normal(0, 0.01, 5000))
    assert metrics.volatility(daily, 252) == pytest.approx(0.01 * np.sqrt(252), rel=0.05)


def test_sharpe_is_zero_for_constant_returns():
    assert metrics.sharpe(pd.Series([0.001] * 50)) == 0.0


def test_max_drawdown_matches_manual_calculation():
    # +100%, then -50% -> back to the start: a 50% drawdown from the peak.
    returns = pd.Series([1.0, -0.5])
    assert metrics.max_drawdown(returns) == pytest.approx(-0.5)


def test_max_drawdown_duration_counts_underwater_bars():
    returns = pd.Series([0.1, -0.05, -0.05, 0.2, 0.0])
    assert metrics.max_drawdown_duration(returns) == 2


def test_sortino_ignores_upside_volatility():
    up_only = pd.Series([0.01, 0.02, 0.03])
    assert metrics.sortino(up_only) == float("inf")


def test_profit_factor():
    returns = pd.Series([0.02, -0.01, 0.01, -0.01])
    assert metrics.profit_factor(returns) == pytest.approx(1.5)


def test_beta_against_itself_is_one():
    rng = np.random.default_rng(1)
    series = pd.Series(rng.normal(0, 0.01, 300))
    beta, alpha = metrics.beta_alpha(series, series)
    assert beta == pytest.approx(1.0)
    assert alpha == pytest.approx(0.0, abs=1e-9)


def test_beta_of_double_leverage_is_two():
    rng = np.random.default_rng(2)
    bench = pd.Series(rng.normal(0, 0.01, 300))
    beta, _ = metrics.beta_alpha(bench * 2, bench)
    assert beta == pytest.approx(2.0)


def test_empty_series_does_not_raise():
    stats = metrics.performance_stats(pd.Series(dtype=float))
    assert stats["total_return"] == 0.0
    assert stats["sharpe"] == 0.0


def test_var_and_cvar_ordering():
    rng = np.random.default_rng(3)
    returns = pd.Series(rng.normal(0, 0.01, 2000))
    var = metrics.value_at_risk(returns)
    cvar = metrics.conditional_value_at_risk(returns)
    assert cvar <= var < 0

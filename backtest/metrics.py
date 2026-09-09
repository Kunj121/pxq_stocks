"""Performance statistics for a return series.

Everything here is pure pandas/numpy — no I/O, no Alpaca — so it is the easiest
part of the package to test and to reuse against a live-trading equity curve.

Conventions
-----------
* Returns are **simple** (not log) per-bar returns.
* Annualisation uses the bar frequency's ``periods_per_year`` (252 for daily).
* Sharpe and Sortino are computed on excess returns over ``risk_free`` (an
  *annual* rate, de-annualised internally).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def _clean(returns: pd.Series) -> pd.Series:
    return pd.Series(returns).replace([np.inf, -np.inf], np.nan).dropna().astype(float)


def total_return(returns: pd.Series) -> float:
    """Compounded return over the whole sample."""
    r = _clean(returns)
    return float((1 + r).prod() - 1) if len(r) else 0.0


def cagr(returns: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR) -> float:
    """Compound annual growth rate, from bar count (not calendar dates)."""
    r = _clean(returns)
    if len(r) == 0 or periods_per_year <= 0:
        return 0.0
    years = len(r) / periods_per_year
    growth = float((1 + r).prod())
    if years <= 0 or growth <= 0:
        return 0.0
    return growth ** (1 / years) - 1


def volatility(returns: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised standard deviation of returns."""
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe(
    returns: pd.Series,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    risk_free: float = 0.0,
) -> float:
    """Annualised Sharpe ratio of excess returns."""
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    excess = r - risk_free / periods_per_year
    sigma = excess.std(ddof=1)
    if sigma == 0:
        return 0.0
    return float(excess.mean() / sigma * np.sqrt(periods_per_year))


def sortino(
    returns: pd.Series,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    risk_free: float = 0.0,
) -> float:
    """Sharpe's downside-only cousin: penalises only returns below the target."""
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    excess = r - risk_free / periods_per_year
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf") if excess.mean() > 0 else 0.0
    # Root-mean-square of the downside, measured against the full sample length.
    dd = np.sqrt((downside**2).sum() / len(excess))
    if dd == 0:
        return 0.0
    return float(excess.mean() / dd * np.sqrt(periods_per_year))


def drawdown_series(returns: pd.Series) -> pd.Series:
    """Fractional drawdown from the running peak, per bar."""
    r = _clean(returns)
    curve = (1 + r).cumprod()
    return curve / curve.cummax() - 1


def max_drawdown(returns: pd.Series) -> float:
    """Worst peak-to-trough decline, as a negative fraction."""
    dd = drawdown_series(returns)
    return float(dd.min()) if len(dd) else 0.0


def calmar(returns: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR) -> float:
    """CAGR divided by the absolute max drawdown."""
    mdd = abs(max_drawdown(returns))
    if mdd == 0:
        return 0.0
    return cagr(returns, periods_per_year) / mdd


def max_drawdown_duration(returns: pd.Series) -> int:
    """Longest run of bars spent below a previous peak."""
    dd = drawdown_series(returns)
    if dd.empty:
        return 0
    underwater = dd < 0
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return int(longest)


def win_rate(returns: pd.Series) -> float:
    """Fraction of bars with a positive return."""
    r = _clean(returns)
    nonzero = r[r != 0]
    return float((nonzero > 0).mean()) if len(nonzero) else 0.0


def profit_factor(returns: pd.Series) -> float:
    """Gross gains divided by gross losses."""
    r = _clean(returns)
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def value_at_risk(returns: pd.Series, level: float = 0.05) -> float:
    """Historical VaR: the ``level`` quantile of the return distribution."""
    r = _clean(returns)
    return float(r.quantile(level)) if len(r) else 0.0


def conditional_value_at_risk(returns: pd.Series, level: float = 0.05) -> float:
    """Expected shortfall: the mean of returns at or below the VaR threshold."""
    r = _clean(returns)
    if not len(r):
        return 0.0
    threshold = r.quantile(level)
    tail = r[r <= threshold]
    return float(tail.mean()) if len(tail) else 0.0


def beta_alpha(
    returns: pd.Series,
    benchmark: pd.Series,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> tuple[float, float]:
    """OLS beta against a benchmark, and the annualised alpha that falls out.

    Both series are aligned on their shared index first; non-overlapping bars are
    dropped rather than filled.
    """
    joined = pd.concat([_clean(returns), _clean(benchmark)], axis=1, join="inner").dropna()
    if len(joined) < 2:
        return 0.0, 0.0
    strat, bench = joined.iloc[:, 0], joined.iloc[:, 1]
    variance = bench.var(ddof=1)
    if variance == 0:
        return 0.0, 0.0
    b = float(strat.cov(bench) / variance)
    a = float((strat.mean() - b * bench.mean()) * periods_per_year)
    return b, a


def performance_stats(
    returns: pd.Series,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
    risk_free: float = 0.0,
    benchmark: pd.Series | None = None,
) -> dict[str, float]:
    """The full statistics dictionary used by reports and ``BacktestResult``."""
    r = _clean(returns)
    stats: dict[str, float] = {
        "periods": float(len(r)),
        "total_return": total_return(r),
        "cagr": cagr(r, periods_per_year),
        "volatility": volatility(r, periods_per_year),
        "sharpe": sharpe(r, periods_per_year, risk_free),
        "sortino": sortino(r, periods_per_year, risk_free),
        "max_drawdown": max_drawdown(r),
        "max_drawdown_bars": float(max_drawdown_duration(r)),
        "calmar": calmar(r, periods_per_year),
        "win_rate": win_rate(r),
        "profit_factor": profit_factor(r),
        "var_95": value_at_risk(r, 0.05),
        "cvar_95": conditional_value_at_risk(r, 0.05),
        "best_period": float(r.max()) if len(r) else 0.0,
        "worst_period": float(r.min()) if len(r) else 0.0,
    }
    if benchmark is not None:
        b, a = beta_alpha(r, benchmark, periods_per_year)
        stats["beta"] = b
        stats["alpha"] = a
        stats["benchmark_return"] = total_return(benchmark)
        stats["benchmark_sharpe"] = sharpe(benchmark, periods_per_year, risk_free)
        stats["excess_return"] = stats["total_return"] - stats["benchmark_return"]
    return stats

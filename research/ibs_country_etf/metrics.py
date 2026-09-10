"""Performance statistics for the IBS replication.

The paper reports Sharpe almost exclusively, so that is the primary column, with
the usual companions added because a Sharpe alone hides how a strategy fails.

Conventions, stated because the paper does not state them:
- **Sharpe** is annualised daily Sharpe with a **zero** risk-free rate. The paper
  gives no rf, and its 2009-2019 window was mostly ZIRP, so rf = 0 matches it.
- **Volatility** is daily sigma annualised by sqrt(252).
- **CAGR** is geometric, from the compounded equity curve.
- **Time in** is the share of sessions holding any position — the column the
  paper uses to argue Min-Max beats the threshold strategy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    return float((1.0 - equity / equity.cummax()).max())


def sharpe(returns: pd.Series) -> float:
    sd = returns.std(ddof=1)
    if not sd or np.isnan(sd):
        return float("nan")
    return float(returns.mean() / sd * np.sqrt(TRADING_DAYS))


def summarize(result: dict, label: str = "") -> dict:
    """Table-shaped statistics for one `ibs.run` result."""
    r = result["returns"].dropna()
    eq = (1.0 + r).cumprod()
    n = len(r)
    years = n / TRADING_DAYS if n else np.nan
    return {
        "strategy": label or result["config"].label,
        "sharpe": sharpe(r),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if n and years else np.nan,
        "total_return": float(eq.iloc[-1] - 1) if n else np.nan,
        "volatility": float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)),
        "mdd": max_drawdown(eq) if n else np.nan,
        "hit_ratio": float((r > 0).mean()) if n else np.nan,
        "worst_day": float(r.min()) if n else np.nan,
        "time_in": result.get("time_in", np.nan),
        "days": n,
    }


def benchmark_row(close: pd.Series, label: str) -> dict:
    r = close.pct_change().dropna()
    eq = (1.0 + r).cumprod()
    years = len(r) / TRADING_DAYS
    return {
        "strategy": label, "sharpe": sharpe(r),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1),
        "total_return": float(eq.iloc[-1] - 1),
        "volatility": float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)),
        "mdd": max_drawdown(eq), "hit_ratio": float((r > 0).mean()),
        "worst_day": float(r.min()), "time_in": 1.0, "days": len(r),
    }


def fmt(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("cagr", "total_return", "volatility", "mdd", "hit_ratio",
              "worst_day", "time_in"):
        if c in out:
            out[c] = out[c].map(lambda v: f"{v:.1%}" if pd.notna(v) else "N/A")
    if "sharpe" in out:
        out["sharpe"] = out["sharpe"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "N/A")
    return out

"""Performance statistics matching the columns of the paper's Table 2.

The paper does not define every column, so the choices made here are stated
explicitly rather than left implicit:

- **IRR** is the geometric annual growth rate (CAGR), not a cash-flow IRR —
  there are no external flows, so the two coincide.
- **Volatility** is the standard deviation of daily returns, annualised by
  sqrt(252).
- **Sharpe** uses a zero risk-free rate. The paper says it omits the risk-free
  rate for simplicity, so this matches.
- **Hit ratio** is the share of *days* with a positive return. That is the only
  reading under which the S&P 500's 54.9% makes sense (a buy-and-hold has no
  trades). Per-*trade* win rate is reported separately as `trade_win_rate`,
  which is the quantity the paper's Tables 4 and 5 call "Win Ratio".
- **Alpha / Beta** come from OLS of daily strategy returns on daily benchmark
  returns; alpha is annualised by multiplying the intercept by 252.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    """Peak-to-trough decline as a positive fraction."""
    peak = equity.cummax()
    return float((1.0 - equity / peak).max())


def regress(strategy: pd.Series, benchmark: pd.Series) -> tuple[float, float, float]:
    """OLS of strategy daily returns on benchmark. Returns (alpha_ann, beta, r2)."""
    joined = pd.concat([strategy, benchmark], axis=1, join="inner").dropna()
    if len(joined) < 30:
        return float("nan"), float("nan"), float("nan")
    y = joined.iloc[:, 0].to_numpy()
    x = joined.iloc[:, 1].to_numpy()
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    alpha, beta = coef
    resid = y - X @ coef
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1.0 - (resid ** 2).sum() / ss_tot if ss_tot > 0 else float("nan")
    return float(alpha * TRADING_DAYS), float(beta), float(r2)


def summarize(returns: pd.Series, equity: pd.Series | None = None,
              benchmark: pd.Series | None = None,
              trades: pd.Series | None = None,
              label: str = "") -> dict:
    """Table-2-shaped statistics for one daily return series."""
    returns = returns.dropna()
    if equity is None:
        equity = (1.0 + returns).cumprod()
    n = len(returns)
    years = n / TRADING_DAYS if n else float("nan")

    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if n else float("nan")
    irr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0) if years else float("nan")
    vol = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))
    sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if returns.std(ddof=1) else float("nan")

    row = {
        "strategy": label,
        "total_return": total,
        "irr": irr,
        "volatility": vol,
        "sharpe": sharpe,
        "hit_ratio": float((returns > 0).mean()),
        "mdd": max_drawdown(equity),
        "worst_day": float(returns.min()),
        "best_day": float(returns.max()),
        "days": n,
    }
    if trades is not None and len(trades):
        row["trade_win_rate"] = float((trades > 0).mean())
        row["avg_trade_r"] = float(trades.mean())
        row["n_trades"] = int(len(trades))
    if benchmark is not None:
        alpha, beta, r2 = regress(returns, benchmark)
        row.update({"alpha": alpha, "beta": beta, "r2": r2})
    return row


def table(rows: list[dict]) -> pd.DataFrame:
    """Assemble summaries into the paper's column order."""
    order = ["strategy", "total_return", "irr", "volatility", "sharpe",
             "hit_ratio", "mdd", "worst_day", "alpha", "beta",
             "trade_win_rate", "avg_trade_r", "n_trades", "days"]
    df = pd.DataFrame(rows)
    cols = [c for c in order if c in df.columns]
    return df[cols]


def format_table(df: pd.DataFrame) -> pd.DataFrame:
    """Percent-formatted copy for display."""
    pct = ["total_return", "irr", "volatility", "hit_ratio", "mdd",
           "worst_day", "best_day", "alpha", "trade_win_rate"]
    out = df.copy()
    for c in pct:
        if c in out.columns:
            out[c] = out[c].map(lambda v: f"{v:.1%}" if pd.notna(v) else "N/A")
    for c in ["sharpe", "beta", "avg_trade_r"]:
        if c in out.columns:
            out[c] = out[c].map(lambda v: f"{v:.2f}" if pd.notna(v) else "N/A")
    return out

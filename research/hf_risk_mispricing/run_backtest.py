"""Run the high-frequency replication; artefacts to out/hf_risk_mispricing/.

    .venv/bin/python research/hf_risk_mispricing/run_backtest.py

Reproduces what is reproducible without a firm-characteristic panel:
  - Table 1 Panel C, the RV / CV / JV decomposition (a direct numeric target)
  - the four-step architecture with plain PCA in place of projected PCA
  - the out-of-sample intraday arbitrage portfolio built from that
See hf_risk_mispricing.md §3.3 for why the projection cannot be run here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars
from hf import (INTERVALS_PER_YEAR, HFConfig, build_weights, decompose,
                detect_jumps, estimate_month, intraday_returns, month_index,
                variation_stats)

OUT = ROOT / "out" / "hf_risk_mispricing"
START, END = "2016-01-04", "2023-12-29"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)
log = logging.getLogger("hf.run")


def load_panel():
    """Read the 30-minute panel built by prepare_panel.py.

    Built by resampling cached 1-minute bars rather than fetching 30-minute bars
    directly: Alpaca aggregates those server-side and a request costs the same
    wall time regardless of bar count, which put a 500-name fetch at ~12 hours.
    Resampling is exact and free. The cost is cross-section size — see the
    notebook's assumptions.
    """
    path = OUT / "panel_30min.parquet"
    if not path.exists():
        raise SystemExit(f"missing {path} — run prepare_panel.py first")
    f = pd.read_parquet(path)
    close = f["close"].unstack("symbol").sort_index()
    volume = f["volume"].unstack("symbol").sort_index()
    return close, volume


def monthly_screen(close: pd.DataFrame, volume: pd.DataFrame, cfg: HFConfig):
    """The paper's liquidity screen, re-evaluated each month: price > $5 and above
    the 25th percentile of that month's dollar volume."""
    month = month_index(close.index)
    px = close.groupby(month).last()
    dv = (close * volume).groupby(month).sum(min_count=1)
    keep = px > cfg.min_price
    cut = dv.quantile(cfg.volume_pctile, axis=1)
    keep &= dv.ge(cut, axis=0)
    return keep


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    close, volume = load_panel()
    log.info("panel: %d intervals x %d symbols", *close.shape)

    cfg = HFConfig()
    rets = intraday_returns(close)
    log.info("detecting jumps (Lee-Mykland, K=%d, alpha=%.2f) ...",
             cfg.lm_window, cfg.lm_alpha)
    jumps = detect_jumps(rets, cfg)
    log.info("  %d jumps in %d returns (%.3f%%)",
             int(jumps.to_numpy().sum()), int(rets.notna().to_numpy().sum()),
             100 * jumps.to_numpy().sum() / max(rets.notna().to_numpy().sum(), 1))

    # ---- Table 1 Panel C ----
    stats = variation_stats(rets, jumps)
    stats = stats[stats["n_obs"] >= cfg.min_obs_per_month]
    stats.to_parquet(OUT / "variation_stats.parquet")
    q = stats[["rv", "cv", "jv", "jump_share", "jump_count", "mean_abs_jump"]]
    panel_c = pd.DataFrame({
        "mean": q.mean(), "P25": q.quantile(.25),
        "median": q.median(), "P75": q.quantile(.75),
    })
    panel_c.to_parquet(OUT / "panel_c.parquet")
    log.info("\n%s", panel_c.round(4).to_string())

    # ---- monthly estimation and the out-of-sample portfolio ----
    keep = monthly_screen(close, volume, cfg)
    month_idx = month_index(rets.index)
    months = sorted(set(month_idx))
    r_c, r_j = decompose(rets, jumps)

    def portfolio(k_c: int, k_j: int, est_months: int = 1) -> pd.Series:
        cfg_i = HFConfig(k_diffusive=k_c, k_jump=k_j)
        out = {}
        for i in range(est_months, len(months)):
            est = months[i - est_months:i]
            hold = months[i]
            sel = keep.loc[est[-1]]
            cols = [c for c in close.columns if sel.get(c, False)]
            if len(cols) < 40:
                continue
            m_est = month_idx.isin(est)
            m_hold = month_idx == hold
            ra = rets.loc[m_est, cols].dropna(how="all")
            if len(ra) < cfg.min_obs_per_month:
                continue
            ra = ra.loc[:, ra.notna().sum() >= 0.8 * len(ra)]
            if ra.shape[1] < 40:
                continue
            cols = list(ra.columns)
            # ra dropped all-NaN rows, so the components must be reindexed to
            # it or the design matrix and the response disagree on length.
            est_df = estimate_month(r_c.loc[ra.index, cols],
                                    r_j.loc[ra.index, cols], ra, cfg_i)
            w = build_weights(est_df["alpha"], ra, cfg_i)
            fwd = rets.loc[m_hold, cols]
            if fwd.empty:
                continue
            out[hold] = float((fwd.fillna(0.0).to_numpy() @ w.to_numpy()).sum())
        return pd.Series(out).sort_index()

    log.info("building the out-of-sample portfolio (K_C=%d, K_J=%d) ...",
             cfg.k_diffusive, cfg.k_jump)
    base = portfolio(cfg.k_diffusive, cfg.k_jump)
    base.to_frame("monthly_return").to_parquet(OUT / "portfolio_returns.parquet")

    # ---- benchmark and CAPM ----
    spy = load_bars("SPY", START, END, "1Day", adjustment="all").frame["close"]
    spy = spy.droplevel("symbol") if "symbol" in spy.index.names else spy
    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    spy_m = spy.resample("ME").last().pct_change().dropna()
    spy_m.index = spy_m.index.to_period("M")

    def summarize(r: pd.Series, label: str) -> dict:
        r = r.dropna()
        if len(r) < 6:
            return {"strategy": label, "months": len(r)}
        ann_ret = r.mean() * 12
        ann_vol = r.std(ddof=1) * np.sqrt(12)
        row = {"strategy": label, "months": len(r),
               "annual_return": ann_ret, "annual_vol": ann_vol,
               "sharpe": ann_ret / ann_vol if ann_vol else np.nan,
               "total_return": float((1 + r).prod() - 1),
               "worst_month": float(r.min()), "hit_ratio": float((r > 0).mean())}
        j = pd.concat([r.rename("p"), spy_m.rename("m")], axis=1).dropna()
        if len(j) > 12:
            X = np.column_stack([np.ones(len(j)), j["m"].to_numpy()])
            coef, *_ = np.linalg.lstsq(X, j["p"].to_numpy(), rcond=None)
            resid = j["p"].to_numpy() - X @ coef
            dof = len(j) - 2
            se = np.sqrt((resid @ resid / dof) * np.linalg.inv(X.T @ X)[0, 0])
            row["capm_alpha_monthly"] = float(coef[0])
            row["capm_alpha_t"] = float(coef[0] / se)
            row["capm_beta"] = float(coef[1])
        return row

    rows = [summarize(base, f"arbitrage portfolio (K_C={cfg.k_diffusive}, K_J={cfg.k_jump})"),
            summarize(spy_m.reindex(base.index).dropna(), "SPY buy & hold")]

    # ---- robustness: factor grid (incl. K_J = 0) and estimation window ----
    grid = []
    for k_c in (1, 2, 3, 4, 5):
        for k_j in (0, 1, 2, 3):
            s = portfolio(k_c, k_j)
            if len(s) > 12:
                ann = s.mean() * 12 / (s.std(ddof=1) * np.sqrt(12))
                grid.append({"k_c": k_c, "k_j": k_j, "sharpe": ann,
                             "annual_return": s.mean() * 12})
    pd.DataFrame(grid).to_parquet(OUT / "factor_grid.parquet", index=False)

    windows = []
    for w in (1, 2, 3, 6, 12):
        s = portfolio(cfg.k_diffusive, cfg.k_jump, est_months=w)
        if len(s) > 12:
            windows.append({"est_months": w,
                            "sharpe": s.mean() * 12 / (s.std(ddof=1) * np.sqrt(12)),
                            "annual_return": s.mean() * 12})
    pd.DataFrame(windows).to_parquet(OUT / "estimation_window.parquet", index=False)

    summary = pd.DataFrame(rows)
    summary.to_parquet(OUT / "summary.parquet", index=False)
    print()
    print(summary.to_string(index=False))
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()

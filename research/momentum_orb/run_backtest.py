"""Run the full ORB replication and write every artefact to out/momentum_orb/.

    .venv/bin/python research/momentum_orb/run_backtest.py

Outputs (parquet, per the repo's parquet-output rule):

    trades_<n>m.parquet   stage-1 panel: every symbol-day, traded or not
    equity_<variant>.parquet
    summary.parquet       the Table-2-shaped comparison
    relvol_buckets.parquet   the Figure-4 data
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from backtest.data import load_bars
from orb import ORBConfig, backtest, build_trade_panels
import metrics as M

OUT = ROOT / "out" / "momentum_orb"
START, END = "2016-01-04", "2023-12-29"
BENCHMARK = "SPY"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)
log = logging.getLogger("orb.run")


def with_start(equity: pd.Series, initial: float) -> pd.Series:
    """Prepend the starting capital.

    `run_portfolio` records equity *after* each day's PnL, so the first row
    already contains day one's return. Measuring total return as
    equity[-1]/equity[0] would silently drop that day. Prepending the starting
    balance one session earlier makes the series start where the account did.
    """
    if equity.empty:
        return equity
    first = equity.index[0] - pd.Timedelta(days=1)
    return pd.concat([pd.Series([initial], index=[first]), equity])


def benchmark_returns() -> pd.Series:
    """SPY daily total return. Adjusted here — unlike the traded universe, the
    benchmark is a return series, so dividends belong in it."""
    px = load_bars(BENCHMARK, START, END, "1Day", adjustment="all").frame
    close = px["close"].droplevel("symbol") if "symbol" in px.index.names else px["close"]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.pct_change().dropna().rename("benchmark")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    universe = pd.read_csv(HERE / "universe_selected.csv")["symbol"].tolist()
    log.info("universe: %d symbols, %s -> %s", len(universe), START, END)

    bench = benchmark_returns()
    bench_equity = with_start(25_000.0 * (1 + bench).cumprod(), 25_000.0)

    rows = [M.summarize(bench, bench_equity, benchmark=bench, label="S&P 500 (SPY)")]
    equities = {"S&P 500 (SPY)": bench_equity}
    bucket_frames = []

    # One pass over the minute bars builds every panel. Each symbol's minute
    # history is ~800k rows; re-reading it per variant is what makes this slow.
    configs = {f"trades_{m}m": ORBConfig(opening_minutes=m) for m in (5, 15, 30, 60)}
    configs["trades_5m_pessimistic"] = ORBConfig(opening_minutes=5,
                                                 same_bar_policy="pessimistic")
    log.info("building %d trade panels in one pass over %d symbols ...",
             len(configs), len(universe))
    panels = build_trade_panels(universe, START, END, configs,
                                cache_dir=OUT, progress=True)

    for minutes in (5, 15, 30, 60):
        panel = panels.get(f"trades_{minutes}m", pd.DataFrame())
        if panel.empty:
            log.warning("no data for the %d-minute panel", minutes)
            continue
        log.info("  %d-minute: %d symbol-days, %d triggered", minutes, len(panel),
                 int(panel["exit_reason"].isin(["stop", "eod"]).sum()))

        variants = [("ORB + RelVol", ORBConfig(opening_minutes=minutes))]
        if minutes == 5:
            variants.insert(0, ("ORB Base", ORBConfig(opening_minutes=minutes,
                                                      min_rel_volume=None, top_n=None)))
        for vname, cfg in variants:
            res = backtest(panel, cfg)
            daily = res["daily"]
            if daily.empty:
                continue
            daily.index = pd.to_datetime(daily.index).tz_localize(None).normalize()
            label = f"{minutes}m-{vname}" if vname == "ORB + RelVol" else vname
            eq_series = with_start(daily["equity"], cfg.initial_capital)
            rows.append(M.summarize(
                daily["return"], eq_series, benchmark=bench,
                trades=res["fills"]["pnl_r"], label=label))
            equities[label] = eq_series
            daily.to_parquet(
                OUT / f"equity_{minutes}m_{vname.split()[-1].lower()}.parquet")
            if minutes == 5 and vname == "ORB + RelVol":
                res["fills"].to_parquet(OUT / "fills_5m_relvol.parquet", index=False)

        # Figure 4 data: eligible on filters 1-3 only, bucketed by RelVol.
        base_cfg = ORBConfig(opening_minutes=minutes, min_rel_volume=None, top_n=None)
        elig = backtest(panel, base_cfg)["selected"]
        t = elig[elig["exit_reason"].isin(["stop", "eod"])].copy()
        bins = [0, .25, .5, .75, 1., 1.5, 2., 3., 5., 10., 30., np.inf]
        t["bucket"] = pd.cut(t["rel_volume"], bins)
        g = (t.groupby("bucket", observed=True)["pnl_r"]
               .agg(n="count", avg_r="mean", win_rate=lambda x: (x > 0).mean())
               .reset_index())
        g["opening_minutes"] = minutes
        g["bucket"] = g["bucket"].astype(str)
        bucket_frames.append(g)

    # Intrabar bracket. Entry bars split three ways: provably stopped, provably
    # safe, and genuinely ambiguous. Only the ambiguous set is in dispute; the
    # default lets it survive, "pessimistic" charges all of it as stopped.
    pess = panels.get("trades_5m_pessimistic", pd.DataFrame())
    if not pess.empty:
        pcfg = ORBConfig(opening_minutes=5, same_bar_policy="pessimistic")
        r = backtest(pess, pcfg)
        d = r["daily"]
        if not d.empty:
            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
            lbl = "5m-ORB + RelVol [intrabar: pessimistic]"
            eq_p = with_start(d["equity"], pcfg.initial_capital)
            rows.append(M.summarize(d["return"], eq_p, benchmark=bench,
                                    trades=r["fills"]["pnl_r"], label=lbl))
            equities[lbl] = eq_p

    # COMBO: equal-weight the four RelVol time-frame variants.
    tf_keys = [k for k in equities if k.endswith("ORB + RelVol")]  # exact suffix excludes the sensitivity run
    if len(tf_keys) > 1:
        rets = pd.concat([equities[k].pct_change().rename(k) for k in tf_keys], axis=1)
        combo = rets.mean(axis=1).dropna()
        combo_eq = with_start(25_000.0 * (1 + combo).cumprod(), 25_000.0)
        rows.append(M.summarize(combo, combo_eq, benchmark=bench, label="COMBO"))
        equities["COMBO"] = combo_eq

    summary = M.table(rows)
    summary.to_parquet(OUT / "summary.parquet", index=False)
    pd.concat(equities, axis=1).to_parquet(OUT / "equity_curves.parquet")
    if bucket_frames:
        pd.concat(bucket_frames, ignore_index=True).to_parquet(
            OUT / "relvol_buckets.parquet", index=False)

    print()
    print(M.format_table(summary).to_string(index=False))
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()

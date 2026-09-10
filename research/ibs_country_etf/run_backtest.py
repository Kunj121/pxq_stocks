"""Run the IBS replication and write every artefact to out/ibs_country_etf/.

    .venv/bin/python research/ibs_country_etf/run_backtest.py

The paper covers 2009-2019 but Alpaca equities begin 2016-01-04, so results are
reported over three windows instead of one: the slice of the paper's period we
can see, everything after it, and everything after the paper was published.
"""

from __future__ import annotations

import functools
import logging
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars
from ibs import (DEVELOPED, EMERGING, UNIVERSE, IBSConfig, compute_ibs,
                 hit_probabilities, minmax_weights, run, threshold_weights)
import metrics as M

OUT = ROOT / "out" / "ibs_country_etf"
START, END = "2016-01-04", "2026-09-08"
PUBLISHED = "2023-06-14"

WINDOWS = {
    "overlap 2016-2019": ("2016-01-04", "2019-12-31"),
    "out-of-sample 2020-2026": ("2020-01-01", "2026-09-08"),
    "post-publication 2023-06+": (PUBLISHED, "2026-09-08"),
    "full 2016-2026": (START, END),
}

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)
log = logging.getLogger("ibs.run")


def load():
    f = load_bars(list(UNIVERSE), START, END, "1Day", adjustment="all").frame
    high, low, close, open_ = (f[c].unstack("symbol") for c in
                               ("high", "low", "close", "open"))
    return high, low, close, open_


def sl(df, w):
    a, b = WINDOWS[w]
    return df.loc[a:b]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    high, low, close, open_ = load()
    ibs1 = compute_ibs(high, low, close)
    log.info("universe %d ETFs, %d sessions %s -> %s",
             len(close.columns), len(close), close.index[0].date(), close.index[-1].date())

    # ---- headline: Min-Max, all 15, across every window ----
    rows = []
    for w in WINDOWS:
        r = run(sl(ibs1, w), sl(close, w), IBSConfig())
        rows.append({**M.summarize(r, f"MinMax all-15 | {w}"), "window": w})
        rows.append({**M.benchmark_row(sl(close, w)["IVV"], f"IVV buy & hold | {w}"),
                     "window": w})
    headline = pd.DataFrame(rows)
    headline.to_parquet(OUT / "headline.parquet", index=False)

    # ---- mechanism: paper Table 3 ----
    probs = []
    for w in WINDOWS:
        p = hit_probabilities(sl(ibs1, w), sl(close, w)).reset_index()
        p["window"] = w
        probs.append(p)
    pd.concat(probs, ignore_index=True).to_parquet(OUT / "hit_probabilities.parquet",
                                                   index=False)

    # ---- long-only vs short-only vs both ----
    legs = []
    for w in WINDOWS:
        for lbl, cfg in [("long+short", IBSConfig()),
                         ("long only", IBSConfig(long_only=True)),
                         ("short only", IBSConfig(short_only=True))]:
            r = run(sl(ibs1, w), sl(close, w), cfg)
            legs.append({**M.summarize(r, lbl), "window": w, "leg": lbl})
    pd.DataFrame(legs).to_parquet(OUT / "legs.parquet", index=False)

    # ---- sweeps: N, holding period, IBS window ----
    sweeps = []
    for w in WINDOWS:
        for n in (1, 2, 3, 4, 5, 6, 7):
            r = run(sl(ibs1, w), sl(close, w), IBSConfig(n_positions=n))
            sweeps.append({"window": w, "sweep": "n_positions", "value": n,
                           "sharpe": M.summarize(r)["sharpe"]})
        for h in (1, 2, 3, 4):
            r = run(sl(ibs1, w), sl(close, w), IBSConfig(hold_days=h))
            sweeps.append({"window": w, "sweep": "hold_days", "value": h,
                           "sharpe": M.summarize(r)["sharpe"]})
        for k in (1, 2, 3, 4):
            ibsk = compute_ibs(high, low, close, window=k)
            r = run(sl(ibsk, w), sl(close, w), IBSConfig(ibs_window=k))
            sweeps.append({"window": w, "sweep": "ibs_window", "value": k,
                           "sharpe": M.summarize(r)["sharpe"]})
    pd.DataFrame(sweeps).to_parquet(OUT / "sweeps.parquet", index=False)

    # ---- close-to-close vs open-to-open (paper Table 5) ----
    oo = []
    ibs_o = compute_ibs(high, low, close)   # signal is always from the close bar
    for w in WINDOWS:
        c2c = run(sl(ibs1, w), sl(close, w), IBSConfig())
        o2o = run(sl(ibs_o, w), sl(open_, w), IBSConfig(price_field="open"))
        oo.append({"window": w, "close_to_close": M.summarize(c2c)["sharpe"],
                   "open_to_open": M.summarize(o2o)["sharpe"]})
    pd.DataFrame(oo).to_parquet(OUT / "open_vs_close.parquet", index=False)

    # ---- borrow-rate sensitivity (paper Table 6) ----
    borrow = []
    for w in WINDOWS:
        for rate in (0.0, 0.0001, 0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003):
            r = run(sl(ibs1, w), sl(close, w), IBSConfig(borrow_rate_daily=rate))
            r_lo = run(sl(ibs1, w), sl(close, w),
                       IBSConfig(long_only=True, borrow_rate_daily=rate))
            borrow.append({"window": w, "daily_rate": rate,
                           "sharpe_long_short": M.summarize(r)["sharpe"],
                           "sharpe_long_only": M.summarize(r_lo)["sharpe"]})
    pd.DataFrame(borrow).to_parquet(OUT / "borrow.parquet", index=False)

    # ---- Min-Max vs threshold (paper Figs 9-10) ----
    comp = []
    for w in WINDOWS:
        for n in (1, 2, 3):
            mm = run(sl(ibs1, w), sl(close, w), IBSConfig(n_positions=n))
            th = run(sl(ibs1, w), sl(close, w), IBSConfig(n_positions=n),
                     weight_fn=threshold_weights)
            comp.append({"window": w, "n_positions": n,
                         "minmax_sharpe": M.summarize(mm)["sharpe"],
                         "minmax_time_in": mm["time_in"],
                         "threshold_sharpe": M.summarize(th)["sharpe"],
                         "threshold_time_in": th["time_in"]})
    pd.DataFrame(comp).to_parquet(OUT / "minmax_vs_threshold.parquet", index=False)

    # ---- single-ETF threshold strategies (paper Table 2) ----
    single = []
    for w in WINDOWS:
        i, c = sl(ibs1, w), sl(close, w)
        for sym in UNIVERSE:
            if sym not in i.columns:
                continue
            # Single-ETF threshold: long IBS<0.2, short IBS>0.8, else flat.
            # `require_both` is the paper's BASKET rule (§4.8) — one ETF can
            # never be below 0.2 and above 0.8 on the same day, so imposing it
            # here would mean the strategy never trades.
            single_fn = functools.partial(threshold_weights, require_both=False)
            r = run(i[[sym]], c[[sym]], IBSConfig(), weight_fn=single_fn)
            s = M.summarize(r, sym)
            single.append({**s, "window": w, "ticker": sym})
    pd.DataFrame(single).to_parquet(OUT / "single_etf_threshold.parquet", index=False)

    # ---- basket size (paper Fig 3) and emerging vs developed (Tables 7-8) ----
    rng = random.Random(20260909)
    basket = []
    syms = [s for s in UNIVERSE if s in close.columns]
    for w in WINDOWS:
        i, c = sl(ibs1, w), sl(close, w)
        for size in range(2, len(syms) + 1):
            for _ in range(40):
                pick = rng.sample(syms, size)
                r = run(i[pick], c[pick], IBSConfig())
                basket.append({"window": w, "size": size, "kind": "random",
                               "sharpe": M.summarize(r)["sharpe"],
                               "basket": ",".join(sorted(pick))})
        for kind, group in (("emerging", EMERGING), ("developed", DEVELOPED)):
            g = [s for s in group if s in c.columns]
            r = run(i[g], c[g], IBSConfig())
            basket.append({"window": w, "size": len(g), "kind": kind,
                           "sharpe": M.summarize(r)["sharpe"],
                           "basket": ",".join(sorted(g))})
    pd.DataFrame(basket).to_parquet(OUT / "baskets.parquet", index=False)

    # ---- equity curves for the charts ----
    curves = {}
    for w in ("full 2016-2026", "post-publication 2023-06+"):
        i, c = sl(ibs1, w), sl(close, w)
        curves[f"MinMax long+short | {w}"] = run(i, c, IBSConfig())["equity"]
        curves[f"MinMax long only | {w}"] = run(i, c, IBSConfig(long_only=True))["equity"]
        curves[f"IVV buy & hold | {w}"] = (1 + c["IVV"].pct_change()).cumprod().dropna()
    pd.concat(curves, axis=1).to_parquet(OUT / "equity_curves.parquet")

    print()
    print(M.fmt(headline[["strategy", "sharpe", "cagr", "total_return",
                          "volatility", "mdd", "time_in", "days"]]).to_string(index=False))
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()

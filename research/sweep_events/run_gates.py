"""Score every logged sweep against the entry gates, and report which gate sets
would have fired.

    python research/sweep_events/run_gates.py

Writes out/sweep_events/gate_scores.parquet. Bars come from Alpaca via
backtest.data (historical lane); nothing here touches a live quote.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars          # noqa: E402
from gates import GATES, PROVENANCE, evaluate  # noqa: E402

OUT = ROOT / "out" / "sweep_events"
HOURLY_START, DAILY_START = "2025-09-19", "2024-09-01"

#: Gate subsets worth comparing. `all_nine` is the framework as written; the
#: others drop gates that failed for structural rather than informative reasons
#: — see notes/entry_gate_checks.md.
VARIANTS = {
    "all_nine":      GATES,
    "drop_rvol":     [g for g in GATES if g != "rvol_ok"],
    "drop_pctB":     [g for g in GATES if g != "pctB_ok"],
    "drop_both":     [g for g in GATES if g not in ("rvol_ok", "pctB_ok")],
    "trend_core":    ["daily_stack", "ema_cross", "adx_ok", "di_ok", "delta_ok"],
}


def add_sector_conflict(res: pd.DataFrame, sweeps: pd.DataFrame,
                        hourly: pd.DataFrame, days: int = 5,
                        rho: float = 0.70) -> pd.DataFrame:
    """Flag sweeps that disagree with a correlated name within `days`.

    Uses realised return correlation rather than the vendor's sector string —
    SNDK is labelled "Unknown" yet ran 0.91 correlated to MU, and on 9 Jul the
    two were swept ten minutes apart in opposite directions. One of them was
    always going to be wrong; the label would not have told you.
    """
    close = hourly["close"].unstack("symbol").sort_index()
    daily = close.groupby(close.index.date).last()
    ret = np.log(daily.astype(float)).diff()
    sw = sweeps.reset_index(drop=True)
    ts = pd.to_datetime(sw.swept_at)

    conflicts, partners = [], []
    for i, a in sw.iterrows():
        hits = []
        for j, b in sw.iterrows():
            if i == j or a.symbol == b.symbol or a.right == b.right:
                continue
            if abs((ts[i] - ts[j]).days) > days:
                continue
            window = ret.loc[:min(ts[i], ts[j]).date()].tail(60)
            if a.symbol not in window or b.symbol not in window:
                continue
            c = window[a.symbol].corr(window[b.symbol])
            if pd.notna(c) and c >= rho:
                hits.append(f"{b.label} (rho={c:.2f})")
        conflicts.append(bool(hits))
        partners.append("; ".join(hits))
    res = res.copy()
    res["sector_conflict"] = conflicts
    res["conflict_with"] = partners
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-earnings", action="store_true",
                    help="keep sweeps whose contract spans an earnings report "
                         "(excluded by default — an event gamble, not a read)")
    a = ap.parse_args()

    sweeps = pd.read_parquet(HERE / "sweeps.parquet")
    if not a.include_earnings and "tradeable" in sweeps:
        dropped = sweeps[~sweeps.tradeable]
        for _, r in dropped.iterrows():
            print(f"  excluded (earnings {r.earnings_in_window}): {r.label}")
        sweeps = sweeps[sweeps.tradeable]
    syms = sorted(sweeps.symbol.unique())
    hourly = load_bars(syms, HOURLY_START, timeframe="1Hour", session="regular").frame
    daily = load_bars(syms, DAILY_START, timeframe="1Day").frame

    rows = [evaluate(s, hourly.xs(s["symbol"], level="symbol").sort_index(),
                     daily.xs(s["symbol"], level="symbol").sort_index())
            for _, s in sweeps.iterrows()]
    res = pd.DataFrame(rows)
    res = add_sector_conflict(res, sweeps, hourly)

    for name, gset in VARIANTS.items():
        res[name] = res[gset].sum(axis=1).astype(int)
        res[name + "_n"] = len(gset)

    OUT.mkdir(parents=True, exist_ok=True)
    res.to_parquet(OUT / "gate_scores.parquet", index=False)

    pd.set_option("display.width", 220)
    print("\nGATE DETAIL  (provenance: " +
          ", ".join(f"{g}={PROVENANCE[g]}" for g in GATES) + ")\n")
    print(res.set_index("sweep")[
        ["direction", *GATES]].to_string())

    print("\nSCORES BY GATE SUBSET\n")
    tbl = res.set_index("sweep")[list(VARIANTS)]
    for name in VARIANTS:
        tbl[name] = tbl[name].astype(str) + "/" + str(len(VARIANTS[name]))
    print(tbl.to_string())

    extra = res[["sweep", "px_over_sma200", "px_over_sma50",
                 "sector_conflict", "conflict_with"]]
    print("\nRECORDED, NOT YET GATED  (overextension + correlated-flow disagreement)\n")
    print(extra.to_string(index=False))

    print("\nPER-GATE PASS RATE\n")
    rate = (res[GATES].mean() * 100).round(0).astype(int).astype(str) + "%"
    print(pd.DataFrame({"passes": res[GATES].sum(), "rate": rate,
                        "from": pd.Series(PROVENANCE)}).to_string())
    print(f"\nwrote -> {OUT / 'gate_scores.parquet'}")


if __name__ == "__main__":
    main()

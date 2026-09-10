"""Build the 30-minute panel by resampling cached 1-minute bars.

Why not fetch 30-minute bars directly: Alpaca aggregates them server-side from
trades, and a request costs roughly the same wall time as the equivalent
1-minute request regardless of how many bars come back — measured at 822 bars/s
for 30Min against 13,932 bars/s for 1Min over the same span. Fetching the
cross-section at 30-minute resolution would take ~12 hours; resampling the
1-minute bars already on disk takes seconds and yields identical bars.

The 09:30 alignment is exact: 13 bars per session, 09:30 through 15:30.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from backtest.data import load_bars

START, END = "2016-01-04", "2023-12-29"
OUT = ROOT / "out" / "hf_risk_mispricing"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)

AGG = {"open": "first", "high": "max", "low": "min",
       "close": "last", "volume": "sum"}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    src = ROOT / "research" / "momentum_orb" / "universe_selected.csv"
    symbols = pd.read_csv(src)["symbol"].tolist()
    print(f"resampling {len(symbols)} symbols of cached 1-minute bars")

    frames = []
    for i, sym in enumerate(symbols, 1):
        try:
            m = load_bars(sym, START, END, "1Min", session="regular",
                          adjustment="raw").frame
        except Exception as exc:
            print(f"  {sym}: {type(exc).__name__} {exc}")
            continue
        if m.empty:
            continue
        if "symbol" in m.index.names:
            m = m.droplevel("symbol")
        # label='left' so a bar stamped 09:30 covers 09:30-09:59, matching
        # Alpaca's own 30Min bars and the paper's 13-bars-per-session grid.
        b = m.resample("30min", label="left", closed="left").agg(AGG).dropna(how="all")
        b = b[b.index.time >= pd.Timestamp("09:30").time()]
        b = b[b.index.time <= pd.Timestamp("15:30").time()]
        b["symbol"] = sym
        frames.append(b.reset_index())
        if i % 20 == 0:
            print(f"  [{i:3d}/{len(symbols)}]", flush=True)

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.set_index(["timestamp", "symbol"]).sort_index()
    path = OUT / "panel_30min.parquet"
    panel.to_parquet(path)

    close = panel["close"].unstack("symbol")
    per_day = close.groupby(close.index.normalize()).size()
    print(f"\n{len(panel):,} bars | {close.shape[1]} symbols | "
          f"{close.index[0].date()} -> {close.index[-1].date()}")
    print(f"bars per session: median {per_day.median():.0f} "
          f"(expected 13), min {per_day.min()}, max {per_day.max()}")
    print(f"-> {path}")


if __name__ == "__main__":
    main()

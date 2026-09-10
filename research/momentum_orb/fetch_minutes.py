"""Download 1-minute regular-session bars for the selected universe.

Fetches one symbol at a time so the on-disk cache fills incrementally and an
interrupted run resumes instead of restarting. Prices are pulled RAW
(unadjusted) to match the paper, which used unadjusted IQFeed intraday data
"ensuring that the database was not influenced by any retrospective price
adjustments".
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from backtest.data import load_bars

START, END = "2016-01-04", "2023-12-29"
PROGRESS = HERE / "fetch_progress.csv"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest.client").setLevel(logging.WARNING)


def main() -> None:
    uni = pd.read_csv(HERE / "universe_selected.csv")
    symbols = uni["symbol"].tolist()
    done = set()
    if PROGRESS.exists():
        done = set(pd.read_csv(PROGRESS)["symbol"])

    rows = []
    t_start = time.time()
    for i, sym in enumerate(symbols, 1):
        if sym in done:
            continue
        t0 = time.time()
        try:
            panel = load_bars(sym, START, END, "1Min",
                              session="regular", adjustment="raw")
            n = len(panel.frame)
            err = ""
        except Exception as exc:  # keep going; one bad symbol must not kill the run
            n, err = 0, f"{type(exc).__name__}: {exc}"[:200]
        dt = time.time() - t0
        row = {"symbol": sym, "rows": n, "seconds": round(dt, 1), "error": err}
        rows.append(row)
        # Append only the row just finished. Rewriting the whole accumulated
        # list each time duplicates every earlier entry on a resumed run.
        write_header = not PROGRESS.exists()
        pd.DataFrame([row]).to_csv(PROGRESS, mode="a", header=write_header,
                                   index=False)
        elapsed = time.time() - t_start
        print(f"[{i:3d}/{len(symbols)}] {sym:6s} {n:>9,} rows  {dt:6.1f}s  "
              f"elapsed {elapsed/60:5.1f}m {err}", flush=True)

    print(f"\ndone in {(time.time()-t_start)/60:.1f} min")


if __name__ == "__main__":
    main()

"""Download 30-minute regular-session bars for the selected cross-section.

Resumable: progress is appended one row at a time, and symbols already recorded
are skipped.

Prices are pulled RAW to match the paper's TAQ source. It makes no difference to
the signal — intraday returns are computed *within* a session, and a split or
dividend factor is common to every bar of a day, so it cancels in the ratio. The
overnight return, the only one that would be affected, is excluded by the paper's
design and by ours.
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

START, END = "2016-01-04", "2026-09-08"
PROGRESS = HERE / "fetch_progress.csv"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest.client").setLevel(logging.WARNING)


BATCH = 25   # symbols per request


def main() -> None:
    syms = pd.read_csv(HERE / "universe_selected.csv")["symbol"].tolist()
    done = set()
    if PROGRESS.exists():
        done = set(pd.read_csv(PROGRESS)["symbol"])
    todo = [s for s in syms if s not in done]
    print(f"{len(todo)} of {len(syms)} symbols to fetch", flush=True)

    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        t = time.time()
        try:
            # One multi-symbol request per page amortises the per-window request
            # overhead across the batch. Fetching these one at a time is ~10x
            # slower: the client chunks by date window regardless of timeframe,
            # so a 30-minute pull costs nearly as many round trips as a 1-minute
            # one while carrying a thirtieth of the data.
            frame = load_bars(chunk, START, END, "30Min",
                              session="regular", adjustment="raw").frame
            counts = frame.groupby(level="symbol").size().to_dict()
            err = ""
        except Exception as exc:
            counts, err = {}, f"{type(exc).__name__}: {exc}"[:200]
        dt = time.time() - t
        rows = [{"symbol": s, "rows": int(counts.get(s, 0)),
                 "seconds": round(dt / len(chunk), 1), "error": err}
                for s in chunk]
        pd.DataFrame(rows).to_csv(PROGRESS, mode="a",
                                  header=not PROGRESS.exists(), index=False)
        total = sum(r["rows"] for r in rows)
        print(f"[{min(i+BATCH, len(todo)):3d}/{len(todo)}] {len(chunk)} syms  "
              f"{total:>9,} bars  {dt:5.1f}s  elapsed {(time.time()-t0)/60:5.1f}m {err}",
              flush=True)
    print(f"\ndone in {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()

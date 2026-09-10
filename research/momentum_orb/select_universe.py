"""Pick the backtest universe by rule, and freeze it to disk.

Rule, applied once and recorded so the run is reproducible:

1. Keep every ticker the paper names in its 5-minute ORB tables — all 25 best
   AND all 25 worst. Both tails, so the universe is not selected on outcome.
2. Fill the rest with the highest median dollar-volume names among the
   remaining candidates, ranked on 2016 only. Ranking on the first year of the
   sample, not the whole sample, keeps the choice causal.

Delisted names are kept. Alpaca serves their bars up to the delisting date, and
trading them only on days where data exists introduces no look-ahead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE_ = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE_))
sys.path.insert(0, str(HERE_.parent.parent))  # repo root, for `backtest`

from backtest.data import load_bars
from universe import PAPER_BEST, PAPER_WORST, candidates

HERE = Path(__file__).resolve().parent
TARGET_SIZE = 100
START, END = "2016-01-04", "2023-12-29"
RANK_YEAR_END = "2016-12-31"

# The 5-minute columns of Tables 4 and 5 — the strategy the paper headlines.
BEST_5M = PAPER_BEST.split()[:25]
WORST_5M = PAPER_WORST.split()[:25]


def main() -> None:
    cands = candidates()
    daily = load_bars(cands, START, END, "1Day", adjustment="raw").frame
    close = daily["close"].unstack("symbol")
    vol = daily["volume"].unstack("symbol")

    available = set(close.columns)
    core = [t for t in BEST_5M + WORST_5M if t in available]

    # Rank the remainder on 2016 dollar volume only — causal, not full-sample.
    rank_window = (close.index <= pd.Timestamp(RANK_YEAR_END, tz=close.index.tz))
    dollar_vol = (close[rank_window] * vol[rank_window]).median().dropna()
    rest = dollar_vol.drop(labels=[t for t in core if t in dollar_vol.index])
    fill = rest.sort_values(ascending=False).head(TARGET_SIZE - len(core)).index.tolist()

    selected = sorted(set(core) | set(fill))
    coverage = close[selected].notna().sum()

    out = pd.DataFrame({
        "symbol": selected,
        "in_paper_5m_tables": [s in core for s in selected],
        "days_with_data": [int(coverage[s]) for s in selected],
        "first_day": [close[s].first_valid_index().date().isoformat() for s in selected],
        "last_day": [close[s].last_valid_index().date().isoformat() for s in selected],
    })
    out["full_history"] = out["days_with_data"] >= len(close) * 0.98
    path = HERE / "universe_selected.csv"
    out.to_csv(path, index=False)

    print(f"{len(selected)} symbols selected of {len(cands)} candidates")
    print(f"  from the paper's 5m tables : {out['in_paper_5m_tables'].sum()}")
    print(f"  full history (>=98% days)  : {out['full_history'].sum()}")
    print(f"  partial (delisted / late IPO): {(~out['full_history']).sum()}")
    print(f"  -> {path}")
    print()
    print("partial-history names:")
    print(out[~out["full_history"]][["symbol", "first_day", "last_day",
                                     "days_with_data"]].to_string(index=False))


if __name__ == "__main__":
    main()

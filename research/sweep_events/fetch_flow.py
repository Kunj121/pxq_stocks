"""Pull every ISO sweep print over a date range and cache it to parquet.

The directional-sweep strategy needs two populations at once: the *trigger*
(large, delta-banded prints) and the *context* used to confirm it (every other
sweep on the same ticker). Those cannot come from the same query — at a $1M floor
a session holds ~13 prints across ~11 tickers, so almost every ticker has exactly
one and there is nothing to count. The floor here is deliberately low; filter up
to the trigger threshold afterwards.

    python research/sweep_events/fetch_flow.py 2026-06-01 2026-09-21

Re-running skips sessions already cached, so extending a range is cheap.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "quantdata"))
from client import QuantData, QuantDataError  # noqa: E402

CACHE = HERE.parents[1] / "data" / "sweep_flow"
PREMIUM_FLOOR = 100_000

KEEP = ["ts", "ticker", "contractType", "strikePrice", "expirationDate", "dte",
        "stockPrice", "optionPrice", "bidPrice", "askPrice", "bidAskSpread",
        "size", "premium", "impliedVolatility", "greeks.delta", "greeks.gamma",
        "greeks.theta", "greeks.vega", "volume", "openInterest", "tradeSideCode",
        "sentimentType", "isGoldenSweep", "isOpeningPosition", "isETF", "isIndex",
        "moneyness.moneyType", "sector", "industry", "tradeType"]


def sessions(start: str, end: str) -> list[str]:
    """NYSE-ish weekdays. A holiday simply returns nothing and is cached empty."""
    days = pd.bdate_range(start, end)
    return [d.strftime("%Y-%m-%d") for d in days]


def fetch_session(qd: QuantData, day: str) -> pd.DataFrame:
    frame = qd.pages(
        "qd_get_order_flow_consolidated",
        sessionDate=day,
        tradeConsolidationTypes=["SWEEP"],
        tradeTypes=["ISO"],
        premiumRange={"min": PREMIUM_FLOOR},
    )
    if not len(frame):
        return frame
    frame = frame[[c for c in KEEP if c in frame.columns]].copy()
    frame["session"] = day
    return frame


def main() -> None:
    start, end = sys.argv[1], sys.argv[2]
    CACHE.mkdir(parents=True, exist_ok=True)
    qd = QuantData()

    for day in sessions(start, end):
        out = CACHE / f"{day}.parquet"
        if out.exists():
            continue
        try:
            frame = fetch_session(qd, day)
        except QuantDataError as exc:
            print(f"  {day}  FAILED {str(exc)[:80]}")
            continue
        # Cache empties too — a market holiday should not be re-requested on
        # every run just because it produced no rows.
        frame.to_parquet(out, index=False)
        print(f"  {day}  {len(frame):4} prints"
              f"{'  (holiday/empty)' if not len(frame) else ''}")

    files = sorted(CACHE.glob("*.parquet"))
    total = sum(len(pd.read_parquet(f)) for f in files)
    print(f"\ncache: {len(files)} sessions, {total} prints -> {CACHE}")


if __name__ == "__main__":
    main()

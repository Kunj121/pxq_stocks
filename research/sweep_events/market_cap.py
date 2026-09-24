"""Market-cap buckets for the directional-trade universe.

Source: Robinhood MCP `get_equity_fundamentals`, same two-step as `earnings.py` —
the MCP tool is callable from Claude's tool layer, not from Python, so the pull is
"Claude calls it in batches of 10, the JSON lands in
`data/fundamentals_snapshot_<date>.json`, `ingest()` folds it to parquet".

**This is a snapshot, not a time series.** The vendor serves today's capitalization
only, so `asof_date` stamps the whole file and the bucket a symbol carries is its bucket
*now*, not on the day its sweep printed. Safe for slicing the universe by size or
sector; NOT safe to join onto `entry_ts` as though it were point-in-time — a name
that doubled since the signal is bucketed at the post-move size. Robinhood serves no
historical share count, so a point-in-time version would have to come from elsewhere.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / "data"
STORE = DATA / "market_cap.parquet"

# Edges are left-closed, right-open: $2B lands in Mid, $10B in Large, $200B in Mega.
# The brief's table starts at $250M; anything under that gets its own bucket rather
# than a null, so a small name can never silently read as "no data".
BUCKETS = [
    ("Micro cap",   0.0,            250e6),
    ("Small cap",   250e6,            2e9),
    ("Mid cap",       2e9,           10e9),
    ("Large cap",    10e9,          200e9),
    ("Mega cap",    200e9,  float("inf")),
]
EDGES = [b[1] for b in BUCKETS] + [float("inf")]
LABELS = [b[0] for b in BUCKETS]

# Vendor reports fund AUM in the market_cap field for ETFs. That is not a company
# capitalization and must not be read as one — flagged, not dropped, so a caller
# choosing to keep them does so deliberately.
FUNDS = {"DRAM", "ETHA", "RAM"}


def bucket(caps) -> pd.Categorical:
    """Map market caps to the size buckets. NaN in, NaN out."""
    return pd.cut(pd.Series(caps), bins=EDGES, labels=LABELS, right=False)


def ingest(snapshot: str | Path | None = None) -> pd.DataFrame:
    """Fold a fundamentals snapshot into `data/market_cap.parquet`."""
    if snapshot is None:
        found = sorted(DATA.glob("fundamentals_snapshot_*.json"))
        if not found:
            raise FileNotFoundError(f"no fundamentals_snapshot_*.json under {DATA}")
        snapshot = found[-1]

    raw = json.load(open(snapshot))
    frame = pd.DataFrame(raw["results"])
    frame["market_cap"] = pd.to_numeric(frame["market_cap"], errors="coerce")
    frame["cap_bucket"] = bucket(frame["market_cap"])
    frame["is_fund"] = frame["symbol"].isin(FUNDS)
    # Named `asof_date`, not `asof`: DataFrame.asof is a method, and a column
    # named over it is unreachable as an attribute.
    frame["asof_date"] = pd.Timestamp(raw["market_date"])
    frame = frame.sort_values("market_cap", ascending=False).reset_index(drop=True)
    frame.to_parquet(STORE, index=False)
    return frame


def frame() -> pd.DataFrame:
    if not STORE.exists():
        raise FileNotFoundError(f"{STORE} missing — run ingest() first")
    return pd.read_parquet(STORE)


def attach(trades: pd.DataFrame, on: str = "ticker") -> pd.DataFrame:
    """Left-join market cap and bucket onto a trade log.

    Left join on purpose: a symbol with no snapshot row keeps its trade and gets a
    null bucket, so a coverage gap shows up in a groupby instead of silently
    shrinking the sample.
    """
    caps = frame()[["symbol", "market_cap", "cap_bucket", "is_fund"]]
    return trades.merge(caps, how="left", left_on=on, right_on="symbol").drop(columns="symbol")


if __name__ == "__main__":
    out = ingest()
    print(f"wrote {len(out)} symbols -> {STORE}  (asof {out.asof_date.iloc[0]:%Y-%m-%d})")
    print(out.groupby("cap_bucket", observed=False).size().to_string())

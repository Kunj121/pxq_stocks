"""Same-session ISO sweep context around each directional trigger.

For every trade in `data/directional_trades.parquet`, count the *other* ISO sweeps
that printed on the same ticker, on the same session, split two ways:

- **prior / post** — before or after the trigger's own print
- **same / opposite** — direction agreeing with the trigger or against it

Direction is `contractType` x `tradeSideCode`, reused from `directional.direction`:
buying calls or selling puts is bullish. Call/put alone inverts the majority of
prints, so it is never used on its own here.

**The `post_*` columns are lookahead and must never enter an entry rule.** They
describe flow that had not happened when the trigger fired; `directional.confirm`
exists for the point-in-time version. They are here to answer "did the rest of the
tape follow this print or fade it", which is a post-hoc question about signal
quality, not a filter you can trade.

Prints with no aggressor side (`tradeSideCode` neither buy nor sell) have no
direction and count toward neither same nor opposite — the same convention
`confirm()` uses. `*_n` columns therefore need not sum to the session's total.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import directional as dxn

HERE = Path(__file__).resolve().parent
DATA = HERE.parents[1] / "data"
TRADES = DATA / "directional_trades.parquet"
STORE = DATA / "directional_trades_flow.parquet"

COUNT_COLS = ["prior_same_n", "prior_opp_n", "post_same_n", "post_opp_n"]
PREMIUM_COLS = ["prior_same_premium", "prior_opp_premium",
                "post_same_premium", "post_opp_premium"]


def _naive_et(ts: pd.Series) -> pd.Series:
    """Flow timestamps are tz-aware ET; the trade log's entry_ts is naive ET."""
    return ts.dt.tz_convert("America/New_York").dt.tz_localize(None)


def context(trades: pd.DataFrame, flow: pd.DataFrame | None = None) -> pd.DataFrame:
    """Attach the eight same-session flow columns to a trade log."""
    flow = dxn.load() if flow is None else flow
    flow = flow[flow["tradeType"] == "ISO"].copy()
    flow["ets"] = _naive_et(flow["ts"])

    # One pass per (ticker, session) rather than per trade: the same ticker often
    # triggers several times a day and would otherwise re-scan the same prints.
    groups = {k: g for k, g in flow.groupby(["ticker", "session"], sort=False)}
    rows = []
    for trade in trades.itertuples():
        day = groups.get((trade.ticker, trade.session))
        if day is None:
            rows.append({c: pd.NA for c in COUNT_COLS + PREMIUM_COLS})
            continue

        # Exclude the trigger's own print — it is neither prior nor post to itself.
        others = day[day["ets"] != trade.entry_ts]
        before = others[others["ets"] < trade.entry_ts]
        after = others[others["ets"] > trade.entry_ts]

        row = {}
        for name, part in (("prior", before), ("post", after)):
            same = part[part["direction"] == trade.direction]
            opp = part[part["direction"].notna() & (part["direction"] != trade.direction)]
            row[f"{name}_same_n"] = len(same)
            row[f"{name}_opp_n"] = len(opp)
            row[f"{name}_same_premium"] = float(same["premium"].sum())
            row[f"{name}_opp_premium"] = float(opp["premium"].sum())
        rows.append(row)

    return pd.concat([trades.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def build() -> pd.DataFrame:
    out = context(pd.read_parquet(TRADES))
    out.to_parquet(STORE, index=False)
    return out


if __name__ == "__main__":
    out = build()
    print(f"wrote {len(out)} trades -> {STORE}\n")
    print(out[COUNT_COLS + PREMIUM_COLS].describe().T.to_string(
        float_format=lambda x: f"{x:,.1f}"))

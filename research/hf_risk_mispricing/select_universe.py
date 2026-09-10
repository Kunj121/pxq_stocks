"""Pick the cross-section for the high-frequency panel, by rule, and freeze it.

The paper screens ~6,600 US equities down to an average of 1,336 per month using
price > $5 and a 25th-percentile monthly volume cut. We cannot reproduce its
breadth — Alpaca's asset master is *today's* tradable list, so anything delisted
between 2000 and now is invisible — but we can reproduce its *screen*.

Ranking is done on **2016 alone**, the first year of available history, so the
universe is not selected on outcomes over the sample it is then tested on.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from backtest.data import load_bars, load_universe

TARGET = 500
RANK_YEAR = ("2016-01-04", "2016-12-31")
MIN_PRICE = 5.0
CHUNK = 400

# The paper studies individual equities. An ETF is a linear combination of the
# other names in the panel, so leaving them in would manufacture exactly the
# factor structure the PCA is meant to discover. Alpaca's asset master has no
# ETF flag, so they are identified by name.
FUND_WORDS = (" ETF", " ETN", "ETF ", "Trust", " Fund", "Index Shares",
              "iShares", "SPDR", "Vanguard", "Invesco", "ProShares", "Direxion",
              "VanEck", "Global X", "WisdomTree", "First Trust", "Schwab U.S.",
              "Select Sector", "Ultra", "Bear ", "Bull ", "Depositary Receipt")


def is_fund(name: str) -> bool:
    n = str(name or "")
    return any(w.lower() in n.lower() for w in FUND_WORDS)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)


def main() -> None:
    assets = load_universe(exchanges=("NASDAQ", "NYSE", "ARCA"), tradable_only=True)
    funds = assets["name"].map(is_fund)
    print(f"{len(assets)} tradable names; excluding {int(funds.sum())} funds/ETFs")
    assets = assets[~funds]
    cands = sorted(assets.index)
    print(f"{len(cands)} operating-company candidates")

    stats = []
    for i in range(0, len(cands), CHUNK):
        chunk = cands[i:i + CHUNK]
        try:
            f = load_bars(chunk, *RANK_YEAR, "1Day", adjustment="raw",
                          use_cache=False).frame
        except Exception as exc:
            print(f"  chunk {i//CHUNK}: {type(exc).__name__} {exc}")
            continue
        if f.empty:
            continue
        dv = (f["close"] * f["volume"]).groupby(level="symbol").median()
        px = f["close"].groupby(level="symbol").median()
        n = f["close"].groupby(level="symbol").count()
        stats.append(pd.DataFrame({"dollar_volume_2016": dv,
                                   "median_price_2016": px, "days_2016": n}))
        print(f"  [{min(i+CHUNK, len(cands)):5d}/{len(cands)}] "
              f"{sum(len(s) for s in stats):5d} with 2016 data", flush=True)

    s = pd.concat(stats)
    s = s[(s.median_price_2016 > MIN_PRICE) & (s.days_2016 >= 200)]
    print(f"\n{len(s)} names priced over ${MIN_PRICE:.0f} with a full 2016")

    sel = s.sort_values("dollar_volume_2016", ascending=False).head(TARGET)
    sel = sel.join(assets["name"], how="left")
    sel = sel.sort_index()
    sel.index.name = "symbol"
    out = HERE / "universe_selected.csv"
    sel.to_csv(out)
    print(f"selected {len(sel)} names -> {out}")
    print(f"  median 2016 dollar volume: ${sel.dollar_volume_2016.median()/1e6:,.0f}M")
    print(f"  smallest included:         ${sel.dollar_volume_2016.min()/1e6:,.0f}M")
    print("  sample:", " ".join(sel.index[:20]))


if __name__ == "__main__":
    main()

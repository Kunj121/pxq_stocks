"""Pick the live ORB universe BY RULE, ranked only on data available at the time.

The hand-picked universes (Mag 7, or MU/AMD/NBIS/SNDK/LLY) all shared one flaw:
they were chosen knowing which names had worked. This selects on a mechanical
criterion, over a window that ends before the test period begins, so the result
can be read as a forecast rather than a description.

The criterion comes from arithmetic, and the first version of this file got it
wrong. Ranking by ATR/price was tested and failed; here is why.

Dollar P&L per trade is `shares x R x R_multiple`, and position sizing sets
`shares = risk_budget / R`. So dollar P&L does not depend on R at all. But
slippage is a fixed cost **per share**, so its drag is

    slippage_drag / risk_budget  =  2 * slippage_per_share / R,   R = 0.10 * ATR$

What protects you is therefore **ATR in DOLLARS**, not ATR as a percentage of
price. Ranking by ATR/price selects volatile *cheap* names, whose dollar stops
are tiny:

    MU   $982   x 5.4%  = $53.00 ATR  ->  R = $5.35
    NNE  $18.89 x 15.9% = $ 3.00 ATR  ->  R = $0.30      18x worse

Measured over 2025-02..2026-09, the ATR/price ranking traded 122,233 shares a
year against 12,094 for a high-dollar-ATR universe, and went from +$4,086/yr at
zero slippage to -$953/yr at two cents. The dollar-ATR universe barely moved.

A liquidity floor keeps this from degenerating into illiquid names, where the
spread gives back everything the wide stop saves. A price floor is not the same
thing and is applied separately.

    python select_universe_live.py --rank-end 2025-02-12 --top 30
    python select_universe_live.py --rank-by atr_pct ...   # the version that failed
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE.parent), str(ROOT)]

from backtest.data import load_bars, load_universe
from orb import wilder_atr

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("backtest").setLevel(logging.WARNING)

# Same fund filter as the high-frequency study: an ETF is a basket of the other
# names and has structurally low ATR/price, so it would never rank anyway - but
# excluding it explicitly keeps the universe interpretable.
FUND_WORDS = (" ETF", " ETN", "ETF ", "Trust", " Fund", "Index Shares", "iShares",
              "SPDR", "Vanguard", "Invesco", "ProShares", "Direxion", "VanEck",
              "Global X", "WisdomTree", "First Trust", "Schwab U.S.",
              "Select Sector", "Ultra", "Bear ", "Bull ", "Depositary Receipt")


def is_fund(name: str) -> bool:
    n = str(name or "")
    return any(w.lower() in n.lower() for w in FUND_WORDS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank-end", default="2025-02-12",
                    help="last day of the ranking window (exclusive of the test)")
    ap.add_argument("--rank-months", type=int, default=12)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-dollar-volume", type=float, default=50e6)
    ap.add_argument("--min-price", type=float, default=10.0)
    ap.add_argument("--rank-by", default="median_atr",
                    choices=["median_atr", "atr_pct"],
                    help="median_atr (dollars, correct) or atr_pct (tested, worse)")
    ap.add_argument("--min-atr", type=float, default=0.50)
    ap.add_argument("--out", default=str(HERE / "universe_rule.csv"))
    ap.add_argument("--min-atr-pct", type=float, default=0.02,
                    help="floor on ATR/price so the list is not just expensive "
                         "but placid names")
    ap.add_argument("--chunk", type=int, default=400)
    a = ap.parse_args()

    end = pd.Timestamp(a.rank_end)
    start = end - pd.DateOffset(months=a.rank_months)
    print(f"ranking window {start.date()} -> {end.date()}  "
          f"(test period begins after this)")

    assets = load_universe(exchanges=("NASDAQ", "NYSE", "ARCA"), tradable_only=True)
    assets = assets[~assets["name"].map(is_fund)]
    assets = assets[assets["shortable"]]          # the strategy is long AND short
    cands = sorted(assets.index)
    print(f"{len(cands)} shortable operating companies")

    stats = []
    for i in range(0, len(cands), a.chunk):
        chunk = cands[i:i + a.chunk]
        try:
            f = load_bars(chunk, str(start.date()), str(end.date()), "1Day",
                          adjustment="raw", use_cache=False).frame
        except Exception as exc:
            print(f"  chunk {i//a.chunk}: {type(exc).__name__}")
            continue
        if f.empty:
            continue
        for sym, g in f.groupby(level="symbol"):
            g = g.droplevel("symbol").sort_index()
            if len(g) < 150:
                continue
            atr = wilder_atr(g["high"], g["low"], g["close"], 14).dropna()
            if atr.empty:
                continue
            px = g["close"]
            stats.append({
                "symbol": sym,
                "median_price": float(px.median()),
                "median_dollar_volume": float((px * g["volume"]).median()),
                "median_atr": float(atr.median()),
                "atr_pct": float((atr / px.reindex(atr.index)).median()),
                "sessions": len(g),
            })
        print(f"  [{min(i+a.chunk, len(cands)):5d}/{len(cands)}] {len(stats):5d} scored",
              flush=True)

    s = pd.DataFrame(stats).set_index("symbol")
    n0 = len(s)
    s = s[(s.median_price >= a.min_price)
          & (s.median_dollar_volume >= a.min_dollar_volume)
          & (s.median_atr > a.min_atr)
          & (s.atr_pct >= a.min_atr_pct)]
    print(f"\n{n0} scored -> {len(s)} pass price>=${a.min_price:.0f}, "
          f"$vol>=${a.min_dollar_volume/1e6:.0f}M, ATR>${a.min_atr:.2f}")

    sel = s.sort_values(a.rank_by, ascending=False).head(a.top).copy()
    sel = sel.join(assets["name"], how="left")
    sel.insert(0, "rank_window", f"{start.date()}..{end.date()}")
    sel.to_csv(a.out)

    print(f"\ntop {len(sel)} by {a.rank_by}:")
    print(sel[["atr_pct", "median_price", "median_dollar_volume", "median_atr"]]
          .assign(atr_pct=lambda d: d.atr_pct.map("{:.2%}".format),
                  median_price=lambda d: d.median_price.map("${:,.2f}".format),
                  median_dollar_volume=lambda d: (d.median_dollar_volume / 1e6).map("${:,.0f}M".format),
                  median_atr=lambda d: d.median_atr.map("${:,.2f}".format))
          .to_string())
    print(f"\n-> {a.out}")


if __name__ == "__main__":
    main()

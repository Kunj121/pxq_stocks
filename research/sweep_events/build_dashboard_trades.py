"""Assemble `data/dashboard_trades.parquet` — the trade log everything downstream reads.

This file closes the reproducibility gap recorded in `jump_sweeps.md` §10: the 55-column
log was assembled once in a session and never committed as code, while `dashboard_data.py`,
`exit_lab.py`, `event_study.py` and `stopping.py` all read it. Nothing could be rebuilt
from source.

It takes `directional_trades_flow.parquet` (26 columns) and adds the 30 that the dashboard
and every study since depend on:

- **confirmation** (`confirm_path`, `confirm_ts`, `confirm_spot`, `confirm_delay_min`,
  `trigger_iv`) — from `hedged_backtest.confirm`
- **the swept print itself** (`swept_*`, `side`, `vendor_sentiment`, `isGoldenSweep`,
  `isOpeningPosition`, `isETF`) — joined from the flow cache on (ticker, trigger minute)
- **beta** — `hedged_backtest.betas`, OLS on sessions strictly before entry
- **fundamentals** (`market_cap`, `cap_bucket`, `sector`, `industry`, `is_fund`) —
  `market_cap.attach`, a today-snapshot; symbols absent from it get nulls rather than
  blocking the build
- **`entry_iv`** — implied from the *real* entry fill by `hedged_backtest.implied_vol`,
  falling back to the trigger print's vendor IV. This is the vol every option path is
  marked with, so it is the single most load-bearing derived column here.
- **`simulable`** — has a fill, a strike and a settlement, and is not a suspected split.

    python research/sweep_events/build_dashboard_trades.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import directional as D  # noqa: E402
import hedged_backtest as H  # noqa: E402
import market_cap as MC  # noqa: E402

DATA = HERE.parents[1] / "data"
SRC = DATA / "directional_trades_flow.parquet"
OUT = DATA / "dashboard_trades.parquet"
TZ = "America/New_York"
BUDGET = 5_000.0          # $100k account at 5% a position, as hedged_backtest.build defaults

SWEPT = {"expirationDate": "swept_expiry", "strikePrice": "swept_strike",
         "contractType": "swept_right", "dte": "swept_dte", "optionPrice": "swept_price",
         "bidAskSpread": "swept_spread", "size": "swept_size", "impliedVolatility": "swept_iv",
         "volume": "swept_volume", "openInterest": "swept_oi", "tradeSideCode": "side",
         "sentimentType": "vendor_sentiment", "isGoldenSweep": "isGoldenSweep",
         "isOpeningPosition": "isOpeningPosition", "isETF": "isETF",
         "moneyness.moneyType": "swept_moneyness"}


def attach_swept(trades: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    """Join each trigger back to its own print.

    Two prints can share a ticker and a minute — ARM printed three times inside
    13:17-13:18 — so the join is on the exact naive-ET timestamp, which is what
    `directional.triggers()` wrote into `entry_ts` in the first place.
    """
    f = flow.copy()
    f["entry_ts"] = f["ts"].dt.tz_convert(TZ).dt.tz_localize(None)
    cols = [c for c in SWEPT if c in f.columns]
    f = (f[["ticker", "entry_ts", "premium"] + cols]
         .rename(columns=SWEPT)
         .drop_duplicates(["ticker", "entry_ts", "premium"]))
    # (ticker, entry_ts) is not unique — ARM printed three times inside 13:17-13:18 —
    # so premium disambiguates, matching the trigger row `directional.triggers()` emitted.
    out = trades.merge(f, left_on=["ticker", "entry_ts", "sweep_premium"],
                       right_on=["ticker", "entry_ts", "premium"], how="left").drop(columns="premium")
    missing = out["side"].isna().sum()
    if missing:
        print(f"  warning: {missing} triggers did not match a flow print")
    return out


def entry_iv(trades: pd.DataFrame) -> pd.DataFrame:
    """Vol implied by the real entry fill; the vendor's trigger IV as a fallback."""
    out = trades.copy()
    iv, src = [], []
    for t in out.itertuples():
        v = np.nan
        if not (pd.isna(t.entry_price) or pd.isna(t.strike) or pd.isna(t.expiry)):
            tau = ((pd.Timestamp(t.expiry) + pd.Timedelta(hours=16) - pd.Timestamp(t.confirm_ts))
                   .total_seconds() / 86400 / 365)
            v = H.implied_vol(float(t.entry_price), float(t.confirm_spot), float(t.strike), tau, t.right)
        if np.isnan(v):
            iv.append(getattr(t, "trigger_iv", np.nan)); src.append("trigger" if not pd.isna(getattr(t, "trigger_iv", np.nan)) else None)
        else:
            iv.append(v); src.append("fill")
    out["entry_iv"] = iv
    out["entry_iv_source"] = src
    return out


def build() -> pd.DataFrame:
    if not SRC.exists():
        sys.exit(f"{SRC} missing — run backtest_directional.py then same_day_flow.py first")
    trades = pd.read_parquet(SRC)
    flow = D.load()
    print(f"{len(trades)} triggers · flow {len(flow):,} prints "
          f"{flow.session.min()} .. {flow.session.max()}")

    out = attach_swept(trades, flow)
    out = H.confirm(out, flow)
    # A post-confirmed entry is filled at the CONFIRMING print, not the trigger: the strike
    # is re-chosen at that spot and the fill re-fetched. Skipping this leaves ~50 trades
    # carrying the trigger's contract, which is not what was traded.
    out = H.refill(out, flow)

    # beta needs split-adjusted daily closes for every ticker plus SPY, over a window
    # that reaches BETA_SESSIONS before the earliest trigger.
    from backtest.data import load_bars
    syms = sorted(out.ticker.unique().tolist()) + ["SPY"]
    start = (pd.Timestamp(out.session.min()) - pd.Timedelta(days=400)).date().isoformat()
    end = pd.Timestamp(out.session.max()).date().isoformat()
    adj = load_bars(syms, start, end, timeframe="1Day", adjustment="all").field("close")
    adj.index = adj.index.tz_localize(None).normalize()
    # Budget-dependent skips, in hedged_backtest.build's order and with its defaults
    # ($100k account, 5% a position). These are strategy parameters living in a data
    # file — inherited for fidelity with the log every study so far has read.
    budget = BUDGET
    out["contracts"] = np.where(out.skip.isna(),
                                np.floor(budget / out.contract_cost.fillna(np.inf)), 0).astype(int)
    out.loc[out.skip.isna() & (out.contracts == 0), "skip"] = "unaffordable"
    out["beta"] = H.betas(out, adj)
    out.loc[out.skip.isna() & out.beta.isna(), "skip"] = "insufficient_beta_history"
    out.loc[out.skip.isna() & out.trigger_iv.isna(), "skip"] = "no_trigger_iv"
    out = out.drop(columns="contracts")

    out = entry_iv(out)

    try:
        caps = MC.frame()[["symbol", "market_cap", "cap_bucket", "sector", "industry", "is_fund"]]
        out = out.merge(caps, left_on="ticker", right_on="symbol", how="left").drop(columns="symbol")
        gap = out.market_cap.isna().sum()
        if gap:
            print(f"  note: {gap} trades have no fundamentals snapshot "
                  f"({out[out.market_cap.isna()].ticker.nunique()} tickers) — left null")
    except FileNotFoundError:
        print("  note: no market_cap.parquet — fundamentals columns left null")
        for c in ("market_cap", "cap_bucket", "sector", "industry", "is_fund"):
            out[c] = None

    # Volume against open interest on the swept contract: >1 means the day's trading
    # exceeded everything outstanding, which reads as new positioning rather than a
    # close. Derived here so it can be filtered like any other column.
    out["vol_oi"] = (out.swept_volume / out.swept_oi.replace(0, np.nan)).round(3)

    out["simulable"] = (out.entry_price.notna() & out.strike.notna()
                        & out.exit_spot.notna() & (out.skip != "split_suspected"))
    out.to_parquet(OUT, index=False)
    return out


if __name__ == "__main__":
    out = build()
    print(f"\nwrote {OUT} — {len(out)} rows x {len(out.columns)} columns")
    print(f"  simulable {int(out.simulable.sum())}  ·  "
          f"entry_iv from fill {int((out.entry_iv_source == 'fill').sum())}  ·  "
          f"beta {int(out.beta.notna().sum())}")
    print(f"  expiries: {out.expiry.dropna().nunique()} cycles  "
          f"{out.session.min()} .. {out.session.max()}")

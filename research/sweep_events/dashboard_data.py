"""Data files for the signal-exploration dashboard.

Writes `out/dashboard/bars.js` (15-minute regular-hours bars, raw price space,
every ticker in the trade log) and `out/dashboard/trades.js` (one row per
trigger with every column the backtests produced). The page marks each option
with Black-Scholes along the underlying's 15-minute path, off the vol implied
by the real entry fill, so it needs the strike's own unadjusted price space —
hence `adjustment="raw"`.

Bars go through `AlpacaClient.stock_bars` directly rather than `load_bars`: a
date-only `end` on a window this long is read as reaching into the SIP embargo
and 403s, an explicit RFC-3339 end does not. Cached to
`data/bars_15min_raw.parquet` so a rerun makes no calls.

    python research/sweep_events/dashboard_data.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / "data"
OUT = ROOT / "out" / "dashboard"
BARS_CACHE = DATA / "bars_15min_raw.parquet"
TRADES = DATA / "dashboard_trades.parquet"
TZ = "America/New_York"
START, END = "2026-06-01", "2026-09-21T21:00:00Z"


def fetch_bars(tickers: list[str], start: str = None, end: str = None) -> pd.DataFrame:
    """15-minute raw bars for `tickers`, fetching what the cache does not already cover.

    Coverage is checked per symbol on DATES, not just on presence. A symbol cached over
    a narrower window than the one asked for is refetched and replaced — widening the
    window used to be silently ignored, because the old check was `set(tickers) - set(cached.symbol)`
    and every symbol was already "present".
    """
    start = start or START
    end = end or END
    cached = pd.read_parquet(BARS_CACHE) if BARS_CACHE.exists() else None
    want_a = pd.Timestamp(start).tz_localize(TZ)
    want_b = pd.Timestamp(end).tz_convert(TZ) if pd.Timestamp(end).tz is not None else pd.Timestamp(end).tz_localize(TZ)
    covered: set[str] = set()
    if cached is not None:
        cov = cached.groupby("symbol")["ts"].agg(["min", "max"])
        slack = pd.Timedelta(days=5)          # listings, holidays, and the first bar of a window
        covered = set(cov.index[(cov["min"] <= want_a + slack) & (cov["max"] >= want_b - slack)])
    missing = sorted(set(tickers) - covered)
    if not missing:
        return cached
    if cached is not None and missing:
        # drop the stale rows for the symbols being refetched, so the concat cannot duplicate
        cached = cached[~cached.symbol.isin(missing)]
    import sys
    sys.path.insert(0, str(ROOT))
    from backtest.client import AlpacaClient

    frames = [] if cached is None else [cached]
    with AlpacaClient() as client:
        for i in range(0, len(missing), 10):
            batch = missing[i:i + 10]
            got = client.stock_bars(batch, "15Min", start, end, adjustment="raw")
            for sym, bars in got.items():
                if not bars:
                    continue
                f = pd.DataFrame(bars)
                f["symbol"] = sym
                frames.append(f)
            print(f"  bars {min(i + 10, len(missing))}/{len(missing)} …", flush=True)
    new = pd.concat(frames[1:] if cached is not None else frames, ignore_index=True)
    new = new.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    new["ts"] = pd.to_datetime(new["ts"], utc=True).dt.tz_convert(TZ)
    # regular session only: bar START in [09:30, 16:00)
    mins = new["ts"].dt.hour * 60 + new["ts"].dt.minute
    new = new[(mins >= 570) & (mins < 960)][["symbol", "ts", "open", "high", "low", "close", "volume"]]
    out = pd.concat(([cached] if cached is not None else []) + [new], ignore_index=True).sort_values(["symbol", "ts"]).reset_index(drop=True)
    out.to_parquet(BARS_CACHE, index=False)
    return out


def trade_windows(trades: pd.DataFrame, lead_days: int = 6) -> dict[str, list[tuple]]:
    """Per-ticker merged [entry - lead, expiry] intervals for the simulable trades.

    The page only ever draws bars around a signal, so shipping a ticker's whole
    history is waste: at 12 expiry cycles the untrimmed payload is 34 MB of bars and
    24 MB of prints, over the 16 MB-per-file artifact limit and far past usable load
    time. Windowing to what the trades actually need keeps it at roughly a fifth.
    """
    wins: dict[str, list] = {}
    for r in trades.itertuples():
        if pd.isna(r.expiry):
            continue
        a = pd.Timestamp(r.confirm_ts) - pd.Timedelta(days=lead_days)
        z = pd.Timestamp(r.expiry) + pd.Timedelta(days=1)
        wins.setdefault(r.ticker, []).append((a, z))
    out = {}
    for tk, ivs in wins.items():
        ivs = sorted(ivs)
        merged = [list(ivs[0])]
        for a, z in ivs[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], z)
            else:
                merged.append([a, z])
        out[tk] = [tuple(m) for m in merged]
    return out


def in_windows(frame: pd.DataFrame, when: pd.Series, wins: dict, keep_symbols=()) -> pd.Series:
    """Boolean mask: rows whose ticker/time falls in one of that ticker's windows."""
    sym = frame["symbol"] if "symbol" in frame.columns else frame["ticker"]
    t = when.values
    mask = np.zeros(len(frame), dtype=bool)
    for tk, ivs in wins.items():
        sel = (sym.values == tk)
        if not sel.any():
            continue
        for a, z in ivs:
            mask |= sel & (t >= np.datetime64(a)) & (t <= np.datetime64(z))
    for k in keep_symbols:
        mask |= (sym.values == k)
    return pd.Series(mask, index=frame.index)


def epoch_min(ts: pd.Series) -> pd.Series:
    """Minutes since the epoch, whatever the series' resolution.

    `astype("int64")` on a datetime64[ms] series yields milliseconds, not
    nanoseconds — the trade log is stored at ms — so divide by a Timedelta
    instead of a fixed integer.
    """
    utc = ts.dt.tz_convert("UTC")
    # nullable: a skipped trigger with no contract has no expiry, and NaT must
    # come out as null, not as an int-cast of the sentinel
    return ((utc - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1)).astype("Int64")


def write_bars(bars: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    parts = []
    for sym, g in bars.groupby("symbol"):
        rec = {
            "t": epoch_min(g["ts"]).tolist(),
            "o": np.round(g["open"].values, 4).tolist(),
            "h": np.round(g["high"].values, 4).tolist(),
            "l": np.round(g["low"].values, 4).tolist(),
            "c": np.round(g["close"].values, 4).tolist(),
            "v": g["volume"].astype("int64").tolist(),
        }
        parts.append(f"{json.dumps(sym)}:{json.dumps(rec, separators=(',', ':'))}")
    body = "window.BARS={" + ",".join(parts) + "};"
    (OUT / "bars.js").write_text(body)
    print(f"bars.js: {len(bars):,} bars, {bars.symbol.nunique()} tickers, {len(body)/1e6:.1f} MB")


def write_trades() -> None:
    t = pd.read_parquet(TRADES)
    for col in ("entry_ts", "confirm_ts"):
        t[col] = epoch_min(pd.to_datetime(t[col]).dt.tz_localize(TZ))
    t["expiry_close"] = epoch_min((pd.to_datetime(t["expiry"]) + pd.Timedelta(hours=16)).dt.tz_localize(TZ))
    t["session"] = pd.to_datetime(t["session"]).dt.strftime("%Y-%m-%d")
    for col in t.columns:
        if str(t[col].dtype).startswith("datetime"):
            t[col] = t[col].dt.strftime("%Y-%m-%d")
    t = t.astype(object).where(pd.notna(t), None)
    rows = [{k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()} for r in t.to_dict("records")]
    body = "window.TRADES=" + json.dumps(rows, separators=(",", ":")) + ";"
    (OUT / "trades.js").write_text(body)
    print(f"trades.js: {len(rows)} rows, {len(t.columns)} columns, {len(body)/1e3:.0f} KB")


def write_sweeps(tickers: list[str]) -> None:
    """Every cached ISO print on the trade-log tickers, for the ticker view.

    Direction is aggressor-side via `directional.direction`; a print with no
    aggressor ships with `dir` null rather than being forced to a side.
    """
    import sys
    sys.path.insert(0, str(HERE))
    import directional as D

    flow = D.load()
    flow = flow[flow["ticker"].isin(tickers)].copy()
    if WINDOWS:
        flow = flow[in_windows(flow, flow["ts"].dt.tz_convert(TZ).dt.tz_localize(None), WINDOWS)]
    flow["ts_min"] = epoch_min(flow["ts"])
    keep = {"ticker": "ticker", "ts_min": "ts", "contractType": "right", "strikePrice": "strike",
            "expirationDate": "expiry", "dte": "dte", "premium": "premium", "size": "size",
            "optionPrice": "price", "stockPrice": "spot", "tradeSideCode": "side", "direction": "dir",
            "isGoldenSweep": "golden", "isOpeningPosition": "opening", "greeks.delta": "delta",
            "impliedVolatility": "iv", "volume": "volume", "openInterest": "oi", "moneyness.moneyType": "moneyness"}
    f = flow[list(keep)].rename(columns=keep)
    f["expiry"] = pd.to_datetime(f["expiry"]).dt.strftime("%Y-%m-%d")
    for c in ("dte", "delta", "iv", "price", "spot"):
        f[c] = f[c].round(4)
    f = f.astype(object).where(pd.notna(f), None)
    rows = [{k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()} for r in f.to_dict("records")]
    body = "window.SWEEPS=" + json.dumps(rows, separators=(",", ":")) + ";"
    (OUT / "sweeps.js").write_text(body)
    print(f"sweeps.js: {len(rows):,} prints on {f.ticker.nunique()} tickers, {len(body)/1e6:.1f} MB")


def write_golden(tickers: list[str]) -> None:
    """Every vendor-flagged golden sweep in the cache, on every ticker.

    The raw tab lists these unfiltered; `has_bars` says whether the ticker view
    can chart it (only the trade-log tickers carry 15-minute bars) and
    `in_backtest` whether the print was also a directional trigger.
    """
    import sys
    sys.path.insert(0, str(HERE))
    import directional as D

    flow = D.load()
    g = flow[flow["isGoldenSweep"] == True].copy()  # noqa: E712
    g["ts_min"] = epoch_min(g["ts"])
    trades = pd.read_parquet(TRADES)
    trig = set(zip(trades.ticker, epoch_min(pd.to_datetime(trades.entry_ts).dt.tz_localize(TZ))))
    g["in_backtest"] = [(a, b) in trig for a, b in zip(g.ticker, g.ts_min)]
    g["has_bars"] = g["ticker"].isin(tickers)
    g["vol_oi"] = (g["volume"] / g["openInterest"].replace(0, np.nan)).round(2)
    keep = {"ticker": "ticker", "ts_min": "ts", "session": "session", "contractType": "right", "strikePrice": "strike",
            "expirationDate": "expiry", "dte": "dte", "premium": "premium", "size": "size", "optionPrice": "price",
            "bidPrice": "bid", "askPrice": "ask", "bidAskSpread": "spread", "stockPrice": "spot", "tradeSideCode": "side",
            "sentimentType": "vendor_sentiment", "direction": "dir", "isOpeningPosition": "opening", "isETF": "isETF",
            "isIndex": "isIndex", "greeks.delta": "delta", "greeks.gamma": "gamma", "greeks.theta": "theta",
            "greeks.vega": "vega", "impliedVolatility": "iv", "volume": "volume", "openInterest": "oi", "vol_oi": "vol_oi",
            "moneyness.moneyType": "moneyness", "sector": "sector", "industry": "industry",
            "in_backtest": "in_backtest", "has_bars": "has_bars"}
    f = g[list(keep)].rename(columns=keep).sort_values("ts")
    f["expiry"] = pd.to_datetime(f["expiry"]).dt.strftime("%Y-%m-%d")
    f["session"] = pd.to_datetime(f["session"]).dt.strftime("%Y-%m-%d")
    for c in ("dte", "delta", "gamma", "theta", "vega", "iv", "price", "spot", "bid", "ask", "spread"):
        f[c] = f[c].round(4)
    f = f.astype(object).where(pd.notna(f), None)
    rows = [{k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()} for r in f.to_dict("records")]
    body = "window.GOLDEN=" + json.dumps(rows, separators=(",", ":")) + ";"
    (OUT / "golden.js").write_text(body)
    print(f"golden.js: {len(rows)} golden prints on {f.ticker.nunique()} tickers, "
          f"{int(f.in_backtest.sum())} in backtest, {int(f.has_bars.sum())} chartable")


WINDOWS: dict = {}


if __name__ == "__main__":
    trades = pd.read_parquet(TRADES)
    # The window follows the SIMULABLE trades, not every trigger: a skipped trigger has
    # no path to draw, and the log reaches ~10 months further back than any tradeable
    # signal does. Sizing the pull off `trades.session.min()` silently refetches a year
    # of bars for 122 tickers that nothing will ever read.
    sim = trades[trades.simulable]
    tickers = sorted(sim.ticker.unique().tolist())
    start = (pd.Timestamp(sim.session.min()) - pd.Timedelta(days=6)).date().isoformat()
    end = (pd.Timestamp(sim.expiry.dropna().max()) + pd.Timedelta(days=3)).strftime("%Y-%m-%dT21:00:00Z")
    print(f"bars window {start} .. {end[:10]} for {len(tickers)} tickers")
    bars = fetch_bars(tickers + ["SPY"], start, end)   # SPY rides along for the chart overlay
    WINDOWS = trade_windows(sim)
    trimmed = bars[in_windows(bars, bars["ts"].dt.tz_localize(None), WINDOWS, keep_symbols=("SPY",))]
    print(f"trimmed bars to trade windows: {len(trimmed):,} of {len(bars):,} rows")
    write_bars(trimmed)
    write_trades()
    write_sweeps(tickers)
    write_golden(tickers)

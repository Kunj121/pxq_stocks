"""Confirmed-and-hedged variant of the directional-sweep strategy, $100k account.

Two rules layered on `backtest_directional.py`, each measured separately so the
report can say which one did the work:

**1. Confirmation.** A trigger is taken only if at least one other same-direction
ISO sweep printed on the ticker that session — either *before* the trigger (enter
at the trigger's own fill, nothing changes) or *within two hours after* it (enter
at the first confirming print, with a fresh option fill at that time). Triggers
with neither are dropped. Direction is aggressor-side, from `directional.direction`.

**2. Beta hedge.** Each option position's market exposure — delta x 100 x spot x
contracts x beta — is offset with SPY shares: short SPY against long calls, long
SPY against long puts. Beta is the OLS slope of the ticker's daily log returns on
SPY's over the 120 sessions before entry. Delta is Black-Scholes off the trigger's
own implied vol, re-marked at every close and the SPY leg rebalanced to match.
The hedge is unwound at the expiry close.

**2b. SPY-option hedge (variant D).** The same beta-adjusted delta-dollars, but
offset with long SPY options instead of shares: puts against a long stock call,
calls against a long put, at-the-money at entry on the position's own monthly
expiry. Contracts are sized off the SPY option's delta, which comes from the
implied vol backed out of that day's real traded close (Alpaca option bars), and
re-sized at every close. Capital is the premium paid — no margin — which is the
whole reason to try it; the cost is paying theta on both legs.

**2c. Portfolio-net hedge (variant E).** Same share hedge as C, but one SPY
position for the whole book: each day the target is the *sum* of every open
position's signed hedge shares, so BULL and BEAR positions offset. Hedge P&L is
linear in shares and therefore identical to C's on the same trades — only the
margin changes, from 50% of every leg to 50% of the net.

**Sizing.** 5% of the account per position, whole contracts, so a $600 name may
fit one contract or none. A new position also needs Reg T initial margin on its
SPY leg — 50% of the hedge notional — out of free cash. Because the hedge is
sized off delta-dollars rather than premium, it is the margin, not the option
cost, that caps how many positions can be open at once.

**What is and is not modelled.** Option P&L is realised at expiry as intrinsic,
exactly as the base backtest does; `equity_mtm` additionally marks open options
with Black-Scholes at the same constant vol, and is the curve to read drawdowns
from. SPY borrow, dividends and commissions are ignored — a few basis points on a
five-week hold. Margin reserve is recomputed daily from the live hedge notional.

**Lookahead.** Post-confirmation is *not* lookahead: the entry is at the
confirming print's own timestamp, and only prints before that timestamp inform
the decision. Beta uses sessions strictly before the entry session.

    python research/sweep_events/hedged_backtest.py           # uses cached fills
    python research/sweep_events/hedged_backtest.py --refill  # refetch post-path fills
    python research/sweep_events/hedged_backtest.py --account 500000 --pct 0.01

SPY option bars are cached in data/spy_hedge_bars*.parquet on first use.

Results, 2026-06-01 .. 2026-09-21 (441 triggers, 188 confirmed candidates)
--------------------------------------------------------------------------
**The account cannot run the spec.** Median hedge notional is $94.6k against a
$4.4k option cost — 21x — because betas on this universe run ~2.0 and the hedge
is delta-dollars, not premium. Buying power per position is ~$52k; taking every
candidate would need ~$1.7M on an average day and $3.4M at peak. At $100k the
hedged run accepted 13 of 188, chosen by arrival order.

**Confirmation does not rank triggers.** Capacity-free option-leg mean return:
prior-confirmed -0.199 (n=146), post-confirmed +0.028 (n=42), unconfirmed +0.010
(n=62). Confirmed vs unconfirmed, Mann-Whitney p=0.54. The rule drops 62 triggers
and keeps the worse ones.

**The hedge works mechanically and barely helps.** corr(option, hedge) = -0.33;
net P&L correlation with SPY falls to ~-0.1 from +0.10/-0.40 — the market factor
is out. But per-trade std only drops 5,198 -> 4,912: a long ATM option is convex
and bounded, a 21x linear SPY leg is neither, and beta error at beta 2 is large.
Capacity-free, the hedge leg made +$96.8k against -$112.5k on options, mostly
by being short SPY through July.

**SPY options as the hedge (D) solve capital and lose more.** Buying power per
position falls to $6.6k, but the leg bleeds 18.9% of premium paid: -$80.4k
capacity-free against +$96.8k for shares, on the same market bet (corr +0.84).
Net -$192.9k, the worst variant. Long options on both legs is paying theta twice.

**Netting the hedge (E) cuts margin ~4.5x on the full book, not 10x.** Daily
net/gross hedge notional averages 0.23 across all 188 candidates; the book is
net short SPY 64% of days, long 35%, balanced almost never. Running every
candidate needs ~$445k of buying power on an average day and $2.0M at peak.
Worse, admission under net margin is path-dependent: a position let in because
an opposite one offset it is stranded when that one expires, and the reserve
balloons — at $100k E was in margin breach 49% of days, worst shortfall $492k.

**Account-level P&L below full capacity is arrival-order noise.** Every hedged
run at $100k or $500k takes a subset chosen by which signals arrived when cash
was free, and the taken subset's option-leg P&L differed from the refused
subset's by 3-4x in both directions across runs. The capacity-free totals are
the only numbers that compare rules: unhedged -$112.5k, share-hedged -$15.7k,
SPY-option-hedged -$192.9k. No variant is positive.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "quantdata"))
sys.path.insert(0, str(HERE.parents[1]))

import directional as D  # noqa: E402
from backtest.data import load_bars  # noqa: E402
from backtest_directional import entry_fill  # noqa: E402

DATA = HERE.parents[1] / "data"
TRADES_IN = DATA / "directional_trades_flow.parquet"
FILLS_CACHE = DATA / "hedged_entry_fills.parquet"
TRADES_OUT = DATA / "hedged_trades.parquet"
EQUITY_OUT = DATA / "hedged_equity.parquet"
SPY_BARS_CACHE = DATA / "spy_hedge_bars.parquet"          # daily closes per contract
SPY_HOURLY_CACHE = DATA / "spy_hedge_bars_hourly.parquet"  # entry-day hourly bars

ACCOUNT = 100_000.0
POSITION_PCT = 0.05
CONFIRM_WINDOW = pd.Timedelta(hours=2)
BETA_SESSIONS = 120
BETA_MIN_SESSIONS = 60
MARGIN = 0.50               # Reg T initial on the SPY leg, either side
TZ = "America/New_York"
YEAR = 365.0


# ---------------------------------------------------------------- confirmation
def confirm(trades: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    """Decide which triggers are taken and when.

    Adds `confirm_path` (prior / post / None), `confirm_ts` (the entry time —
    unchanged for prior, the confirming print for post), `confirm_spot` and
    `confirm_delay_min`. Unconfirmed tradeable triggers get skip='unconfirmed'.
    """
    flow = flow.copy()
    flow["ets"] = flow["ts"].dt.tz_convert(TZ).dt.tz_localize(None)
    days = {k: g for k, g in flow.groupby(["ticker", "session"], sort=False)}

    out = trades.copy()
    out["confirm_path"] = None
    out["confirm_ts"] = out["entry_ts"]
    out["confirm_spot"] = out["entry_spot"]
    out["confirm_delay_min"] = 0.0
    out["trigger_iv"] = np.nan

    for i, t in out.iterrows():
        day = days.get((t.ticker, t.session))
        if day is not None:
            own = day[day["ets"] == t.entry_ts]
            if len(own):
                out.at[i, "trigger_iv"] = float(own["impliedVolatility"].iloc[0]) / 100.0
        if t.skip is not None:
            continue
        if t.prior_same_n > 0:
            out.at[i, "confirm_path"] = "prior"
            continue
        window = day[(day["ets"] > t.entry_ts)
                     & (day["ets"] <= t.entry_ts + CONFIRM_WINDOW)
                     & (day["direction"] == t.direction)]
        if len(window):
            first = window.sort_values("ets").iloc[0]
            out.at[i, "confirm_path"] = "post"
            out.at[i, "confirm_ts"] = first["ets"]
            out.at[i, "confirm_spot"] = float(first["stockPrice"])
            out.at[i, "confirm_delay_min"] = (first["ets"] - t.entry_ts).total_seconds() / 60
        else:
            out.at[i, "skip"] = "unconfirmed"
    return out


def refill(trades: pd.DataFrame, flow: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    """Fresh option fills for post-confirmed entries, cached to parquet.

    The base backtest filled at the trigger's timestamp. A post-confirmed entry
    happens later, at a spot that may have moved, so the ATM strike is re-chosen
    at the confirming print's spot and the fill is the first option print at or
    after that time. Intrinsic at expiry is recomputed for the new strike.
    """
    post = trades[trades["confirm_path"] == "post"]
    if not len(post):
        return trades

    cached = pd.read_parquet(FILLS_CACHE) if FILLS_CACHE.exists() and not refresh else None
    if cached is not None and set(zip(post.ticker, post.entry_ts)) <= set(zip(cached.ticker, cached.entry_ts)):
        fills = cached
    else:
        from client import QuantData
        qd = QuantData()
        grid = {k: np.sort(g["strikePrice"].unique())
                for k, g in flow.groupby(["ticker", "expirationDate"])}
        rows = []
        for n, t in enumerate(post.itertuples(), 1):
            strike, price, vol = entry_fill(qd, t.ticker, t.expiry, t.confirm_spot, t.right,
                                            t.session, t.confirm_ts, grid.get((t.ticker, t.expiry)))
            rows.append({"ticker": t.ticker, "entry_ts": t.entry_ts,
                         "strike": strike, "entry_price": price, "entry_volume": vol})
            if n % 10 == 0:
                print(f"  refill {n}/{len(post)} …", flush=True)
        fills = pd.DataFrame(rows)
        fills.to_parquet(FILLS_CACHE, index=False)

    out = trades.copy()
    lookup = fills.set_index(["ticker", "entry_ts"])
    for i, t in post.iterrows():
        f = lookup.loc[(t.ticker, t.entry_ts)]
        if f["entry_price"] is None or pd.isna(f["entry_price"]) or f["entry_price"] <= 0:
            out.at[i, "skip"] = "no_entry_print"
            continue
        strike = float(f["strike"])
        intrinsic = (max(0.0, t.exit_spot - strike) if t.right == "CALL"
                     else max(0.0, strike - t.exit_spot))
        out.at[i, "strike"] = strike
        out.at[i, "entry_price"] = float(f["entry_price"])
        out.at[i, "entry_volume"] = f["entry_volume"]
        out.at[i, "exit_price"] = intrinsic
        out.at[i, "ret"] = intrinsic / float(f["entry_price"]) - 1.0
        out.at[i, "contract_cost"] = float(f["entry_price"]) * 100
    return out


# ----------------------------------------------------------------------- beta
def betas(trades: pd.DataFrame, adj_close: pd.DataFrame) -> pd.Series:
    """OLS slope of ticker daily log returns on SPY's, sessions strictly before entry."""
    rets = np.log(adj_close).diff()
    spy = rets["SPY"]
    out = pd.Series(np.nan, index=trades.index)
    for i, t in trades.iterrows():
        if t.skip is not None:
            continue
        entry_day = pd.Timestamp(t.confirm_ts).normalize()
        hist = rets.loc[rets.index < entry_day].tail(BETA_SESSIONS)
        pair = pd.concat([hist[t.ticker], spy.loc[hist.index]], axis=1).dropna()
        if len(pair) < BETA_MIN_SESSIONS:
            continue
        x, y = pair.iloc[:, 1].values, pair.iloc[:, 0].values
        out.at[i] = float(np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1))
    return out


# ------------------------------------------------------------ black-scholes
def bs_delta(S, K, tau, sigma, right: str):
    if tau <= 0:
        itm = (S > K) if right == "CALL" else (S < K)
        return (1.0 if itm else 0.0) if right == "CALL" else (-1.0 if itm else 0.0)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * tau) / (sigma * np.sqrt(tau))
    return float(norm.cdf(d1)) if right == "CALL" else float(norm.cdf(d1) - 1.0)


def bs_price(S, K, tau, sigma, right: str):
    if tau <= 0:
        return max(0.0, S - K) if right == "CALL" else max(0.0, K - S)
    sq = sigma * np.sqrt(tau)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * tau) / sq
    d2 = d1 - sq
    if right == "CALL":
        return float(S * norm.cdf(d1) - K * norm.cdf(d2))
    return float(K * norm.cdf(-d2) - S * norm.cdf(-d1))


# ----------------------------------------------------------------- hedge path
def hedge_path(t, raw_close: pd.DataFrame, spy_hourly: pd.Series) -> pd.DataFrame:
    """Daily rows from entry to expiry: delta, hedge shares, hedge P&L, option mark.

    Entry-day row: delta off the entry spot, SPY off the hourly bar containing
    the entry, P&L from there to the close. Every later row is close-to-close.
    The expiry row unwinds: P&L accrues to the close, then shares go to zero.
    """
    entry_ts = pd.Timestamp(t.confirm_ts)
    entry_day = entry_ts.normalize()
    expiry = pd.Timestamp(t.expiry)
    expiry_close = expiry + pd.Timedelta(hours=16)
    sessions = raw_close.index[(raw_close.index >= entry_day) & (raw_close.index <= expiry)]
    if not len(sessions):
        return pd.DataFrame()
    n = t.contracts
    K, sigma, right, beta = t.strike, t.trigger_iv, t.right, t.beta

    # SPY at entry: the regular-hours bar containing the entry, its close —
    # within an hour of the print, without pretending to a minute fill.
    bar = spy_hourly.loc[spy_hourly.index <= entry_ts]
    spy_entry = float(bar.iloc[-1]) if len(bar) else float(raw_close.loc[sessions[0], "SPY"])

    rows = []
    S, spy = float(t.confirm_spot), spy_entry
    tau = (expiry_close - entry_ts).total_seconds() / 86400 / YEAR
    delta = bs_delta(S, K, tau, sigma, right)
    shares = -beta * delta * 100 * n * S / spy      # short for calls, long for puts
    prev_spy, prev_shares = spy, shares

    for d in sessions:
        S = float(raw_close.loc[d, t.ticker])
        spy = float(raw_close.loc[d, "SPY"])
        if d == sessions[-1]:
            S = float(t.exit_spot)                   # settle exactly as the base backtest
            tau, delta = 0.0, bs_delta(S, K, 0.0, sigma, right)
        else:
            tau = (expiry_close - (d + pd.Timedelta(hours=16))).total_seconds() / 86400 / YEAR
            delta = bs_delta(S, K, tau, sigma, right)
        pnl = prev_shares * (spy - prev_spy)
        shares = 0.0 if d == sessions[-1] else -beta * delta * 100 * n * S / spy
        rows.append({"session": d, "spot": S, "spy": spy, "delta": delta,
                     "hedge_shares": shares, "hedge_notional": abs(shares) * spy,
                     "hedge_reserve": MARGIN * abs(shares) * spy,   # Reg T on the SPY leg
                     "hedge_pnl": pnl,
                     "option_mark": bs_price(S, K, tau, sigma, right) * 100 * n})
        prev_spy, prev_shares = spy, shares
    return pd.DataFrame(rows)


# ------------------------------------------------------- SPY-option hedge
def spy_option_symbol(expiry: str, strike: float, right: str) -> str:
    e = pd.Timestamp(expiry).strftime("%y%m%d")
    return f"SPY{e}{right[0]}{int(round(strike * 1000)):08d}"


def implied_vol(price: float, S: float, K: float, tau: float, right: str) -> float:
    """Back the vol out of a traded price. Below intrinsic or degenerate -> NaN."""
    if tau <= 0 or price <= 0:
        return np.nan
    intrinsic = max(0.0, S - K) if right == "CALL" else max(0.0, K - S)
    if price <= intrinsic + 1e-6:
        return np.nan
    try:
        return float(brentq(lambda v: bs_price(S, K, tau, v, right) - price, 1e-3, 5.0, xtol=1e-6))
    except ValueError:
        return np.nan


def fetch_spy_option_bars(contracts: list[tuple[str, str]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily bars for each (symbol, entry_day), plus that entry day's hourly bars.

    Alpaca is the historical-data lane. Everything is cached to parquet keyed on
    the OCC symbol, so a rerun with the same contracts makes no calls.
    """
    import time
    from datetime import datetime, timedelta

    daily = pd.read_parquet(SPY_BARS_CACHE) if SPY_BARS_CACHE.exists() else pd.DataFrame(columns=["symbol", "session", "close"])
    hourly = pd.read_parquet(SPY_HOURLY_CACHE) if SPY_HOURLY_CACHE.exists() else pd.DataFrame(columns=["symbol", "ts", "close"])
    have_d, have_h = set(daily.symbol), set(zip(hourly.symbol, pd.to_datetime(hourly.ts).dt.normalize())) if len(hourly) else set()
    need = [(sym, day) for sym, day in contracts if sym not in have_d or (sym, pd.Timestamp(day)) not in have_h]
    if need:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from backtest.config import get_settings
        st = get_settings()
        client = OptionHistoricalDataClient(st.api_key, st.secret_key)
        new_d, new_h = [], []
        for n, (sym, day) in enumerate(need, 1):
            expiry = datetime.strptime(sym[3:9], "%y%m%d")
            day = pd.Timestamp(day).to_pydatetime()
            if sym not in have_d:
                df = client.get_option_bars(OptionBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                                                              start=day - timedelta(days=1), end=expiry + timedelta(days=1))).df
                if len(df):
                    df = df.reset_index()
                    new_d.append(pd.DataFrame({"symbol": sym, "session": pd.to_datetime(df["timestamp"]).dt.tz_convert(TZ).dt.tz_localize(None).dt.normalize(), "close": df["close"].astype(float)}))
                have_d.add(sym)
                time.sleep(0.35)
            df = client.get_option_bars(OptionBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Hour,
                                                          start=day, end=day + timedelta(days=1))).df
            if len(df):
                df = df.reset_index()
                new_h.append(pd.DataFrame({"symbol": sym, "ts": pd.to_datetime(df["timestamp"]).dt.tz_convert(TZ).dt.tz_localize(None), "close": df["close"].astype(float)}))
            time.sleep(0.35)
            if n % 25 == 0:
                print(f"  spy option bars {n}/{len(need)} …", flush=True)
        if new_d:
            daily = pd.concat([daily] + new_d, ignore_index=True).drop_duplicates(["symbol", "session"])
        if new_h:
            hourly = pd.concat([hourly] + new_h, ignore_index=True).drop_duplicates(["symbol", "ts"])
        daily.to_parquet(SPY_BARS_CACHE, index=False)
        hourly.to_parquet(SPY_HOURLY_CACHE, index=False)
    return daily, hourly


def option_hedge_path(t, share_path: pd.DataFrame, daily: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    """Re-express the share hedge as long SPY options, priced off real bars.

    `share_path` supplies the target hedge shares each day (the beta-adjusted
    delta-dollars of the stock option); this converts them to whole SPY contracts
    via the SPY option's own delta. P&L is close-to-close on the traded price;
    the expiry row settles at intrinsic off SPY's close. Capital reserved is the
    live premium of the contracts held.
    """
    if not len(share_path):
        return share_path
    right = "PUT" if t.right == "CALL" else "CALL"
    expiry = pd.Timestamp(t.expiry)
    expiry_close = expiry + pd.Timedelta(hours=16)
    entry_ts = pd.Timestamp(t.confirm_ts)
    spy0 = float(share_path["spy"].iloc[0])
    K = float(round(spy0))                         # SPY lists $1 strikes near the money
    sym = spy_option_symbol(t.expiry, K, right)

    px = daily[daily.symbol == sym].set_index("session")["close"].sort_index()
    px = px.reindex(share_path["session"]).ffill()
    hb = hourly[(hourly.symbol == sym) & (hourly.ts <= entry_ts) & (hourly.ts >= entry_ts.normalize())]
    p_entry = float(hb.sort_values("ts")["close"].iloc[-1]) if len(hb) else float(px.iloc[0]) if px.notna().iloc[0] else np.nan
    if np.isnan(p_entry):
        return pd.DataFrame()

    rows, prev_p, prev_m, last_iv = [], p_entry, 0, np.nan
    for k, r in share_path.reset_index(drop=True).iterrows():
        d = r["session"]
        is_last = k == len(share_path) - 1
        spy = float(r["spy"])
        if k == 0:
            p, tau = p_entry, (expiry_close - entry_ts).total_seconds() / 86400 / YEAR
        elif is_last:
            p, tau = (max(0.0, K - spy) if right == "PUT" else max(0.0, spy - K)), 0.0
        else:
            p, tau = float(px.loc[d]), (expiry_close - (d + pd.Timedelta(hours=16))).total_seconds() / 86400 / YEAR
        iv = implied_vol(p, spy, K, tau, right) if tau > 0 else np.nan
        if np.isnan(iv):
            iv = last_iv if not np.isnan(last_iv) else float(t.trigger_iv) * 0.6   # fallback: never used on a liquid ATM
        last_iv = iv
        d_spy = bs_delta(spy, K, tau, iv, right)
        target = abs(r["hedge_shares"]) / (abs(d_spy) * 100) if abs(d_spy) > 1e-3 else 0.0
        m = 0 if is_last else int(round(target))
        pnl = prev_m * (p - prev_p) * 100
        rows.append({"session": d, "spot": r["spot"], "spy": spy, "delta": r["delta"],
                     "spy_symbol": sym, "spy_px": p, "spy_iv": iv, "spy_delta": d_spy,
                     "spy_contracts": m, "hedge_notional": abs(r["hedge_shares"]) * spy,
                     "hedge_reserve": m * p * 100, "hedge_pnl": pnl, "option_mark": r["option_mark"]})
        prev_p, prev_m = p, m
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ portfolio
def portfolio(trades: pd.DataFrame, paths: dict, sessions: pd.DatetimeIndex,
              hedged: bool, label: str, account: float = ACCOUNT, net_hedge: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the account day by day. Returns (equity curve, trades with outcome)."""
    tr = trades.copy()
    tr["accepted"] = False
    tr["reject"] = None
    tr["option_pnl"] = np.nan
    tr["hedge_pnl"] = np.nan
    tr["net_pnl"] = np.nan
    tr["hedge_notional_entry"] = np.nan
    tr["hedge_reserve_entry"] = np.nan

    cash = account
    open_pos: dict[int, dict] = {}
    curve = []
    by_day = {d: g.sort_values("confirm_ts") for d, g in
              tr[tr.skip.isna()].assign(_d=lambda x: pd.to_datetime(x.confirm_ts).dt.normalize()).groupby("_d")}

    for d in sessions:
        # entries first, in print order, against margin already committed today
        for i, t in by_day.get(d, pd.DataFrame()).iterrows():
            path = paths.get(i)
            if path is None or not len(path):
                tr.at[i, "reject"] = "no_path"
                continue
            cost = t.contracts * t.contract_cost
            if net_hedge:
                # one SPY leg for the book: reserve is on the NET shares, so an
                # opposite-direction entry can lower it
                spy_d = path["spy"].iloc[0]
                net_now = sum(p["path"]["hedge_shares"].iloc[p["k"]] for p in open_pos.values())
                reserve_after = MARGIN * abs(net_now + path["hedge_shares"].iloc[0]) * spy_d
                reserve0 = reserve_after - MARGIN * abs(net_now) * spy_d
                committed = 0.0
                ok = cash - reserve_after >= cost
            else:
                reserve0 = path["hedge_reserve"].iloc[0] if hedged else 0.0
                committed = sum(p["path"]["hedge_reserve"].iloc[p["k"]]
                                for p in open_pos.values()) if hedged else 0.0
                ok = cash - committed >= cost + reserve0
            if not ok:
                tr.at[i, "reject"] = "insufficient_buying_power"
                continue
            cash -= cost
            open_pos[i] = {"path": path, "k": 0, "cost": cost}
            tr.at[i, "accepted"] = True
            tr.at[i, "hedge_notional_entry"] = path["hedge_notional"].iloc[0]
            tr.at[i, "hedge_reserve_entry"] = reserve0

        # then the day's hedge P&L and any expiries
        hedge_today = 0.0
        for i in list(open_pos):
            p = open_pos[i]
            row = p["path"].iloc[p["k"]]
            assert row["session"] == d, (i, row["session"], d)
            if hedged:
                cash += row["hedge_pnl"]; hedge_today += row["hedge_pnl"]
            if p["k"] == len(p["path"]) - 1:            # expiry
                t = tr.loc[i]
                settle = t.exit_price * 100 * t.contracts
                cash += settle
                tr.at[i, "option_pnl"] = settle - p["cost"]
                tr.at[i, "hedge_pnl"] = p["path"]["hedge_pnl"].sum() if hedged else 0.0
                tr.at[i, "net_pnl"] = tr.at[i, "option_pnl"] + tr.at[i, "hedge_pnl"]
                del open_pos[i]
            else:
                p["k"] += 1

        at_cost = sum(p["cost"] for p in open_pos.values())
        marks = sum(p["path"]["option_mark"].iloc[p["k"]] for p in open_pos.values())
        if net_hedge and open_pos:
            any_p = next(iter(open_pos.values()))
            margin = MARGIN * abs(sum(p["path"]["hedge_shares"].iloc[p["k"]] for p in open_pos.values())) * any_p["path"]["spy"].iloc[any_p["k"]]
        else:
            margin = sum(p["path"]["hedge_reserve"].iloc[p["k"]] for p in open_pos.values()) if hedged else 0.0
        curve.append({"session": d, "variant": label, "cash": cash, "open_positions": len(open_pos),
                      "margin_reserved": margin, "hedge_pnl_day": hedge_today,
                      "equity_realized": cash + at_cost, "equity_mtm": cash + marks})
    return pd.DataFrame(curve), tr


# --------------------------------------------------------------------- report
def _dd(series: pd.Series) -> float:
    peak = series.cummax()
    return float(((series - peak) / peak).min())


def summarize(curve: pd.DataFrame, tr: pd.DataFrame, label: str, paths: dict, hedged: bool) -> dict:
    cand = tr[tr.skip.isna() & tr.index.isin(paths)]
    cf_opt = float(((cand.exit_price - cand.entry_price) * 100 * cand.contracts).sum())
    cf_hedge = float(sum(paths[i]["hedge_pnl"].sum() for i in cand.index)) if hedged else 0.0
    cf_bp = pd.Series({i: cand.at[i, "contract_cost"] * cand.at[i, "contracts"] + (paths[i]["hedge_reserve"].iloc[0] if hedged else 0.0) for i in cand.index})
    acc = tr[tr.accepted]
    done = acc[acc.net_pnl.notna()]
    halves = {h: done[(done.confirm_ts < "2026-08-01") == (h == "Jun-Jul")] for h in ("Jun-Jul", "Aug-Sep")}
    return {
        "variant": label,
        "candidates": int(tr.skip.isna().sum()),
        "accepted": int(tr.accepted.sum()),
        "rejected_bp": int((tr.reject == "insufficient_buying_power").sum()),
        "win_rate": float((done.net_pnl > 0).mean()) if len(done) else np.nan,
        "option_pnl": float(done.option_pnl.sum()),
        "hedge_pnl": float(done.hedge_pnl.sum()),
        "net_pnl": float(done.net_pnl.sum()),
        "final_equity": float(curve.equity_realized.iloc[-1]),
        "max_dd_realized": _dd(curve.equity_realized),
        "max_dd_mtm": _dd(curve.equity_mtm),
        "max_open": int(curve.open_positions.max()),
        "pnl_JunJul": float(halves["Jun-Jul"].net_pnl.sum()),
        "pnl_AugSep": float(halves["Aug-Sep"].net_pnl.sum()),
        "cf_option_pnl": cf_opt,
        "cf_hedge_pnl": cf_hedge,
        "cf_net_pnl": cf_opt + cf_hedge,
        "cf_bp_per_pos_median": float(cf_bp.median()),
    }


def build(refresh_fills: bool = False, account: float = ACCOUNT, position_pct: float = POSITION_PCT):
    budget = account * position_pct
    tag = "" if (account, position_pct) == (ACCOUNT, POSITION_PCT) else f"_{account/1000:.0f}k_{position_pct*100:g}pct"
    flow = D.load()
    base = pd.read_parquet(TRADES_IN)
    tickers = sorted(base.ticker.unique().tolist()) + ["SPY"]

    adj = load_bars(tickers, "2025-11-01", "2026-09-21", timeframe="1Day", adjustment="all").field("close")
    raw = load_bars(tickers, "2026-05-01", "2026-09-21", timeframe="1Day", adjustment="raw").field("close")
    adj.index = adj.index.tz_localize(None).normalize()
    raw.index = raw.index.tz_localize(None).normalize()
    spy_h = load_bars("SPY", "2026-06-01", "2026-09-21", timeframe="1Hour", session="regular").field("close")["SPY"]
    spy_h.index = spy_h.index.tz_localize(None)
    sessions = raw.index[(raw.index >= "2026-06-01") & (raw.index <= "2026-09-21")]

    # --- confirmed variant: decide, refill, size, beta -------------------------
    conf = refill(confirm(base, flow), flow, refresh=refresh_fills)
    conf["contracts"] = np.where(conf.skip.isna(),
                                 np.floor(budget / conf.contract_cost.fillna(np.inf)), 0).astype(int)
    conf.loc[conf.skip.isna() & (conf.contracts == 0), "skip"] = "unaffordable"
    conf["beta"] = betas(conf, adj)
    conf.loc[conf.skip.isna() & conf.beta.isna(), "skip"] = "insufficient_beta_history"
    conf.loc[conf.skip.isna() & conf.trigger_iv.isna(), "skip"] = "no_trigger_iv"

    # --- base variant: the same sizing, no confirmation, original fills ---------
    unconf = base.copy()
    unconf["confirm_ts"] = unconf["entry_ts"]; unconf["confirm_spot"] = unconf["entry_spot"]
    unconf["confirm_path"] = "none"
    unconf["contracts"] = np.where(unconf.skip.isna(),
                                   np.floor(budget / unconf.contract_cost.fillna(np.inf)), 0).astype(int)
    unconf.loc[unconf.skip.isna() & (unconf.contracts == 0), "skip"] = "unaffordable"
    unconf["beta"] = np.nan; unconf["trigger_iv"] = conf["trigger_iv"]

    print("building hedge paths …", flush=True)
    paths_conf = {i: hedge_path(t, raw, spy_h) for i, t in conf[conf.skip.isna()].iterrows()}
    # unhedged variants still need a path for the option mark and the expiry date;
    # beta=0 gives zero shares and zero hedge P&L
    z = unconf[unconf.skip.isna()].copy(); z["beta"] = 0.0
    paths_base = {i: hedge_path(t, raw, spy_h) for i, t in z[z.trigger_iv.notna()].iterrows()}
    zc = conf[conf.skip.isna()].copy(); zc["beta"] = 0.0
    paths_conf_unhedged = {i: hedge_path(t, raw, spy_h) for i, t in zc.iterrows()}

    print("fetching SPY option bars …", flush=True)
    conf_c = conf[conf.skip.isna()]
    contracts = sorted({(spy_option_symbol(t.expiry, float(round(paths_conf[i]["spy"].iloc[0])), "PUT" if t.right == "CALL" else "CALL"),
                         pd.Timestamp(t.confirm_ts).normalize()) for i, t in conf_c.iterrows() if len(paths_conf[i])})
    spy_d, spy_h_opt = fetch_spy_option_bars(contracts)
    paths_spyopt = {i: option_hedge_path(t, paths_conf[i], spy_d, spy_h_opt) for i, t in conf_c.iterrows()}

    runs = [
        ("A_base_unhedged", unconf, paths_base, False),
        ("B_confirmed_unhedged", conf, paths_conf_unhedged, False),
        ("C_confirmed_hedged", conf, paths_conf, True),
        ("D_confirmed_spyopt", conf, paths_spyopt, True),
        ("E_confirmed_net_hedge", conf, paths_conf, True),
    ]
    curves, summaries, trade_frames = [], [], {}
    for label, tr, paths, hedged in runs:
        curve, out = portfolio(tr, paths, sessions, hedged, label, account, net_hedge=label.startswith("E_"))
        curves.append(curve); summaries.append(summarize(curve, out, label, paths, hedged)); trade_frames[label] = out

    pd.concat(curves).to_parquet(EQUITY_OUT.with_stem(EQUITY_OUT.stem + tag), index=False)
    final = trade_frames["C_confirmed_hedged"].copy()
    final["net_pnl_unhedged"] = trade_frames["B_confirmed_unhedged"]["net_pnl"]
    for c in ("accepted", "net_pnl", "hedge_pnl", "hedge_reserve_entry"):
        final[f"spyopt_{c}"] = trade_frames["D_confirmed_spyopt"][c]
    final["net_accepted"] = trade_frames["E_confirmed_net_hedge"]["accepted"]
    # capacity-free per trade: what each leg would have done had every candidate been taken
    final["cf_option_pnl"] = (final.exit_price - final.entry_price) * 100 * final.contracts
    final["cf_hedge_pnl_share"] = pd.Series({i: p["hedge_pnl"].sum() for i, p in paths_conf.items() if len(p)})
    final["cf_hedge_pnl_spyopt"] = pd.Series({i: p["hedge_pnl"].sum() for i, p in paths_spyopt.items() if len(p)})
    final["cf_spyopt_premium0"] = pd.Series({i: p["hedge_reserve"].iloc[0] for i, p in paths_spyopt.items() if len(p)})
    final["cf_spy_ret"] = pd.Series({i: p["spy"].iloc[-1] / p["spy"].iloc[0] - 1 for i, p in paths_conf.items() if len(p)})
    final.to_parquet(TRADES_OUT.with_stem(TRADES_OUT.stem + tag), index=False)
    return pd.DataFrame(summaries).set_index("variant"), final, pd.concat(curves)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refill", action="store_true")
    ap.add_argument("--account", type=float, default=ACCOUNT)
    ap.add_argument("--pct", type=float, default=POSITION_PCT, help="position budget as a fraction of the account")
    a = ap.parse_args()
    summary, trades, curves = build(refresh_fills=a.refill, account=a.account, position_pct=a.pct)
    print(f"account ${a.account:,.0f}   position ${a.account*a.pct:,.0f} ({a.pct:.1%})")
    pd.set_option("display.width", 200)
    print("\n=== skip reasons (confirmed variant) ===")
    print(trades.skip.value_counts(dropna=False).to_string())
    print("\n=== confirmation paths among candidates ===")
    print(trades[trades.skip.isna()].confirm_path.value_counts().to_string())
    print("\n=== summary ===")
    print(summary.T.to_string(float_format=lambda x: f"{x:,.3f}" if abs(x) < 10 else f"{x:,.0f}"))
    print("\nwrote data/hedged_trades*.parquet, data/hedged_equity*.parquet")

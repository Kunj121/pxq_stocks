"""Feature construction for the directional-sweep strategy.

Two ideas, kept separate because they fail differently.

**Direction** is `contractType` x `tradeSideCode`, never `contractType` alone.
Buying calls or selling puts is bullish; selling calls or buying puts is bearish.
On the golden-sweep screen 58% of prints land on the bid, so reading direction
from call/put alone inverts the majority of signals.

**Confirmation** is the one-sidedness of the other flow on that ticker, not how
much of it there is. A raw count of prior sweeps correlates +0.41 with "is an
ETF" and only -0.03 with the conviction ratio below: counting selects QQQ, SPY
and the mega-caps, which sweep constantly whatever they are about to do.

Every feature here is point-in-time — built only from prints that had already
happened when the trigger printed.
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE.parents[1] / "data" / "sweep_flow"

BUY_SIDES = ("ASK", "ABOVE_ASK")
SELL_SIDES = ("BID", "BELOW_BID")
DELTA_MIN, DELTA_MAX = 0.30, 0.70
TRIGGER_PREMIUM = 1_000_000

# SNDK trades near $1,640, so one contract costs a median $15,544 — 1.6x the next
# worst name and unaffordable at any realistic position cap. Excluded by name.
# Note this treats the symptom: ARM ($9.7k) and MU ($9.0k) are barely better, and
# MU alone is 65 of 471 triggers. `max_contract_cost` is the rule that generalises.
EXCLUDE_TICKERS = ("SNDK",)


def load(cache: Path | str = CACHE) -> pd.DataFrame:
    """Every cached ISO sweep print, chronologically, with direction attached."""
    files = sorted(glob.glob(f"{cache}/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no cached flow under {cache} — run fetch_flow.py")
    flow = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    flow["ts"] = pd.to_datetime(flow["ts"])
    flow = flow.sort_values("ts").reset_index(drop=True)
    flow["absdelta"] = flow["greeks.delta"].abs()
    flow["direction"] = direction(flow)
    # Signed premium is what makes one-sidedness measurable: a bullish print adds,
    # a bearish one subtracts, and a mid-market print (no aggressor) counts zero
    # rather than being forced to a side.
    flow["signed_premium"] = flow["premium"] * flow["direction"].map(
        {"BULL": 1, "BEAR": -1}).fillna(0)
    return flow


def direction(flow: pd.DataFrame) -> pd.Series:
    """BULL / BEAR / None from contract type crossed with the aggressor side."""
    buy = flow["tradeSideCode"].isin(BUY_SIDES)
    sell = flow["tradeSideCode"].isin(SELL_SIDES)
    call = flow["contractType"] == "CALL"
    put = flow["contractType"] == "PUT"
    return pd.Series(
        np.where((buy & call) | (sell & put), "BULL",
                 np.where((buy & put) | (sell & call), "BEAR", None)),
        index=flow.index, dtype=object)


def third_friday(year: int, month: int) -> pd.Timestamp:
    first = pd.Timestamp(year, month, 1)
    return first + pd.Timedelta(days=(4 - first.dayofweek) % 7 + 14)


def monthly_expiries(flow: pd.DataFrame, window: int = 3) -> list[pd.Timestamp]:
    """The monthly expiries that actually traded, discovered from the flow.

    Computing the third Friday is not enough: when it falls on a holiday the
    monthly moves. June 2026's third Friday is the 19th — Juneteenth — and the
    listed monthly is Thursday the 18th, which carried 1,311 prints against
    fewer than 100 for every neighbouring date. `backtest_strategy.third_friday`
    returns the 19th and, because that backtest prices with Black-Scholes rather
    than traded bars, it silently models a contract that never existed.

    So the monthly is taken as the *most-traded* listed expiry within `window`
    days of each third Friday. Volume settles it unambiguously.
    """
    listed = pd.to_datetime(pd.Series(sorted(flow["expirationDate"].unique())))
    counts = flow["expirationDate"].value_counts()

    best: dict[tuple[int, int], tuple[int, pd.Timestamp]] = {}
    for expiry in listed:
        anchor = third_friday(expiry.year, expiry.month)
        if abs((expiry - anchor).days) > window:
            continue
        prints = int(counts.get(expiry.strftime("%Y-%m-%d"), 0))
        key = (expiry.year, expiry.month)
        if key not in best or prints > best[key][0]:
            best[key] = (prints, expiry)
    return sorted(e for _, e in best.values())


def next_monthly(entry, expiries: list[pd.Timestamp], min_days: int = 7):
    """First listed monthly at least `min_days` beyond the entry."""
    entry = pd.Timestamp(entry)
    if entry.tz is not None:
        entry = entry.tz_convert("America/New_York").tz_localize(None)
    for expiry in expiries:
        if (expiry - entry.normalize()).days >= min_days:
            return expiry
    return None


def is_fund(flow: pd.DataFrame) -> pd.Series:
    """ETF or index underlying — 37% of all sweep prints.

    Excluded by default. A sweep on QQQ, SPY, SMH or a leveraged product like
    BITX or SPXS is portfolio hedging or index expression, not a view on a
    company, and it is the wrong population for a signal whose whole premise is
    that someone knows something specific. `isIndex` catches only NDX and RUT
    here, but it is the same argument, so both go.
    """
    return (flow["isETF"].fillna(False).astype(bool)
            | flow["isIndex"].fillna(False).astype(bool))


def triggers(flow: pd.DataFrame, premium: int = TRIGGER_PREMIUM,
             delta_band: tuple[float, float] = (DELTA_MIN, DELTA_MAX),
             exclude_funds: bool = True,
             exclude_tickers: tuple[str, ...] = EXCLUDE_TICKERS,
             max_contract_cost: float | None = None) -> pd.DataFrame:
    """The prints that fire the strategy: big, delta-banded, directionally clean.

    A print with no aggressor side has no direction, so it cannot be traded on
    and is dropped rather than defaulted to a side.

    `max_contract_cost` screens on what one contract actually costs
    (`optionPrice` x 100) rather than on a name. Prefer it to `exclude_tickers`
    once a position cap is fixed: it states the real constraint and keeps
    applying as new expensive names appear.
    """
    lo, hi = delta_band
    hit = (flow["premium"] >= premium) & flow["absdelta"].between(lo, hi) & flow["direction"].notna()
    if exclude_funds:
        hit &= ~is_fund(flow)
    if exclude_tickers:
        hit &= ~flow["ticker"].isin(exclude_tickers)
    if max_contract_cost is not None:
        hit &= (flow["optionPrice"] * 100) <= max_contract_cost
    out = flow[hit].copy()
    # Naive New York wall-clock, which is what every calendar here compares
    # against — fomc.is_decision_day, earnings.next_in_window and the option-bar
    # timestamps all speak it. Attaching it once beats each caller converting.
    out["entry_ts"] = out["ts"].dt.tz_convert("America/New_York").dt.tz_localize(None)
    return out


def confirm(flow: pd.DataFrame, trig: pd.DataFrame, lookback_sessions: int = 0) -> pd.DataFrame:
    """Attach point-in-time context from the same ticker's other sweeps.

    `lookback_sessions=0` uses the trigger's own session only; 1 adds the prior
    session, and so on. Returns the trigger frame with:

    - `prior_n`        how many sweeps preceded it — a liquidity measure
    - `same_dir_n`     how many of those agreed with the trigger's direction
    - `opp_dir_n`      how many disagreed
    - `net_dir_n`      same minus opposite — a count, but a signed one
    - `conviction`     net/gross signed premium in [-1, +1], scale-free
    - `aligned`        conviction signed toward the trigger's own direction

    `same_dir_n` is the literal "value count of sweeps in the same direction".
    It is kept alongside `conviction` because the two disagree about which
    triggers to keep, and only a return test settles which is right.
    """
    sessions = sorted(flow["session"].unique())
    index = {s: i for i, s in enumerate(sessions)}
    out = []

    for row in trig.itertuples():
        first = index[row.session] - lookback_sessions
        window = flow[
            (flow["ticker"] == row.ticker)
            & (flow["session"].map(index) >= max(first, 0))
            & (flow["ts"] < row.ts)
        ]
        gross = window["premium"].sum()
        net = window["signed_premium"].sum()
        conviction = net / gross if gross > 0 else np.nan
        same = int((window["direction"] == row.direction).sum())
        opposite = int(window["direction"].notna().sum() - same)
        out.append({
            "prior_n": len(window),
            "prior_premium": gross,
            "same_dir_n": same,
            "opp_dir_n": opposite,
            "net_dir_n": same - opposite,
            "conviction": conviction,
            "aligned": conviction if row.direction == "BULL" else -conviction,
        })

    return pd.concat([trig.reset_index(drop=True), pd.DataFrame(out)], axis=1)

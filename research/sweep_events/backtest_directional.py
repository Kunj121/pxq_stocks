"""Price the directional-sweep strategy against real traded option bars.

For each trigger: buy the ATM contract on the next third-Friday monthly, in the
direction the sweep implies, and hold to expiry.

**Entry is a real traded price** — the first bar the contract printed at or after
the sweep, from `qd_get_option_price_over_time`. Nothing here is Black-Scholes;
`backtest_strategy.price_path()` simulates and this deliberately does not.

**Exit is intrinsic at expiry**, valued off the underlying's close on expiry day.
That is what a held option actually settles at, and it sidesteps the fact that an
option bar only exists where the contract traded — a deep out-of-the-money
contract stops trading before expiry precisely when it is worthless.

**Everything stays in the vendor's unadjusted price space.** The strike, the
sweep's spot, and the settlement close all come from Quant Data, so they are
directly comparable. Mixing in Alpaca's split-adjusted bars is what produced the
fictional CRWD +5,368% before; `split_guard()` still checks for a split inside
the holding window, which genuinely breaks the contract terms.

    python research/sweep_events/backtest_directional.py

Takes ~15 minutes: one or more option-bar calls per trigger, plus a settlement
close per (ticker, expiry). Results are written once at the end, so an
interrupted run leaves nothing — rerun it rather than expecting a resume.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "quantdata"))

import directional as D  # noqa: E402
import earnings as E  # noqa: E402
import fomc  # noqa: E402
from backtest_strategy import atm_strike  # noqa: E402
from client import QuantData, QuantDataError  # noqa: E402

TRADES = HERE.parents[1] / "data" / "directional_trades.parquet"
MIN_DAYS = 7           # roll past a monthly closer than this
STRIKE_TRIES = 4       # nearest strikes to spot, closest first


def _bars(qd: QuantData, ticker: str, expiry: str, strike: float,
          right: str, session: str, period: str):
    try:
        frame, _ = qd.frame("qd_get_option_price_over_time", ticker=ticker,
                            expirationDate=expiry, strikePrice=float(strike),
                            contractType=right, sessionDate=session,
                            aggregationPeriod=period)
        return frame
    except (QuantDataError, Exception):
        return pd.DataFrame()


def strike_ladder(spot: float, listed: np.ndarray | None = None) -> list[float]:
    """Strikes to try, nearest-to-spot first.

    The vendor's strike snapshots only reach back a few weeks, so the grid comes
    from the strikes that actually traded on that ticker and expiry in the
    cached flow. That is real listing data, unlike `atm_strike`'s guessed step
    ladder, which produced $1.15 increments on a $230 stock.
    """
    candidates = {float(atm_strike(spot))}
    if listed is not None and len(listed):
        # Observed strikes are the ones that were SWEPT, not every strike listed,
        # so on their own they can sit far from the money — IBM's nearest swept
        # strike was 327.5 against a 315.58 spot. Merging the heuristic ATM in and
        # sorting by distance to spot takes the true ATM when it trades and falls
        # back to a real neighbour when it does not.
        candidates |= set(listed[np.argsort(np.abs(listed - spot))][:4].tolist())
    return sorted(candidates, key=lambda k: abs(k - spot))[:STRIKE_TRIES]


def entry_fill(qd: QuantData, ticker: str, expiry: str, spot: float, right: str,
               session: str, when: pd.Timestamp, listed: np.ndarray | None = None):
    """First traded price at or after the sweep. Returns (strike, price, volume).

    Bar timestamps come back tz-aware; the calendar logic upstream works in naive
    New York wall-clock. Align here rather than stripping the bars, so the
    comparison cannot silently shift by the UTC offset.
    """
    when = pd.Timestamp(when)
    if when.tz is None:
        when = when.tz_localize("America/New_York")
    for strike in strike_ladder(spot, listed):
        bars = _bars(qd, ticker, expiry, strike, right, session, "ONE_MINUTE")
        if not len(bars):
            continue
        after = bars[bars["ts"] >= when]
        # No print after the sweep means the contract went quiet for the rest of
        # the session — not fillable, so the trigger is dropped rather than
        # filled at a price from before the signal existed.
        if not len(after):
            continue
        row = after.iloc[0]
        return float(strike), float(row["openPrice"]), int(bars["volume"].sum())
    return None, None, None


def settle_close(qd: QuantData, ticker: str, session: str, cache: dict):
    """Underlying close on a session, vendor (unadjusted) space."""
    key = (ticker, session)
    if key in cache:
        return cache[key]
    try:
        bars, _ = qd.frame("qd_get_stock_price_over_time", ticker=ticker,
                           sessionDate=session, aggregationPeriod="ONE_HOUR")
        close = float(bars["closePrice"].iloc[-1]) if len(bars) else None
    except Exception:
        close = None
    cache[key] = close
    return close


def split_guard(entry_spot: float, exit_spot: float) -> bool:
    """True if the move looks like a split rather than a price move."""
    if not entry_spot or not exit_spot:
        return False
    ratio = exit_spot / entry_spot
    return any(abs(ratio - r) < 0.08 for r in (0.5, 0.25, 0.2, 0.1, 2.0, 3.0, 4.0))


def build() -> pd.DataFrame:
    qd = QuantData()
    flow = D.load()
    trig = D.triggers(flow).sort_values("ts").reset_index(drop=True)
    expiries = D.monthly_expiries(flow)
    cal = E.frame()
    covered = set(cal["symbol"])
    cal_from, cal_to = cal["date"].min(), cal["date"].max()

    # strikes that actually traded, per (ticker, expiry) — free, from the cache
    grid = {k: np.sort(g["strikePrice"].unique())
            for k, g in flow.groupby(["ticker", "expirationDate"])}

    closes: dict = {}
    out = []
    for n, t in enumerate(trig.itertuples(), 1):
        entry_ts = pd.Timestamp(t.ts).tz_convert("America/New_York").tz_localize(None)
        row = {"ticker": t.ticker, "session": t.session, "entry_ts": entry_ts,
               "direction": t.direction, "sweep_premium": t.premium,
               "sweep_delta": t.absdelta, "entry_spot": t.stockPrice,
               "prior_n": np.nan, "skip": None}

        if fomc.is_decision_day(entry_ts):
            row["skip"] = "fomc_day"
            out.append(row); continue
        if t.ticker not in covered:
            row["skip"] = "no_earnings_coverage"
            out.append(row); continue

        expiry = D.next_monthly(entry_ts, expiries, min_days=MIN_DAYS)
        if expiry is None:
            row["skip"] = "no_monthly_listed"
            out.append(row); continue
        row["expiry"] = expiry.date().isoformat()

        # `next_in_window` returns None both for "no report in this window" and for
        # "the calendar does not reach these dates" — indistinguishable, and the second
        # silently passes the veto. Symbol coverage above does not imply date coverage,
        # so a trigger whose holding window falls outside the calendar's span is
        # rejected rather than waved through.
        if entry_ts < cal_from or expiry > cal_to:
            row["skip"] = "no_earnings_coverage"
            out.append(row); continue

        if E.next_in_window(t.ticker, entry_ts, expiry + pd.Timedelta(hours=16), cal):
            row["skip"] = "earnings_in_window"
            out.append(row); continue

        right = "CALL" if t.direction == "BULL" else "PUT"
        row["right"] = right
        listed = grid.get((t.ticker, row["expiry"]))
        strike, fill, vol = entry_fill(qd, t.ticker, row["expiry"], t.stockPrice,
                                       right, t.session, entry_ts, listed)
        if fill is None or fill <= 0:
            row["skip"] = "no_entry_print"
            out.append(row); continue

        exit_spot = settle_close(qd, t.ticker, row["expiry"], closes)
        if exit_spot is None:
            row["skip"] = "no_settlement_price"
            out.append(row); continue
        if split_guard(t.stockPrice, exit_spot):
            row["skip"] = "split_suspected"
            out.append(row); continue

        intrinsic = (max(0.0, exit_spot - strike) if right == "CALL"
                     else max(0.0, strike - exit_spot))
        row.update({"strike": strike, "entry_price": fill, "entry_volume": vol,
                    "exit_spot": exit_spot, "exit_price": intrinsic,
                    "ret": intrinsic / fill - 1.0,
                    "contract_cost": fill * 100})
        out.append(row)
        if n % 25 == 0:
            print(f"  {n}/{len(trig)} …", flush=True)

    frame = pd.DataFrame(out)
    frame.to_parquet(TRADES, index=False)
    return frame


if __name__ == "__main__":
    result = build()
    traded = result[result.skip.isna()]
    print(f"\n{len(result)} triggers -> {len(traded)} priced")
    print(result.skip.value_counts(dropna=False).rename("n").to_string())
    if len(traded):
        print(f"\nmean return {traded.ret.mean():+.1%}   median {traded.ret.median():+.1%}   "
              f"win rate {(traded.ret > 0).mean():.0%}")
    print(f"-> {TRADES}")

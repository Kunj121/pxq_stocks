"""Real-time golden-sweep alerts to Slack.

    python research/quantdata/sweep_alerts.py --dry-run    # print, post nothing
    python research/quantdata/sweep_alerts.py              # watch and post
    python research/quantdata/sweep_alerts.py --once       # one pass, for a scheduler

The screen
----------
A print alerts when **all** of these hold:

    tradeConsolidationType   SWEEP
    tradeType                ISO          — it actually swept multiple venues
    underlying               not an ETF, not an index
    |delta|                  0.30 - 0.70
    aggressor side           known (MID_MARKET has none, so no direction)
    premium                  >= PREMIUM_FLOOR

`ISO` is the definition of "golden" used here, and it is deliberately **not**
the vendor's `isGoldenSweep` badge. The badge is a loose unusual-activity
marker: on 2026-09-21 at a $1M floor it flagged 121 prints against 13 for
ISO + SWEEP, overlapping on 9, and 70 of the golden-only prints were `AUTO` —
ordinary electronic fills. See `golden_sweeps()` in feeds.py.

Why the alert names a direction and not just call/put
-----------------------------------------------------
Direction is `contractType` x `tradeSideCode`. Buying calls or selling puts is
bullish; selling calls or buying puts is bearish. **58% of qualifying ISO
sweeps print on the bid** — sold, not bought — so reading a call as bullish
inverts the majority of signals. Every alert carries the contract type you
asked for *and* the side that settles what it means.

Cold start does not blast the backlog
--------------------------------------
The feed serves the whole session, so a first run at noon would otherwise fire
every alert the morning already produced. On a session it has not seen, the
watcher records what has already printed and alerts only on what comes next.
Pass `--backfill` when you genuinely want the day so far.

Cost
----
One paginated call chain per poll. Keep PREMIUM_FLOOR meaningful: the floor is
applied server-side, so a high floor is also a cheap one. Measured on
2026-09-22, a full session after the delta and ETF cuts:

    floor        alerts/session
    $0                      472
    $100k                   122
    $250k                    47
    $500k                    13
    $1M                       3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "research" / "sweep_events"), str(ROOT)]

import directional as D  # noqa: E402
import feeds as F  # noqa: E402
from client import QuantDataError  # noqa: E402

#: Default premium floor. $1M is the level the directional strategy triggers on
#: (`directional.TRIGGER_PREMIUM`) and keeps the channel to a few alerts a day.
#: Lower it for more coverage; the table in the module docstring is the cost.
PREMIUM_FLOOR = 1_000_000

DELTA_MIN, DELTA_MAX = D.DELTA_MIN, D.DELTA_MAX

#: Premium floor for the *context* pull that measures repeated flow. Much lower
#: than the alert floor on purpose: recurrence is about whether a name is being
#: worked steadily, and $200k prints are evidence of that even though they do
#: not alert on their own. Six sessions at this floor is ~1,900 rows in 1.6s.
CONTEXT_PREMIUM = 100_000

#: Sessions of prior flow used to judge recurrence. 0 disables the context pull.
LOOKBACK_SESSIONS = 5

#: Volume / prior open interest. The vendor's `volume` is today's contract
#: volume and `openInterest` is the prior session's close, so the ratio is a
#: like-for-like "how much of today's trade is new". Bands are read, not law:
#: a high ratio says fresh, never which way.
VOL_OI_BANDS = ((0.5, "routine"), (2.0, "notable"), (5.0, "fresh"))
VOL_OI_EXTREME = "investigate"
#: Open interest of zero is not a huge ratio, it is a contract that did not
#: exist yesterday. Division would give inf and hide the more useful fact.
VOL_OI_NEW = "new strike"

#: Where fired alerts are recorded. Gitignored with the rest of `data/`.
#: This doubles as the dedup ledger — one artefact, so a restart cannot replay
#: alerts that a separate state file might have disagreed about.
ALERT_DIR = ROOT / "data" / "sweep_alerts"

MARKET_OPEN, MARKET_CLOSE = "09:30", "16:00"

#: Columns kept in the ledger. The raw print has 48; these are the ones an alert
#: is built from plus what makes a fired alert auditable afterwards.
LEDGER_COLUMNS = ["id", "ts", "ticker", "contractType", "strikePrice",
                  "expirationDate", "dte", "direction", "tradeSideCode",
                  "tradeType", "absdelta", "impliedVolatility", "premium", "optionPrice",
                  "stockPrice", "size", "volume", "openInterest", "vol_oi",
                  "isOpeningPosition", "sector",
                  "prior_n", "same_dir_n", "opp_dir_n", "conviction", "aligned",
                  "near_strike_n", "same_expiry_n", "lookback_sessions"]

#: What a CALL print must have done, to alert. Puts are never constrained by
#: this: a put's meaning is already carried by its side crossed with its type,
#: and the asymmetry is deliberate rather than an oversight.
#:
#:   any        no constraint (the default, and prior behaviour)
#:   bullish    bought — ASK or ABOVE_ASK, so direction is BULL
#:   above-ask  paid through the offer, the most urgent version
#:
#: `above-ask` is much rarer than it sounds: 77 of 1,135 call prints over
#: 15-22 September, and none at all among the prints that cleared the live
#: screen. See the README before turning it on.
#: What "golden sweep" means when pulling. Two defensible definitions, and they
#: are nearly disjoint, so this is a real choice rather than a knob.
#:
#:   iso    ISO + SWEEP. The routing fact: the order swept multiple exchanges.
#:   badge  the vendor's own `isGoldenSweep`. Its population is entirely SWEEP
#:          consolidation but only 3% ISO — 52% is `AUTO` (ordinary electronic
#:          fills) and 43% `MULTI_AUTO_COB` (multi-leg).
#:   both   carries the badge AND routed as ISO. The strictest reading.
#:
#: Measured over 18-23 September: `iso` yields about 1 alert a session through
#: the full screen, `badge` about 13.
#:
#: **The badge admits multi-leg prints.** A `MULTI_*_COB` is one leg of a
#: spread, so the aggressor side on it describes that leg, not the position.
#: `direction` will still be assigned and should be trusted less there.
GOLDEN_MODES = {
    "iso": {"trade_types": ("ISO",), "golden": None},
    "badge": {"trade_types": None, "golden": True},
    "both": {"trade_types": ("ISO",), "golden": True},
}

CALL_SIDES = {
    "any": None,
    "bullish": ("ASK", "ABOVE_ASK"),
    "above-ask": ("ABOVE_ASK",),
}

SIDE_PHRASE = {
    "ABOVE_ASK": "*paid THROUGH the offer*",
    "ASK": "lifted the ask",
    "BID": "hit the bid",
    "BELOW_BID": "hit below the bid",
}


def session_today() -> str:
    return pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")


def market_is_open(now: pd.Timestamp | None = None) -> bool:
    """Weekday, 09:30-16:00 ET. Holidays simply return no prints, so they are
    not special-cased — a holiday poll is one cheap empty call."""
    now = now or pd.Timestamp.now(tz="America/New_York")
    return now.weekday() < 5 and MARKET_OPEN <= now.strftime("%H:%M") <= MARKET_CLOSE


def ledger_path(session: str) -> Path:
    return ALERT_DIR / f"alerts_{session}.parquet"


def load_ledger(session: str) -> pd.DataFrame:
    path = ledger_path(session)
    if path.is_file():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=LEDGER_COLUMNS)


def append_ledger(session: str, rows: pd.DataFrame) -> None:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    keep = [c for c in LEDGER_COLUMNS if c in rows.columns]
    combined = pd.concat([load_ledger(session), rows[keep]], ignore_index=True)
    combined.to_parquet(ledger_path(session), index=False)


def _et(value: str, end_of_day: bool = False) -> pd.Timestamp:
    """Parse a user-supplied date or datetime as Eastern.

    A bare `YYYY-MM-DD` used as an upper bound means the end of that day. Read
    literally it would mean 00:00, so `--signal-max 2026-09-22` would exclude
    the entire session it names — a filter that silently returns nothing is
    worse than one that errors.
    """
    stamp = pd.Timestamp(value)
    if end_of_day and stamp == stamp.normalize() and len(str(value).strip()) <= 10:
        stamp = stamp + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return stamp.tz_localize("America/New_York") if stamp.tz is None else stamp


def vol_oi(flow: pd.DataFrame) -> pd.Series:
    """Today's volume over prior open interest. NaN where OI is zero.

    NaN rather than infinity, because a zero-OI contract is a *new strike*
    rather than a contract with an enormous ratio, and the two deserve
    different words in an alert.
    """
    oi = flow["openInterest"].where(flow["openInterest"] > 0)
    return flow["volume"] / oi


def vol_oi_band(ratio: float, open_interest) -> str:
    """The interpretation attached to a ratio. Says fresh, never says which way."""
    if open_interest is not None and not pd.isna(open_interest) and open_interest == 0:
        return VOL_OI_NEW
    if pd.isna(ratio):
        return "?"
    for threshold, label in VOL_OI_BANDS:
        if ratio < threshold:
            return label
    return VOL_OI_EXTREME


def prepare(flow: pd.DataFrame) -> pd.DataFrame:
    """Attach the derived columns every other function here expects."""
    if not len(flow):
        return flow
    flow = flow.copy()
    flow["absdelta"] = flow["greeks.delta"].abs()
    flow["direction"] = D.direction(flow)
    # Signed premium makes one-sidedness measurable: bullish adds, bearish
    # subtracts, and a print with no aggressor counts zero rather than being
    # forced onto a side. Mirrors directional.load().
    flow["signed_premium"] = flow["premium"] * flow["direction"].map(
        {"BULL": 1, "BEAR": -1}).fillna(0)
    flow["vol_oi"] = vol_oi(flow)
    return flow


def screen(flow: pd.DataFrame, premium_floor: float = PREMIUM_FLOOR,
           delta_band: tuple[float, float] = (DELTA_MIN, DELTA_MAX),
           dte_band: tuple[float, float] = (0.0, float("inf")),
           min_vol_oi: float = 0.0, min_oi: int = 0,
           call_side: str = "any",
           contract_types: tuple[str, ...] = (),
           sides: tuple[str, ...] = (),
           trade_types: tuple[str, ...] = (),
           expiry_min: str | None = None, expiry_max: str | None = None,
           signal_min: str | None = None, signal_max: str | None = None,
           exclude_funds: bool = True,
           exclude_tickers: tuple[str, ...] = ()) -> pd.DataFrame:
    """Apply the per-print alert screen to a frame of ISO sweep prints.

    Every band is inclusive at both ends. Defaults are the permissive ones
    except `delta_band`, so adding a parameter never silently narrows what
    already alerts.

    `exclude_tickers` defaults to **empty**, unlike `directional.triggers()`,
    which drops SNDK. That exclusion is about what a contract costs to buy, not
    about whether the print is a real signal — a position-sizing rule has no
    business suppressing an alert. Pass it explicitly if you want it.

    `min_vol_oi` treats a zero-OI contract as passing any threshold: it is the
    freshest thing on the tape, and dropping it for an undefined ratio would
    invert the filter's intent.

    `contract_types` keeps only CALL or only PUT; `sides` keeps only the named
    aggressor sides; `trade_types` keeps only the named routing types (`ISO`,
    `AUTO`, `MULTI_AUTO_COB` and the rest). All are empty by default, meaning
    no constraint.

    `trade_types` filters what was already pulled, so it composes with any
    `GOLDEN_MODES` choice rather than replacing it. Note what that combination
    costs in badge mode: only ~3% of badge prints are ISO, so
    `trade_types=("ISO",)` there took 63 alerts to 3 over four sessions.

    There is no separate sentiment filter because there is no separate thing to
    filter: the vendor's `sentimentType` agrees with `direction` on 100% of
    prints (BULLISH/BEARISH/NEUTRAL against BULL/BEAR/None), being the same
    function of contract type and aggressor side. Filtering `direction` *is*
    filtering sentiment.

    `call_side` constrains CALL prints only — see `CALL_SIDES`. Puts pass
    whatever they did, which is the asymmetry being asked for, not a bug.

    `expiry_min` / `expiry_max` bound the **absolute** expiration date, as
    `YYYY-MM-DD`. They are independent of `dte_band`, which bounds the same
    thing relatively; use the absolute form to pin one expiry cycle and the
    relative form for a rolling window.

    `signal_min` / `signal_max` bound **when the print happened** — the date
    the signal was given, not the date it expires. Both accept `YYYY-MM-DD` or
    `YYYY-MM-DD HH:MM`, read as Eastern. A bare date on `signal_max` means the
    end of that day rather than midnight at its start, so
    `signal_min=signal_max="2026-09-22"` selects that whole session instead of
    nothing, which is what anyone typing one date means.

    `min_oi` is the separate, blunter statement that a contract with no prior
    open interest should not alert at all. It is applied on its own and is NOT
    subject to that exemption — the two would otherwise contradict each other,
    and an explicit floor should win over an exemption built into a different
    filter. Open interest is an integer, so "greater than 1" is `min_oi=2`.
    """
    if not len(flow):
        return flow
    if "direction" not in flow.columns:
        flow = prepare(flow)

    lo, hi = delta_band
    dte_lo, dte_hi = dte_band
    hit = ((flow["premium"] >= premium_floor)
           & flow["absdelta"].between(lo, hi)
           & flow["direction"].notna()
           & flow["dte"].between(dte_lo, dte_hi))
    if min_vol_oi > 0:
        new_strike = flow["openInterest"].fillna(-1) == 0
        hit &= (flow["vol_oi"] >= min_vol_oi) | new_strike
    if min_oi > 0:
        # A null open interest is unknown, not zero, so it is not silently
        # treated as failing a floor it was never measured against.
        hit &= flow["openInterest"].fillna(min_oi) >= min_oi
    if call_side not in CALL_SIDES:
        raise ValueError(f"call_side must be one of {sorted(CALL_SIDES)}")
    allowed = CALL_SIDES[call_side]
    if allowed:
        is_call = flow["contractType"] == "CALL"
        hit &= ~is_call | (flow["tradeSideCode"].isin(allowed)
                           & (flow["direction"] == "BULL"))
    if contract_types:
        hit &= flow["contractType"].isin(contract_types)
    if sides:
        hit &= flow["tradeSideCode"].isin(sides)
    if trade_types:
        hit &= flow["tradeType"].isin(trade_types)
    if expiry_min:
        hit &= flow["expirationDate"] >= expiry_min
    if expiry_max:
        hit &= flow["expirationDate"] <= expiry_max
    if signal_min:
        hit &= flow["ts"] >= _et(signal_min)
    if signal_max:
        hit &= flow["ts"] <= _et(signal_max, end_of_day=True)
    if exclude_funds:
        hit &= ~D.is_fund(flow)
    if exclude_tickers:
        hit &= ~flow["ticker"].isin(exclude_tickers)
    return flow[hit].sort_values("ts").reset_index(drop=True)


#: Strike proximity counted as "the same neighbourhood", as a fraction of the
#: trigger's strike. 10% is wide enough that 85/90/95 on a $87 name all count,
#: narrow enough that a $11 strike does not absorb a $30 one.
STRIKE_NEIGHBOURHOOD = 0.10


def context(session: str, lookback: int = LOOKBACK_SESSIONS,
            premium_min: float = CONTEXT_PREMIUM,
            golden: str = "iso") -> pd.DataFrame:
    """Prior sessions of ISO sweep flow, for judging recurrence.

    Pulled at a much lower premium floor than the alert itself: a $200k print
    does not deserve an alert but is real evidence that a name is being worked.
    Six sessions is roughly 1,900 rows and under two seconds, so this is
    refreshed once per session rather than per poll.

    Returns empty when `lookback` is 0, which disables recurrence entirely.
    """
    if lookback <= 0:
        return pd.DataFrame()
    end = pd.Timestamp(session) - pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=lookback * 2 + 4)   # calendar slack for weekends
    frame = F.sweeps(start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), premium_min=premium_min,
                     **GOLDEN_MODES[golden])
    if not len(frame):
        return frame
    sessions = sorted(frame["session"].unique())[-lookback:]
    return prepare(frame[frame["session"].isin(sessions)]).reset_index(drop=True)


def recurrence(hits: pd.DataFrame, flow: pd.DataFrame) -> pd.DataFrame:
    """Attach repeated-flow features to each alert, point-in-time.

    One large print can be a hedge. The same direction turning up again and
    again in a name — especially near the same strike or expiry — is the thing
    worth noticing, so every feature here counts only prints that had **already
    happened** when the trigger printed.

    `conviction` is net over gross signed premium in [-1, +1] and `aligned` is
    that signed toward the trigger's own direction, so +1 means the prior flow
    agreed completely and -1 means it was entirely the other way. The ratio is
    used rather than a count because a raw count of prior sweeps correlates
    +0.41 with merely being a heavily traded name and only -0.03 with the
    ratio: counting selects the usual suspects, not conviction.

    Returns `hits` unchanged, plus null feature columns, when there is no
    context to judge against — a missing history is not a reason to drop a
    signal.
    """
    blank = {"prior_n": pd.NA, "same_dir_n": pd.NA, "opp_dir_n": pd.NA,
             "conviction": float("nan"), "aligned": float("nan"),
             "same_expiry_n": pd.NA, "near_strike_n": pd.NA,
             "lookback_sessions": 0}
    if not len(hits):
        return hits
    # `session` is what orders the lookback. The live feed always supplies it;
    # a hand-built frame may not, and losing enrichment beats losing the alert.
    if not len(flow) or "session" not in flow or "session" not in hits:
        return hits.assign(**blank)

    combined = pd.concat([flow, hits], ignore_index=True)
    combined = combined.drop_duplicates(subset="id").sort_values("ts")

    scored = D.confirm(combined, hits, lookback_sessions=len(
        combined["session"].unique()))

    extra = []
    for row in hits.itertuples():
        window = combined[(combined["ticker"] == row.ticker)
                          & (combined["ts"] < row.ts)]
        same_dir = window[window["direction"] == row.direction]
        span = abs(row.strikePrice) * STRIKE_NEIGHBOURHOOD
        extra.append({
            "same_expiry_n": int((same_dir["expirationDate"] == row.expirationDate).sum()),
            "near_strike_n": int((same_dir["strikePrice"] - row.strikePrice).abs().le(span).sum()),
            "lookback_sessions": int(flow["session"].nunique()),
        })
    return pd.concat([scored.reset_index(drop=True),
                      pd.DataFrame(extra)], axis=1)


def _num(value, fmt: str = ",.0f", dash: str = "?") -> str:
    """Format a number, or a dash when the vendor left the field empty."""
    return dash if value is None or pd.isna(value) else format(value, fmt)


def _fields(row) -> dict:
    """A plain dict of one row, whether it arrived as a namedtuple or a Series.

    Attribute access is not safe here. `size` is a real column in this feed and
    also a pandas attribute, so `row.size` on a Series silently returns the
    element count instead of the trade size — the right type, a plausible
    number, and completely wrong. Going through a dict removes the whole class
    of collision, `index` and `count` included.
    """
    if hasattr(row, "_asdict"):          # itertuples namedtuple
        return dict(row._asdict())
    return dict(row)                      # Series or mapping


def format_alert(row) -> str:
    """One print as a Slack message. Ticker, strike, expiry and type lead.

    Four lines, plus a fifth when recurrence context is available:
    *what contract*, *how it was read*, *what it cost*, *today's context*, and
    *how it sits against recent sessions*.

    `size` and `volume` are paired deliberately. `size` is this print alone;
    `volume` is everything that contract traded today, so the two together say
    whether the sweep IS the day's activity or merely joins it. Open interest
    is the denominator that makes volume mean something — volume above open
    interest is the standard read for positions being opened, not closed.

    Cost per contract is `optionPrice * 100`, shown because it is the number
    that decides whether a signal is actionable at all: SNDK prints near
    $13,550 a contract against IBRX at $169. `optionPrice * 100 * size`
    reconciles to `premium` within a rounding error, the gap being that a sweep
    fills at several prices while `optionPrice` reports one.

    `impliedVolatility` arrives already scaled as a percent (72.8, not 0.728).

    The routing type leads the second line, **bold when it is `ISO`**. In badge
    mode most prints are not: 52% are `AUTO` electronic fills and 43% are
    multi-leg `MULTI_*_COB`, whose aggressor side describes one leg rather than
    the position. Showing it costs nothing and makes that visible per alert,
    where filtering on it would cut 63 alerts to 3 over four sessions.
    """
    f = _fields(row)
    strike = f"{f['strikePrice']:,.2f}".rstrip("0").rstrip(".")
    arrow = "🟢" if f["direction"] == "BULL" else "🔴"
    side = SIDE_PHRASE.get(f["tradeSideCode"], f["tradeSideCode"])
    # The full date, not just the clock. Scrolling back through Slack, "13:27"
    # alone is unreadable — you are relying on Slack's own message timestamp to
    # tell you which session a signal belongs to, and that is the delivery time,
    # not the print time. They differ whenever the watcher restarts or catches up.
    when = pd.Timestamp(f["ts"]).tz_convert("America/New_York").strftime(
        "%a %d %b %H:%M:%S")
    dte = f"{int(f['dte'])}dte" if pd.notna(f.get("dte")) else "?dte"

    # `or "?"` is not enough: a missing value arrives as NaN, which is truthy,
    # so the alert would read "nan" where it means "not supplied".
    kind = f.get("tradeType")
    kind = "?" if kind is None or pd.isna(kind) else str(kind)
    # Bold only ISO: it is the one value that says the order genuinely swept
    # multiple exchanges, and bolding everything would emphasise nothing.
    kind = f"*{kind}*" if kind == "ISO" else kind

    spot = _num(f.get("stockPrice"), ",.2f")
    size = _num(f.get("size"))
    volume = _num(f.get("volume"))
    oi = _num(f.get("openInterest"))
    iv = _num(f.get("impliedVolatility"), ",.1f")

    price = f.get("optionPrice")
    each = _num(price, ",.2f")
    per_contract = _num(None if price is None or pd.isna(price) else price * 100)

    band = vol_oi_band(f.get("vol_oi", float("nan")), f.get("openInterest"))
    if band == VOL_OI_NEW:
        ratio = f" ({VOL_OI_NEW})"
    elif pd.isna(f.get("vol_oi", float("nan"))):
        ratio = ""
    else:
        ratio = f" ({f['vol_oi']:.1f}x — {band})"

    # The recurrence line is omitted rather than shown empty: with no context
    # pulled there is nothing to say, and "0 prior" would read as evidence of
    # absence rather than absence of evidence.
    recur = ""
    prior = f.get("prior_n")
    if prior is not None and not pd.isna(prior):
        days = int(f.get("lookback_sessions") or 0)
        same = int(f.get("same_dir_n") or 0)
        aligned = f.get("aligned")
        agree = ("" if aligned is None or pd.isna(aligned)
                 else f" · conviction {aligned:+.2f}")
        near = int(f.get("near_strike_n") or 0)
        expiry_n = int(f.get("same_expiry_n") or 0)
        recur = (f"\n{days}d flow: {int(prior)} prior · {same} same way"
                 f"{agree} · {near} near strike · {expiry_n} same expiry")

    return (
        f"{arrow} *{f['ticker']}* {f['contractType']} ${strike} "
        f"exp {f['expirationDate']} ({dte})\n"
        f"{kind} · {f['direction']} · {side} · Δ {f['absdelta']:.2f} · IV {iv}%\n"
        f"${f['premium']:,.0f} premium · ${each} ea · "
        f"${per_contract} per contract · {when} ET\n"
        f"spot ${spot} · size {size} · vol {volume} vs OI {oi}{ratio}"
        f"{recur}"
    )


def poll(session: str, seen: set[str], premium_floor: float,
         exclude_tickers: tuple[str, ...] = (), ctx: pd.DataFrame | None = None,
         min_conviction: float | None = None, min_prior: int = 0,
         golden: str = "iso", **screen_kw) -> pd.DataFrame:
    """Fetch, screen, score recurrence, and return prints not already in `seen`.

    Today's flow is fetched at the *context* floor rather than the alert floor,
    so sub-threshold prints on the same name still count toward recurrence.
    That costs about 0.2s a poll and is what makes intraday repetition visible
    at all.

    The recurrence filters let a print through when its features are null. A
    name with no history is unproven, not disproven, and dropping it would make
    the filter quietly reject every first sighting.
    """
    fetch_floor = min(CONTEXT_PREMIUM, premium_floor)
    flow = prepare(F.sweeps(session, premium_min=fetch_floor,
                            **GOLDEN_MODES[golden]))
    if not len(flow):
        return pd.DataFrame()

    hits = screen(flow, premium_floor=premium_floor,
                  exclude_tickers=exclude_tickers, **screen_kw)
    if not len(hits):
        return hits
    fresh = hits[~hits["id"].astype(str).isin(seen)]
    if not len(fresh):
        return fresh

    history = (pd.concat([ctx, flow], ignore_index=True)
               if ctx is not None and len(ctx) else flow)
    fresh = recurrence(fresh.reset_index(drop=True), history)

    if min_prior:
        fresh = fresh[fresh["same_dir_n"].fillna(min_prior) >= min_prior]
    if min_conviction is not None:
        fresh = fresh[fresh["aligned"].fillna(min_conviction) >= min_conviction]
    return fresh.reset_index(drop=True)


def run(premium_floor: float = PREMIUM_FLOOR, interval: int = 60,
        once: bool = False, dry_run: bool = False, backfill: bool = False,
        exclude_tickers: tuple[str, ...] = (), session: str | None = None,
        delta_band: tuple[float, float] = (DELTA_MIN, DELTA_MAX),
        dte_band: tuple[float, float] = (0.0, float("inf")),
        min_vol_oi: float = 0.0, min_oi: int = 0, call_side: str = "any",
        contract_types: tuple[str, ...] = (), sides: tuple[str, ...] = (),
        trade_types: tuple[str, ...] = (),
        expiry_min: str | None = None, expiry_max: str | None = None,
        signal_min: str | None = None, signal_max: str | None = None,
        golden: str = "iso", lookback: int = LOOKBACK_SESSIONS,
        min_conviction: float | None = None, min_prior: int = 0) -> int:
    poster = None
    if not dry_run:
        from execution.slack import Slack, SlackError
        try:
            poster = Slack()
        except SlackError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1

    session = session or session_today()
    screen_kw = dict(delta_band=delta_band, dte_band=dte_band,
                     min_vol_oi=min_vol_oi, min_oi=min_oi,
                     call_side=call_side, contract_types=contract_types,
                     sides=sides, trade_types=trade_types,
                     expiry_min=expiry_min, expiry_max=expiry_max,
                     signal_min=signal_min, signal_max=signal_max)
    if golden not in GOLDEN_MODES:
        print(f"FATAL: --golden must be one of {sorted(GOLDEN_MODES)}", file=sys.stderr)
        return 1
    filters = dict(min_conviction=min_conviction, min_prior=min_prior,
                   golden=golden)

    def load_context(day: str) -> pd.DataFrame:
        if lookback <= 0:
            return pd.DataFrame()
        try:
            frame = context(day, lookback=lookback, golden=golden)
            print(f"context: {len(frame)} prints over "
                  f"{frame['session'].nunique() if len(frame) else 0} prior sessions")
            return frame
        except QuantDataError as exc:
            # Recurrence is enrichment, not the signal. Losing it must not stop
            # the watcher from alerting on today's prints.
            print(f"context unavailable, recurrence disabled: {str(exc)[:90]}",
                  file=sys.stderr)
            return pd.DataFrame()

    ledger = load_ledger(session)
    seen: set[str] = set(ledger["id"].astype(str)) if len(ledger) else set()
    cold = not len(ledger)

    dte_note = ("" if dte_band == (0.0, float("inf"))
                else f" · dte {dte_band[0]:g}-{dte_band[1]:g}")
    voi_note = "" if min_vol_oi <= 0 else f" · vol/OI >= {min_vol_oi:g}"
    oi_note = "" if min_oi <= 0 else f" · OI >= {min_oi:,}"
    call_note = "" if call_side == "any" else f" · calls {call_side}"
    exp_note = "".join(f" · expiry {op} {v}" for op, v in
                       ((">=", expiry_min), ("<=", expiry_max)) if v)
    sig_note = "".join(f" · signal {op} {v}" for op, v in
                       ((">=", signal_min), ("<=", signal_max)) if v)
    gold_note = f" · golden={golden}"
    cp_note = f" · {'/'.join(contract_types).lower()} only" if contract_types else ""
    side_note = f" · side {'/'.join(sides).lower()}" if sides else ""
    type_note = f" · type {'/'.join(trade_types)}" if trade_types else ""
    print(f"session {session} · floor ${premium_floor:,.0f} · "
          f"delta {delta_band[0]}-{delta_band[1]}{dte_note}{voi_note}{oi_note}{call_note}{exp_note}{sig_note}{gold_note}{cp_note}{side_note}{type_note} · "
          f"funds excluded · {len(seen)} already alerted")
    ctx = load_context(session)

    if cold and not backfill:
        # Record the backlog as seen without posting it. Wanting to know about
        # a sweep in real time is not the same as wanting the morning replayed.
        try:
            backlog = poll(session, set(), premium_floor, exclude_tickers,
                           ctx=ctx, **filters, **screen_kw)
        except QuantDataError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1
        if len(backlog):
            append_ledger(session, backlog)
            seen |= set(backlog["id"].astype(str))
            print(f"cold start: {len(backlog)} prints already today, marked seen "
                  f"(--backfill to post them)")

    while True:
        now = pd.Timestamp.now(tz="America/New_York")
        if now.strftime("%Y-%m-%d") != session:
            session = now.strftime("%Y-%m-%d")     # rolled past midnight
            seen, cold = set(load_ledger(session)["id"].astype(str)), True
            print(f"--- new session {session} ---")
            ctx = load_context(session)            # yesterday is history now

        if market_is_open(now):
            try:
                fresh = poll(session, seen, premium_floor, exclude_tickers,
                             ctx=ctx, **filters, **screen_kw)
                for row in fresh.itertuples():
                    text = format_alert(row)
                    print(f"\n{text}\n")
                    if poster:
                        try:
                            poster.post(text)
                        except Exception as exc:  # noqa: BLE001
                            # Never lose the watcher to a posting failure. The
                            # print above is the fallback record.
                            print(f"  slack: not sent ({exc})", file=sys.stderr)
                    seen.add(str(row.id))
                if len(fresh):
                    append_ledger(session, fresh)
            except QuantDataError as exc:
                print(f"  feed error, retrying next poll: {str(exc)[:120]}",
                      file=sys.stderr)
        elif not once:
            print(f"\rmarket closed ({now:%H:%M} ET) — idle", end="", flush=True)

        if once:
            return 0
        time.sleep(interval)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--premium", type=float, default=PREMIUM_FLOOR,
                   help=f"minimum premium (default {PREMIUM_FLOOR:,})")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument("--once", action="store_true", help="single pass, then exit")
    p.add_argument("--dry-run", action="store_true", help="print alerts, post nothing")
    p.add_argument("--backfill", action="store_true",
                   help="on a fresh session, post the prints already on the tape")
    p.add_argument("--exclude", nargs="*", default=[], metavar="TICKER",
                   help="tickers to suppress (e.g. --exclude SNDK)")
    p.add_argument("--session", help="override the session date, YYYY-MM-DD")
    p.add_argument("--delta-min", type=float, default=DELTA_MIN)
    p.add_argument("--delta-max", type=float, default=DELTA_MAX)
    p.add_argument("--dte-min", type=float, default=0.0,
                   help="minimum days to expiry (default: no minimum)")
    p.add_argument("--dte-max", type=float, default=float("inf"),
                   help="maximum days to expiry (default: no maximum)")
    p.add_argument("--min-vol-oi", type=float, default=0.0, metavar="RATIO",
                   help="minimum volume/open-interest; a zero-OI strike always "
                        "passes (default: off)")
    p.add_argument("--min-oi", type=int, default=0, metavar="N",
                   help="minimum open interest, inclusive. Open interest is an "
                        "integer, so 'greater than 1' is --min-oi 2")
    p.add_argument("--call-side", choices=sorted(CALL_SIDES), default="any",
                   help="what a CALL must have done: 'bullish' (ASK or "
                        "ABOVE_ASK) or 'above-ask' (strictly through the "
                        "offer). Puts are never constrained")
    p.add_argument("--expiry-min", metavar="YYYY-MM-DD",
                   help="earliest expiration date, inclusive")
    p.add_argument("--expiry-max", metavar="YYYY-MM-DD",
                   help="latest expiration date, inclusive")
    p.add_argument("--contract", nargs="*", default=[], choices=["CALL", "PUT"],
                   metavar="TYPE", help="keep only CALL or only PUT")
    p.add_argument("--side", nargs="*", default=[], metavar="SIDE",
                   choices=list(D.BUY_SIDES) + list(D.SELL_SIDES) + ["MID_MARKET"],
                   help="keep only these aggressor sides, e.g. --side ABOVE_ASK")
    p.add_argument("--trade-type", nargs="*", default=[], metavar="TYPE",
                   help="keep only these routing types, e.g. --trade-type ISO. "
                        "Composes with --golden rather than replacing it")
    p.add_argument("--golden", choices=sorted(GOLDEN_MODES), default="iso",
                   help="what counts as a golden sweep: 'iso' (routing), "
                        "'badge' (the vendor's isGoldenSweep) or 'both'")
    p.add_argument("--signal-min", metavar="WHEN",
                   help="earliest print time: YYYY-MM-DD or 'YYYY-MM-DD HH:MM', "
                        "Eastern")
    p.add_argument("--signal-max", metavar="WHEN",
                   help="latest print time. A bare date means the end of that day")
    p.add_argument("--lookback", type=int, default=LOOKBACK_SESSIONS,
                   metavar="SESSIONS",
                   help=f"prior sessions used to judge recurrence "
                        f"(default {LOOKBACK_SESSIONS}; 0 disables)")
    p.add_argument("--min-prior", type=int, default=0, metavar="N",
                   help="require N prior same-direction prints on the ticker")
    p.add_argument("--min-conviction", type=float, default=None, metavar="X",
                   help="require aligned conviction >= X, in [-1, 1]")
    args = p.parse_args()

    try:
        return run(premium_floor=args.premium, interval=args.interval,
                   once=args.once, dry_run=args.dry_run, backfill=args.backfill,
                   exclude_tickers=tuple(t.upper() for t in args.exclude),
                   session=args.session,
                   delta_band=(args.delta_min, args.delta_max),
                   dte_band=(args.dte_min, args.dte_max),
                   min_vol_oi=args.min_vol_oi, min_oi=args.min_oi,
                   call_side=args.call_side, expiry_min=args.expiry_min,
                   expiry_max=args.expiry_max, signal_min=args.signal_min,
                   signal_max=args.signal_max,
                   contract_types=tuple(args.contract), sides=tuple(args.side),
                   trade_types=tuple(t.upper() for t in args.trade_type),
                   golden=args.golden,
                   lookback=args.lookback,
                   min_conviction=args.min_conviction, min_prior=args.min_prior)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One place to pull every kind of data the sweep work needs.

    from feeds import sweeps, golden_sweeps, stock_bars, option_bars

Each function returns a tidy DataFrame and documents what it costs, because the
feeds have very different shapes: sweep flow is paginated per session, bars are
one call per contract-session, and Alpaca's lane is a bulk fetch.

Which backend serves what — `data-routing` has two lanes and Quant Data adds a
third, read-only historical one for what neither of the others carries:

| Want | Use | Why |
|---|---|---|
| Option sweep prints | `sweeps()` | only Quant Data has them |
| One option contract's bars | `option_bars()` | only Quant Data has them |
| One ticker, intraday, one session | `stock_bars()` | same unadjusted space as the strikes |
| Many tickers, long history, daily | `alpaca_bars()` | Alpaca is the bulk lane, split-adjusted |
| Earnings dates | `sweep_events.earnings` | Robinhood-sourced parquet |
| FOMC decision days | `sweep_events.fomc` | Fed calendar |

**The adjustment trap.** Quant Data prices are *unadjusted* — the price as it
traded. Alpaca's are *split-adjusted*. A strike is unadjusted, so comparing it to
an Alpaca close across a split is what produced the fictional CRWD +5,368%. Stay
in one space: use `stock_bars()` when the number meets a strike, `alpaca_bars()`
for return series and indicators.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import QuantData, QuantDataError  # noqa: E402

__all__ = ["sweeps", "golden_sweeps", "flagged", "all_prints", "stock_bars",
           "option_bars", "alpaca_bars", "sessions_between", "PERIODS",
           "TRADE_TYPES", "SIDES", "FLAGS"]

PERIODS = ("ONE_MINUTE", "FIVE_MINUTE", "TEN_MINUTE", "FIFTEEN_MINUTE",
           "THIRTY_MINUTE", "ONE_HOUR", "TWO_HOUR", "FOUR_HOUR", "ONE_DAY")

#: `ISO` is a true intermarket sweep. `AUTO` is an ordinary electronic fill.
#: Anything carrying COB / SPRD / TIED is multi-leg, so its direction is
#: ambiguous and it does not belong in a directional screen.
TRADE_TYPES = ("ISO", "AUTO", "CROSS", "CROSS_ISO", "AUCT", "AUCT_ISO", "FLR")

#: Aggressor side. ABOVE_ASK/ASK are buyers lifting; BID/BELOW_BID are sellers
#: hitting. MID_MARKET has no aggressor and therefore no direction.
SIDES = ("ABOVE_ASK", "ASK", "MID_MARKET", "BID", "BELOW_BID")

#: Boolean badges the server will filter on, as `filterExpression` fields. These
#: are the vendor's own classifications and are *not* interchangeable with the
#: ISO screen — see `golden_sweeps()` for how far apart they actually are.
FLAGS = {
    "golden": "IS_GOLDEN_SWEEP",
    "unusual": "IS_UNUSUAL",
    "opening": "IS_OPENING_POSITION",
    "vol_over_oi": "IS_VOLUME_GREATER_THAN_OPEN_INTEREST",
}

_QD: QuantData | None = None


def _qd() -> QuantData:
    global _QD
    if _QD is None:
        _QD = QuantData()
    return _QD


def sessions_between(start: str, end: str) -> list[str]:
    """Weekday sessions in a range. Holidays simply return no rows."""
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


def all_prints(session: str, *, premium_min: float | None = None,
               tickers: list[str] | None = None, consolidated: bool = True,
               **extra) -> pd.DataFrame:
    """Every consolidated option print for a session — no type filter at all.

    The unfiltered feed. ~4,000 prints a session with no premium floor, so pass
    `premium_min` unless you really want all of it. Set `consolidated=False` for
    per-exchange legs (a sweep across N venues becomes N rows).
    """
    args: dict = dict(extra)
    if premium_min is not None:
        args["premiumRange"] = {"min": premium_min}
    if tickers:
        args["tickers"] = tickers
    tool = ("qd_get_order_flow_consolidated" if consolidated
            else "qd_get_order_flow_unconsolidated")
    return _qd().pages(tool, sessionDate=session, **args)


def sweeps(session: str | None = None, *, start: str | None = None,
           end: str | None = None, premium_min: float = 100_000,
           trade_types: list[str] | None = ("ISO",),
           tickers: list[str] | None = None,
           contract_types: list[str] | None = None,
           sides: list[str] | None = None,
           golden: bool | None = None,
           progress: bool = False, **extra) -> pd.DataFrame:
    """Sweep prints, filtered server-side wherever the API supports it.

    Defaults reproduce the screen the strategy uses: consolidation SWEEP, type
    ISO, premium >= $100k. Pass `trade_types=None` to drop the ISO restriction.

    Cost: one paginated call chain per session, ~4 pages at the default floor.
    A month is roughly 80 calls — for anything repeated, cache it with
    `sweep_events/fetch_flow.py` and read the parquet instead.

        sweeps("2026-09-21")                              # one session
        sweeps(start="2026-09-01", end="2026-09-21")      # a range
        sweeps("2026-09-21", premium_min=1e6, golden=True)
        sweeps("2026-09-21", tickers=["NVDA"], contract_types=["CALL"])
    """
    if session is None and not (start and end):
        raise ValueError("pass session=, or both start= and end=")

    args: dict = dict(extra)
    args["tradeConsolidationTypes"] = extra.pop("tradeConsolidationTypes", ["SWEEP"])
    if premium_min is not None:
        args["premiumRange"] = {"min": premium_min}
    if trade_types:
        args["tradeTypes"] = list(trade_types)
    if tickers:
        args["tickers"] = tickers
    if contract_types:
        args["contractTypes"] = contract_types
    if sides:
        args["tradeSideCodes"] = sides
    if golden is not None:
        # isGoldenSweep is not a convenience filter; it goes through the
        # expression form. Booleans travel as strings there.
        args["filterExpression"] = {"field": "IS_GOLDEN_SWEEP", "operation": "EQUALS",
                                    "values": [str(bool(golden)).lower()]}

    days = [session] if session else sessions_between(start, end)
    frames = []
    for day in days:
        try:
            page = _qd().pages("qd_get_order_flow_consolidated", sessionDate=day, **args)
        except QuantDataError as exc:
            if progress:
                print(f"  {day} failed: {str(exc)[:70]}")
            continue
        if len(page):
            page = page.copy()
            page["session"] = day
            frames.append(page)
        if progress:
            print(f"  {day}  {len(page)} prints")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def golden_sweeps(session: str | None = None, *, premium_min: float = 1_000_000,
                  **kwargs) -> pd.DataFrame:
    """Sweeps the vendor itself flags `isGoldenSweep`, at a $1M floor by default.

    **This is a different population from "ISO + SWEEP", not a subset of it.**
    On 2026-09-21 at a $1M floor:

        ISO + SWEEP        13 prints
        isGoldenSweep     121 prints
        in both             9

    Of the 112 golden-only prints, **70 are `AUTO`** — ordinary electronic fills,
    not intermarket sweeps — and 42 are `MULTI_*_COB`, which are multi-leg and so
    directionally ambiguous. Four ISO sweeps do not carry the flag at all.

    So the vendor's badge is a loose "unusual activity" marker, while `ISO` is a
    statement about how the order routed. The strategy's premise — that the print
    swept multiple exchanges — is the ISO condition, and keeping `trade_types` at
    its ISO default is what enforces it. Passing `trade_types=None` here gives the
    vendor's badge alone, which will pull in AUTO and multi-leg flow.
    """
    return sweeps(session, premium_min=premium_min, golden=True, **kwargs)


def flagged(flag: str, session: str, *, premium_min: float = 100_000,
            tickers: list[str] | None = None,
            trade_types: list[str] | None = ("ISO",),
            consolidation: list[str] | None = ("SWEEP",),
            **extra) -> pd.DataFrame:
    """Every print carrying one of the vendor's boolean badges.

    `flag` is a key of `FLAGS`. Filtering happens server-side, which matters:
    the unfiltered feed pages out at 20,000 rows, so counting a badge by pulling
    everything and filtering locally silently truncates and undercounts.

        flagged("unusual", "2026-09-22")
        flagged("opening", "2026-09-22", premium_min=500_000)

    **Defaults to ISO + SWEEP**, like `sweeps()`, because a badge on its own
    says nothing about how the order routed. Pass `trade_types=None` for the
    raw badge population — but know what that is: on 2026-09-22 only 2.9% of
    `isUnusual` prints and 2.6% of `isOpeningPosition` prints were ISO sweeps.
    The rest are `MULTI_*_COB` multi-leg and `AUTO` electronic fills.

    Restricting a badge to ISO makes it a **subset of `sweeps()`**, not a
    separate population: all 110 ISO-sweep `isUnusual` prints and all 238
    `isOpeningPosition` ones that session were already in the ISO sweep feed.
    The badges are attributes of a print, not different kinds of flow.

    Note `golden` returns the same rows at any floor below $1M, because the
    badge already implies roughly that size — its smallest print on 2026-09-22
    was $1,000,273. That is not the filter being ignored.
    """
    if flag not in FLAGS:
        raise ValueError(f"unknown flag {flag!r}; expected one of {sorted(FLAGS)}")
    args: dict = dict(extra)
    if premium_min is not None:
        args["premiumRange"] = {"min": premium_min}
    if tickers:
        args["tickers"] = tickers
    if trade_types:
        args["tradeTypes"] = list(trade_types)
    if consolidation:
        args["tradeConsolidationTypes"] = list(consolidation)
    args["filterExpression"] = {"field": FLAGS[flag], "operation": "EQUALS",
                                "values": ["true"]}
    frame = _qd().pages("qd_get_order_flow_consolidated", sessionDate=session, **args)
    if len(frame):
        frame = frame.copy()
        frame["session"] = session
    return frame


def stock_bars(ticker: str, session: str, period: str = "ONE_HOUR") -> pd.DataFrame:
    """Underlying OHLC for one ticker on one session, **unadjusted**.

    One call. Use this when the price has to be comparable to a strike.
    """
    frame, _ = _qd().frame("qd_get_stock_price_over_time", ticker=ticker,
                           sessionDate=session, aggregationPeriod=period)
    return frame


def option_bars(ticker: str, expiry: str, strike: float, right: str,
                session: str, period: str = "ONE_MINUTE") -> pd.DataFrame:
    """OHLC + volume for ONE option contract on ONE session.

    `right` is "CALL" or "PUT"; `expiry` is YYYY-MM-DD. One call per
    contract-session, so a multi-day hold is one call per day.

    Returns empty when the contract did not trade — which is information, not an
    error. Bars only exist where trades happened, so their density *is* a
    liquidity measure, and a contract that was never listed (a third Friday that
    fell on a holiday, say) looks identical to one that simply went quiet.
    """
    try:
        frame, _ = _qd().frame("qd_get_option_price_over_time", ticker=ticker,
                               expirationDate=expiry, strikePrice=float(strike),
                               contractType=right, sessionDate=session,
                               aggregationPeriod=period)
        return frame
    except QuantDataError:
        return pd.DataFrame()


def alpaca_bars(symbols, start: str, end: str | None = None,
                timeframe: str = "1Day", **kwargs):
    """Bulk historical bars from Alpaca — the repo's historical lane.

    Split-**adjusted**, cached on disk, many symbols and years in one call. Use
    it for return series, indicators and anything spanning a corporate action.
    Do not use it where the price meets a strike — see the module docstring.

    Returns a `BarPanel`; `.frame` is the DataFrame.
    """
    from backtest import load_bars

    return load_bars(symbols, start, end, timeframe=timeframe, **kwargs)

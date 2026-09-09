"""Market data loading: Alpaca REST -> pandas, with an on-disk cache.

Every price series used by a backtest enters the process through this module.
There is no yfinance/CSV-from-the-internet path, by design.

Conventions
-----------
* Index is **timezone-aware ``America/New_York``**. Alpaca serves UTC; daily bars
  arrive stamped ``05:00Z`` (= midnight ET), so ET is the natural presentation.
* Bars are **split- and dividend-adjusted** (``adjustment="all"``) by default,
  which is what you want for a return series.
* The cache stores only *settled* bars — anything from the current session is
  returned to the caller but never written to disk, so a partial day cannot
  poison a later run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from backtest.client import AlpacaClient
from backtest.config import Settings, get_settings

log = logging.getLogger(__name__)

MARKET_TZ = "America/New_York"
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]

#: Alpaca's short field names -> ours.
_FIELD_MAP = {
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "n": "trade_count",
    "vw": "vwap",
    "t": "timestamp",
}

_TIMEFRAME_RE = re.compile(r"^(\d+)\s*(min|minute|hour|day|week|month)s?$", re.IGNORECASE)

#: US equities trade ~252 sessions of 6.5 hours a year.
TRADING_DAYS_PER_YEAR = 252
TRADING_MINUTES_PER_DAY = 390

#: Alpaca's equity history begins here. Earlier dates return an empty response
#: rather than an error, which is easy to mistake for a bad symbol.
ALPACA_HISTORY_START = date(2016, 1, 4)

#: Intraday bar requests come back covering the *whole* extended day, 04:00-20:00
#: ET, not just the regular session — 865 one-minute bars a day rather than 390.
#: Pre- and post-market bars are extremely thin (a median of a few hundred shares
#: against ~50k in the regular session), so including them by accident both
#: wrecks annualisation and invites the engine to "fill" orders where there is no
#: real liquidity. Hence the explicit session filter, defaulting to `regular`.
SESSIONS: dict[str, tuple[time, time]] = {
    "regular": (time(9, 30), time(16, 0)),
    "extended": (time(4, 0), time(20, 0)),
}
SESSION_MINUTES = {"regular": 390, "extended": 960}


class DataError(RuntimeError):
    """Raised when Alpaca returns no usable data for a request."""


@dataclass(frozen=True)
class TimeFrame:
    """A parsed Alpaca timeframe, e.g. ``1Day``, ``15Min``, ``1Hour``.

    ``session_minutes`` is how many minutes of the trading day the bars actually
    cover — 390 for the regular session, 960 once extended hours are included. It
    only affects intraday annualisation, but it affects it by a factor of 2.5.
    """

    amount: int
    unit: str  # min | hour | day | week | month
    session_minutes: int = TRADING_MINUTES_PER_DAY

    @classmethod
    def parse(cls, text: str, session_minutes: int = TRADING_MINUTES_PER_DAY) -> "TimeFrame":
        match = _TIMEFRAME_RE.match(text.strip())
        if not match:
            raise ValueError(
                f"Unrecognised timeframe {text!r}. Use forms like '1Day', '1Hour', '15Min'."
            )
        amount, unit = int(match.group(1)), match.group(2).lower()
        unit = {"minute": "min"}.get(unit, unit)
        return cls(amount, unit, session_minutes)

    #: Our lowercase unit -> the capitalisation Alpaca's API expects.
    _ALPACA_UNITS = {
        "min": "Min",
        "hour": "Hour",
        "day": "Day",
        "week": "Week",
        "month": "Month",
    }

    @property
    def alpaca(self) -> str:
        """The string Alpaca's API expects, e.g. ``15Min``."""
        return f"{self.amount}{self._ALPACA_UNITS[self.unit]}"

    @property
    def is_intraday(self) -> bool:
        return self.unit in ("min", "hour")

    @property
    def minutes(self) -> int:
        """How many minutes one bar spans. Only meaningful intraday."""
        return self.amount * (60 if self.unit == "hour" else 1)

    @property
    def periods_per_year(self) -> float:
        """Bars per year — the annualisation factor for Sharpe and vol."""
        if self.unit == "min":
            return TRADING_DAYS_PER_YEAR * self.session_minutes / self.amount
        if self.unit == "hour":
            return TRADING_DAYS_PER_YEAR * (self.session_minutes / 60) / self.amount
        if self.unit == "day":
            return TRADING_DAYS_PER_YEAR / self.amount
        if self.unit == "week":
            return 52 / self.amount
        return 12 / self.amount

    def __str__(self) -> str:
        return self.alpaca


class BarPanel:
    """OHLCV bars for one or more symbols on a shared timeline.

    Internally a long ``DataFrame`` indexed by ``(timestamp, symbol)``. The wide
    accessors (:attr:`close`, :attr:`open`, ...) return ``timestamp x symbol``
    frames, which is what strategies usually want.
    """

    def __init__(self, frame: pd.DataFrame, timeframe: TimeFrame, feed: str = "") -> None:
        if not isinstance(frame.index, pd.MultiIndex):
            raise TypeError("BarPanel expects a (timestamp, symbol) MultiIndex")
        self.frame = frame.sort_index()
        self.timeframe = timeframe
        self.feed = feed

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_alpaca(
        cls, payload: dict[str, list[dict[str, Any]]], timeframe: TimeFrame, feed: str = ""
    ) -> "BarPanel":
        rows = []
        for symbol, bars in payload.items():
            for bar in bars:
                row = {_FIELD_MAP[k]: v for k, v in bar.items() if k in _FIELD_MAP}
                row["symbol"] = symbol
                rows.append(row)
        if not rows:
            return cls(_empty_frame(), timeframe, feed)

        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
            MARKET_TZ
        )
        # `n` (trade count) and `vw` (VWAP) are optional in Alpaca's response, and
        # absent entirely on some feeds. NaN keeps the frame a clean float64.
        for column in BAR_COLUMNS:
            if column not in frame:
                frame[column] = np.nan
        frame = frame.set_index(["timestamp", "symbol"])[BAR_COLUMNS]
        return cls(frame.astype("float64"), timeframe, feed)

    # -- accessors ------------------------------------------------------------

    @property
    def symbols(self) -> list[str]:
        return sorted(self.frame.index.get_level_values("symbol").unique())

    @property
    def timestamps(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(
            self.frame.index.get_level_values("timestamp").unique()
        ).sort_values()

    def field(self, name: str) -> pd.DataFrame:
        """A ``timestamp x symbol`` frame for one OHLCV field."""
        if name not in BAR_COLUMNS:
            raise KeyError(f"{name!r} is not one of {BAR_COLUMNS}")
        return self.frame[name].unstack("symbol").sort_index()

    open = property(lambda self: self.field("open"))
    high = property(lambda self: self.field("high"))
    low = property(lambda self: self.field("low"))
    close = property(lambda self: self.field("close"))
    volume = property(lambda self: self.field("volume"))
    vwap = property(lambda self: self.field("vwap"))

    def returns(self, field: str = "close") -> pd.DataFrame:
        """Simple bar-over-bar returns."""
        return self.field(field).pct_change()

    def bar_at(self, timestamp: pd.Timestamp) -> pd.DataFrame:
        """All symbols' bars for a single timestamp, indexed by symbol."""
        return self.frame.xs(timestamp, level="timestamp")

    def head_until(self, timestamp: pd.Timestamp) -> "BarPanel":
        """A copy containing only bars at or before ``timestamp``."""
        mask = self.frame.index.get_level_values("timestamp") <= timestamp
        return BarPanel(self.frame[mask], self.timeframe, self.feed)

    def dropna_symbols(self, min_coverage: float = 0.9) -> "BarPanel":
        """Drop symbols whose close series is mostly missing.

        Useful when a universe mixes recent IPOs with long histories.
        """
        closes = self.close
        keep = closes.columns[closes.notna().mean() >= min_coverage]
        mask = self.frame.index.get_level_values("symbol").isin(keep)
        return BarPanel(self.frame[mask], self.timeframe, self.feed)

    def __len__(self) -> int:
        return len(self.timestamps)

    def __repr__(self) -> str:
        stamps = self.timestamps
        span = f"{stamps[0].date()} -> {stamps[-1].date()}" if len(stamps) else "empty"
        return (
            f"BarPanel({len(self.symbols)} symbols, {len(stamps)} bars, "
            f"{self.timeframe}, {span}, feed={self.feed or 'n/a'})"
        )


def _empty_frame() -> pd.DataFrame:
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex([], tz=MARKET_TZ), pd.Index([], dtype=object)],
        names=["timestamp", "symbol"],
    )
    return pd.DataFrame(columns=BAR_COLUMNS, index=index, dtype="float64")


# -- date helpers -------------------------------------------------------------


def _to_date(value: str | date | datetime | pd.Timestamp) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _last_settled_date() -> date:
    """The most recent date whose bars are safe to cache.

    Yesterday, in market time. Today's bar is still forming (or the session has
    not closed), so it is never written to disk.
    """
    return (pd.Timestamp.now(tz=MARKET_TZ) - timedelta(days=1)).date()


# -- disk cache ---------------------------------------------------------------


class BarCache:
    """Per-symbol CSV cache with a recorded coverage window.

    Layout::

        data/alpaca_cache/stocks/1Day/sip_all/AAPL.csv.gz
        data/alpaca_cache/stocks/1Day/sip_all/AAPL.meta.json

    The sidecar records the contiguous ``[start, end]`` the file is known to
    cover, so an empty stretch (a symbol that had not listed yet) is not
    mistaken for a cache miss.
    """

    def __init__(self, root: Path, asset_class: str, timeframe: TimeFrame, variant: str) -> None:
        self.dir = root / asset_class / timeframe.alpaca / variant
        self.dir.mkdir(parents=True, exist_ok=True)

    def _paths(self, symbol: str) -> tuple[Path, Path]:
        safe = symbol.replace("/", "-")
        return self.dir / f"{safe}.csv.gz", self.dir / f"{safe}.meta.json"

    def read(self, symbol: str) -> tuple[pd.DataFrame | None, tuple[date, date] | None]:
        data_path, meta_path = self._paths(symbol)
        if not (data_path.is_file() and meta_path.is_file()):
            return None, None
        try:
            meta = json.loads(meta_path.read_text())
            covered = (_to_date(meta["start"]), _to_date(meta["end"]))
            frame = pd.read_csv(data_path, parse_dates=["timestamp"])
        except (ValueError, KeyError, OSError) as exc:
            log.warning("Ignoring unreadable cache for %s: %s", symbol, exc)
            return None, None
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
            MARKET_TZ
        )
        return frame.set_index("timestamp").sort_index(), covered

    def write(self, symbol: str, frame: pd.DataFrame, covered: tuple[date, date]) -> None:
        if covered[1] < covered[0]:
            return
        data_path, meta_path = self._paths(symbol)
        out = frame.copy()
        out.index = out.index.tz_convert("UTC")
        out.to_csv(data_path, index_label="timestamp")
        meta_path.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "start": covered[0].isoformat(),
                    "end": covered[1].isoformat(),
                    "rows": int(len(out)),
                    "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                indent=2,
            )
        )


# -- public loaders -----------------------------------------------------------


def load_bars(
    symbols: str | Sequence[str],
    start: str | date,
    end: str | date | None = None,
    timeframe: str | TimeFrame = "1Day",
    *,
    session: str = "regular",
    adjustment: str = "all",
    feed: str | None = None,
    asset_class: str = "stocks",
    use_cache: bool = True,
    client: AlpacaClient | None = None,
    settings: Settings | None = None,
) -> BarPanel:
    """Load OHLCV bars from Alpaca, transparently cached on disk.

    Parameters
    ----------
    symbols:
        One symbol or a list. Crypto symbols look like ``"BTC/USD"`` and require
        ``asset_class="crypto"``.
    start, end:
        Inclusive date bounds (``"YYYY-MM-DD"`` or a ``date``). ``end`` defaults
        to today.
    timeframe:
        ``"1Day"`` (default), ``"1Hour"``, ``"15Min"``, ``"1Week"``, ...
    session:
        Intraday only; ignored for daily bars and crypto. ``"regular"`` (default)
        keeps 09:30-16:00 ET. ``"extended"`` keeps everything Alpaca returns,
        04:00-20:00 ET — useful for studying the open and close auctions, but
        those bars are thin enough that fills modelled in them are fiction.
    adjustment:
        ``"all"`` (splits + dividends, the default and the right choice for
        return series), ``"split"``, ``"dividend"``, or ``"raw"``.
    feed:
        Overrides the configured feed (``sip`` by default; ``iex`` is the free
        fallback). Ignored for crypto.
    use_cache:
        Set ``False`` to force a fresh pull and refresh the cache.

    Returns
    -------
    BarPanel
    """
    settings = settings or get_settings()
    if session not in SESSIONS:
        raise ValueError(f"session must be one of {sorted(SESSIONS)}, got {session!r}")
    if isinstance(timeframe, TimeFrame):
        timeframe = replace(timeframe, session_minutes=SESSION_MINUTES[session])
    else:
        timeframe = TimeFrame.parse(str(timeframe), SESSION_MINUTES[session])
    symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)
    symbol_list = sorted({s.strip().upper() for s in symbol_list if s.strip()})
    if not symbol_list:
        raise ValueError("No symbols given")

    start_d = _to_date(start)
    end_d = _to_date(end) if end is not None else pd.Timestamp.now(tz=MARKET_TZ).date()
    if end_d < start_d:
        raise ValueError(f"end ({end_d}) is before start ({start_d})")
    if asset_class == "stocks" and start_d < ALPACA_HISTORY_START:
        log.warning(
            "Requested start %s predates Alpaca's equity history (%s); the earlier "
            "portion will come back empty.",
            start_d,
            ALPACA_HISTORY_START,
        )

    is_crypto = asset_class == "crypto"
    resolved_feed = "" if is_crypto else (feed or settings.stock_feed)
    variant = "crypto" if is_crypto else f"{resolved_feed}_{adjustment}"
    cache = BarCache(settings.cache_dir, asset_class, timeframe, variant) if use_cache else None

    owns_client = client is None
    client = client or AlpacaClient(settings)
    try:
        per_symbol: dict[str, pd.DataFrame] = {}
        to_fetch: dict[str, tuple[date, date]] = {}

        for symbol in symbol_list:
            cached, covered = cache.read(symbol) if cache else (None, None)
            if cached is not None and covered is not None:
                per_symbol[symbol] = cached
                missing = _missing_range((start_d, end_d), covered)
                if missing:
                    to_fetch[symbol] = missing
            else:
                to_fetch[symbol] = (start_d, end_d)

        # Group symbols that need the identical window into one multi-symbol call.
        for window, batch in _group_by_window(to_fetch).items():
            fetched = _fetch(
                client, batch, timeframe, window[0], window[1], adjustment, resolved_feed, is_crypto
            )
            panel = BarPanel.from_alpaca(fetched, timeframe, resolved_feed)
            for symbol in batch:
                new_rows = _symbol_frame(panel, symbol)
                merged = _merge_frames(per_symbol.get(symbol), new_rows)
                per_symbol[symbol] = merged
                if cache is not None:
                    _, old_covered = cache.read(symbol)
                    covered = _union_range(old_covered, window)
                    settle = _last_settled_date()
                    covered = (covered[0], min(covered[1], settle))
                    cache.write(
                        symbol,
                        merged[merged.index.date <= covered[1]] if len(merged) else merged,
                        covered,
                    )
    finally:
        if owns_client:
            client.close()

    frame = _stack(per_symbol, start_d, end_d)
    if timeframe.is_intraday and not is_crypto:
        frame = _filter_session(frame, session, timeframe)
    panel = BarPanel(frame, timeframe, resolved_feed)
    if len(panel) == 0:
        raise DataError(
            f"Alpaca returned no bars for {symbol_list} between {start_d} and {end_d} "
            f"({timeframe}, feed={resolved_feed or 'crypto'}). Check the symbols, the "
            "date range, and that your account can access this feed."
        )
    return panel


def _fetch(
    client: AlpacaClient,
    symbols: list[str],
    timeframe: TimeFrame,
    start: date,
    end: date,
    adjustment: str,
    feed: str,
    is_crypto: bool,
) -> dict[str, list[dict[str, Any]]]:
    # Alpaca's `end` is exclusive at the instant level; pushing to the next day's
    # midnight makes the requested end date inclusive, which is what callers expect.
    end_param = (end + timedelta(days=1)).isoformat()
    log.info(
        "Fetching %s %s bars for %s: %s -> %s",
        timeframe,
        "crypto" if is_crypto else feed,
        ", ".join(symbols[:6]) + (" ..." if len(symbols) > 6 else ""),
        start,
        end,
    )
    if is_crypto:
        return client.crypto_bars(symbols, timeframe.alpaca, start.isoformat(), end_param)
    return client.stock_bars(
        symbols, timeframe.alpaca, start.isoformat(), end_param, adjustment=adjustment, feed=feed
    )


def _symbol_frame(panel: BarPanel, symbol: str) -> pd.DataFrame:
    if symbol not in panel.frame.index.get_level_values("symbol"):
        return pd.DataFrame(columns=BAR_COLUMNS, index=pd.DatetimeIndex([], tz=MARKET_TZ, name="timestamp"))
    return panel.frame.xs(symbol, level="symbol").sort_index()


def _merge_frames(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    if old is None or old.empty:
        return new
    if new.empty:
        return old
    # New bars win on collision — they may carry a corrected adjustment factor.
    combined = pd.concat([old[~old.index.isin(new.index)], new])
    return combined.sort_index()


def _missing_range(
    wanted: tuple[date, date], covered: tuple[date, date]
) -> tuple[date, date] | None:
    """The window still to fetch, or ``None`` if the cache already covers it.

    A gap on both sides means the cache is an island inside the request; we then
    refetch the whole span rather than tracking disjoint intervals.
    """
    want_start, want_end = wanted
    have_start, have_end = covered
    if want_start >= have_start and want_end <= have_end:
        return None
    if want_end < have_start or want_start > have_end:
        return wanted  # no overlap at all
    if want_start < have_start and want_end > have_end:
        return wanted  # holes on both sides
    if want_start < have_start:
        return (want_start, have_start - timedelta(days=1))
    return (have_end + timedelta(days=1), want_end)


def _union_range(
    old: tuple[date, date] | None, new: tuple[date, date]
) -> tuple[date, date]:
    if old is None:
        return new
    # Only extend across a contiguous join; otherwise trust the newer window.
    if new[0] > old[1] + timedelta(days=1) or new[1] < old[0] - timedelta(days=1):
        return new
    return (min(old[0], new[0]), max(old[1], new[1]))


def _group_by_window(
    to_fetch: dict[str, tuple[date, date]]
) -> dict[tuple[date, date], list[str]]:
    grouped: dict[tuple[date, date], list[str]] = {}
    for symbol, window in to_fetch.items():
        grouped.setdefault(window, []).append(symbol)
    return grouped


def _stack(per_symbol: dict[str, pd.DataFrame], start: date, end: date) -> pd.DataFrame:
    pieces = []
    for symbol, frame in per_symbol.items():
        if frame.empty:
            continue
        window = frame[(frame.index.date >= start) & (frame.index.date <= end)]
        if window.empty:
            continue
        window = window.copy()
        window["symbol"] = symbol
        pieces.append(window.set_index("symbol", append=True))
    if not pieces:
        return _empty_frame()
    out = pd.concat(pieces)
    out.index.names = ["timestamp", "symbol"]
    return out[BAR_COLUMNS].sort_index()


def _filter_session(frame: pd.DataFrame, session: str, timeframe: TimeFrame) -> pd.DataFrame:
    """Keep bars whose *interval* overlaps ``session``, in market time.

    Overlap rather than start-time membership, because a bar is labelled by when
    it opens but covers the span after it. Minute-based timeframes tile a 6.5-hour
    session exactly, so the two rules agree; hourly bars do not, and there the
    difference matters — the bar stamped 09:00 carries the 09:30 open and several
    million shares. Filtering on start time alone silently discards it.

    The cache stores the unfiltered superset, so switching session never triggers
    a refetch.
    """
    opens, closes = SESSIONS[session]
    stamps = frame.index.get_level_values("timestamp")
    start_minutes = stamps.hour * 60 + stamps.minute
    end_minutes = start_minutes + timeframe.minutes
    session_open = opens.hour * 60 + opens.minute
    session_close = closes.hour * 60 + closes.minute
    keep = (start_minutes < session_close) & (end_minutes > session_open)
    return frame[keep]


def load_calendar(
    start: str | date,
    end: str | date,
    *,
    client: AlpacaClient | None = None,
    settings: Settings | None = None,
) -> pd.DataFrame:
    """Alpaca's official market calendar, indexed by session date.

    Columns: ``open``, ``close``, ``session_open``, ``session_close``. Use it to
    confirm a backtest's bar count matches the real number of sessions, or to
    detect half days.
    """
    settings = settings or get_settings()
    owns_client = client is None
    client = client or AlpacaClient(settings)
    try:
        rows = client.calendar(_to_date(start).isoformat(), _to_date(end).isoformat())
    finally:
        if owns_client:
            client.close()
    if not rows:
        return pd.DataFrame(columns=["open", "close", "session_open", "session_close"])
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame.set_index("date").sort_index()


def load_universe(
    *,
    exchanges: Iterable[str] = ("NASDAQ", "NYSE", "ARCA"),
    tradable_only: bool = True,
    shortable_only: bool = False,
    client: AlpacaClient | None = None,
    settings: Settings | None = None,
) -> pd.DataFrame:
    """The tradable US equity asset master, from Alpaca.

    Handy for screening a universe before pulling bars for it. Note this is
    *today's* asset list — using it to pick symbols for a historical backtest
    introduces survivorship bias (see the README).
    """
    settings = settings or get_settings()
    owns_client = client is None
    client = client or AlpacaClient(settings)
    try:
        rows = client.assets()
    finally:
        if owns_client:
            client.close()
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    wanted = {e.upper() for e in exchanges}
    frame = frame[frame["exchange"].str.upper().isin(wanted)]
    if tradable_only:
        frame = frame[frame["tradable"]]
    if shortable_only:
        frame = frame[frame["shortable"]]
    return frame.set_index("symbol").sort_index()

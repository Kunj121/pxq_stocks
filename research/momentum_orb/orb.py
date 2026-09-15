"""5-minute Opening Range Breakout on Stocks in Play — Zarattini, Barbon & Aziz (2024).

Replication engine. See momentum_orb.md for the strategy and its data requirements.

The design turns on one observation: a trade's outcome **in units of R** — entry
price, stop level, exit price, R multiple — is fully determined by the opening
range and ATR(14). It does not depend on position size at all. So the simulation
splits cleanly in two:

    Stage 1  per symbol, streaming   ->  one row per symbol-day: what the trade
                                          would have done, in R
    Stage 2  across the whole panel  ->  filters, top-N selection, position
                                          sizing, leverage cap, commissions,
                                          compounding equity

Stage 1 never holds more than one symbol's minute bars in memory, which is what
makes 70M bars tractable. Stage 2 operates on ~200k rows.

Every lookback is causal: ATR, average volume, and the Relative Volume
denominator all use days strictly before the trade date.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

MARKET_TZ = "America/New_York"
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ORBConfig:
    """Strategy parameters. Defaults are the paper's headline configuration."""

    opening_minutes: int = 5
    lookback: int = 14

    # entry filters
    min_price: float = 5.0
    min_avg_volume: float = 1_000_000.0
    min_atr: float = 0.50
    min_rel_volume: float | None = 1.0   # None disables -> Base Strategy
    top_n: int | None = 20               # None disables -> trade every qualifier

    # risk
    #
    # `stop_atr_frac` alone reproduces the paper (10% of ATR) and is the default,
    # so every replication result stays comparable. The live runner overrides it
    # with a FLEXIBLE band — see `stop_distance`.
    stop_atr_frac: float = 0.10
    #: Upper bound when widening. None = fixed width, the paper's rule.
    stop_atr_frac_max: float | None = None
    #: Dollar floor below which a stop is bid-ask noise rather than a level.
    #: A sweep over 29,370 trades favours tighter stops monotonically down to 5%,
    #: but a 5% stop is a median 9 cents — at or inside the spread on many names,
    #: where 1-minute bar lows cannot see the bounce that would trigger it. So
    #: take the tighter end and widen only where the dollars get too thin.
    min_stop_dollars: float = 0.0
    risk_per_trade: float = 0.01
    max_leverage: float = 4.0

    # accounting
    initial_capital: float = 25_000.0
    commission_per_share: float = 0.0035

    # How to treat a stop level that lies inside the bar that triggered entry.
    #
    #   "certain"     (default) the stop counts on the entry bar only when it is
    #                 *provable* from OHLC: either the fill was at the bar's open
    #                 (so the whole bar is post-entry), or the bar closes beyond
    #                 the stop (so the level was crossed after entry and held).
    #                 Otherwise the scan starts on the next bar.
    #   "pessimistic" the ambiguous set is all charged as stopped. Lower bound.
    #                 Excludes excursions that are *provably* pre-entry, so this
    #                 is a legitimate bound rather than a strawman.
    #   "optimistic"  the ambiguous set all survives. Upper bound. Identical to
    #                 "certain" by construction — kept as an explicit name so the
    #                 bracket reads as a bracket.
    same_bar_policy: str = "certain"

    def stop_distance(self, atr):
        """Dollar stop distance for a given ATR (scalar or array).

        Fixed at `stop_atr_frac` unless a band is configured, in which case the
        stop widens toward `stop_atr_frac_max` only where the base width falls
        below `min_stop_dollars`.
        """
        import numpy as _np
        base = self.stop_atr_frac * atr
        if self.stop_atr_frac_max is None or self.min_stop_dollars <= 0:
            return base
        ceiling = self.stop_atr_frac_max * atr
        return _np.minimum(ceiling, _np.maximum(base, self.min_stop_dollars))

    @property
    def label(self) -> str:
        bits = [f"{self.opening_minutes}m-ORB"]
        if self.min_rel_volume is not None:
            bits.append(f"RelVol>={self.min_rel_volume:g}")
        if self.top_n:
            bits.append(f"top{self.top_n}")
        return " ".join(bits)


# --------------------------------------------------------------------------- #
# daily-frequency inputs: ATR(14) and average volume
# --------------------------------------------------------------------------- #

def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 14) -> pd.Series:
    """Wilder's ATR — the definition the paper cites (Wilder, 1978).

    Not a simple rolling mean: Wilder smooths with alpha = 1/period. Using a
    plain mean gives a noticeably different, jumpier ATR and therefore a
    different stop distance on every trade.
    """
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder smoothing == EWM with alpha = 1/period, adjust=False.
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def daily_features(daily: pd.DataFrame, cfg: ORBConfig) -> pd.DataFrame:
    """Per symbol-day ATR(14) and average volume, both shifted one day.

    Shifting is the whole point: on the morning of day t you know day t-1's
    close but not day t's. An unshifted ATR would leak the trade day's own range
    into the stop that governs that trade.
    """
    out = []
    for symbol, grp in daily.groupby(level="symbol"):
        g = grp.droplevel("symbol").sort_index()
        atr = wilder_atr(g["high"], g["low"], g["close"], cfg.lookback)
        avg_vol = g["volume"].rolling(cfg.lookback, min_periods=cfg.lookback).mean()
        out.append(pd.DataFrame({
            "symbol": symbol,
            "date": g.index.date,
            # shift(1) -> as known at the open of the next session
            "atr": atr.shift(1).to_numpy(),
            "avg_volume": avg_vol.shift(1).to_numpy(),
            "prev_close": g["close"].shift(1).to_numpy(),
        }))
    frame = pd.concat(out, ignore_index=True)
    return frame.set_index(["date", "symbol"]).sort_index()


# --------------------------------------------------------------------------- #
# stage 1 — per-symbol intraday simulation
# --------------------------------------------------------------------------- #

def _session_bounds(cfg: ORBConfig):
    open_min = SESSION_OPEN[0] * 60 + SESSION_OPEN[1]
    return open_min, open_min + cfg.opening_minutes


def simulate_symbol(minutes: pd.DataFrame, atr_by_date: pd.Series,
                    cfg: ORBConfig) -> pd.DataFrame:
    """Every day's ORB trade for one symbol, in R. No position sizing here.

    `minutes` is a tz-aware 1-minute OHLCV frame for a single symbol.
    `atr_by_date` maps date -> ATR(14) as known at that day's open.
    """
    if minutes.empty:
        return pd.DataFrame()

    idx = minutes.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert(MARKET_TZ)
    minutes = minutes.copy()
    minutes.index = idx

    minute_of_day = idx.hour * 60 + idx.minute
    dates = idx.date
    or_start, or_end = _session_bounds(cfg)

    o = minutes["open"].to_numpy(float)
    h = minutes["high"].to_numpy(float)
    l = minutes["low"].to_numpy(float)
    c = minutes["close"].to_numpy(float)
    v = minutes["volume"].to_numpy(float)
    mod = minute_of_day.to_numpy()

    rows = []
    # Contiguous day blocks — the index is sorted, so boundaries are enough.
    day_change = np.flatnonzero(np.r_[True, dates[1:] != dates[:-1]])
    day_starts = day_change
    day_ends = np.r_[day_change[1:], len(dates)]

    for s, e in zip(day_starts, day_ends):
        day = dates[s]
        m = mod[s:e]

        in_or = (m >= or_start) & (m < or_end)
        after_or = (m >= or_end) & (m < SESSION_CLOSE[0] * 60)
        if not in_or.any() or not after_or.any():
            continue
        # Require the opening range to actually start at 09:30. A day whose
        # first print is 09:33 has no comparable opening range.
        if m[in_or][0] != or_start:
            continue

        oi = np.flatnonzero(in_or) + s
        ai = np.flatnonzero(after_or) + s

        or_open, or_close = o[oi[0]], c[oi[-1]]
        or_high, or_low = h[oi].max(), l[oi].min()
        or_volume = v[oi].sum()

        atr = atr_by_date.get(day, np.nan)
        row = {
            "date": day, "or_open": or_open, "or_high": or_high,
            "or_low": or_low, "or_close": or_close, "or_volume": or_volume,
            "atr": atr, "day_open": or_open,
            "eod_close": c[ai[-1]],
            "direction": 0, "entry_price": np.nan, "stop_price": np.nan,
            "exit_price": np.nan, "exit_reason": "no_signal",
            "risk_per_share": np.nan, "pnl_per_share": np.nan, "pnl_r": np.nan,
            "entry_minute": np.nan, "same_bar_stop": False,
            "entry_bar_ambiguous": False,
        }

        direction = 1 if or_close > or_open else (-1 if or_close < or_open else 0)
        row["direction"] = direction
        if direction == 0:
            row["exit_reason"] = "doji"
            rows.append(row); continue
        if not np.isfinite(atr) or atr <= 0:
            row["exit_reason"] = "no_atr"
            rows.append(row); continue

        trigger = or_high if direction == 1 else or_low
        dh, dl, dc, do = h[ai], l[ai], c[ai], o[ai]

        # Entry: first bar whose range reaches the resting stop order.
        hit = np.flatnonzero(dh >= trigger) if direction == 1 else np.flatnonzero(dl <= trigger)
        if hit.size == 0:
            row["exit_reason"] = "never_triggered"
            rows.append(row); continue
        k = hit[0]

        # A bar that opens beyond the trigger fills at the open, not the
        # trigger — the stop order becomes marketable on the gap.
        if direction == 1:
            entry = max(trigger, do[k])
        else:
            entry = min(trigger, do[k])

        risk = float(cfg.stop_distance(atr))
        stop = entry - direction * risk

        # Bars strictly after the entry bar: unambiguously post-entry.
        if direction == 1:
            later = np.flatnonzero(dl[k + 1:] <= stop)
        else:
            later = np.flatnonzero(dh[k + 1:] >= stop)

        # The entry bar needs care. A long is filled when the bar trades up
        # through the range high; the bar's LOW may well have printed earlier,
        # before the order existed. Charging that low against the position is
        # not conservatism, it is counting price action that preceded the fill.
        if direction == 1:
            excursion_hit = dl[k] <= stop
            close_beyond = dc[k] <= stop
            opened_beyond = do[k] <= stop
        else:
            excursion_hit = dh[k] >= stop
            close_beyond = dc[k] >= stop
            opened_beyond = do[k] >= stop
        gap_fill = entry == do[k]   # filled at the open => whole bar is post-entry

        # Sort the entry bar into three cases, only one of which is a guess.
        #
        #   provably stopped   the bar closes beyond the stop (it was crossed
        #                      after entry and stayed there), or the fill was at
        #                      the open so the entire bar is post-entry.
        #   provably NOT       the bar OPENED beyond the stop. For a long that
        #                      means price rose from below the stop, through it,
        #                      up to the trigger — so the bar's low necessarily
        #                      predates the fill and cannot be charged.
        #   ambiguous          opened between stop and trigger, dipped through
        #                      the stop, closed back beyond it. One-minute OHLC
        #                      cannot order the dip against the breakout. This
        #                      is the only set the bracket disagrees about.
        provably_stopped = bool(close_beyond or (gap_fill and excursion_hit))
        provably_safe = bool(opened_beyond and not gap_fill)
        ambiguous = bool(excursion_hit and not provably_stopped and not provably_safe)

        policy = cfg.same_bar_policy
        if policy == "optimistic":
            entry_bar_stop = provably_stopped
        elif policy == "pessimistic":
            entry_bar_stop = provably_stopped or ambiguous
        else:  # "certain"
            entry_bar_stop = provably_stopped

        same_bar = bool(excursion_hit)
        if entry_bar_stop:
            exit_price, reason = stop, "stop"
        elif later.size:
            exit_price, reason = stop, "stop"
        else:
            exit_price, reason = dc[-1], "eod"

        pnl_ps = (exit_price - entry) * direction
        row.update({
            "entry_price": entry, "stop_price": stop, "exit_price": exit_price,
            "exit_reason": reason, "risk_per_share": risk,
            "pnl_per_share": pnl_ps, "pnl_r": pnl_ps / risk,
            "entry_minute": int(m[after_or][k]), "same_bar_stop": same_bar,
            "entry_bar_ambiguous": ambiguous,
        })
        rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# stage 1 driver
# --------------------------------------------------------------------------- #

def build_trade_panel(symbols, start: str, end: str, cfg: ORBConfig,
                      cache_path: Path | None = None,
                      progress: bool = True) -> pd.DataFrame:
    """One row per symbol-day: the ORB trade that would have been taken, in R.

    Filters are NOT applied here — every day is simulated so that the Relative
    Volume / PnL relationship (the paper's Figure 4) can be measured across the
    whole distribution, including the trades the strategy declines to take.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from backtest.data import load_bars

    if cache_path and cache_path.exists():
        log.info("reading cached trade panel %s", cache_path)
        return pd.read_parquet(cache_path)

    daily = load_bars(list(symbols), start, end, "1Day", adjustment="raw").frame
    feats = daily_features(daily, cfg)

    frames = []
    for i, sym in enumerate(symbols, 1):
        try:
            panel = load_bars(sym, start, end, "1Min",
                              session="regular", adjustment="raw")
        except Exception as exc:
            log.warning("%s: minute bars unavailable (%s)", sym, exc)
            continue
        minutes = panel.frame
        if minutes.empty:
            continue
        if "symbol" in minutes.index.names:
            minutes = minutes.droplevel("symbol")
        try:
            atr_series = feats.xs(sym, level="symbol")["atr"]
        except KeyError:
            continue
        out = simulate_symbol(minutes, atr_series, cfg)
        if out.empty:
            continue
        out["symbol"] = sym
        # carry the daily-frequency filter inputs across
        f = feats.xs(sym, level="symbol")
        out["avg_volume"] = out["date"].map(f["avg_volume"])
        frames.append(out)
        if progress:
            print(f"  [{i:3d}/{len(symbols)}] {sym:6s} {len(out):5d} days", flush=True)

    panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if panel.empty:
        return panel

    panel = add_relative_volume(panel, cfg)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        panel.to_parquet(cache_path, index=False)
    return panel


def build_trade_panels(symbols, start: str, end: str,
                       configs: dict[str, ORBConfig],
                       cache_dir: Path | None = None,
                       progress: bool = True) -> dict[str, pd.DataFrame]:
    """Build several trade panels in ONE pass over the minute bars.

    Each symbol's minute history is ~800k rows and re-reading it once per
    opening-range length dominates the runtime. Every config shares the same
    bars and the same daily features, so load once and simulate all of them.

    Returns {name: panel}. Panels already cached on disk are read back and
    excluded from the pass; if every panel is cached, no bars are touched.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from backtest.data import load_bars

    panels: dict[str, pd.DataFrame] = {}
    todo: dict[str, ORBConfig] = {}
    for name, cfg in configs.items():
        path = cache_dir / f"{name}.parquet" if cache_dir else None
        if path and path.exists():
            panels[name] = pd.read_parquet(path)
        else:
            todo[name] = cfg
    if not todo:
        return panels

    lookbacks = {c.lookback for c in todo.values()}
    if len(lookbacks) != 1:
        raise ValueError("all configs must share a lookback to share daily features")
    ref_cfg = next(iter(todo.values()))

    daily = load_bars(list(symbols), start, end, "1Day", adjustment="raw").frame
    feats = daily_features(daily, ref_cfg)

    collected: dict[str, list[pd.DataFrame]] = {name: [] for name in todo}
    for i, sym in enumerate(symbols, 1):
        try:
            minutes = load_bars(sym, start, end, "1Min",
                                session="regular", adjustment="raw").frame
        except Exception as exc:
            log.warning("%s: minute bars unavailable (%s)", sym, exc)
            continue
        if minutes.empty:
            continue
        if "symbol" in minutes.index.names:
            minutes = minutes.droplevel("symbol")
        try:
            f = feats.xs(sym, level="symbol")
        except KeyError:
            continue
        for name, cfg in todo.items():
            out = simulate_symbol(minutes, f["atr"], cfg)
            if out.empty:
                continue
            out["symbol"] = sym
            out["avg_volume"] = out["date"].map(f["avg_volume"])
            collected[name].append(out)
        if progress:
            print(f"  [{i:3d}/{len(symbols)}] {sym}", flush=True)

    for name, frames in collected.items():
        panel = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not panel.empty:
            panel = add_relative_volume(panel, todo[name])
            if cache_dir:
                cache_dir.mkdir(parents=True, exist_ok=True)
                panel.to_parquet(cache_dir / f"{name}.parquet", index=False)
        panels[name] = panel
    return panels


def add_relative_volume(panel: pd.DataFrame, cfg: ORBConfig) -> pd.DataFrame:
    """RelVol = today's opening-range volume / mean of the prior 14 days'.

    The denominator is shifted, so it never contains today. A stock's first 14
    sessions therefore have no RelVol and cannot be traded by the RelVol
    variant — correct, not a bug.
    """
    panel = panel.sort_values(["symbol", "date"]).copy()
    grp = panel.groupby("symbol")["or_volume"]
    denom = grp.transform(
        lambda s: s.shift(1).rolling(cfg.lookback, min_periods=cfg.lookback).mean()
    )
    panel["or_volume_avg"] = denom
    panel["rel_volume"] = panel["or_volume"] / denom
    return panel


# --------------------------------------------------------------------------- #
# stage 2 — filters, selection, sizing, equity
# --------------------------------------------------------------------------- #

def apply_filters(panel: pd.DataFrame, cfg: ORBConfig) -> pd.DataFrame:
    """The paper's filters 1-3 (always) and 4 (RelVol variant only)."""
    m = (
        panel["day_open"].gt(cfg.min_price)
        & panel["avg_volume"].ge(cfg.min_avg_volume)
        & panel["atr"].gt(cfg.min_atr)
        & panel["direction"].ne(0)
    )
    if cfg.min_rel_volume is not None:
        m &= panel["rel_volume"].ge(cfg.min_rel_volume)
    return panel[m].copy()


def select_daily(eligible: pd.DataFrame, cfg: ORBConfig) -> pd.DataFrame:
    """Top-N by Relative Volume each day. Without a RelVol filter there is no
    ranking key in the paper, so the Base Strategy trades every qualifier."""
    if not cfg.top_n:
        return eligible
    if cfg.min_rel_volume is None:
        return eligible
    return (eligible.sort_values(["date", "rel_volume"], ascending=[True, False])
                    .groupby("date", group_keys=False).head(cfg.top_n))


def run_portfolio(selected: pd.DataFrame, cfg: ORBConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compound the equity curve day by day.

    Sizing: shares such that a stop-out costs `risk_per_trade` of equity. If the
    resulting gross exposure breaches `max_leverage`, every position that day is
    scaled down pro-rata — which is what actually happens with 20 names each
    risking 1%, and is why realized risk per trade is usually well under 1%.
    """
    equity = cfg.initial_capital
    day_rows, fill_rows = [], []

    traded = selected[selected["exit_reason"].isin({"stop", "eod"})]
    for day, grp in traded.groupby("date", sort=True):
        risk = grp["risk_per_share"].to_numpy(float)
        entry = grp["entry_price"].to_numpy(float)
        pnl_ps = grp["pnl_per_share"].to_numpy(float)

        shares = np.floor(cfg.risk_per_trade * equity / risk)
        shares = np.maximum(shares, 0.0)

        gross = float((shares * entry).sum())
        cap = cfg.max_leverage * equity
        scale = 1.0
        if gross > cap and gross > 0:
            scale = cap / gross
            shares = np.floor(shares * scale)

        commission = cfg.commission_per_share * shares.sum() * 2.0  # in and out
        pnl = float((shares * pnl_ps).sum()) - commission
        start_equity = equity
        equity += pnl

        day_rows.append({
            "date": day, "n_trades": int((shares > 0).sum()),
            "gross_exposure": float((shares * entry).sum()),
            "leverage": float((shares * entry).sum()) / start_equity if start_equity else 0.0,
            "leverage_scale": scale, "commission": commission,
            "pnl": pnl, "return": pnl / start_equity if start_equity else 0.0,
            "equity": equity,
        })
        f = grp.copy()
        f["shares"] = shares
        f["position_pnl"] = shares * pnl_ps
        f["commission"] = cfg.commission_per_share * shares * 2.0
        fill_rows.append(f)

        if equity <= 0:
            log.warning("equity wiped out on %s", day)
            break

    daily = pd.DataFrame(day_rows)
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"])
        daily = daily.set_index("date")
    fills = pd.concat(fill_rows, ignore_index=True) if fill_rows else pd.DataFrame()
    return daily, fills


def backtest(panel: pd.DataFrame, cfg: ORBConfig) -> dict:
    """Filters -> selection -> portfolio, in one call."""
    eligible = apply_filters(panel, cfg)
    selected = select_daily(eligible, cfg)
    daily, fills = run_portfolio(selected, cfg)
    return {"config": cfg, "eligible": eligible, "selected": selected,
            "daily": daily, "fills": fills}

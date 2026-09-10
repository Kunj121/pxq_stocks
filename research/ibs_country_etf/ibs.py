"""Internal Bar Strength Min-Max on country ETFs — Pandey & Joshi (2023).

Replication engine. See ibs_country_etf.md for the strategy and its data needs.

Everything here is daily and vectorised; the whole universe is 15 ETFs, so there
is no streaming trick to play. The thing that actually matters is **causality**:

    IBS on day t is computed from day t's bar and the position is entered at
    that same close. The return it earns is t -> t+1.

So the weight series must be shifted one day against the return series. Getting
that backwards computes the return of a position entered *before* the bar that
produced its signal, which prints a spectacular and completely fake Sharpe. The
`test_ibs.py` suite pins this with a synthetic series whose answer is known.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252

# Table 1 of the paper. Note EWI is iShares MSCI *Italy*; the paper's table
# labels it South Korea (South Korea would be EWY) and its basket labels use
# "sk" for it throughout. We follow what the paper computed, not what it wrote.
UNIVERSE = {
    "PIN": "India",       "FXI": "China",        "EWI": "Italy (paper: 'sk')",
    "EWW": "Mexico",      "EZA": "South Africa", "EWT": "Taiwan",
    "EWJ": "Japan",       "IVV": "USA",          "EWU": "UK",
    "EZU": "EU",          "EWA": "Australia",    "EWS": "Singapore",
    "EWC": "Canada",      "EIS": "Israel",       "EWZ": "Brazil",
}

EMERGING = ["EWZ", "PIN", "EWI", "FXI", "EWW", "EZA", "EWT"]
DEVELOPED = ["EWJ", "IVV", "EWU", "EZU", "EWA", "EWC", "EWS", "EIS"]


@dataclass(frozen=True)
class IBSConfig:
    """Strategy parameters. Defaults are the paper's headline Min-Max."""

    n_positions: int = 1          # long the N lowest IBS, short the N highest
    hold_days: int = 1            # holding period
    ibs_window: int = 1           # days over which the IBS high/low are taken
    long_only: bool = False
    short_only: bool = False
    borrow_rate_daily: float = 0.0001    # 0.01%/day, the paper's assumption
    gross_exposure: float = 1.0          # dollar-neutral at 100% gross
    price_field: str = "close"           # "open" gives the open-to-open variant
    slippage_bps: float = 0.0            # per side, on traded notional

    @property
    def label(self) -> str:
        bits = [f"MinMax N={self.n_positions}"]
        if self.hold_days != 1:
            bits.append(f"hold={self.hold_days}d")
        if self.ibs_window != 1:
            bits.append(f"IBS{self.ibs_window}d")
        if self.long_only:
            bits.append("long-only")
        if self.short_only:
            bits.append("short-only")
        if self.price_field != "close":
            bits.append(self.price_field + "-to-" + self.price_field)
        return " ".join(bits)


# --------------------------------------------------------------------------- #
# signal
# --------------------------------------------------------------------------- #

def compute_ibs(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
                window: int = 1) -> pd.DataFrame:
    """IBS = (Close - Low_n) / (High_n - Low_n), per ETF per day.

    A zero-range bar (High == Low) has an undefined IBS. It is left NaN rather
    than filled with 0.5 — filling would invent a neutral signal where there is
    none and, worse, make that ETF eligible for the Min-Max ranking.
    """
    if window > 1:
        high = high.rolling(window, min_periods=window).max()
        low = low.rolling(window, min_periods=window).min()
    span = high - low
    ibs = (close - low) / span.where(span > 0)
    return ibs


def minmax_weights(ibs: pd.DataFrame, cfg: IBSConfig) -> pd.DataFrame:
    """Target weights from the IBS cross-section, as decided at each day's close.

    Long the `n_positions` lowest IBS, short the `n_positions` highest, equally
    weighted within each leg and scaled so gross exposure is `gross_exposure`.
    A day with too few ETFs to fill both legs is skipped entirely.
    """
    n = cfg.n_positions
    ranks_low = ibs.rank(axis=1, method="first", ascending=True)
    ranks_high = ibs.rank(axis=1, method="first", ascending=False)
    available = ibs.notna().sum(axis=1)

    need = n if (cfg.long_only or cfg.short_only) else 2 * n
    tradable = available >= need

    longs = (ranks_low <= n) & ibs.notna()
    shorts = (ranks_high <= n) & ibs.notna()
    if cfg.long_only:
        shorts = shorts & False
    if cfg.short_only:
        longs = longs & False

    w = pd.DataFrame(0.0, index=ibs.index, columns=ibs.columns)
    legs = int(not cfg.long_only) + int(not cfg.short_only)  # 2 normally
    per_leg = cfg.gross_exposure / max(legs, 1)
    w = w.mask(longs, per_leg / n)
    w = w.mask(shorts, -per_leg / n)
    w.loc[~tradable] = 0.0
    return w


def threshold_weights(ibs: pd.DataFrame, cfg: IBSConfig,
                      low_thr: float = 0.2, high_thr: float = 0.8,
                      require_both: bool = True) -> pd.DataFrame:
    """The comparison strategy: long IBS < 0.2, short IBS > 0.8.

    `require_both` reproduces the paper's rule that no trade is entered unless
    both thresholds are crossed on the same day.
    """
    longs = ibs < low_thr
    shorts = ibs > high_thr
    if cfg.long_only:
        shorts = shorts & False
    if cfg.short_only:
        longs = longs & False

    n_long = longs.sum(axis=1)
    n_short = shorts.sum(axis=1)
    if require_both and not (cfg.long_only or cfg.short_only):
        active = (n_long > 0) & (n_short > 0)
    else:
        active = (n_long > 0) | (n_short > 0)

    legs = int(not cfg.long_only) + int(not cfg.short_only)
    per_leg = cfg.gross_exposure / max(legs, 1)
    w = pd.DataFrame(0.0, index=ibs.index, columns=ibs.columns)
    w = w.add(longs.div(n_long.replace(0, np.nan), axis=0).fillna(0.0) * per_leg)
    w = w.sub(shorts.div(n_short.replace(0, np.nan), axis=0).fillna(0.0) * per_leg)
    w.loc[~active] = 0.0
    return w


# --------------------------------------------------------------------------- #
# portfolio
# --------------------------------------------------------------------------- #

def hold_overlap(weights: pd.DataFrame, hold_days: int) -> pd.DataFrame:
    """Spread each day's signal over `hold_days` overlapping tranches.

    With a 3-day hold you run three books a third the size, opened on three
    consecutive days. The average of the last `hold_days` target-weight rows is
    exactly that portfolio, and it keeps gross exposure constant instead of
    letting it triple.
    """
    if hold_days <= 1:
        return weights
    return weights.rolling(hold_days, min_periods=1).mean()


def run(ibs: pd.DataFrame, prices: pd.DataFrame, cfg: IBSConfig,
        weight_fn=minmax_weights) -> dict:
    """Daily returns of the strategy.

    `prices` is the series positions are marked on — closes for the base case,
    opens for the open-to-open variant.
    """
    w_target = weight_fn(ibs, cfg)
    w = hold_overlap(w_target, cfg.hold_days)

    returns = prices.pct_change()

    # THE causality line, and it depends on where you transact.
    #
    #   close-to-close: the signal IS the close of day t, and you trade at that
    #       same close. The return earned is close_t -> close_{t+1}, which is
    #       `returns` at t+1. Lag 1.
    #
    #   open-to-open: you still only know the signal after day t's close, so the
    #       earliest entry is the OPEN of day t+1, exiting at the open of t+2.
    #       The return earned is open_{t+1} -> open_{t+2}, which is `returns` at
    #       t+2. Lag 2.
    #
    # Using lag 1 on opens would measure open_t -> open_{t+1}, a window that
    # CONTAINS day t's move — the very move that produced the signal. That is an
    # inverted look-ahead and it prints a huge negative Sharpe, not a small one.
    lag = 2 if cfg.price_field == "open" else 1
    w_held = w.shift(lag)
    gross_ret = (w_held * returns).sum(axis=1, min_count=1)

    short_notional = w_held.clip(upper=0).abs().sum(axis=1)
    borrow = short_notional * cfg.borrow_rate_daily

    turnover = (w_held - w_held.shift(1)).abs().sum(axis=1).fillna(0.0)
    slip = turnover * cfg.slippage_bps / 10_000.0

    net = (gross_ret - borrow - slip).dropna()
    equity = (1.0 + net).cumprod()

    return {
        "config": cfg,
        "weights": w_held,
        "returns": net,
        "gross_returns": gross_ret.dropna(),
        "equity": equity,
        "turnover": turnover,
        "time_in": float((w_held.abs().sum(axis=1) > 0).reindex(net.index).mean()),
        "borrow_drag": float(borrow.reindex(net.index).sum()),
    }


# --------------------------------------------------------------------------- #
# the mechanism: paper Table 3
# --------------------------------------------------------------------------- #

def hit_probabilities(ibs: pd.DataFrame, close: pd.DataFrame,
                      low_thr: float = 0.2, high_thr: float = 0.8) -> pd.DataFrame:
    """P(next-day move favours the position), per ETF, by IBS bucket.

    This is the paper's Table 3 and the claim worth testing first: it needs only
    daily bars and if it fails, the Sharpe is not worth computing.
    """
    fwd = close.pct_change().shift(-1)   # return from this close to the next
    rows = []
    for sym in ibs.columns:
        s, r = ibs[sym], fwd[sym]
        lo = r[(s < low_thr) & r.notna()]
        hi = r[(s > high_thr) & r.notna()]
        rows.append({
            "ticker": sym,
            "long_on_ibs_below": float((lo > 0).mean()) if len(lo) else np.nan,
            "n_long": len(lo),
            "short_on_ibs_above": float((hi < 0).mean()) if len(hi) else np.nan,
            "n_short": len(hi),
        })
    return pd.DataFrame(rows).set_index("ticker")

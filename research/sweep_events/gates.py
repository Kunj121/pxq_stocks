"""Reconstruct the entry gates at a sweep's timestamp.

Every indicator is computed from bars that had **closed** before the sweep
printed — an hourly bar stamped 10:00 closes at 11:00, so a 10:32 sweep sees
the 09:00 bar and no later. Nothing here may peek.

Gate provenance is tracked explicitly: `SPEC` gates come from
`papers/intraday_signal.md`; `MINE` gates were invented during design and have
never been validated. See `notes/entry_gate_checks.md`.
"""
from __future__ import annotations

from math import erf, log, sqrt

import numpy as np
import pandas as pd

#: gate -> which section of papers/intraday_signal.md it comes from. All nine
#: are the paper's own; the invented gates (DTE floor, 52-week range band, EPS
#: beat count, volume>OI) are deliberately NOT here — they gate the flow and the
#: fundamentals, not the regime, and they are catalogued in
#: notes/entry_gate_checks.md with their provenance marked.
PROVENANCE = {
    "daily_stack": "§1", "px_vs_vwap": "§1", "ema_cross": "§1",
    "rsi_ok": "§1", "adx_ok": "§1", "di_ok": "§1",
    "rvol_ok": "§1", "pctB_ok": "§2/§3", "delta_ok": "§8",
}
GATES = list(PROVENANCE)


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def hourly_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """Hourly regime indicators for one symbol's OHLCV frame."""
    o, h, l, c, v = bars.open, bars.high, bars.low, bars.close, bars.volume
    out = pd.DataFrame(index=bars.index)
    out["close"] = c
    out["ema20"] = c.ewm(span=20, adjust=False).mean()
    out["ema50"] = c.ewm(span=50, adjust=False).mean()

    delta = c.diff()
    rs = _wilder(delta.clip(lower=0), 14) / _wilder((-delta).clip(lower=0), 14)
    out["rsi14"] = 100 - 100 / (1 + rs)

    # ADX(14) with the +DI/-DI components the MCP does not serve.
    up, dn = h.diff(), -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=bars.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=bars.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, 14)
    out["pdi"] = 100 * _wilder(plus_dm, 14) / atr
    out["mdi"] = 100 * _wilder(minus_dm, 14) / atr
    dx = 100 * (out.pdi - out.mdi).abs() / (out.pdi + out.mdi)
    out["adx14"] = _wilder(dx, 14)

    # Bollinger centred on SMA20 — the spec's M_t. The Robinhood MCP returns an
    # EMA20 centre instead, which understates %B; see notes/entry_gate_checks.md.
    m, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    out["bb_mid"], out["bb_up"], out["bb_lo"] = m, m + 2 * sd, m - 2 * sd

    day = pd.Series(bars.index.date, index=bars.index)
    out["svwap"] = (bars.vwap * v).groupby(day).cumsum() / v.groupby(day).cumsum()

    # RVOL_TOD: cumulative volume so far today vs the median cumulative volume
    # at the same clock hour over the prior 20 sessions.
    cumv = v.groupby(day).cumsum()
    grid = pd.DataFrame({"cumv": cumv, "hour": bars.index.hour, "day": day.values})
    base = (grid.pivot_table(index="day", columns="hour", values="cumv")
                .shift(1).rolling(20, min_periods=10).median())
    out["rvol_tod"] = [
        cv / base.loc[d, hh] if (d in base.index and not np.isnan(base.loc[d, hh])) else np.nan
        for cv, hh, d in zip(grid.cumv, grid.hour, grid.day)
    ]
    return out


# ---- Black-Scholes, r=q=0: back IV out of the print, then take delta --------
_N = lambda x: 0.5 * (1 + erf(x / sqrt(2)))


def _bs(S, K, T, sig, right):
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if right == "C" else (K - S))
    d1 = (log(S / K) + 0.5 * sig * sig * T) / (sig * sqrt(T))
    d2 = d1 - sig * sqrt(T)
    return S * _N(d1) - K * _N(d2) if right == "C" else K * _N(-d2) - S * _N(-d1)


def implied_vol(S, K, T, price, right) -> float:
    lo, hi = 1e-4, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _bs(S, K, T, mid, right) < price else (lo, mid)
    return (lo + hi) / 2


def bs_delta(S, K, T, sig, right) -> float:
    d1 = (log(S / K) + 0.5 * sig * sig * T) / (sig * sqrt(T))
    return _N(d1) if right == "C" else _N(d1) - 1


#: Split factors we will silently correct for. The vendor records the price as
#: it traded; Alpaca returns split-ADJUSTED history, so a stock that split after
#: the sweep makes the vendor's strike and spot incomparable to the bars. CRWD
#: split 4:1 after its 26 Jun sweep and produced a fictional +5,368% before this
#: check existed.
_SPLIT_FACTORS = (2, 3, 4, 5, 6, 8, 10, 20)


def split_factor(vendor_spot: float, bar_close: float, tol: float = 0.03,
                 bar_low: float | None = None, bar_high: float | None = None):
    """Return (factor, ok). factor divides vendor prices into adjusted terms.

    ok=False means the mismatch is real but not a recognisable split — the
    sweep should be dropped rather than guessed at.

    Pass bar_low/bar_high when available: a print inside the bar's RANGE is
    reconciled even if far from its close. CAR swung 595.73-716.05 in the hour
    of its 21 Apr sweep, and comparing to the close alone wrongly condemned it.
    """
    if bar_low is not None and bar_high is not None and bar_low <= vendor_spot <= bar_high:
        return 1.0, True
    r = vendor_spot / bar_close
    if 0.90 <= r <= 1.10:
        return 1.0, True
    for f in _SPLIT_FACTORS:
        for cand in (f, 1 / f):
            if abs(r / cand - 1) <= tol:
                return float(cand), True
    return r, False


def evaluate(sweep, hourly: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """Score one sweep. Bearish sweeps mirror every directional condition."""
    ind = hourly_indicators(hourly)
    t = pd.Timestamp(sweep["swept_at"], tz=ind.index.tz)
    closed = ind[ind.index + pd.Timedelta(hours=1) <= t]
    if closed.empty:
        raise ValueError(f"no closed bars before {t} for {sweep['symbol']}")
    r = closed.iloc[-1]

    # daily bars may arrive tz-aware or naive depending on the loader; compare
    # both sides as naive dates so the cutoff is "strictly prior sessions".
    didx = daily.index
    didx = didx.tz_localize(None) if getattr(didx, "tz", None) is not None else didx
    d = daily[didx.normalize() < t.tz_localize(None).normalize()]
    Cd = d.close.iloc[-1]
    s50 = d.close.rolling(50).mean().iloc[-1]
    s200 = d.close.rolling(200).mean().iloc[-1]

    S, K, px, right = sweep["spot"], sweep["strike"], sweep["price"], sweep["right"]
    # Time to expiry in years, measured from the print to the 16:00 close on the
    # expiry date. Whole days would give T=0 for a same-day (0-DTE) sweep and
    # divide by zero in the greeks; floor at one minute.
    exp_ts = pd.Timestamp(sweep["expiry"]) + pd.Timedelta(hours=16)
    T = max((exp_ts - t.tz_localize(None)).total_seconds() / 86400 / 365, 1 / 525_600)
    # Prefer the vendor's greeks when the log carries them — they are struck off
    # the mid, whereas a price-implied vol inherits the sweep's aggressive fill
    # and runs 1-3 vol points high. Fall back to the back-out otherwise.
    iv_bo = implied_vol(S, K, T, px, right)
    dl_bo = bs_delta(S, K, T, iv_bo, right)
    vd, vi = sweep.get("vendor_delta"), sweep.get("vendor_iv")
    use_vendor = vd is not None and pd.notna(vd)
    iv = (vi / 100) if use_vendor and pd.notna(vi) else iv_bo
    dl = float(vd) if use_vendor else dl_bo
    pctB = (S - r.bb_lo) / (r.bb_up - r.bb_lo)
    bull = right == "C"

    return {
        "sweep": sweep["label"], "symbol": sweep["symbol"],
        "direction": "bull" if bull else "bear",
        "daily_stack": bool(Cd > s50 > s200) if bull else bool(Cd < s50 < s200),
        "px_vs_vwap": bool(S > r.svwap) if bull else bool(S < r.svwap),
        "ema_cross": bool(r.ema20 > r.ema50) if bull else bool(r.ema20 < r.ema50),
        "rsi": round(r.rsi14, 1),
        "rsi_ok": bool(50 <= r.rsi14 <= 70) if bull else bool(30 <= r.rsi14 <= 50),
        "adx": round(r.adx14, 1), "adx_ok": bool(r.adx14 >= 20),
        "pdi": round(r.pdi, 1), "mdi": round(r.mdi, 1),
        "di_ok": bool(r.pdi > r.mdi) if bull else bool(r.mdi > r.pdi),
        "rvol_tod": round(float(r.rvol_tod), 2) if pd.notna(r.rvol_tod) else np.nan,
        "rvol_ok": bool(r.rvol_tod >= 1.5) if pd.notna(r.rvol_tod) else False,
        "pctB": round(pctB, 2),
        "pctB_ok": bool(pctB <= 0.40) if bull else bool(pctB >= 0.60),
        # How stretched the name is, as a number rather than the stack's yes/no.
        # SNDK was swept at 2.52x its 200-day after a +2185% year; the binary
        # stack passed it all the way down a 56% crash because the averages lag
        # a parabola. Recorded for later testing, not yet a gate.
        "px_over_sma200": round(S / s200, 2) if s200 and not pd.isna(s200) else np.nan,
        "px_over_sma50": round(S / s50, 2) if s50 and not pd.isna(s50) else np.nan,
        "iv_at_print": round(iv * 100, 2), "delta_at_print": round(dl, 3),
        "delta_src": "vendor" if use_vendor else "back-out",
        "iv_backout": round(iv_bo * 100, 2), "delta_backout": round(dl_bo, 3),
        "delta_ok": bool(0.40 <= abs(dl) <= 0.70),
    }

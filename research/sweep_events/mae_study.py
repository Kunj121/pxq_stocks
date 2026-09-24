"""MAE/MFE study — how much room does a winner need, and does waiting help?

Six sweeps cannot calibrate a stop. This study answers the same question on a
much larger sample by dropping the sweep itself and conditioning only on the
*technical state* the sweeps fired in — a state that recurs hundreds of times
across the panel.

    python research/sweep_events/mae_study.py [--horizon 70] [--dte 21]

What it measures, per event:
  MAE   worst option P&L, as % of premium paid, before the horizon
  MFE   best option P&L
  term  P&L at the horizon

Split MAE by terminal outcome and the stop question becomes readable: a stop is
only useful where the loser MAE distribution separates from the winner one.

ASSUMPTIONS — all of them load the dice, none of them are hidden:
  * A sweep carries no information beyond the state it fires in. Almost certainly
    false; it is the price of a usable sample. Test it later by comparing real
    sweep MAE against this baseline as sweeps.parquet grows.
  * Implied vol is the trailing 30-session realised vol and is CONSTANT through
    the hold. Real vega P&L is therefore missing entirely.
  * Entries and exits fill at the bar close with no spread or slippage. On a
    0.4-delta contract a 2% spread is ~2% of premium per side.
  * The universe is eight high-beta names (MU realised ~80% vol over the year).
    Results are quoted in premium-% and ATR units; the premium-% figures will not
    transfer to a 25%-vol name.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars            # noqa: E402
from gates import _bs, bs_delta, hourly_indicators  # noqa: E402

OUT = ROOT / "out" / "sweep_events"
START = "2025-09-19"
MIN_GAP = 35          # bars between events on one symbol (~5 sessions)
TARGET_DELTA = 0.40


def realised_vol(close: pd.Series, sessions: int = 30) -> pd.Series:
    """Trailing annualised close-to-close vol, computed daily, held flat
    intraday so an entry at 11:00 uses only completed sessions."""
    daily = close.groupby(close.index.date).last()
    r = np.log(daily / daily.shift())
    rv = r.rolling(sessions).std() * np.sqrt(252)
    rv = rv.shift(1)                       # strictly prior sessions
    return pd.Series(close.index.date, index=close.index).map(rv)


def find_events(ind: pd.DataFrame, rv: pd.Series) -> pd.DatetimeIndex:
    """Bars in the state the six observed sweeps fired in: a live uptrend,
    price above session VWAP, and price extended in its hourly range."""
    m = (
        (ind.adx14 >= 20)
        & (ind.pdi > ind.mdi)
        & (ind.close > ind.svwap)
        & (((ind.close - ind.bb_lo) / (ind.bb_up - ind.bb_lo)) >= 0.60)
        & ind.ema20.notna() & rv.notna() & (rv > 0)
    )
    hits = ind.index[m.fillna(False)]
    keep, last = [], -10**9
    for i, t in zip(np.flatnonzero(m.fillna(False)), hits):
        if i - last >= MIN_GAP:
            keep.append(t); last = i
    return pd.DatetimeIndex(keep)


def option_path(S: np.ndarray, hours: np.ndarray, sigma: float, dte_days: float):
    """Price a fresh 0.4-delta call struck at S[0], repriced along the path."""
    T0 = dte_days / 365.0
    f = lambda K: bs_delta(S[0], K, T0, sigma, "C") - TARGET_DELTA
    try:
        K = brentq(f, S[0] * 0.8, S[0] * 2.5, xtol=1e-4)
    except ValueError:
        return None, None
    prem = _bs(S[0], K, T0, sigma, "C")
    if prem <= 0:
        return None, None
    T = np.maximum(T0 - hours / 24.0 / 365.0, 1e-6)
    px = np.array([_bs(s, K, t, sigma, "C") for s, t in zip(S, T)])
    return prem, (px - prem) / prem * 100.0      # P&L as % of premium


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=70, help="bars held (~10 sessions)")
    ap.add_argument("--dte", type=float, default=21.0, help="calendar DTE at entry")
    ap.add_argument("--wait", type=int, default=35, help="bars allowed for a pullback")
    ap.add_argument("--control", type=int, default=6,
                    help="random control entries per conditional event (0 disables). "
                         "WITHOUT THIS THE STUDY IS UNINTERPRETABLE — the universe "
                         "ran hard over the window, so any long call looks profitable.")
    a = ap.parse_args()

    sw = pd.read_parquet(HERE / "sweeps.parquet")
    syms = sorted(set(sw.symbol) | {"HOOD", "INTU"})
    bars = load_bars(syms, START, timeframe="1Hour", session="regular").frame

    rows = []
    for sym in syms:
        d = bars.xs(sym, level="symbol").sort_index()
        ind = hourly_indicators(d)
        rv = realised_vol(d.close)
        pctB = (ind.close - ind.bb_lo) / (ind.bb_up - ind.bb_lo)
        events = find_events(ind, rv)
        idx = d.index

        for t in events:
            i = idx.get_loc(t)
            if i + a.horizon >= len(idx):
                continue
            sigma = float(rv.iloc[i])

            for rule in ("at_signal", "pullback"):
                if rule == "at_signal":
                    j = i
                else:                       # first bar within --wait where %B <= 0.40
                    win = pctB.iloc[i + 1: i + 1 + a.wait]
                    hit = np.flatnonzero((win <= 0.40).values)
                    if not len(hit):
                        rows.append(dict(symbol=sym, event=t, rule=rule, entered=False))
                        continue
                    j = i + 1 + int(hit[0])
                if j + a.horizon >= len(idx):
                    continue

                S = d.close.values[j: j + a.horizon + 1]
                hrs = np.arange(len(S), dtype=float)
                prem, pnl = option_path(S, hrs, sigma, a.dte)
                if prem is None:
                    continue
                rows.append(dict(
                    symbol=sym, event=t, rule=rule, entered=True,
                    entry_at=idx[j], wait_bars=j - i, sigma=round(sigma, 3),
                    premium=round(prem * 100, 1),
                    mae=round(float(pnl.min()), 1), mfe=round(float(pnl.max()), 1),
                    term=round(float(pnl[-1]), 1),
                    hrs_to_peak=int(np.argmax(pnl))))
        # Unconditional control: the same machinery at random bars on the same
        # symbol and window. If the conditional set does not beat this, the state
        # filter is measuring the universe's drift, not an edge.
        if a.control:
            rng = np.random.default_rng(7)
            valid = [k for k in range(60, len(idx) - a.horizon - 1)
                     if pd.notna(rv.iloc[k]) and rv.iloc[k] > 0]
            n_pick = min(len(valid), max(1, len(events) * a.control))
            for k in rng.choice(valid, size=n_pick, replace=False):
                S = d.close.values[k: k + a.horizon + 1]
                prem, pnl = option_path(S, np.arange(len(S), dtype=float),
                                        float(rv.iloc[k]), a.dte)
                if prem is None:
                    continue
                rows.append(dict(symbol=sym, event=idx[k], rule="control",
                                 entered=True, entry_at=idx[k], wait_bars=0,
                                 sigma=round(float(rv.iloc[k]), 3),
                                 premium=round(prem * 100, 1),
                                 mae=round(float(pnl.min()), 1),
                                 mfe=round(float(pnl.max()), 1),
                                 term=round(float(pnl[-1]), 1),
                                 hrs_to_peak=int(np.argmax(pnl))))

    res = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    res.to_parquet(OUT / "mae_study.parquet", index=False)
    report(res, a)


def cluster_boot(df: pd.DataFrame, col: str, n: int = 2000) -> tuple[float, float]:
    """Resample whole symbols — events inside one symbol are not independent."""
    syms = df.symbol.unique()
    rng = np.random.default_rng(0)
    means = []
    for _ in range(n):
        pick = rng.choice(syms, size=len(syms), replace=True)
        v = np.concatenate([df.loc[df.symbol == s, col].values for s in pick])
        if len(v):
            means.append(v.mean())
    return tuple(np.percentile(means, [2.5, 97.5]))


def report(res: pd.DataFrame, a) -> None:
    ent = res[res.entered]
    print(f"\n{'='*74}\nMAE / MFE STUDY — {a.horizon} bars held, {a.dte:.0f}d DTE, "
          f"0.40-delta call\n{'='*74}")

    for rule in ("at_signal", "pullback", "control"):
        g = ent[ent.rule == rule]
        if g.empty:
            continue
        offered = res[res.rule == rule]
        lo, hi = cluster_boot(g, "term")
        print(f"\n--- {rule} --- {len(g)} entries"
              f"{f' of {len(offered)} signals ({100*len(g)/len(offered):.0f}% taken)' if rule=='pullback' else ''}")
        print(f"  terminal P&L   mean {g.term.mean():+6.1f}%   median {g.term.median():+6.1f}%"
              f"   95% CI [{lo:+.1f}, {hi:+.1f}]   win rate {100*(g.term>0).mean():.0f}%")
        print(f"  MAE            mean {g.mae.mean():6.1f}%   median {g.mae.median():6.1f}%"
              f"   p10 {g.mae.quantile(.10):6.1f}%")
        print(f"  MFE            mean {g.mfe.mean():+6.1f}%   median {g.mfe.median():+6.1f}%"
              f"   hrs to peak (median) {g.hrs_to_peak.median():.0f}")

        # NB: splitting MAE by terminal outcome is CIRCULAR — a trade that ended
        # up a winner necessarily drew down less. The control prints the same
        # separation. Use it to describe, never to choose a stop; the only valid
        # test of a stop is realised P&L with and without it.
        w, l = g[g.term > 0], g[g.term <= 0]
        if len(w) and len(l):
            print(f"\n  MAE by outcome        winners (n={len(w)})   losers (n={len(l)})")
            for q in (.10, .25, .50):
                print(f"    {int(q*100):>2}th pct           {w.mae.quantile(q):7.1f}%"
                      f"        {l.mae.quantile(q):7.1f}%")
            print(f"    mean                {w.mae.mean():7.1f}%        {l.mae.mean():7.1f}%")
            for s in (25, 35, 50, 65, 80):
                kw, kl = (w.mae <= -s).mean(), (l.mae <= -s).mean()
                print(f"    stop at -{s:>2}%  kills {100*kw:4.0f}% of winners, "
                      f"{100*kl:4.0f}% of losers   (edge {100*(kl-kw):+5.0f} pts)")
    print()


if __name__ == "__main__":
    main()

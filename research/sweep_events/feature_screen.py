"""Which technicals actually rank forward option outcomes?

`mae_study.py` tested one hand-picked state and found no edge against random
entry. That tests a *level* ("is this state good?"). Confirmation is a *ranking*
question ("among candidates, which are better?"), and a feature can rank well
while having no standalone edge. This screens ~30 features on the ranking
question instead.

    python research/sweep_events/feature_screen.py [--stride 7] [--horizon 70]

Metric is the information coefficient: Spearman rank correlation between the
feature at entry and the forward P&L of a 0.40-delta call. |IC| > ~0.05 that
survives a cluster bootstrap is worth a second look; anything less is noise.
Quintile spread (top minus bottom fifth, in premium-%) says whether the IC is
economically interesting or merely statistically visible.

Caveats carried over from mae_study: constant IV, no spread or slippage, seven
high-beta names over one bullish year, and events clustered within symbol so the
effective sample is far below the row count.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import spearmanr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars                 # noqa: E402
from gates import bs_delta, hourly_indicators       # noqa: E402
from mae_study import realised_vol                  # noqa: E402

OUT = ROOT / "out" / "sweep_events"
SYMS = ["AMD", "HOOD", "INTU", "META", "MRVL", "MU", "TSLA"]
START, TARGET_DELTA = "2025-09-19", 0.40


def _norm_cdf(x):
    return 0.5 * (1.0 + np.vectorize(lambda z: __import__("math").erf(z / np.sqrt(2)))(x))


def bs_call_vec(S, K, T, sig):
    S, T = np.asarray(S, float), np.maximum(np.asarray(T, float), 1e-9)
    d1 = (np.log(S / K) + 0.5 * sig * sig * T) / (sig * np.sqrt(T))
    return S * _norm_cdf(d1) - K * _norm_cdf(d1 - sig * np.sqrt(T))


def features(d: pd.DataFrame, ind: pd.DataFrame, rv: pd.Series) -> pd.DataFrame:
    c, h, l, v = d.close, d.high, d.low, d.volume
    atr = (pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1)
           .max(axis=1).ewm(alpha=1 / 14, adjust=False).mean())
    day = pd.Series(d.index.date, index=d.index)
    f = pd.DataFrame(index=d.index)

    f["rsi14"] = ind.rsi14
    f["rsi7"] = 100 - 100 / (1 + (c.diff().clip(lower=0).ewm(alpha=1/7, adjust=False).mean()
                 / (-c.diff()).clip(lower=0).ewm(alpha=1/7, adjust=False).mean()))
    f["adx14"] = ind.adx14
    f["di_spread"] = ind.pdi - ind.mdi
    f["pctB"] = (ind.close - ind.bb_lo) / (ind.bb_up - ind.bb_lo)
    f["bb_width_atr"] = (ind.bb_up - ind.bb_lo) / atr
    f["vwap_dist_atr"] = (c - ind.svwap) / atr
    f["ema20_dist_atr"] = (c - ind.ema20) / atr
    f["ema50_dist_atr"] = (c - ind.ema50) / atr
    f["ema_spread_atr"] = (ind.ema20 - ind.ema50) / atr
    f["ema20_slope"] = ind.ema20.diff(7) / atr
    f["ema50_slope"] = ind.ema50.diff(7) / atr
    for n in (7, 14, 35, 70, 140):
        f[f"ret_{n}h"] = c.pct_change(n) * 100
    f["ret_1d_z"] = (c.pct_change(7) / (rv / np.sqrt(252)))
    f["atr_pct"] = atr / c * 100
    f["rvol"] = rv * 100
    f["rvol_chg"] = rv / rv.shift(70) - 1
    f["vol_ratio"] = v / v.rolling(70).mean()
    f["vol_trend"] = v.rolling(7).mean() / v.rolling(70).mean()
    f["dollar_vol_z"] = ((c * v).rolling(7).mean() /
                         (c * v).rolling(140).mean())
    f["hi_70_dist_atr"] = (c - h.rolling(70).max()) / atr
    f["lo_70_dist_atr"] = (c - l.rolling(70).min()) / atr
    f["range_pos_70"] = ((c - l.rolling(70).min()) /
                         (h.rolling(70).max() - l.rolling(70).min()))
    f["up_bar_frac_35"] = (c.diff() > 0).rolling(35).mean()
    f["gap_open_atr"] = ((c.groupby(day).transform("first") -
                          c.groupby(day).transform("last").shift(7)) / atr)
    f["hour"] = d.index.hour
    f["choppiness"] = (c.diff().abs().rolling(35).sum() /
                       (c.rolling(35).max() - c.rolling(35).min()))
    f["consec_up_days"] = (c.groupby(day).transform("last").diff() > 0) \
        .groupby((c.groupby(day).transform("last").diff() <= 0).cumsum()).cumsum()
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=7, help="sample every Nth bar")
    ap.add_argument("--horizon", type=int, default=70)
    ap.add_argument("--dte", type=float, default=21.0)
    a = ap.parse_args()

    bars = load_bars(SYMS, START, timeframe="1Hour", session="regular").frame
    frames = []
    for sym in SYMS:
        d = bars.xs(sym, level="symbol").sort_index()
        ind = hourly_indicators(d)
        rv = realised_vol(d.close)
        F = features(d, ind, rv)
        idx = np.arange(140, len(d) - a.horizon - 1, a.stride)
        idx = [i for i in idx if pd.notna(rv.iloc[i]) and rv.iloc[i] > 0]

        terms, keep = [], []
        for i in idx:
            sig = float(rv.iloc[i])
            S = d.close.values[i: i + a.horizon + 1]
            T0 = a.dte / 365.0
            try:
                K = brentq(lambda k: bs_delta(S[0], k, T0, sig, "C") - TARGET_DELTA,
                           S[0] * 0.8, S[0] * 2.5, xtol=1e-4)
            except ValueError:
                continue
            prem = float(bs_call_vec(S[:1], K, [T0], sig)[0])
            if prem <= 1e-6:
                continue
            T = np.maximum(T0 - np.arange(len(S)) / 24.0 / 365.0, 1e-9)
            px = bs_call_vec(S, K, T, sig)
            terms.append((px[-1] - prem) / prem * 100)
            keep.append(i)
        g = F.iloc[keep].copy()
        g["term"], g["symbol"] = terms, sym
        frames.append(g)

    X = pd.concat(frames).replace([np.inf, -np.inf], np.nan)
    OUT.mkdir(parents=True, exist_ok=True)
    X.to_parquet(OUT / "feature_screen.parquet")

    cols = [c for c in X.columns if c not in ("term", "symbol")]
    rng = np.random.default_rng(3)
    syms = X.symbol.unique()
    out = []
    for c in cols:
        s = X[[c, "term", "symbol"]].dropna()
        if len(s) < 200:
            continue
        ic = spearmanr(s[c], s.term).correlation
        boot = []
        for _ in range(500):
            pick = rng.choice(syms, len(syms), replace=True)
            v = pd.concat([s[s.symbol == q] for q in pick])
            if v[c].nunique() > 5:
                boot.append(spearmanr(v[c], v.term).correlation)
        lo, hi = np.percentile(boot, [2.5, 97.5]) if boot else (np.nan, np.nan)
        q = pd.qcut(s[c], 5, labels=False, duplicates="drop")
        spread = s.term[q == q.max()].mean() - s.term[q == 0].mean()
        out.append(dict(feature=c, n=len(s), IC=round(ic, 3),
                        ci_lo=round(lo, 3), ci_hi=round(hi, 3),
                        robust=bool(lo * hi > 0), q5_minus_q1=round(spread, 1)))
    r = pd.DataFrame(out).sort_values("IC", key=abs, ascending=False)
    r.to_parquet(OUT / "feature_ic.parquet", index=False)
    pd.set_option("display.width", 200)
    print(f"\nFEATURE SCREEN — n={len(X)} samples, {a.horizon}-bar forward "
          f"0.40-delta call P&L\nIC = Spearman rank corr; robust = cluster-bootstrap "
          f"CI excludes zero\n")
    print(r.to_string(index=False))
    print(f"\nrobust features: {int(r.robust.sum())} of {len(r)}")


if __name__ == "__main__":
    main()

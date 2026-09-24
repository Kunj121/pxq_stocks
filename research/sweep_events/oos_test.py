"""Out-of-sample test of a measured gate set.

feature_screen.py chose its features by measuring the whole window, so testing
them on that window is circular. This splits the year in half: features are
selected on H1 alone, and the resulting composite is evaluated on H2 against a
random-entry control drawn from H2.

    python research/sweep_events/oos_test.py

Three arms on H2:
  measured   top quintile of a composite built from H1-selected features
  paper      the intraday_signal.md state (ADX/DI/VWAP/%B) — the old gate set
  control    every sampled bar (i.e. random entry)

No thresholds are fitted: features are z-scored with H1 statistics and summed,
and the gate is simply the top quintile of that score within H2.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import spearmanr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from backtest.data import load_bars                        # noqa: E402
from gates import bs_delta, hourly_indicators              # noqa: E402
from mae_study import realised_vol                         # noqa: E402
from feature_screen import features, bs_call_vec, SYMS     # noqa: E402

OUT = ROOT / "out" / "sweep_events"
SPLIT, HOR, DTE, STRIDE, TARGET_DELTA = "2026-03-19", 70, 21.0, 7, 0.40
N_FEATURES = 4


def build() -> pd.DataFrame:
    bars = load_bars(SYMS, "2025-09-19", timeframe="1Hour", session="regular").frame
    out = []
    for sym in SYMS:
        d = bars.xs(sym, level="symbol").sort_index()
        ind = hourly_indicators(d)
        rv = realised_vol(d.close)
        F = features(d, ind, rv)
        pctB = (ind.close - ind.bb_lo) / (ind.bb_up - ind.bb_lo)
        paper = ((ind.adx14 >= 20) & (ind.pdi > ind.mdi)
                 & (ind.close > ind.svwap) & (pctB >= 0.60)).fillna(False)

        idx = [i for i in range(140, len(d) - HOR - 1, STRIDE)
               if pd.notna(rv.iloc[i]) and rv.iloc[i] > 0]
        terms, keep = [], []
        for i in idx:
            sig = float(rv.iloc[i]); S = d.close.values[i: i + HOR + 1]; T0 = DTE / 365
            try:
                K = brentq(lambda k: bs_delta(S[0], k, T0, sig, "C") - TARGET_DELTA,
                           S[0] * .8, S[0] * 2.5, xtol=1e-4)
            except ValueError:
                continue
            prem = float(bs_call_vec(S[:1], K, [T0], sig)[0])
            if prem <= 1e-6:
                continue
            T = np.maximum(T0 - np.arange(len(S)) / 24 / 365, 1e-9)
            terms.append((bs_call_vec(S, K, T, sig)[-1] - prem) / prem * 100)
            keep.append(i)
        g = F.iloc[keep].copy()
        g["term"], g["symbol"] = terms, sym
        g["paper_state"] = paper.iloc[keep].values
        out.append(g)
    return pd.concat(out).replace([np.inf, -np.inf], np.nan)


def cluster_ci(df, col, n=2000, seed=11):
    rng = np.random.default_rng(seed); syms = df.symbol.unique(); m = []
    for _ in range(n):
        pick = rng.choice(syms, len(syms), replace=True)
        v = np.concatenate([df.loc[df.symbol == s, col].values for s in pick])
        if len(v):
            m.append(v.mean())
    return np.percentile(m, [2.5, 97.5])


def arm(name, df, ref=None):
    lo, hi = cluster_ci(df, "term")
    line = (f"  {name:<10} n={len(df):>4}   mean {df.term.mean():+7.1f}%   "
            f"median {df.term.median():+6.1f}%   win {100*(df.term>0).mean():3.0f}%   "
            f"95% CI [{lo:+.1f}, {hi:+.1f}]")
    if ref is not None:
        line += f"   vs control {df.term.mean()-ref:+.1f}"
    print(line)


def main() -> None:
    X = build()
    X["half"] = np.where(X.index < pd.Timestamp(SPLIT, tz=X.index.tz), "H1", "H2")
    H1, H2 = X[X.half == "H1"], X[X.half == "H2"]
    cols = [c for c in X.columns
            if c not in ("term", "symbol", "half", "paper_state", "hour",
                         "consec_up_days")]

    ic = {}
    for c in cols:
        s = H1[[c, "term"]].dropna()
        if len(s) > 150:
            ic[c] = spearmanr(s[c], s.term).correlation
    ic = pd.Series(ic).dropna().sort_values(key=abs, ascending=False)
    chosen = ic.head(N_FEATURES)
    print(f"\nH1 ({H1.index.min():%Y-%m-%d} -> {H1.index.max():%Y-%m-%d}, n={len(H1)}) "
          f"selected, top {N_FEATURES} by |IC|:")
    for k, v in chosen.items():
        print(f"    {k:<18} IC {v:+.3f}")

    # z-score with H1 statistics only, then sum signed contributions
    score = pd.Series(0.0, index=H2.index)
    for k, v in chosen.items():
        mu, sd = H1[k].mean(), H1[k].std()
        if not sd or np.isnan(sd):
            continue
        score += np.sign(v) * ((H2[k] - mu) / sd).fillna(0.0)
    H2 = H2.assign(score=score)
    fires = H2[H2.score >= H2.score.quantile(0.80)]

    print(f"\nH2 ({H2.index.min():%Y-%m-%d} -> {H2.index.max():%Y-%m-%d}) — out of sample\n")
    ctrl = H2.term.mean()
    arm("control", H2)
    arm("measured", fires, ctrl)
    arm("paper", H2[H2.paper_state], ctrl)

    print("\n  IC of each selected feature, H1 -> H2 (does it persist?)")
    for k in chosen.index:
        s2 = H2[[k, "term"]].dropna()
        print(f"    {k:<18} H1 {chosen[k]:+.3f}   H2 "
              f"{spearmanr(s2[k], s2.term).correlation:+.3f}")
    OUT.mkdir(parents=True, exist_ok=True)
    H2.assign(selected=",".join(chosen.index)).to_parquet(OUT / "oos_test.parquet")
    print()


if __name__ == "__main__":
    main()

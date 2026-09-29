"""Does a moving-average crossover add anything to the sweep signal?

Overlays an EMA(fast) / SMA(slow) crossover state on the ISO sweep trade log and
grids over (fast, slow, mode) looking for a cell where sweeps aligned with the
trend beat sweeps that fight it.

The grid is the problem, not the method. ~100 cells will hand back a "best" one
whether or not anything is there, and EDGE_TESTS.md Section 5 is explicit that an
unregistered slice is not a test. So every cell is reported three ways -- naive,
cluster-bootstrapped by ticker, and ex-top-ticker -- and the headline number is a
family-wise permutation p-value built from the max |t| across the whole grid.

Causality: a sweep prints intraday, so the last daily bar that had CLOSED before it
is the PRIOR session's. Indicators are shifted accordingly and asserted below.

    python research/sweep_events/ma_grid.py [--boot 2000] [--perm 1000]

Writes out/sweep_events/ma_grid.parquet and ma_grid_trades.parquet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "alpaca_cache" / "stocks" / "1Day" / "sip_all"
OUT = ROOT / "out" / "sweep_events"

# fast < slow is enforced when the grid is built
FASTS = (5, 8, 10, 12, 20, 26, 50)
SLOWS = (20, 50, 100, 200)
# how recently the cross must have happened, in sessions; None = state only
FRESH = (None, 5, 10, 20)


def load_trades() -> pd.DataFrame:
    """The 1,108 simulable trades from the dashboard log."""
    txt = (ROOT / "out" / "dashboard" / "trades.js").read_text()
    df = pd.DataFrame(json.loads(txt[txt.index("[") :].rstrip().rstrip(";")))
    df = df[df.ret.notna()].copy()
    df["session"] = pd.to_datetime(df.session)
    return df.reset_index(drop=True)


def load_bars(tickers) -> dict[str, pd.DataFrame]:
    """Split-adjusted daily closes. sip_all, not sip_raw -- a split inside the
    lookback would otherwise put a step change through every average."""
    bars = {}
    for t in sorted(tickers):
        p = CACHE / f"{t}.csv.gz"
        if not p.exists():
            continue
        d = pd.read_csv(p, usecols=["timestamp", "close"])
        d["date"] = pd.to_datetime(d.timestamp, utc=True).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
        bars[t] = d[["date", "close"]].sort_values("date").reset_index(drop=True)
    return bars


def indicator_panel(bars: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """EMA/SMA for every (fast, slow) in the grid, shifted one session.

    After the shift, row `date` carries only information from bars that closed
    strictly before `date` -- which is what a sweep printing during `date` could
    actually have seen.
    """
    panel = {}
    for t, d in bars.items():
        out = pd.DataFrame({"date": d.date})
        c = d.close
        for f in FASTS:
            out[f"ema{f}"] = c.ewm(span=f, adjust=False).mean().shift(1)
        for s in SLOWS:
            out[f"sma{s}"] = c.rolling(s).mean().shift(1)
        panel[t] = out
    return panel


def attach_state(trades: pd.DataFrame, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """As-of join each trade onto the last indicator row at or before its session.

    The shift already removed the session's own bar, so an exact-date match is the
    prior close. merge_asof handles sweeps on days the name did not trade.
    """
    frames = []
    for t, g in trades.groupby("ticker"):
        if t not in panel:
            continue
        p = panel[t].dropna(subset=["sma20"])
        if p.empty:
            continue
        m = pd.merge_asof(
            g.sort_values("session"),
            p.sort_values("date"),
            left_on="session",
            right_on="date",
            direction="backward",
        )
        frames.append(m)
    return pd.concat(frames, ignore_index=True)


def cell_state(df: pd.DataFrame, fast: int, slow: int, fresh: int | None):
    """(aligned, valid) -- direction agrees with the crossover state, and the state exists.

    BULL wants EMA above SMA, BEAR wants it below. `fresh` additionally demands the
    cross be recent, approximated by the sign having differed `fresh` sessions ago.

    `valid` matters: a name too young for SMA200 yields NaN, and NaN > NaN is False,
    which would file a thin-history trade as *unaligned* instead of dropping it and
    quietly load one arm of the comparison. Callers must subset on it.
    """
    ema, sma = df[f"ema{fast}"], df[f"sma{slow}"]
    valid = ema.notna() & sma.notna()
    above = ema > sma
    aligned = pd.Series(np.where(df.direction.eq("BULL"), above, ~above), index=df.index)
    if fresh is not None:
        age = df[f"cross_age_{fast}_{slow}"]
        valid &= age.notna()
        aligned &= age.le(fresh)
    return aligned & valid, valid


def add_cross_age(trades: pd.DataFrame, bars, panel) -> pd.DataFrame:
    """Sessions since the EMA/SMA sign last flipped, as of the prior close."""
    for t, p in panel.items():
        for f in FASTS:
            for s in SLOWS:
                if f >= s:
                    continue
                sign = (p[f"ema{f}"] > p[f"sma{s}"]).astype(float)
                sign[p[f"sma{s}"].isna()] = np.nan
                grp = (sign != sign.shift()).cumsum()
                p[f"cross_age_{f}_{s}"] = p.groupby(grp).cumcount()
    return attach_state(trades, panel)


def cluster_boot(df: pd.DataFrame, mask: pd.Series, n: int, rng) -> tuple[float, float]:
    """Bootstrap the aligned-minus-unaligned difference, resampling whole tickers.

    NVDA, TSLA and MU are a third of the log; resampling trades would treat their
    repeats as independent evidence and shrink the interval by roughly sqrt(10).
    """
    if n <= 0:  # permutation draws skip the bootstrap
        return np.nan, np.nan
    tickers = df.ticker.unique()
    by = {t: (df.ticker.values == t) for t in tickers}
    diffs = []
    for _ in range(n):
        pick = rng.choice(tickers, len(tickers), replace=True)
        idx = np.concatenate([np.where(by[t])[0] for t in pick])
        m, r = mask.values[idx], df.ret.values[idx]
        if m.sum() < 5 or (~m).sum() < 5:
            continue
        diffs.append(r[m].mean() - r[~m].mean())
    if len(diffs) < n // 4:
        return np.nan, np.nan
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def stratified(sub: pd.DataFrame, m: pd.Series) -> tuple[float, float]:
    """The aligned-minus-unaligned difference held WITHIN direction.

    Alignment is defined against the trade's own direction, so in a trending tape
    the aligned arm fills with BULL and the unaligned arm with BEAR. Since BULL
    trades returned +29% and BEAR -13% unconditionally, an unstratified difference
    recovers that split rather than any property of the crossover -- textbook
    Simpson's paradox. Pooling the two within-direction differences removes it.
    """
    num = den = var = 0.0
    for d in ("BULL", "BEAR"):
        s = sub[sub.direction == d]
        mm = m[sub.direction == d]
        a, b = s.ret[mm].values, s.ret[~mm].values
        if len(a) < 3 or len(b) < 3:
            continue
        w = len(a) + len(b)
        num += w * (a.mean() - b.mean())
        den += w
        var += (w ** 2) * (a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    if den == 0 or var <= 0:
        return np.nan, np.nan
    diff = num / den
    return float(diff), float(diff / (np.sqrt(var) / den))


def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or len(b) < 3:
        return np.nan
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return float((a.mean() - b.mean()) / np.sqrt(va + vb)) if va + vb > 0 else np.nan


def run_grid(df: pd.DataFrame, boot: int, rng, label: str) -> pd.DataFrame:
    rows = []
    top = df.ticker.value_counts().idxmax()
    ex = df[df.ticker != top]
    for f in FASTS:
        for s in SLOWS:
            if f >= s:
                continue
            for fresh in FRESH:
                col = f"cross_age_{f}_{s}"
                if fresh is not None and col not in df:
                    continue
                m, valid = cell_state(df, f, s, fresh)
                sub = df[valid]
                m = m[valid]
                a, b = sub.ret[m].values, sub.ret[~m].values
                if len(a) < 20 or len(b) < 20:
                    continue
                mx, vx = cell_state(ex, f, s, fresh)
                exs, mx = ex[vx], mx[vx]
                strat_d, strat_t = stratified(sub, m)
                lo, hi = cluster_boot(sub, m, boot, rng)
                rows.append(
                    dict(
                        population=label, fast=f, slow=s,
                        fresh=-1 if fresh is None else fresh,
                        n_aligned=len(a), n_other=len(b),
                        n_dropped=int((~valid).sum()),
                        mean_aligned=a.mean(), mean_other=b.mean(),
                        diff=a.mean() - b.mean(), t=welch_t(a, b),
                        win_aligned=(a > 0).mean(), win_other=(b > 0).mean(),
                        boot_lo=lo, boot_hi=hi,
                        diff_ex_top=exs.ret[mx].mean() - exs.ret[~mx].mean(),
                        t_ex_top=welch_t(exs.ret[mx].values, exs.ret[~mx].values),
                        diff_strat=strat_d, t_strat=strat_t,
                        n_tickers=sub.ticker.nunique(),
                    )
                )
    return pd.DataFrame(rows)


def permutation_maxt(df: pd.DataFrame, panel, n_perm: int, rng) -> tuple[float, np.ndarray]:
    """Family-wise null for the whole grid.

    Each trade keeps its ticker and its return but is re-dated to a random session
    of that same ticker, so the crossover state it inherits is decoupled from the
    sweep while every ticker's own MA-state marginal is preserved. The statistic is
    max |t| over all cells -- the thing a grid search actually reports.
    """
    pool = {t: p.dropna(subset=["sma200"]).reset_index(drop=True) for t, p in panel.items()}
    pool = {t: p for t, p in pool.items() if len(p) > 0}
    keep = df[df.ticker.isin(pool)].reset_index(drop=True)
    maxes = []
    ind_cols = [c for c in keep.columns if c.startswith(("ema", "sma", "cross_age_"))]
    for _ in range(n_perm):
        rows = []
        for t, g in keep.groupby("ticker"):
            p = pool[t]
            pick = p.iloc[rng.integers(0, len(p), len(g))][ind_cols].reset_index(drop=True)
            gg = g.reset_index(drop=True)
            gg[ind_cols] = pick
            rows.append(gg)
        sham = pd.concat(rows, ignore_index=True)
        g = run_grid(sham, boot=0, rng=rng, label="perm")
        maxes.append(np.nanmax(np.abs(g.t_strat.values)) if len(g) else np.nan)
    return float(np.nanmean(maxes)), np.array(maxes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--perm", type=int, default=500)
    a = ap.parse_args()
    rng = np.random.default_rng(20260929)

    trades = load_trades()
    bars = load_bars(trades.ticker.unique())
    panel = indicator_panel(bars)
    df = add_cross_age(trades, bars, panel)

    # A name that listed after its own sweep has no prior bar to join to. One does
    # (STUB). Drop it loudly rather than carrying a NaT through the comparisons.
    unmatched = df.date.isna()
    if unmatched.any():
        print(f"dropped {unmatched.sum()} trade(s) with no prior daily bar: "
              f"{', '.join(df.ticker[unmatched].unique())}")
        df = df[~unmatched].reset_index(drop=True)

    # Causality. The asof join may land on the sweep's own session, which is fine
    # because indicator_panel already shifted every column back a session -- so
    # verify the shift itself rather than the join date.
    assert (df.date <= df.session).all(), "indicator row postdates the sweep"
    probe = df.iloc[0]
    b = bars[probe.ticker]
    prior = b[b.date < probe.date].close
    assert abs(prior.rolling(20).mean().iloc[-1] - probe.sma20) < 1e-6, "sma20 is not as-of the prior close"
    print(f"causality ok: {probe.ticker} {probe.session.date()} sees bars through "
          f"{b[b.date < probe.date].date.max().date()}")
    print(f"{len(df):,} trades joined to daily bars   {df.ticker.nunique()} tickers")
    print(f"baseline mean ret {df.ret.mean():+.1%}   median {df.ret.median():+.1%}")

    grids = [run_grid(df, a.boot, rng, "all ISO")]
    badged = df[df.isGoldenSweep].reset_index(drop=True)
    if len(badged) > 100:
        grids.append(run_grid(badged, a.boot, rng, "badged"))
    grid = pd.concat(grids, ignore_index=True)

    OUT.mkdir(parents=True, exist_ok=True)
    grid.to_parquet(OUT / "ma_grid.parquet")
    df.to_parquet(OUT / "ma_grid_trades.parquet")

    for pop, g in grid.groupby("population"):
        g = g.reindex(g.t_strat.abs().sort_values(ascending=False).index)
        print(f"\n=== {pop}: {len(g)} cells, best 8 by |t_strat| ===")
        cols = ["fast", "slow", "fresh", "n_aligned", "n_other", "diff", "t",
                "boot_lo", "boot_hi", "diff_ex_top", "t_ex_top",
                "diff_strat", "t_strat"]
        print(g[cols].head(8).to_string(index=False,
              float_format=lambda v: f"{v:,.3f}"))

    iso = grid[grid.population == "all ISO"]
    print(f"\nbest raw |t| {np.nanmax(np.abs(iso.t.values)):.2f} "
          f"-> best direction-stratified |t| {np.nanmax(np.abs(iso.t_strat.values)):.2f}")
    obs = np.nanmax(np.abs(iso.t_strat.values))
    print(f"observed max |t_strat| across the all-ISO grid: {obs:.2f}")
    if a.perm > 0:
        _, null = permutation_maxt(df, panel, a.perm, rng)
        p = float(np.mean(null >= obs))
        print(f"permutation null max |t_strat|: median {np.nanmedian(null):.2f}  "
              f"95th {np.nanpercentile(null, 95):.2f}  ({a.perm} draws)")
        print(f"FAMILY-WISE p = {p:.3f}")
        np.save(OUT / "ma_grid_null.npy", null)


if __name__ == "__main__":
    main()

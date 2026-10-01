"""Run the regime detection on one stock instead of the index.

The paper is about *market* regimes: its equity series is the S&P 500 and its
other three series are macro context (VIX, 10y, oil).  Swapping a single name
into the equity slot is mechanically easy and interpretively dangerous, so this
module exists to make the substitution reproducible and to print the diagnostics
that say whether it worked.

Two things go wrong on a single name and both are measured by ``diagnose``:

**The macro features stop earning their place.**  VIX, the 10y change and oil
return are market-wide.  On SPY they describe the same object as the equity
features.  On a single stock with a low R-squared to the index they are close to
noise -- DELL is ~73% idiosyncratic -- so the clustering ends up running on the
name's own price, volume and volatility features with three passengers.

**The Bull/Low-Risk and Bear/High-Risk names can become false.**  Section V.F
labels by mean return and the paper's interpretation assumes the high-return
cluster is also the low-volatility one, which is true of an index.  It need not
be true of one stock: a name can trend hard *and* be violent.  ``diagnose``
reports the volatility of each cluster so a mislabelling is visible rather than
inherited.

Usage::

    python research/regime_detection/single_name.py DELL
    python research/regime_detection/single_name.py DELL --k 2 --daily
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from . import features as ft
from . import pipeline as pl
from . import weekly as wk

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "data" / "regime_panel.parquet"


def build_panel(symbol: str, start: str = "2016-01-01") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Panel with ``symbol`` in the equity slot and the paper's macro context.

    Returns ``(panel, index_panel)``.  Bars come from Alpaca with ``adjustment=
    "split"``: splits must be corrected or every moving-average distance breaks
    across one, but dividends must NOT be restated, because the features are
    distances from a price level (the same reasoning as ``fetch_data``'s ``raw``
    for SPY, which never splits).
    """
    from backtest.data import load_bars  # local import: keeps the module importable offline

    index_panel = pd.read_parquet(PANEL)
    bars = load_bars(
        [symbol], start=start, timeframe="1Day", adjustment="split"
    ).frame.reset_index()
    bars = bars[bars.symbol == symbol].set_index("timestamp")
    idx = pd.to_datetime(bars.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    bars.index = idx.normalize()

    panel = (
        pd.DataFrame({"close": bars.close, "volume": bars.volume})
        .join(index_panel[["oil_close", "VIX", "DGS10"]], how="inner")
        .dropna()
    )
    return panel, index_panel


def diagnose(
    panel: pd.DataFrame, index_panel: pd.DataFrame, frame: pd.DataFrame, fit, periods: int
) -> pd.DataFrame:
    """The checks that decide whether the substitution is interpretable at all."""
    name_ret = panel.close.pct_change()
    idx_ret = index_panel.close.pct_change()
    both = pd.DataFrame({"name": name_ret, "index": idx_ret}).dropna()
    beta = float(np.polyfit(both["index"], both["name"], 1)[0])
    r2 = float(np.corrcoef(both["index"], both["name"])[0, 1] ** 2)

    vols = frame.Return.groupby(fit.labels).std() * np.sqrt(periods)
    rets = frame.Return.groupby(fit.labels).mean()
    top, bottom = rets.idxmax(), rets.idxmin()
    sil = fit.selection["silhouette"]

    return pd.DataFrame(
        {
            "value": {
                "observations": len(frame),
                "beta to index": round(beta, 3),
                "R2 to index": round(r2, 3),
                "idiosyncratic share": round(1 - r2, 3),
                "K chosen freely": fit.k,
                "silhouette at K=2": round(float(sil.loc[2]), 4),
                "silhouette at K=3": round(float(sil.loc[3]), 4),
                "best-K margin over runner-up": round(
                    float(sil.max() - sil.drop(sil.idxmax()).max()), 4
                ),
                "vol of highest-return cluster": round(float(vols[top]), 4),
                "vol of lowest-return cluster": round(float(vols[bottom]), 4),
                "low-risk naming valid": bool(vols[top] < vols[bottom]),
            }
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--k", type=int, default=None, help="force K; default lets silhouette choose")
    ap.add_argument("--daily", action="store_true", help="daily instead of weekly")
    a = ap.parse_args()

    panel, index_panel = build_panel(a.symbol, a.start)
    if a.daily:
        frame, names, periods, mom = ft.build_features(panel), list(ft.FEATURES), 252, ("Momentum_20D", "Momentum_60D")
    else:
        frame, names, periods, mom = (
            wk.build_weekly_features(panel), list(wk.WEEKLY_FEATURES), 52, wk.MOMENTUM,
        )

    unit = "sessions" if a.daily else "complete weeks"
    print(f"{a.symbol}: {len(frame):,} {unit}   "
          f"{frame.index.min():%Y-%m-%d} -> {frame.index.max():%Y-%m-%d}")

    fit = pl.fit_regimes(frame, feature_names=names, k=a.k)
    print()
    print(diagnose(panel, index_panel, frame, fit, periods).to_string())
    print()
    print(pl.regime_stats(frame, fit.labels, periods=periods, momentum=mom).T.round(4).to_string())
    print()
    print("transition matrix:")
    print(pl.transition_matrix(fit.labels).round(3).to_string())
    print()
    run = 1
    for i in range(len(fit.labels) - 2, -1, -1):
        if fit.labels.iloc[i] == fit.labels.iloc[-1]:
            run += 1
        else:
            break
    print(f"current state: {fit.labels.iloc[-1]}  ({run} consecutive {unit.split()[-1]})")
    print(f"as of {frame.index.max():%Y-%m-%d}, close {frame.close.iloc[-1]:,.2f}")


if __name__ == "__main__":
    main()

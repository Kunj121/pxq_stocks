"""The paper's clustering features, built causally from the daily panel.

Section IV names fifteen features (the abstract says sixteen -- see
``regime_detection.md``, "What the paper's own numbers disagree about"):

    Return, Log_Return, Volatility_20D, Volatility_60D, Momentum_20D,
    Momentum_60D, Distance_MA50, Distance_MA200, Drawdown, Volume_Ratio,
    VIX, VIX_Change, VIX_Ratio, Treasury_20D_Change, Oil_Return

Everything here is same-day by construction, which is what the paper wants: the
clustering is an explicitly *contemporaneous* description of a day (Section V.F).
Nothing is shifted at this layer.  The one place a lag is mandatory -- the
supervised forecasting target and the strategy's exposure -- is handled in
``pipeline.py``, so that the lag is visible where the causality claim is made
rather than buried in a feature definition.

Every rolling window here is trailing and closed on the current bar, matching a
same-day feature: ``Volatility_20D`` on day *t* uses returns up to and including
*t*.  A 200-session warmup is therefore unavoidable, and rows without a full
``Distance_MA200`` window are dropped rather than back filled.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252

#: The clustering feature set, in the order Section IV lists it.
FEATURES: tuple[str, ...] = (
    "Return",
    "Log_Return",
    "Volatility_20D",
    "Volatility_60D",
    "Momentum_20D",
    "Momentum_60D",
    "Distance_MA50",
    "Distance_MA200",
    "Drawdown",
    "Volume_Ratio",
    "VIX",
    "VIX_Change",
    "VIX_Ratio",
    "Treasury_20D_Change",
    "Oil_Return",
)

#: Deliberately excluded from clustering (Section IV): non-stationary levels.
#: Kept on the frame because Section XI.J's robustness check restores them.
EXCLUDED: tuple[str, ...] = (
    "close",
    "volume",
    "oil_close",
    "DGS10",
    "MA20",
    "MA50",
    "MA200",
    "Running_Max",
)


def build_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Feature frame from the raw panel written by ``fetch_data.py``.

    Parameters
    ----------
    panel:
        Date-indexed, with ``close``, ``volume``, ``oil_close``, ``VIX``, ``DGS10``.

    Returns
    -------
    DataFrame
        The 15 clustering features plus the excluded level series, with the
        warmup rows (incomplete 200-day window) dropped and any infinite value
        from a ratio construction turned into NaN and dropped -- Section V.A.
    """
    out = pd.DataFrame(index=panel.index.copy())
    close = panel["close"].astype(float)
    volume = panel["volume"].astype(float)

    out["Return"] = close.pct_change()
    out["Log_Return"] = np.log(close).diff()
    out["Volatility_20D"] = out["Return"].rolling(20).std() * np.sqrt(TRADING_DAYS)
    out["Volatility_60D"] = out["Return"].rolling(60).std() * np.sqrt(TRADING_DAYS)
    out["Momentum_20D"] = close / close.shift(20) - 1.0
    out["Momentum_60D"] = close / close.shift(60) - 1.0

    out["MA20"] = close.rolling(20).mean()
    out["MA50"] = close.rolling(50).mean()
    out["MA200"] = close.rolling(200).mean()
    out["Distance_MA50"] = close / out["MA50"] - 1.0
    out["Distance_MA200"] = close / out["MA200"] - 1.0

    # Drawdown from the running maximum, expanding from the first bar we hold.
    # Our history starts 2016-01-04, so the running max is anchored later than
    # the paper's; the 200-day warmup drop covers the re-anchoring (SPY made new
    # highs in July 2016, well inside the discarded window).
    out["Running_Max"] = close.cummax()
    out["Drawdown"] = close / out["Running_Max"] - 1.0

    out["Volume_Ratio"] = volume / volume.rolling(20).mean()

    vix = panel["VIX"].astype(float)
    out["VIX"] = vix
    out["VIX_Change"] = vix.diff()
    out["VIX_Ratio"] = vix / vix.rolling(20).mean()

    out["Treasury_20D_Change"] = panel["DGS10"].astype(float).diff(20)
    out["Oil_Return"] = panel["oil_close"].astype(float).pct_change()

    out["close"] = close
    out["volume"] = volume
    out["oil_close"] = panel["oil_close"].astype(float)
    out["DGS10"] = panel["DGS10"].astype(float)

    # Section V.A: infinities from ratio construction become NaN, then drop.
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out[~out.index.duplicated(keep="last")]
    return out.dropna(subset=list(FEATURES))


def widened_features(frame: pd.DataFrame) -> list[str]:
    """Section XI.J's widened specification: the 15 plus the excluded levels."""
    return list(FEATURES) + [c for c in EXCLUDED if c in frame.columns]

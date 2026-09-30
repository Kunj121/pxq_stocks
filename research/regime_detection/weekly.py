"""The weekly analogue of the paper's daily feature set.

The paper is daily throughout.  Re-asking it weekly is not a matter of changing a
resample rule, because two of its fifteen features are *estimated* quantities
whose precision depends on how many observations go into them, and the rest are
windows expressed in days.  Three decisions, all of them stated here rather than
buried:

**1. Windows are calendar-matched, not literal.**  ``Volatility_20D`` becomes a
four-*week* window, not a twenty-week one, because the paper's 20 days is one
trading month and the weekly reading of "one month" is four weeks.  Taking the
numbers literally instead would push the ``MA200`` warmup to 200 weeks and throw
away 3.8 of the 10.7 years.  Literal windows are available via
``LITERAL_WINDOWS`` for comparison.

**2. Volatility is estimated from DAILY returns, sampled weekly.**  A 4-week
window holds four weekly returns, and a standard deviation from n=4 is nearly
useless -- its own sampling error is ~40% of the estimate.  So ``Volatility_4W``
is the standard deviation of the *daily* returns inside that four-week window
(~20 observations, exactly what the paper used), annualised by sqrt(252), then
read off at the Friday close.  This is causal: at Friday's close every daily
return in the trailing four weeks is known.  The alternative -- std of 4 weekly
returns -- is reported as ``Volatility_4W_naive`` so the cost of the shortcut is
visible instead of assumed.

**3. Volume is summed within the week; every other series is the Friday close.**
A weekly bar's volume is the week's total, so ``Volume_Ratio`` compares one
week's total against the trailing four-week mean of weekly totals.

Usage::

    from research.regime_detection import weekly
    wpanel = weekly.weekly_panel(daily_panel)
    frame = weekly.build_weekly_features(daily_panel)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_WEEKS = 52
TRADING_DAYS = 252

#: Week-ending convention.  Friday, so a week's label describes a completed week.
RULE = "W-FRI"

#: Calendar-matched windows, in weeks.  4w ~ 20 sessions, 12w ~ 60, 10w ~ 50, 40w ~ 200.
WINDOWS = {"vol_fast": 4, "vol_slow": 12, "mom_fast": 4, "mom_slow": 12, "ma_fast": 10, "ma_slow": 40}

#: The paper's numbers read as weeks instead of days.  Costs 160 extra warmup weeks.
LITERAL_WINDOWS = {"vol_fast": 20, "vol_slow": 60, "mom_fast": 20, "mom_slow": 60, "ma_fast": 50, "ma_slow": 200}

#: The fifteen clustering features, in the order Section IV lists their daily twins.
WEEKLY_FEATURES: tuple[str, ...] = (
    "Return",
    "Log_Return",
    "Volatility_4W",
    "Volatility_12W",
    "Momentum_4W",
    "Momentum_12W",
    "Distance_MA10W",
    "Distance_MA40W",
    "Drawdown",
    "Volume_Ratio",
    "VIX",
    "VIX_Change",
    "VIX_Ratio",
    "Treasury_4W_Change",
    "Oil_Return",
)

EXCLUDED: tuple[str, ...] = (
    "close", "volume", "oil_close", "DGS10", "MA10W", "MA40W", "Running_Max",
)


def weekly_panel(panel: pd.DataFrame, rule: str = RULE) -> pd.DataFrame:
    """Resample the daily panel to week ends.

    Levels take the week's last observation; volume takes the week's sum.  Weeks
    with no trading at all (none in this sample) drop out rather than forward
    fill, so a holiday-shortened week is still a real week but an empty one is
    not invented.
    """
    agg = panel.resample(rule).agg(
        {"close": "last", "volume": "sum", "oil_close": "last", "VIX": "last", "DGS10": "last"}
    )
    return agg.dropna(subset=["close"])


def build_weekly_features(
    panel: pd.DataFrame, windows: dict[str, int] | None = None, rule: str = RULE
) -> pd.DataFrame:
    """Weekly feature frame, built from the DAILY panel.

    The daily panel is the input, not the weekly one, because the volatility
    features need daily returns (see the module docstring).  Everything returned
    is indexed by week-ending date and is same-week by construction -- the
    contemporaneous convention the paper uses for clustering.  No shifting
    happens here; the one mandatory lag lives in ``pipeline.tactical_strategy``.
    """
    w = windows or WINDOWS
    wk = weekly_panel(panel, rule)
    out = pd.DataFrame(index=wk.index.copy())

    close, volume = wk["close"].astype(float), wk["volume"].astype(float)
    out["Return"] = close.pct_change()
    out["Log_Return"] = np.log(close).diff()

    # Volatility from daily returns inside the trailing window, read at week end.
    daily_ret = panel["close"].astype(float).pct_change()
    for tag, weeks in (("Volatility_4W", w["vol_fast"]), ("Volatility_12W", w["vol_slow"])):
        rolled = daily_ret.rolling(weeks * 5).std() * np.sqrt(TRADING_DAYS)
        out[tag] = rolled.resample(rule).last().reindex(out.index)
    # the shortcut, kept only so its inadequacy is measurable
    out["Volatility_4W_naive"] = out["Return"].rolling(w["vol_fast"]).std() * np.sqrt(TRADING_WEEKS)

    out["Momentum_4W"] = close / close.shift(w["mom_fast"]) - 1.0
    out["Momentum_12W"] = close / close.shift(w["mom_slow"]) - 1.0

    out["MA10W"] = close.rolling(w["ma_fast"]).mean()
    out["MA40W"] = close.rolling(w["ma_slow"]).mean()
    out["Distance_MA10W"] = close / out["MA10W"] - 1.0
    out["Distance_MA40W"] = close / out["MA40W"] - 1.0

    out["Running_Max"] = close.cummax()
    out["Drawdown"] = close / out["Running_Max"] - 1.0

    out["Volume_Ratio"] = volume / volume.rolling(w["vol_fast"]).mean()

    vix = wk["VIX"].astype(float)
    out["VIX"] = vix
    out["VIX_Change"] = vix.diff()
    out["VIX_Ratio"] = vix / vix.rolling(w["vol_fast"]).mean()

    out["Treasury_4W_Change"] = wk["DGS10"].astype(float).diff(w["vol_fast"])
    out["Oil_Return"] = wk["oil_close"].astype(float).pct_change()

    out["close"], out["volume"] = close, volume
    out["oil_close"], out["DGS10"] = wk["oil_close"].astype(float), wk["DGS10"].astype(float)

    out = out.replace([np.inf, -np.inf], np.nan)
    out = out[~out.index.duplicated(keep="last")]
    return out.dropna(subset=list(WEEKLY_FEATURES))


def widened_features(frame: pd.DataFrame) -> list[str]:
    """Section XI.J's widened specification, weekly."""
    return list(WEEKLY_FEATURES) + [c for c in EXCLUDED if c in frame.columns]


#: The Table 5 test variables, weekly names -- pass to ``pipeline.significance_tests``.
TEST_VARIABLES: list[str] = [
    "Return", "VIX", "Volatility_4W", "Volatility_12W",
    "Drawdown", "Momentum_4W", "Momentum_12W", "Distance_MA40W",
]

#: Momentum column pair for ``pipeline.regime_stats(momentum=...)``.
MOMENTUM: tuple[str, str] = ("Momentum_4W", "Momentum_12W")

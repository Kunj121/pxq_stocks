"""Classic dual moving-average trend following."""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from backtest.data import BarPanel
from backtest.engine import Context
from backtest.strategies.base import Strategy, equal_weight


class SmaCross(Strategy):
    """Hold a symbol while its fast SMA is above its slow SMA.

    Capital is split equally across whichever symbols are currently in an uptrend,
    so a 3-symbol universe with 1 signal on puts 100% into that one name (subject
    to the engine's ``max_leverage``). With ``long_short=True``, symbols in a
    downtrend are shorted instead of ignored.

    Parameters
    ----------
    fast, slow:
        Lookback windows in bars. ``fast`` must be shorter than ``slow``.
    long_short:
        Short the losers as well as holding the winners.
    gross:
        Total gross exposure to target when at least one signal is on.
    """

    def __init__(
        self, fast: int = 20, slow: int = 100, long_short: bool = False, gross: float = 1.0
    ) -> None:
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be shorter than slow ({slow})")
        self.fast = fast
        self.slow = slow
        self.long_short = long_short
        self.gross = gross
        self.warmup = slow
        self._fast_ma: pd.DataFrame | None = None
        self._slow_ma: pd.DataFrame | None = None

    def prepare(self, panel: BarPanel) -> None:
        # Rolling means are causal, so precomputing the whole frame leaks nothing:
        # row t only ever depends on rows <= t.
        closes = panel.close
        self._fast_ma = closes.rolling(self.fast, min_periods=self.fast).mean()
        self._slow_ma = closes.rolling(self.slow, min_periods=self.slow).mean()

    def target_weights(self, ctx: Context) -> Mapping[str, float]:
        fast = self._fast_ma.iloc[ctx.i]
        slow = self._slow_ma.iloc[ctx.i]
        valid = fast.notna() & slow.notna()

        longs = [s for s in ctx.symbols if valid.get(s, False) and fast[s] > slow[s]]
        if not self.long_short:
            return equal_weight(longs, self.gross)

        shorts = [s for s in ctx.symbols if valid.get(s, False) and fast[s] <= slow[s]]
        weights: dict[str, float] = {}
        if longs:
            weights |= equal_weight(longs, self.gross / 2)
        if shorts:
            weights |= {s: -w for s, w in equal_weight(shorts, self.gross / 2).items()}
        return weights

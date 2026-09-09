"""Short-horizon cross-sectional mean reversion."""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from backtest.data import BarPanel
from backtest.engine import Context
from backtest.strategies.base import Strategy, equal_weight


class MeanReversion(Strategy):
    """Buy what has fallen furthest below its own recent mean.

    Each bar, every symbol gets a z-score of its close against its own trailing
    ``lookback``-bar mean and standard deviation. Symbols below ``-entry_z`` are
    bought; a position is held until its z-score climbs back above ``exit_z``,
    which keeps turnover well below a naive every-bar reshuffle.

    Parameters
    ----------
    lookback:
        Bars in the mean/stdev window.
    entry_z:
        How far below the mean a symbol must fall to be bought (positive number).
    exit_z:
        Z-score at which a held position is released.
    max_positions:
        Cap on simultaneous holdings; the most oversold names win.
    gross:
        Total gross exposure when fully invested.
    """

    def __init__(
        self,
        lookback: int = 20,
        entry_z: float = 1.0,
        exit_z: float = 0.0,
        max_positions: int = 5,
        gross: float = 1.0,
    ) -> None:
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.max_positions = max_positions
        self.gross = gross
        self.warmup = lookback
        self._z: pd.DataFrame | None = None

    def prepare(self, panel: BarPanel) -> None:
        closes = panel.close
        mean = closes.rolling(self.lookback, min_periods=self.lookback).mean()
        std = closes.rolling(self.lookback, min_periods=self.lookback).std(ddof=1)
        self._z = ((closes - mean) / std.replace(0.0, np.nan))

    def target_weights(self, ctx: Context) -> Mapping[str, float]:
        z = self._z.iloc[ctx.i].dropna()
        if z.empty:
            return {}

        held = {s for s, shares in ctx.positions.items() if shares != 0}
        # Keep holdings that have not yet reverted far enough to release.
        keep = {s for s in held if s in z.index and z[s] < self.exit_z}

        candidates = z[z < -self.entry_z].sort_values()
        room = max(0, self.max_positions - len(keep))
        new = [s for s in candidates.index if s not in keep][:room]

        selected = sorted(keep | set(new))
        return equal_weight(selected, self.gross)

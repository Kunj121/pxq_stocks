"""Buy and hold — the benchmark every other strategy has to beat."""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from backtest.engine import HOLD, Context
from backtest.strategies.base import Strategy, equal_weight


class BuyAndHold(Strategy):
    """Buy an equal-weighted basket once and sit on it.

    With ``rebalance=True`` the engine is asked for equal weight on every bar, so
    the book is trimmed back to equal weight as prices drift apart — a different
    (and more expensive) strategy than true buy-and-hold, which is the default.
    """

    def __init__(self, gross: float = 1.0, rebalance: bool = False) -> None:
        self.gross = gross
        self.rebalance = rebalance
        self._bought = False

    def target_weights(self, ctx: Context) -> Mapping[str, float] | object:
        if self._bought and not self.rebalance:
            return HOLD
        # Only buy names that actually have a print on this bar.
        tradable = [s for s in ctx.symbols if pd.notna(ctx.prices.get(s))]
        if not tradable:
            return HOLD
        self._bought = True
        return equal_weight(tradable, self.gross)

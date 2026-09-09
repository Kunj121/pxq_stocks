"""The strategy interface.

A strategy answers one question, once per bar: *given everything known at this
bar's close, what should the portfolio look like?* The answer is a mapping of
symbol to target weight, where weight is a signed fraction of equity::

    {"AAPL": 0.5, "MSFT": 0.5}   # 50/50 long
    {"AAPL": 1.0, "MSFT": -1.0}  # long/short pair, capped by max_leverage
    {}                            # flat: sell everything
    HOLD                          # no change at all; place no orders

Missing symbols are treated as zero, so ``{"AAPL": 1.0}`` in a three-symbol
universe means "all in on AAPL, flat the other two". The engine handles the
one-bar execution delay, share sizing, costs, and the leverage cap — a strategy
never touches cash or share counts.

Note the difference between ``{}`` and ``HOLD``: the first is an instruction to
go flat, the second an instruction to do nothing. Returning ``HOLD`` on bars
where nothing has changed is how a strategy avoids paying slippage to rebalance
away harmless price drift.
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

from backtest.data import BarPanel
from backtest.engine import HOLD, Context


class Strategy:
    """Base class. Subclass and implement :meth:`target_weights`.

    Attributes
    ----------
    warmup:
        Bars to skip before the first decision. Set it to the longest lookback
        the strategy needs, so its first trade is made on a full window rather
        than a truncated one.
    name:
        Label used in reports. Defaults to the class name plus its parameters.
    """

    warmup: int = 0

    def prepare(self, panel: BarPanel) -> None:
        """Optional hook: precompute indicators once, before the loop starts.

        Anything computed here is visible at every bar, so only use it for
        transformations that are causal (rolling, shifted) — never for
        whole-sample statistics like a full-history mean or z-score, which would
        leak the future into every decision.
        """

    def target_weights(self, ctx: Context) -> Mapping[str, float] | object:
        """Return target weights for this bar, or :data:`~backtest.engine.HOLD`."""
        raise NotImplementedError

    @property
    def name(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in sorted(self.params().items()))
        return f"{type(self).__name__}({params})" if params else type(self).__name__

    def params(self) -> dict[str, object]:
        """Public attributes, for report labelling."""
        return {
            k: v
            for k, v in vars(self).items()
            if not k.startswith("_") and isinstance(v, (int, float, str, bool))
        }

    def __repr__(self) -> str:
        return f"<{self.name}>"


def equal_weight(symbols: list[str], gross: float = 1.0) -> dict[str, float]:
    """Split ``gross`` evenly across ``symbols``. Empty list -> flat."""
    if not symbols:
        return {}
    return {s: gross / len(symbols) for s in symbols}


def rolling_mean(series: pd.Series, window: int) -> float:
    """Last value of a rolling mean, or NaN if the window is not full yet."""
    if len(series) < window:
        return float("nan")
    return float(series.iloc[-window:].mean())

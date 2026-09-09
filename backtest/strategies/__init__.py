"""Bundled strategies, plus a name registry for the CLI.

Add your own by subclassing :class:`~backtest.strategies.base.Strategy` and
calling :func:`register`.
"""

from __future__ import annotations

from backtest.strategies.base import Strategy, equal_weight
from backtest.strategies.buy_and_hold import BuyAndHold
from backtest.strategies.mean_reversion import MeanReversion
from backtest.strategies.sma_cross import SmaCross

_REGISTRY: dict[str, type[Strategy]] = {}


def register(name: str, cls: type[Strategy]) -> type[Strategy]:
    """Make ``cls`` available to the CLI under ``name``."""
    _REGISTRY[name.lower()] = cls
    return cls


def get_strategy(name: str) -> type[Strategy]:
    """Look up a registered strategy class by name."""
    try:
        return _REGISTRY[name.lower()]
    except KeyError:
        raise KeyError(
            f"Unknown strategy {name!r}. Available: {', '.join(list_strategies())}"
        ) from None


def list_strategies() -> list[str]:
    return sorted(_REGISTRY)


register("buy_and_hold", BuyAndHold)
register("sma_cross", SmaCross)
register("mean_reversion", MeanReversion)

__all__ = [
    "BuyAndHold",
    "MeanReversion",
    "SmaCross",
    "Strategy",
    "equal_weight",
    "get_strategy",
    "list_strategies",
    "register",
]

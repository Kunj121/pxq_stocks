"""Backtesting toolkit for pxq_stocks.

All market data comes from the Alpaca Market Data API — nothing else. See
``backtest/README.md`` for the full guide.

Typical use::

    from backtest import load_bars, run, SmaCross

    panel = load_bars(["AAPL", "MSFT"], "2020-01-01", "2024-12-31")
    result = run(SmaCross(fast=20, slow=100), panel)
    print(result.summary())
"""

from backtest.config import Settings, get_settings
from backtest.data import BarPanel, load_bars, load_calendar
from backtest.engine import HOLD, BacktestResult, Context, Costs, run
from backtest.metrics import performance_stats
from backtest.strategies import (
    BuyAndHold,
    MeanReversion,
    SmaCross,
    Strategy,
    get_strategy,
    list_strategies,
)

__all__ = [
    "HOLD",
    "BacktestResult",
    "BarPanel",
    "BuyAndHold",
    "Context",
    "Costs",
    "MeanReversion",
    "Settings",
    "SmaCross",
    "Strategy",
    "get_settings",
    "get_strategy",
    "list_strategies",
    "load_bars",
    "load_calendar",
    "performance_stats",
    "run",
]

__version__ = "0.1.0"

"""Synthetic fixtures so the test suite never calls Alpaca."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.data import BAR_COLUMNS, MARKET_TZ, BarPanel, TimeFrame


def make_panel(
    prices: dict[str, list[float]], start: str = "2024-01-02", timeframe: str = "1Day"
) -> BarPanel:
    """Build a BarPanel from close prices; open == previous close, flat bars."""
    length = len(next(iter(prices.values())))
    index = pd.date_range(start, periods=length, freq="B", tz=MARKET_TZ)
    rows = []
    for symbol, closes in prices.items():
        for i, (timestamp, close) in enumerate(zip(index, closes)):
            open_ = closes[i - 1] if i else close
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "open": open_,
                    "high": max(open_, close),
                    "low": min(open_, close),
                    "close": close,
                    "volume": 1_000_000.0,
                    "trade_count": 1_000.0,
                    "vwap": (open_ + close) / 2,
                }
            )
    frame = pd.DataFrame(rows).set_index(["timestamp", "symbol"])[BAR_COLUMNS]
    return BarPanel(frame.astype(float), TimeFrame.parse(timeframe), feed="test")


@pytest.fixture
def flat_panel() -> BarPanel:
    """One symbol, constant price — any strategy should end where it started."""
    return make_panel({"AAA": [100.0] * 20})


@pytest.fixture
def trend_panel() -> BarPanel:
    """One symbol compounding at 1% per bar."""
    closes = [100.0 * (1.01**i) for i in range(60)]
    return make_panel({"AAA": closes})


@pytest.fixture
def two_symbol_panel() -> BarPanel:
    rng = np.random.default_rng(7)
    a = 100 * np.cumprod(1 + rng.normal(0.001, 0.01, 120))
    b = 50 * np.cumprod(1 + rng.normal(0.000, 0.02, 120))
    return make_panel({"AAA": list(a), "BBB": list(b)})

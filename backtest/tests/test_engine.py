"""Engine behaviour: no look-ahead, correct accounting, costs applied."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.engine import HOLD, Context, Costs, run
from backtest.strategies import BuyAndHold, SmaCross
from backtest.strategies.base import Strategy
from backtest.tests.conftest import make_panel


class AlwaysFull(Strategy):
    """100% long the first symbol, every bar."""

    def target_weights(self, ctx: Context):
        return {ctx.symbols[0]: 1.0}


class Flat(Strategy):
    def target_weights(self, ctx: Context):
        return {}


class RecordsHistory(Strategy):
    """Captures the history it was shown, so we can assert on look-ahead."""

    def __init__(self) -> None:
        self.seen: list[pd.Timestamp] = []

    def target_weights(self, ctx: Context):
        self.seen.append(ctx.history("close").index[-1])
        assert ctx.history("close").index[-1] == ctx.timestamp
        return {}


def test_flat_strategy_preserves_cash(flat_panel):
    result = run(Flat(), flat_panel, initial_cash=50_000)
    assert result.equity.iloc[-1] == pytest.approx(50_000)
    assert result.fills.empty


def test_history_never_extends_past_current_bar(trend_panel):
    strategy = RecordsHistory()
    run(strategy, trend_panel)
    # A decision is made on every bar except the last (nothing left to fill on).
    assert strategy.seen == list(trend_panel.timestamps[:-1])


def test_fills_happen_at_the_next_bar_open(trend_panel):
    result = run(AlwaysFull(), trend_panel, costs=Costs(slippage_bps=0))
    first = result.fills.iloc[0]
    # Decision on bar 0's close -> fill at bar 1's open.
    assert first["timestamp"] == trend_panel.timestamps[1]
    expected_open = trend_panel.open.iloc[1]["AAA"]
    assert first["reference_price"] == pytest.approx(expected_open)


def test_no_trade_on_the_first_bar(trend_panel):
    result = run(AlwaysFull(), trend_panel)
    assert result.fills["timestamp"].min() > trend_panel.timestamps[0]


def test_full_investment_tracks_the_asset(trend_panel):
    """Fully invested from bar 1 -> returns match the asset from bar 1 onward."""
    result = run(AlwaysFull(), trend_panel, costs=Costs(slippage_bps=0))
    closes = trend_panel.close["AAA"]
    asset_return = closes.iloc[-1] / closes.iloc[1] - 1
    strategy_return = result.equity.iloc[-1] / result.equity.iloc[1] - 1
    assert strategy_return == pytest.approx(asset_return, rel=1e-6)


def test_slippage_is_always_adverse(trend_panel):
    result = run(AlwaysFull(), trend_panel, costs=Costs(slippage_bps=50))
    buys = result.fills[result.fills["shares"] > 0]
    assert (buys["price"] > buys["reference_price"]).all()
    assert result.total_slippage > 0


def test_commission_reduces_equity(trend_panel):
    free = run(AlwaysFull(), trend_panel, costs=Costs(slippage_bps=0))
    paid = run(
        AlwaysFull(), trend_panel, costs=Costs(slippage_bps=0, commission_per_share=0.01)
    )
    assert paid.equity.iloc[-1] < free.equity.iloc[-1]
    assert paid.total_commission > 0


def test_cash_plus_positions_equals_equity(two_symbol_panel):
    result = run(BuyAndHold(rebalance=True), two_symbol_panel)
    closes = two_symbol_panel.close
    for timestamp in result.equity.index:
        holdings = (result.positions.loc[timestamp] * closes.loc[timestamp]).sum()
        assert result.equity[timestamp] == pytest.approx(
            result.cash[timestamp] + holdings, rel=1e-9
        )


class Overweight(Strategy):
    """Asks for 100% of equity in every symbol, i.e. N-times leverage."""

    def target_weights(self, ctx):
        return {s: 1.0 for s in ctx.symbols}


@pytest.mark.parametrize("cap", [0.5, 1.0, 2.0])
def test_leverage_cap_scales_targets(two_symbol_panel, cap):
    result = run(Overweight(), two_symbol_panel, max_leverage=cap)
    # Exposure is set at the open and measured at the close, so intra-bar price
    # drift lets it wander a little past the cap before the next rebalance.
    assert result.gross_exposure.max() == pytest.approx(cap, rel=0.1)
    assert result.gross_exposure.iloc[-1] > cap * 0.85


def test_unknown_symbol_is_rejected(flat_panel):
    class Typo(Strategy):
        def target_weights(self, ctx):
            return {"NOPE": 1.0}

    with pytest.raises(KeyError, match="NOPE"):
        run(Typo(), flat_panel)


def test_whole_share_mode_never_holds_fractions(trend_panel):
    result = run(AlwaysFull(), trend_panel, allow_fractional=False)
    assert (result.positions["AAA"] % 1 == 0).all()


def test_warmup_delays_the_first_decision():
    closes = [100 + i for i in range(40)]
    panel = make_panel({"AAA": closes})
    strategy = SmaCross(fast=5, slow=10)
    result = run(strategy, panel)
    # warmup == slow == 10, decision on bar 10 -> first fill on bar 11.
    assert result.fills["timestamp"].min() == panel.timestamps[11]


def test_missing_prices_do_not_trade_or_crash():
    panel = make_panel({"AAA": [100.0] * 10, "BBB": [50.0] * 10})
    frame = panel.frame.copy()
    frame.loc[(panel.timestamps[4], "BBB"), ["open", "close"]] = float("nan")
    panel.frame = frame.sort_index()

    result = run(BuyAndHold(rebalance=True), panel)
    assert result.equity.notna().all()
    assert not result.fills["price"].isna().any()


def test_buy_and_hold_without_rebalancing_trades_once(two_symbol_panel):
    result = run(BuyAndHold(rebalance=False), two_symbol_panel)
    assert result.fills["timestamp"].nunique() == 1


def test_rebalancing_buy_and_hold_keeps_trading(two_symbol_panel):
    result = run(BuyAndHold(rebalance=True), two_symbol_panel)
    assert result.fills["timestamp"].nunique() > 1


def test_hold_places_no_orders(two_symbol_panel):
    class NeverTrades(Strategy):
        def target_weights(self, ctx):
            return HOLD

    result = run(NeverTrades(), two_symbol_panel, initial_cash=10_000)
    assert result.fills.empty
    assert result.equity.iloc[-1] == pytest.approx(10_000)


def test_empty_mapping_liquidates(two_symbol_panel):
    class BuyThenFlatten(Strategy):
        def target_weights(self, ctx):
            return {ctx.symbols[0]: 1.0} if ctx.i < 5 else {}

    result = run(BuyThenFlatten(), two_symbol_panel)
    assert result.positions.iloc[-1].abs().sum() == pytest.approx(0.0)


def test_returning_none_is_an_error(two_symbol_panel):
    class Forgetful(Strategy):
        def target_weights(self, ctx):
            return None

    with pytest.raises(TypeError, match="returned None"):
        run(Forgetful(), two_symbol_panel)


def test_short_position_profits_when_price_falls():
    class AlwaysShort(Strategy):
        def target_weights(self, ctx):
            return {"AAA": -1.0}

    closes = [100.0 * (0.99**i) for i in range(30)]
    panel = make_panel({"AAA": closes})
    result = run(AlwaysShort(), panel, costs=Costs(slippage_bps=0))
    assert result.equity.iloc[-1] > result.equity.iloc[0]
    assert (result.positions["AAA"] < 0).any()


def test_too_few_bars_raises():
    panel = make_panel({"AAA": [100.0]})
    with pytest.raises(ValueError, match="at least 2 bars"):
        run(Flat(), panel)


def test_financing_charges_accrue_on_borrowed_cash(trend_panel):
    free = run(AlwaysFull(), trend_panel, costs=Costs(slippage_bps=0))
    # A cash rate on a fully invested book leaves ~no idle cash to earn on,
    # so use the flat-strategy case where everything sits in cash.
    earning = run(Flat(), trend_panel, costs=Costs(cash_rate=0.05))
    assert earning.equity.iloc[-1] > free.initial_cash

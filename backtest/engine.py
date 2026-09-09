"""The backtest engine.

Model
-----
A strategy sees the world **at the close of bar t** and its orders fill **at the
open of bar t+1**. That one-bar delay is enforced by the loop itself, not by
strategy discipline, which removes the most common source of look-ahead bias.

Strategies express intent as *target portfolio weights* — ``{"AAPL": 0.5}`` means
"hold 50% of equity in AAPL". The engine turns the delta between current and
target weights into share trades, applies slippage and commission, and moves
cash. Weights are signed: negative is short. Returning :data:`HOLD` instead of a
mapping leaves the book alone, which is how a strategy avoids paying to rebalance
away price drift it does not care about.

What is modelled
----------------
* Fractional or whole shares.
* Per-share and basis-point commissions, with a per-order minimum.
* Slippage as a fixed spread cost in basis points, always adverse.
* Interest earned on idle cash and charged on debit balances / short proceeds.
* Gross-exposure cap (leverage) applied by scaling the target vector.

What is not
-----------
Market impact beyond fixed slippage, partial fills, borrow availability for
shorts, intraday stops, or taxes. See the README's "Known limitations".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

import numpy as np
import pandas as pd

from backtest.data import BarPanel, TimeFrame
from backtest.metrics import performance_stats

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from backtest.strategies.base import Strategy

log = logging.getLogger(__name__)


class _Hold:
    """Sentinel type for :data:`HOLD`."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "HOLD"


#: Returned by a strategy that wants no change at all this bar. Distinct from an
#: empty mapping, which means "target zero weight everywhere", i.e. liquidate.
HOLD = _Hold()


@dataclass(frozen=True)
class Costs:
    """Transaction and financing cost model.

    Defaults describe a retail account at a zero-commission broker (Alpaca):
    no commission, one basis point of slippage per trade, no financing.
    """

    #: Basis points of the fill price given up to the spread/impact, per trade.
    slippage_bps: float = 1.0
    #: Dollars per share, e.g. 0.005 for a per-share pricing schedule.
    commission_per_share: float = 0.0
    #: Basis points of notional.
    commission_bps: float = 0.0
    #: Floor on commission for any non-zero order.
    min_commission: float = 0.0
    #: Annualised rate earned on positive cash balances.
    cash_rate: float = 0.0
    #: Annualised rate charged on debit balances and short market value.
    borrow_rate: float = 0.0

    def commission(self, shares: float, price: float) -> float:
        if shares == 0:
            return 0.0
        fee = abs(shares) * self.commission_per_share
        fee += abs(shares * price) * self.commission_bps / 10_000
        return max(fee, self.min_commission)

    def fill_price(self, price: float, side: int) -> float:
        """Adverse-fill price: buys pay up, sells receive less."""
        return price * (1 + side * self.slippage_bps / 10_000)


@dataclass
class Fill:
    """One executed trade."""

    timestamp: pd.Timestamp
    symbol: str
    shares: float  # signed: positive buy, negative sell
    price: float  # slippage-adjusted
    reference_price: float  # the bar open before slippage
    commission: float

    @property
    def notional(self) -> float:
        return self.shares * self.price

    @property
    def slippage_cost(self) -> float:
        return abs(self.shares) * abs(self.price - self.reference_price)

    @property
    def side(self) -> str:
        return "buy" if self.shares > 0 else "sell"


@dataclass
class Context:
    """What a strategy is allowed to see at a decision point.

    History accessors are hard-capped at the current bar, so a strategy cannot
    read the future even by accident.
    """

    timestamp: pd.Timestamp
    #: Integer position of ``timestamp`` in the panel's timeline.
    i: int
    symbols: list[str]
    timeframe: TimeFrame
    cash: float
    equity: float
    positions: dict[str, float]
    _fields: dict[str, pd.DataFrame] = field(repr=False, default_factory=dict)

    def history(self, field_name: str = "close", lookback: int | None = None) -> pd.DataFrame:
        """``timestamp x symbol`` history up to and including the current bar."""
        frame = self._fields[field_name]
        start = 0 if lookback is None else max(0, self.i + 1 - lookback)
        return frame.iloc[start : self.i + 1]

    def series(self, symbol: str, field_name: str = "close", lookback: int | None = None) -> pd.Series:
        """One symbol's history up to and including the current bar."""
        return self.history(field_name, lookback)[symbol]

    @property
    def prices(self) -> pd.Series:
        """Current bar's closing prices."""
        return self._fields["close"].iloc[self.i]

    @property
    def weights(self) -> pd.Series:
        """Current portfolio weights (market value / equity)."""
        if self.equity == 0:
            return pd.Series(0.0, index=self.symbols)
        prices = self.prices
        values = {s: self.positions.get(s, 0.0) * prices.get(s, np.nan) for s in self.symbols}
        return pd.Series(values).fillna(0.0) / self.equity


class BacktestResult:
    """Everything a run produced, plus the reporting helpers."""

    def __init__(
        self,
        equity: pd.Series,
        cash: pd.Series,
        positions: pd.DataFrame,
        weights: pd.DataFrame,
        fills: pd.DataFrame,
        panel: BarPanel,
        strategy_name: str,
        costs: Costs,
        initial_cash: float,
    ) -> None:
        self.equity = equity
        self.cash = cash
        self.positions = positions
        self.weights = weights
        self.fills = fills
        self.panel = panel
        self.strategy_name = strategy_name
        self.costs = costs
        self.initial_cash = initial_cash

    @property
    def returns(self) -> pd.Series:
        return self.equity.pct_change().fillna(0.0)

    @property
    def drawdown(self) -> pd.Series:
        peak = self.equity.cummax()
        return self.equity / peak - 1.0

    @property
    def gross_exposure(self) -> pd.Series:
        return self.weights.abs().sum(axis=1)

    @property
    def net_exposure(self) -> pd.Series:
        return self.weights.sum(axis=1)

    @property
    def turnover(self) -> pd.Series:
        """Traded notional per bar, as a fraction of equity."""
        if self.fills.empty:
            return pd.Series(0.0, index=self.equity.index)
        traded = self.fills.assign(gross=self.fills["shares"].abs() * self.fills["price"])
        by_bar = traded.groupby("timestamp")["gross"].sum()
        return (by_bar / self.equity).reindex(self.equity.index).fillna(0.0)

    @property
    def total_commission(self) -> float:
        return float(self.fills["commission"].sum()) if not self.fills.empty else 0.0

    @property
    def total_slippage(self) -> float:
        return float(self.fills["slippage_cost"].sum()) if not self.fills.empty else 0.0

    def stats(self, benchmark: pd.Series | None = None) -> dict[str, float]:
        """Headline performance statistics. See :mod:`backtest.metrics`."""
        out = performance_stats(
            self.returns,
            periods_per_year=self.panel.timeframe.periods_per_year,
            benchmark=benchmark,
        )
        out["trades"] = float(len(self.fills))
        out["avg_turnover"] = float(self.turnover.mean())
        out["total_commission"] = self.total_commission
        out["total_slippage"] = self.total_slippage
        out["final_equity"] = float(self.equity.iloc[-1])
        return out

    def summary(self, benchmark: pd.Series | None = None) -> str:
        """A human-readable block, ready to print."""
        stats = self.stats(benchmark)
        start, end = self.equity.index[0], self.equity.index[-1]
        pct = lambda v: f"{v * 100:,.2f}%"  # noqa: E731
        lines = [
            f"{self.strategy_name}  ({start.date()} -> {end.date()}, "
            f"{len(self.equity)} bars @ {self.panel.timeframe})",
            f"  symbols          {', '.join(self.panel.symbols[:12])}"
            + (" ..." if len(self.panel.symbols) > 12 else ""),
            f"  data feed        {self.panel.feed or 'crypto'} (Alpaca)",
            "",
            f"  start equity     ${self.initial_cash:,.2f}",
            f"  final equity     ${stats['final_equity']:,.2f}",
            f"  total return     {pct(stats['total_return'])}",
            f"  CAGR             {pct(stats['cagr'])}",
            f"  volatility       {pct(stats['volatility'])}",
            f"  Sharpe           {stats['sharpe']:,.2f}",
            f"  Sortino          {stats['sortino']:,.2f}",
            f"  max drawdown     {pct(stats['max_drawdown'])}",
            f"  Calmar           {stats['calmar']:,.2f}",
            f"  win rate         {pct(stats['win_rate'])}",
            f"  best / worst bar {pct(stats['best_period'])} / {pct(stats['worst_period'])}",
            "",
            f"  fills            {int(stats['trades']):,}",
            f"  avg turnover     {pct(stats['avg_turnover'])} of equity per bar",
            f"  commission paid  ${stats['total_commission']:,.2f}",
            f"  slippage paid    ${stats['total_slippage']:,.2f}",
        ]
        if benchmark is not None:
            lines += [
                "",
                f"  benchmark return {pct(stats['benchmark_return'])}",
                f"  alpha (ann.)     {pct(stats['alpha'])}",
                f"  beta             {stats['beta']:,.2f}",
            ]
        return "\n".join(lines)

    def to_frame(self) -> pd.DataFrame:
        """Per-bar equity, cash, drawdown, and exposure — one tidy table."""
        return pd.DataFrame(
            {
                "equity": self.equity,
                "cash": self.cash,
                "returns": self.returns,
                "drawdown": self.drawdown,
                "gross_exposure": self.gross_exposure,
                "net_exposure": self.net_exposure,
                "turnover": self.turnover,
            }
        )

    def __repr__(self) -> str:
        stats = self.stats()
        return (
            f"<BacktestResult {self.strategy_name}: "
            f"return={stats['total_return']:.2%} sharpe={stats['sharpe']:.2f} "
            f"maxdd={stats['max_drawdown']:.2%}>"
        )


def run(
    strategy: "Strategy",
    panel: BarPanel,
    *,
    initial_cash: float = 100_000.0,
    costs: Costs | None = None,
    allow_fractional: bool = True,
    max_leverage: float = 1.0,
    min_trade_notional: float = 1.0,
) -> BacktestResult:
    """Run ``strategy`` over ``panel``.

    Parameters
    ----------
    strategy:
        Any object with a ``target_weights(ctx)`` method. See
        :class:`backtest.strategies.base.Strategy`.
    panel:
        Bars from :func:`backtest.data.load_bars`.
    initial_cash:
        Starting equity, all in cash.
    costs:
        Transaction cost model. Defaults to :class:`Costs` (1bp slippage).
    allow_fractional:
        ``False`` rounds every order toward zero to whole shares.
    max_leverage:
        Cap on gross exposure (sum of absolute weights). The target vector is
        scaled down proportionally when it exceeds this.
    min_trade_notional:
        Orders smaller than this dollar amount are skipped, which stops tiny
        rebalancing dust from dominating the fill log.

    Returns
    -------
    BacktestResult
    """
    costs = costs or Costs()
    timestamps = panel.timestamps
    if len(timestamps) < 2:
        raise ValueError(
            f"Need at least 2 bars to backtest, got {len(timestamps)}. Widen the date range."
        )

    symbols = panel.symbols
    fields = {name: panel.field(name).reindex(columns=symbols) for name in ("open", "close")}
    opens, closes = fields["open"], fields["close"]

    if hasattr(strategy, "prepare"):
        strategy.prepare(panel)
    warmup = int(getattr(strategy, "warmup", 0))

    cash = float(initial_cash)
    positions: dict[str, float] = {s: 0.0 for s in symbols}
    last_price: dict[str, float] = {}
    pending: dict[str, float] | None = None

    equity_history: list[float] = []
    cash_history: list[float] = []
    position_history: list[dict[str, float]] = []
    weight_history: list[dict[str, float]] = []
    fills: list[Fill] = []

    periods_per_year = panel.timeframe.periods_per_year
    financing_dt = 1.0 / periods_per_year if periods_per_year else 0.0

    for i, timestamp in enumerate(timestamps):
        open_row = opens.iloc[i]
        close_row = closes.iloc[i]

        # 1. Execute the previous bar's decision at this bar's open.
        if pending is not None:
            cash = _execute(
                pending,
                timestamp=timestamp,
                open_row=open_row,
                positions=positions,
                cash=cash,
                last_price=last_price,
                costs=costs,
                allow_fractional=allow_fractional,
                min_trade_notional=min_trade_notional,
                fills=fills,
            )
            pending = None

        # 2. Accrue financing on the balance carried through this bar.
        if financing_dt and (costs.cash_rate or costs.borrow_rate):
            cash += _financing(cash, positions, last_price, costs, financing_dt)

        # 3. Mark to market on the close.
        for symbol in symbols:
            price = close_row.get(symbol, np.nan)
            if pd.notna(price):
                last_price[symbol] = float(price)
        equity = cash + sum(
            shares * last_price.get(symbol, 0.0) for symbol, shares in positions.items()
        )

        equity_history.append(equity)
        cash_history.append(cash)
        position_history.append(dict(positions))
        weight_history.append(
            {
                s: (positions[s] * last_price.get(s, 0.0) / equity) if equity else 0.0
                for s in symbols
            }
        )

        # 4. Decide, for execution at the next bar's open. Nothing to do on the
        #    final bar — there is no next open to fill against.
        if i >= warmup and i < len(timestamps) - 1:
            ctx = Context(
                timestamp=timestamp,
                i=i,
                symbols=symbols,
                timeframe=panel.timeframe,
                cash=cash,
                equity=equity,
                positions=dict(positions),
                _fields=fields,
            )
            targets = strategy.target_weights(ctx)
            if targets is None:
                raise TypeError(
                    f"{type(strategy).__name__}.target_weights returned None. Return a "
                    "mapping of symbol -> weight, an empty mapping to go flat, or "
                    "backtest.engine.HOLD to leave the book untouched."
                )
            pending = None if targets is HOLD else _normalise(targets, symbols, max_leverage)

    index = pd.DatetimeIndex(timestamps, name="timestamp")
    return BacktestResult(
        equity=pd.Series(equity_history, index=index, name="equity"),
        cash=pd.Series(cash_history, index=index, name="cash"),
        positions=pd.DataFrame(position_history, index=index).fillna(0.0),
        weights=pd.DataFrame(weight_history, index=index).fillna(0.0),
        fills=_fills_frame(fills),
        panel=panel,
        strategy_name=getattr(strategy, "name", type(strategy).__name__),
        costs=costs,
        initial_cash=initial_cash,
    )


def _normalise(
    targets: Mapping[str, float] | pd.Series,
    symbols: Sequence[str],
    max_leverage: float,
) -> dict[str, float]:
    """Clean a strategy's output: known symbols only, NaNs zeroed, leverage capped."""
    if isinstance(targets, pd.Series):
        targets = targets.to_dict()

    unknown = set(targets) - set(symbols)
    if unknown:
        raise KeyError(
            f"Strategy targeted symbols not in the panel: {sorted(unknown)}. "
            f"Panel holds {list(symbols)}."
        )

    weights = {s: float(targets.get(s, 0.0) or 0.0) for s in symbols}
    weights = {s: (0.0 if not np.isfinite(w) else w) for s, w in weights.items()}

    gross = sum(abs(w) for w in weights.values())
    if max_leverage > 0 and gross > max_leverage:
        scale = max_leverage / gross
        weights = {s: w * scale for s, w in weights.items()}
    return weights


def _execute(
    targets: dict[str, float],
    *,
    timestamp: pd.Timestamp,
    open_row: pd.Series,
    positions: dict[str, float],
    cash: float,
    last_price: dict[str, float],
    costs: Costs,
    allow_fractional: bool,
    min_trade_notional: float,
    fills: list[Fill],
) -> float:
    """Trade toward ``targets`` at this bar's open. Returns the new cash balance."""
    # Value the book at the open, since that is when we transact.
    exec_price: dict[str, float] = {}
    for symbol in positions:
        price = open_row.get(symbol, np.nan)
        exec_price[symbol] = float(price) if pd.notna(price) else last_price.get(symbol, np.nan)

    equity = cash + sum(
        shares * exec_price[symbol]
        for symbol, shares in positions.items()
        if np.isfinite(exec_price.get(symbol, np.nan))
    )
    if equity <= 0:
        log.warning("Equity is %.2f at %s; skipping rebalance.", equity, timestamp)
        return cash

    for symbol, target_weight in targets.items():
        price = exec_price.get(symbol, np.nan)
        if not np.isfinite(price) or price <= 0:
            # No open print for this bar (halt, not yet listed, delisted). Hold.
            continue

        target_shares = target_weight * equity / price
        if not allow_fractional:
            target_shares = float(np.trunc(target_shares))

        delta = target_shares - positions[symbol]
        if abs(delta * price) < min_trade_notional:
            continue

        side = 1 if delta > 0 else -1
        fill_price = costs.fill_price(price, side)
        commission = costs.commission(delta, fill_price)

        positions[symbol] += delta
        cash -= delta * fill_price + commission
        fills.append(
            Fill(
                timestamp=timestamp,
                symbol=symbol,
                shares=delta,
                price=fill_price,
                reference_price=price,
                commission=commission,
            )
        )
    return cash


def _financing(
    cash: float,
    positions: dict[str, float],
    last_price: dict[str, float],
    costs: Costs,
    dt: float,
) -> float:
    """Interest on idle cash, less borrow on debits and short market value."""
    interest = 0.0
    if cash > 0:
        interest += cash * costs.cash_rate * dt
    else:
        interest += cash * costs.borrow_rate * dt  # cash is negative -> a charge
    short_value = sum(
        -shares * last_price.get(symbol, 0.0)
        for symbol, shares in positions.items()
        if shares < 0
    )
    interest -= short_value * costs.borrow_rate * dt
    return interest


def _fills_frame(fills: list[Fill]) -> pd.DataFrame:
    columns = [
        "timestamp",
        "symbol",
        "side",
        "shares",
        "price",
        "reference_price",
        "notional",
        "commission",
        "slippage_cost",
    ]
    if not fills:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "timestamp": f.timestamp,
                "symbol": f.symbol,
                "side": f.side,
                "shares": f.shares,
                "price": f.price,
                "reference_price": f.reference_price,
                "notional": f.notional,
                "commission": f.commission,
                "slippage_cost": f.slippage_cost,
            }
            for f in fills
        ],
        columns=columns,
    )

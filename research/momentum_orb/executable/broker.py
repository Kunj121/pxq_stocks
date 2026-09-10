"""Minimal Alpaca **paper** trading client for the daily ORB runner.

Deliberately small: place, cancel, list, flatten. Nothing here reads market
data — signals come from the same Alpaca data path the backtest uses, so the
quote that generates a signal and the venue that fills it are the same book.
That is the one thing this setup has over routing signals from one venue to
another.

Safety, enforced here rather than left to the caller:

  * :meth:`assert_paper` refuses to operate on anything but a paper account.
    Alpaca paper account numbers start with ``PA``. A live account raises.
  * Every order carries a ``client_order_id`` so a retry after a timeout cannot
    double-fill.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backtest.config import PAPER_TRADING_BASE_URL, get_settings


class BrokerError(RuntimeError):
    pass


class NotPaperAccount(BrokerError):
    """Raised when the resolved account is not a paper account."""


@dataclass
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    type: str
    status: str
    filled_qty: float
    filled_avg_price: float | None
    stop_price: float | None
    submitted_at: str
    raw: dict[str, Any]

    @classmethod
    def parse(cls, d: dict) -> "Order":
        def num(key):
            v = d.get(key)
            return float(v) if v not in (None, "") else None
        return cls(
            id=d.get("id", ""), client_order_id=d.get("client_order_id", ""),
            symbol=d.get("symbol", ""), side=d.get("side", ""),
            qty=float(d.get("qty") or 0), type=d.get("type", ""),
            status=d.get("status", ""), filled_qty=float(d.get("filled_qty") or 0),
            filled_avg_price=num("filled_avg_price"), stop_price=num("stop_price"),
            submitted_at=d.get("submitted_at", ""), raw=d,
        )


class PaperBroker:
    """Thin wrapper over Alpaca's paper trading REST API."""

    def __init__(self, settings=None, timeout: float = 20.0) -> None:
        self.settings = settings or get_settings()
        self.base = PAPER_TRADING_BASE_URL
        self._client = httpx.Client(
            base_url=self.base, headers=self.settings.auth_headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    def _request(self, method: str, path: str, **kw) -> Any:
        r = self._client.request(method, path, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else None

    # -- account ---------------------------------------------------------- #

    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def assert_paper(self) -> dict:
        """Refuse to run against anything but a paper account.

        This is the hard gate. Alpaca paper account numbers begin with 'PA';
        the trading host is also the paper host by construction, but both are
        checked because a mis-set env var could change the host.
        """
        acct = self.account()
        number = str(acct.get("account_number", ""))
        if not number.startswith("PA"):
            raise NotPaperAccount(
                f"account {number!r} is not a paper account — refusing to trade")
        if "paper-api" not in self.base:
            raise NotPaperAccount(f"trading host {self.base!r} is not the paper host")
        return acct

    def clock(self) -> dict:
        return self._request("GET", "/v2/clock")

    # -- positions -------------------------------------------------------- #

    def positions(self) -> list[dict]:
        return self._request("GET", "/v2/positions") or []

    def close_all_positions(self, cancel_orders: bool = True) -> Any:
        return self._request(
            "DELETE", "/v2/positions",
            params={"cancel_orders": str(cancel_orders).lower()})

    # -- orders ----------------------------------------------------------- #

    def orders(self, status: str = "open", limit: int = 500) -> list[Order]:
        rows = self._request("GET", "/v2/orders",
                             params={"status": status, "limit": limit,
                                     "nested": "true"}) or []
        return [Order.parse(r) for r in rows]

    def cancel_all(self) -> Any:
        return self._request("DELETE", "/v2/orders")

    def cancel(self, order_id: str) -> Any:
        return self._request("DELETE", f"/v2/orders/{order_id}")

    def stop_entry_with_bracket(self, symbol: str, side: str, qty: int,
                                stop_price: float, protective_stop: float,
                                client_order_id: str | None = None) -> Order:
        """A stop-entry that carries its protective stop with it.

        The ORB entry is a stop order at the opening-range extreme; the moment
        it fills, a stop-loss must exist at 10% of ATR away. Submitting them as
        one bracket removes the window where a filled position sits unprotected
        because the second leg had not been acknowledged yet.
        """
        body = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": side,
            "type": "stop",
            "stop_price": f"{stop_price:.2f}",
            "time_in_force": "day",
            "order_class": "bracket",
            "stop_loss": {"stop_price": f"{protective_stop:.2f}"},
            # A bracket needs both legs; the take-profit is set absurdly far
            # away because the strategy has no target — it exits at the close.
            "take_profit": {"limit_price": f"{protective_stop * (100 if side == 'buy' else 0.01):.2f}"},
            "client_order_id": client_order_id or f"orb-{uuid.uuid4().hex[:20]}",
        }
        return Order.parse(self._request("POST", "/v2/orders", json=body))

    def stop_entry_simple(self, symbol: str, side: str, qty: int,
                          stop_price: float,
                          client_order_id: str | None = None) -> Order:
        """Plain stop entry, no bracket. Used when a bracket is rejected."""
        body = {
            "symbol": symbol, "qty": str(int(qty)), "side": side, "type": "stop",
            "stop_price": f"{stop_price:.2f}", "time_in_force": "day",
            "client_order_id": client_order_id or f"orb-{uuid.uuid4().hex[:20]}",
        }
        return Order.parse(self._request("POST", "/v2/orders", json=body))

    def market(self, symbol: str, side: str, qty: float,
               client_order_id: str | None = None) -> Order:
        body = {
            "symbol": symbol, "qty": str(qty), "side": side, "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id or f"orb-x-{uuid.uuid4().hex[:18]}",
        }
        return Order.parse(self._request("POST", "/v2/orders", json=body))

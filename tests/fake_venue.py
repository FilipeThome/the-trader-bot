"""A scripted Binance for executor tests: the slice of ``bot.exchange.service`` the executor uses."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from bot.exchange.execution.rules import MarketRules


def _rules() -> MarketRules:
    return MarketRules(
        symbol="BTC/USDT",
        price_tick=Decimal("0.01"),
        amount_tick=None,
        lot_step=Decimal("0.0001"),
        market_step=None,
        min_qty=Decimal("0.0001"),
        max_qty=None,
        market_min_qty=None,
        market_max_qty=None,
        min_notional=Decimal("5"),
        max_notional=None,
        min_price=None,
        max_price=None,
        limit_ticks=(Decimal("0.01"),),
        taker_fee=Decimal("0.001"),
        active=True,
        spot=True,
    )


class Venue:
    """The slice of ``bot.exchange.service`` the executor uses, scripted per order type.

    ``responses`` queues results by ``"{side}:{order_type}"`` (``"sell:limit"``) or,
    for either side, by the order type alone (``"limit"``); the side key wins.
    Unscripted, a buy LIMIT IOC (a strategy entry) fills in full at the ask when the
    ask is at or under its limit, and expires with nothing filled otherwise.
    """

    def __init__(self) -> None:
        self.bid = Decimal("100")
        #: Best ask when it differs from the bid.
        self.ask: Decimal | None = None
        self.budget = 100
        self.calls: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self.counter = 0
        self.blocker: str | None = None
        self.journal_blocked = False
        self.fail_market: Exception | None = None
        #: Raised on a buy LIMIT order (a strategy entry), after it is recorded as sent.
        self.fail_entry: Exception | None = None
        #: Per-order notional cap the gate enforces; None means unreadable.
        self.cap: Decimal | None = Decimal("1000")
        #: Last trade price when it differs from the bid.
        self.last: Decimal | None = None
        #: Raised on the n-th limit sell (1-based), to break a chunked exit midway.
        self.fail_limit_at: int | None = None
        self.limit_calls = 0
        #: Called with the order id on a cancel; its result (None means ok) is returned.
        #: It may move ``orders`` on, as Binance does when a stop fills before the cancel lands.
        self.on_cancel: Callable[[str], dict[str, Any] | None] | None = None

    def submit_order(self, symbol: str, profile_id: str, *, held: Any = None, **kwargs: Any) -> dict[str, Any]:
        entry = kwargs["order_type"] == "limit" and kwargs.get("side") == "buy"
        # "submit:limit" stays an exit sale; an entry IOC logs as "submit:buy:limit".
        self.events.append("submit:buy:limit" if entry else f"submit:{kwargs['order_type']}")
        self.calls.append({"symbol": symbol, "held": held, **kwargs})
        self.counter += 1
        client_id = f"c{self.counter}"
        if kwargs["order_type"] == "market" and self.fail_market is not None:
            raise self.fail_market
        if entry and self.fail_entry is not None:
            raise self.fail_entry
        if kwargs["order_type"] == "limit" and not entry:
            self.limit_calls += 1
            if self.limit_calls == self.fail_limit_at:
                raise RuntimeError("binance.json unreadable")
        queue = self.responses.get(f"{kwargs.get('side', 'buy')}:{kwargs['order_type']}") or self.responses.get(
            kwargs["order_type"]
        )
        if queue:
            result = dict(queue.pop(0))
        elif kwargs["order_type"] == "market":
            notional = Decimal(kwargs["notional"])
            filled = notional / self.bid
            result = {
                "status": "ok",
                "order_id": f"o{self.counter}",
                "filled": str(filled),
                "cost": str(notional),
                "fees": [{"cost": float(filled * Decimal("0.001")), "currency": "BTC"}],
            }
        elif kwargs["order_type"] == "stop_loss_limit":
            result = {"status": "ok", "order_id": f"stop{self.counter}"}
        elif entry:
            qty = Decimal(kwargs["quantity"])
            ask = self.ask if self.ask is not None else self.bid
            if ask > Decimal(kwargs["limit_price"]):
                result = {"status": "ok", "order_id": f"o{self.counter}", "filled": "0", "cost": "0", "fees": []}
            else:
                result = {
                    "status": "ok",
                    "order_id": f"o{self.counter}",
                    "filled": str(qty),
                    "cost": str(qty * ask),
                    "average": str(ask),
                    "fees": [{"cost": float(qty * Decimal("0.001")), "currency": "BTC"}],
                }
        else:
            qty = Decimal(kwargs["quantity"])
            cost = qty * Decimal(kwargs["limit_price"])
            result = {
                "status": "ok",
                "order_id": f"o{self.counter}",
                "filled": str(qty),
                "cost": str(cost),
                "fees": [{"cost": float(cost * Decimal("0.001")), "currency": "USDT"}],
            }
        result.setdefault("client_order_id", client_id)
        return result

    def order_status(self, symbol: str, profile_id: str, client_order_id: str) -> dict[str, Any]:
        self.events.append(f"status:{client_order_id}")
        return self.orders.get(client_order_id, {"status": "not_found"})

    def cancel_order(self, order_id: str, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        self.events.append(f"cancel:{order_id}")
        self.cancelled.append(order_id)
        result = self.on_cancel(order_id) if self.on_cancel is not None else None
        return result if result is not None else {"status": "ok"}

    def get_quote(self, symbol: str, profile_id: str) -> dict[str, Any]:
        last = self.last if self.last is not None else self.bid
        ask = self.ask if self.ask is not None else self.bid
        return {"status": "ok", "quote": {"bid": str(self.bid), "ask": str(ask), "last": str(last)}}

    def reconcile(self, profile_id: str) -> dict[str, Any]:
        self.events.append("reconcile")
        return {"status": "ok"}


@pytest.fixture
def venue(monkeypatch) -> Venue:
    fake = Venue()
    monkeypatch.setattr("bot.exchange.service.submit_order", fake.submit_order)
    monkeypatch.setattr("bot.exchange.service.order_status", fake.order_status)
    monkeypatch.setattr("bot.exchange.service.cancel_order", fake.cancel_order)
    monkeypatch.setattr("bot.exchange.service.get_quote", fake.get_quote)
    monkeypatch.setattr("bot.exchange.service.reconcile", fake.reconcile)
    monkeypatch.setattr("bot.exchange.service.market_rules", lambda *_a: _rules())
    monkeypatch.setattr("bot.exchange.service.free_balance", lambda *_a: Decimal("50"))
    monkeypatch.setattr("bot.exchange.service.orders_remaining", lambda *_a: fake.budget)
    monkeypatch.setattr("bot.exchange.service.order_notional_cap", lambda *_a: fake.cap)
    monkeypatch.setattr("bot.exchange.service.write_blocker", lambda: fake.blocker)
    monkeypatch.setattr("bot.exchange.service.journal_status", lambda: {"blocked": fake.journal_blocked})
    return fake

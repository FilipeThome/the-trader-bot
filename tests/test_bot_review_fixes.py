"""Reviewer findings F1-F5 on waves 1-3: the gate's fee margin, a daily pause that blocks buys only,
a stuck stop whose cancel fails, an unreadable Monte Carlo drawdown and a slow fee warm-up."""

from __future__ import annotations

import threading
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

import bot.runtime as runtime_module
from bot.exchange import service
from bot.exchange.execution.rules import MarketRules
from bot.exchange.service import AccountMarketRules
from bot.executor import Executor, UnprotectedPosition
from bot.risk import utc_day
from bot.store import Store
from fake_venue import _rules
from test_bot_fees import PAPER, fees  # noqa: F401 - the scripted commission fixture
from test_bot_instances import DCA, GRID, _live_runtime, _price
from test_bot_stop_gap import STOP_ORDER_ID, _entered, _executor, _sells, _stuck

GATE_TAKER = Decimal("0.001")


# -- F1: sell margins cover the gate's default fee ------------------------------------------


def test_account_rules_keep_the_market_default_fee_for_the_gate(fees) -> None:  # noqa: F811
    fees["answer"] = {"status": "ok", "maker": "0", "taker": "0"}
    service.trading_fees("BTC/USDT", PAPER)
    rules = service.market_rules("BTC/USDT", PAPER)
    assert isinstance(rules, AccountMarketRules)
    assert rules.taker_fee == Decimal("0") and rules.gate_taker_fee == GATE_TAKER


def _account_rules(taker: Decimal) -> AccountMarketRules:
    base = _rules()
    values = {item.name: getattr(base, item.name) for item in fields(MarketRules)}
    return AccountMarketRules(**{**values, "taker_fee": taker}, gate_taker_fee=GATE_TAKER)


@pytest.mark.parametrize("taker", [Decimal("0"), Decimal("0.0006")])
def test_stop_quantities_pass_the_gate_balance_rule_with_a_lower_account_fee(tmp_path, venue, monkeypatch, taker) -> None:
    held = {"BTC": Decimal("0.1998")}  # 20 USD at 100, less the 0.1% fee paid in base
    monkeypatch.setattr(service, "market_rules", lambda *_a: _account_rules(taker))
    monkeypatch.setattr(service, "free_balance", lambda asset, *_a: held.get(asset, Decimal("50")))
    executor = Executor(Store(tmp_path / "bot.sqlite"), "binance-paper-trade")
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"

    def gate_allows(stop: dict[str, Any]) -> bool:
        # The preflight rule of the unchanged gate: free base >= qty x (1 + the market's fee).
        return held["BTC"] >= Decimal(stop["quantity"]) * (1 + GATE_TAKER)

    stops = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"]
    assert len(stops) == 1 and gate_allows(stops[-1])
    position = executor.store.open_positions()[0]
    held["BTC"] += Decimal("0.0999")  # the 10 USD scale-in, net of its base fee
    assert executor.scale_in(position["id"], Decimal("10"), "grid-buy-1", Decimal("90"), reserve=2) == "comprado"
    stops = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"]
    assert len(stops) == 2 and gate_allows(stops[-1])
    # The fee booked stays the account's own.
    assert executor.store.open_positions()[0]["fees"] == pytest.approx(0.0002 * 100 + 0.0001 * 100)


# -- F2: a daily pause blocks buys only -----------------------------------------------------


def test_a_daily_pause_keeps_the_cycle_running_and_blocks_buys_until_the_next_day(tmp_path, monkeypatch, venue) -> None:
    monkeypatch.setattr(service, "trading_fees", lambda *_a: None)
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["status"] == "ok"
    position = runtime.store.open_positions()[0]
    runtime.store.set_state("running", True)
    runtime.store.record_day(utc_day(), net_pnl=-31.0)
    runtime.tick()
    assert runtime.store.get_state("paused_until") and runtime.running is True
    # Buys are refused everywhere while the pause lasts.
    assert runtime.executor.open_instance(2, "dca", "ETH/USDT", Decimal("10"), Decimal("90"))["error"].startswith("pausado")
    assert runtime.executor.scale_in(position["id"], Decimal("10"), "grid-buy-9", Decimal("85"), reserve=2).startswith("pausado")
    assert runtime.create_instance("dca", "ETH/USDT", DCA)["error"].startswith("pausado")
    assert runtime.start()["status"] == "ok"
    # The next cycle still syncs: a stop the price gapped through is sold.
    venue.orders[position["stop_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "open", "filled": "0", "cost": "0", "order_id": position["stop_order_id"], "price": "84.57"},
    }
    _price(runtime, venue, 86.0)
    venue.bid = Decimal("84")
    runtime.tick()
    sold = runtime.store.position(position["id"])
    assert sold["status"] == "closed" and any(call["side"] == "sell" and call["order_type"] == "limit" for call in venue.calls)
    # The next UTC day buys again.
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=1)
    monkeypatch.setattr("bot.executor._now", lambda: tomorrow)
    assert runtime.executor._buy_block() is None and runtime._paused(tomorrow) is None


# -- F3: a stuck stop whose cancel fails stays on the book ----------------------------------


def test_a_failed_cancel_of_a_stuck_stop_retries_next_sync_without_selling(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    working = venue.on_cancel
    venue.on_cancel = lambda _id: {"status": "error", "error": "timeout"}
    executor.sync_positions()
    left = executor.store.position(position["id"])
    assert left["status"] == "open" and left["stop_order_id"] == STOP_ORDER_ID and _sells(venue) == []
    assert executor.store.get_state("stop_gapped_retries") == {str(position["id"]): 1}
    # The cancel works again: the stop comes off, the IOC sells, and the count is cleared.
    venue.on_cancel = working
    executor.sync_positions()
    assert executor.store.position(position["id"])["status"] == "closed"
    assert executor.store.get_state("stop_gapped_retries") == {}


def test_a_stuck_stop_that_never_comes_off_halts_on_the_third_sync(tmp_path, venue) -> None:
    executor = _executor(tmp_path, [])
    _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    venue.on_cancel = lambda _id: {"status": "error", "error": "timeout"}
    executor.sync_positions()
    executor.sync_positions()
    with pytest.raises(UnprotectedPosition, match="3 ciclos"):
        executor.sync_positions()


# -- F5: the fee warm-up never holds the cycle ----------------------------------------------


def test_a_slow_fee_read_does_not_delay_the_cycle_beyond_the_budget(tmp_path, monkeypatch, venue) -> None:
    release = threading.Event()
    reads: list[str] = []

    def slow(symbol: str, _profile: str):
        reads.append(symbol)
        release.wait(10)
        return (Decimal("0.001"), Decimal("0.00075"))

    monkeypatch.setattr(service, "trading_fees", slow)
    monkeypatch.setattr(runtime_module, "FEE_WARMUP_BUDGET_SECONDS", 0.2)
    runtime = _live_runtime(tmp_path, monkeypatch)
    started = time.monotonic()
    assert runtime._account_takers(["BTC/USDT", "ETH/USDT"]) == {}
    assert runtime._account_takers(["BTC/USDT", "ETH/USDT"]) == {}
    assert time.monotonic() - started < 2.0
    # One read hangs; no second warm-up thread piles up behind it.
    assert reads == ["BTC/USDT"]
    release.set()
    worker = runtime._fee_worker
    assert worker is not None
    worker.join(5)
    assert runtime._account_takers(["BTC/USDT", "ETH/USDT"]) == {"BTC/USDT": 0.00075, "ETH/USDT": 0.00075}

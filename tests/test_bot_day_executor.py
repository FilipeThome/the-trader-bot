from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.executor import Executor
from bot.strategies.day import DAY
from test_bot_executor import PROFILE, Store, _ready

STOP = "stop0"


def _executor(tmp_path) -> Executor:
    return Executor(Store(tmp_path / "bot.sqlite"), PROFILE)


def _open_day(executor: Executor, qty: float, *, avg: float = 100.0, stop: float = 98.0, target: float = 103.0) -> int:
    position_id = executor.store.open_pending("BTC/USDT", "intraday_momentum", stop_price=stop, target_price=target, exit_policy="day")
    assert position_id is not None
    executor.store.update_position(position_id, status="open", qty=qty, avg_price=avg, stop_order_id=STOP)
    return position_id


def test_a_day_entry_records_its_policy_and_time_limit(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    before = datetime.now(timezone.utc)
    decision = executor.enter(_ready(strategy="intraday_momentum", timeframe="15m"))
    assert decision["decision"] == "operar"
    position = executor.store.open_positions()[0]
    assert position["exit_policy"] == "day"
    deadline = datetime.fromisoformat(position["exit_deadline"])
    assert before + DAY.max_hold <= deadline <= datetime.now(timezone.utc) + DAY.max_hold


def test_a_day_entry_needs_orders_for_the_first_target_too(tmp_path, venue) -> None:
    venue.budget = 4
    executor = _executor(tmp_path)
    assert executor.enter(_ready(strategy="intraday_momentum"))["decision"] == "bloqueado"
    # The same budget still covers a swing round trip.
    assert executor.enter(_ready())["decision"] == "operar"


def test_the_first_target_sells_half_and_rests_the_stop_at_break_even(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    assert executor.take_profit(position_id, reserve=1) == "vendido"
    assert venue.events[0] == f"cancel:{STOP}"
    sale = next(call for call in venue.calls if call["order_type"] == "limit")
    stop = next(call for call in venue.calls if call["order_type"] == "stop_loss_limit")
    assert sale["quantity"] == "0.5"
    assert stop["quantity"] == "0.5" and stop["stop_price"] == "100.2"
    position = executor.store.position(position_id)
    assert position["status"] == "open" and position["qty"] == 0.5 and position["tp1_done"] == 1
    assert position["keep_qty"] is None and position["stop_price"] == 100.2
    # A later full exit uses its own order keys.
    executor.exit_position(position_id, "fim")
    keys = [call["idempotency_key"] for call in venue.calls if call["order_type"] == "limit"]
    assert len(set(keys)) == len(keys)


def test_a_partial_fill_keeps_the_old_stop_and_the_next_try_sells_only_down_to_the_kept_quantity(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    venue.responses["limit"] = [
        {"status": "ok", "order_id": "p1", "filled": "0.2", "cost": "20.6", "fees": [{"cost": 0.02, "currency": "USDT"}]}
    ]
    executor.take_profit(position_id, reserve=1)
    position = executor.store.position(position_id)
    assert position["tp1_done"] == 0 and position["keep_qty"] == 0.5 and position["qty"] == 0.8
    restop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"]
    assert restop[-1]["stop_price"] == "98" and restop[-1]["quantity"] == "0.8"
    executor.store.update_position(position_id, stop_order_id="stop-again")
    assert executor.take_profit(position_id, reserve=1) == "vendido"
    sales = [call for call in venue.calls if call["order_type"] == "limit"]
    assert sales[-1]["quantity"] == "0.3"
    position = executor.store.position(position_id)
    assert position["qty"] == 0.5 and position["tp1_done"] == 1


def test_a_first_target_below_the_exchange_minimum_waits_with_the_stop_on(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 0.08)
    venue.bid = Decimal("103.5")
    assert executor.take_profit(position_id, reserve=1) == "parte do alvo abaixo do mínimo da corretora"
    assert venue.calls == [] and venue.cancelled == []


def test_a_rest_below_the_exchange_minimum_sells_everything(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 0.0971)
    venue.bid = Decimal("103.5")
    assert executor.take_profit(position_id, reserve=1) == "vendido"
    position = executor.store.position(position_id)
    # Never "closed with quantity left": the whole position was sold.
    assert position["status"] == "closed"
    assert sum(Decimal(call["quantity"]) for call in venue.calls if call["order_type"] == "limit") == Decimal("0.0971")


def test_a_refused_break_even_stop_exits_the_rest(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    venue.responses["stop_loss_limit"] = [{"status": "error", "reasons": ["refused"]}]
    executor.take_profit(position_id, reserve=1)
    assert executor.store.position(position_id)["status"] == "closed"


def test_the_first_target_waits_without_orders_or_a_safe_break_even(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    venue.budget = 2
    assert executor.take_profit(position_id, reserve=1) == "orçamento diário de ordens"
    venue.budget = 100
    venue.bid = Decimal("100.5")
    # 100.2 is not below 100.5 x (1 - 0.5%): the new stop would trigger at once.
    assert executor.take_profit(position_id, reserve=1) == "break-even acima do bid"
    venue.bid = Decimal("103.5")
    venue.blocker = "halt"
    assert executor.take_profit(position_id, reserve=1) == "halt"
    assert venue.calls == [] and venue.cancelled == []


def test_the_deadline_is_kept_by_a_pending_entry(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    position_id = executor.store.open_pending("BTC/USDT", "vwap_pullback", exit_policy="day", exit_deadline=deadline)
    assert position_id is not None
    assert executor.store.position(position_id)["exit_deadline"] == deadline


def test_an_unknown_first_target_is_booked_by_the_sync_before_the_stop_is_sized(tmp_path, venue) -> None:
    from bot.exchange import service

    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    venue.responses["limit"] = [{"status": "unknown", "error": "timeout"}]
    executor.take_profit(position_id, reserve=1)
    position = executor.store.position(position_id)
    assert position["stop_order_id"] is None and position["keep_qty"] == 0.5 and position["qty"] == 1.0
    # Binance did sell half: the sync reads the first-target order by its key and books it.
    attempt = int(position["exit_attempts"])
    client_id = service.client_order_id(PROFILE, executor._key(position_id, "tp1", attempt))
    venue.orders[client_id] = {"status": "ok", "order": {"status": "closed", "filled": "0.5", "cost": "51.75"}}
    executor.sync_positions()
    position = executor.store.position(position_id)
    assert position["qty"] == 0.5 and position["tp1_done"] == 1 and position["stop_price"] == 100.2
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    assert stop["quantity"] == "0.5" and stop["stop_price"] == "100.2"


def test_a_stop_never_asks_for_more_than_the_balance_held(tmp_path, venue, monkeypatch) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    executor.store.update_position(position_id, stop_order_id=None)
    monkeypatch.setattr("bot.exchange.service.free_balance", lambda *_a: Decimal("0.3"))
    executor.protect(position_id)
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    # Sized like a fill, under the gate's fee margin: the gate wants qty x (1 + fee) free.
    assert stop["quantity"] == "0.2997"
    assert Decimal(stop["quantity"]) * Decimal("1.001") <= Decimal("0.3")
    # The store follows the balance: a later first target or exit is sized on what is held.
    assert executor.store.position(position_id)["qty"] == pytest.approx(0.3 / 1.001)


def test_a_break_even_stop_over_the_order_cap_keeps_the_old_stop(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    venue.cap = Decimal("40")
    assert executor.take_profit(position_id, reserve=1) == "stop do resto acima do teto por ordem: o stop atual fica"
    assert venue.calls == [] and venue.cancelled == []


def test_a_first_target_sold_before_a_crash_is_finished_without_another_sale(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 0.5)
    # The sale completed and was booked, then the process died before the break-even stop.
    executor.store.update_position(position_id, keep_qty=0.5)
    venue.bid = Decimal("103.5")
    assert executor.take_profit(position_id, reserve=1) == "concluído"
    assert not [call for call in venue.calls if call["order_type"] == "limit"]
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    assert stop["quantity"] == "0.5" and stop["stop_price"] == "100.2"
    position = executor.store.position(position_id)
    assert position["tp1_done"] == 1 and position["keep_qty"] is None


def test_a_started_first_target_with_an_unsellable_bit_left_is_finished(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 0.54)
    executor.store.update_position(position_id, keep_qty=0.5)
    venue.bid = Decimal("103.5")
    # 0.04 x 102.98 is under the 5 USDT minimum: no sale, the stop moves to break-even.
    assert executor.take_profit(position_id, reserve=1) == "concluído"
    assert executor.store.position(position_id)["tp1_done"] == 1


def test_an_unread_first_target_never_sells_into_the_kept_half(tmp_path, venue, monkeypatch) -> None:
    executor = _executor(tmp_path)
    position_id = _open_day(executor, 1.0)
    venue.bid = Decimal("103.5")
    venue.responses["limit"] = [{"status": "unknown", "error": "timeout"}]
    executor.take_profit(position_id, reserve=1)
    # Binance sold half, but the order cannot be read: the sync sizes the stop on the balance.
    monkeypatch.setattr("bot.exchange.service.free_balance", lambda *_a: Decimal("0.5"))
    executor.sync_positions()
    assert executor.store.position(position_id)["qty"] == pytest.approx(0.5 / 1.001)
    executor.store.update_position(position_id, stop_order_id="stop-again")
    sold_before = [call for call in venue.calls if call["order_type"] == "limit"]
    assert executor.take_profit(position_id, reserve=1) == "concluído"
    assert [call for call in venue.calls if call["order_type"] == "limit"] == sold_before

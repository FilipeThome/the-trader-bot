from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bot.evaluator import Opportunity
from bot.executor import Executor, UnprotectedPosition
from bot.store import Store

PROFILE = "binance-paper-trade"


def _ready(**overrides: Any) -> Opportunity:
    payload: dict[str, Any] = dict(
        symbol="BTC/USDT",
        strategy="trend",
        action="Comprar",
        score=0.82,
        net_return=0.02,
        risk_pct=0.02,
        status="pronta",
        reason="rompimento",
        timeframe="1h",
        direction=1,
        cost=0.002,
        gross_return=0.04,
        size_usd=100,
        stop_price=98.0,
        target_price=104.0,
    )
    payload.update(overrides)
    return Opportunity(**payload)


def _executor(tmp_path) -> Executor:
    return Executor(Store(tmp_path / "bot.sqlite"), PROFILE)


def test_entry_opens_the_position_and_rests_a_stop_for_the_net_quantity(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    decision = executor.enter(_ready())
    assert decision["decision"] == "operar"
    assert decision["position_id"] == executor.store.open_positions()[0]["id"]
    entry, stop = venue.calls
    # A LIMIT IOC 0.2% above the ask, sized in base: 100 USD / 100.2, floored to the lot.
    assert entry["side"] == "buy" and entry["order_type"] == "limit" and entry["time_in_force"] == "ioc"
    assert entry["quantity"] == "0.998" and entry["limit_price"] == "100.2" and entry["held"] == []
    assert "notional" not in entry
    # 0.998 BTC filled at 100, 0.000998 BTC fee, then the 0.1% sell margin, floored to the lot.
    assert stop["order_type"] == "stop_loss_limit"
    assert stop["quantity"] == "0.996"
    # The plan's 2% stop counts from the fee-inclusive average fill (99.8 / 0.997002), floored to the tick.
    assert stop["stop_price"] == "98.09" and stop["limit_price"] == "97.59"
    position = executor.store.open_positions()[0]
    assert position["stop_order_id"] == "stop2"
    assert executor.store.pnl_days()[0]["trades"] == 1


def test_a_rejected_entry_does_not_block_the_next_one(tmp_path, venue) -> None:
    venue.responses["buy:limit"] = [{"status": "error", "reasons": ["balance_insufficient"]}]
    executor = _executor(tmp_path)
    first = executor.enter(_ready())
    assert first["decision"] == "bloqueado" and "balance_insufficient" in first["reason"]
    assert executor.store.live_positions() == []
    second = executor.enter(_ready())
    assert second["decision"] == "operar"
    assert venue.calls[0]["idempotency_key"] != venue.calls[1]["idempotency_key"]


def test_a_spot_sell_signal_is_never_sent(tmp_path, venue) -> None:
    decision = _executor(tmp_path).enter(_ready(direction=-1))
    assert decision["decision"] == "bloqueado"
    assert venue.calls == []


def test_entry_needs_budget_for_entry_stop_and_exit(tmp_path, venue) -> None:
    venue.budget = 2
    decision = _executor(tmp_path).enter(_ready())
    assert decision["decision"] == "bloqueado"
    assert venue.calls == []


def test_an_idempotent_replay_reads_the_order_instead_of_inventing_a_fill(tmp_path, venue) -> None:
    venue.responses["buy:limit"] = [{"status": "ok", "idempotent_replay": True, "client_order_id": "cid"}]
    venue.orders["cid"] = {"status": "ok", "order": {"status": "canceled", "filled": 0}}
    executor = _executor(tmp_path)
    executor.enter(_ready())
    assert "status:cid" in venue.events
    assert executor.store.live_positions() == []
    assert [call["order_type"] for call in venue.calls] == ["limit"]


def test_target_exit_takes_the_stop_off_then_sells_with_ioc(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    venue.bid = Decimal("105")
    result = executor.exit_position(position["id"], "alvo atingido")
    assert result["status"] == "ok"
    assert venue.cancelled == ["stop2"]
    sell = venue.calls[-1]
    assert sell["order_type"] == "limit" and sell["time_in_force"] == "ioc"
    assert sell["quantity"] == "0.996" and sell["limit_price"] == "104.47"
    assert venue.events.index("cancel:stop2") < len(venue.events) - 1
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    day = executor.store.pnl_days()[0]
    assert day["net_pnl"] == pytest.approx(closed["realized_pnl"])
    assert day["net_pnl"] > 0


def test_an_exit_prices_its_ioc_the_given_gap_below_the_bid(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    venue.bid = Decimal("105")
    result = executor.exit_position(position["id"], "stop acionado sem execução (gap)", gap=Decimal("0.02"))
    assert result["status"] == "ok"
    sell = venue.calls[-1]
    assert sell["order_type"] == "limit" and sell["time_in_force"] == "ioc"
    assert sell["limit_price"] == "102.9"


def test_a_partial_ioc_re_places_the_stop_on_the_remainder(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    venue.responses["sell:limit"] = [{"status": "ok", "filled": "0.5", "cost": "50", "fees": []}]
    result = executor.exit_position(position["id"], "alvo atingido")
    assert result["status"] == "error"
    restop = venue.calls[-1]
    assert restop["order_type"] == "stop_loss_limit" and restop["quantity"] == "0.496"
    stops = [call["idempotency_key"] for call in venue.calls if call["order_type"] == "stop_loss_limit"]
    assert len(set(stops)) == 2
    left = executor.store.position(position["id"])
    assert left is not None and left["status"] == "open" and left["qty"] == pytest.approx(0.496)


def test_a_refused_stop_exits_at_once(tmp_path, venue) -> None:
    venue.responses["stop_loss_limit"] = [{"status": "blocked", "reason": "order breaches max_trades_per_day"}]
    executor = _executor(tmp_path)
    executor.enter(_ready())
    assert [call["order_type"] for call in venue.calls] == ["limit", "stop_loss_limit", "limit"]
    assert [call["side"] for call in venue.calls] == ["buy", "sell", "sell"]
    assert executor.store.open_positions() == []


def test_a_refused_stop_and_a_failed_exit_raise_for_a_halt(tmp_path, venue) -> None:
    venue.responses["stop_loss_limit"] = [{"status": "blocked", "reason": "mandate"}]
    venue.responses["sell:limit"] = [{"status": "error", "reasons": ["balance_insufficient"]}]
    with pytest.raises(UnprotectedPosition):
        _executor(tmp_path).enter(_ready())


def test_sync_reconciles_first_then_opens_a_pending_entry(tmp_path, venue) -> None:
    venue.responses["buy:limit"] = [{"status": "unknown", "error": "timeout", "client_order_id": "cid"}]
    venue.journal_blocked = True
    executor = _executor(tmp_path)
    assert executor.enter(_ready())["decision"] == "pendente"
    client_id = executor.store.live_positions()[0]["entry_client_order_id"]
    venue.orders[client_id] = {
        "status": "ok",
        "order": {"status": "closed", "filled": "1", "cost": "100", "order_id": "o9"},
    }
    executor.sync_positions()
    assert venue.events.index("reconcile") < venue.events.index(f"status:{client_id}")
    position = executor.store.open_positions()[0]
    assert position["stop_order_id"]


def test_a_stop_filled_on_the_exchange_closes_the_position(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    venue.orders[position["stop_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "closed", "filled": "0.998", "cost": "97.8", "order_id": "stop2"},
    }
    executor.sync_positions()
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert closed["realized_pnl"] < 0


def test_flatten_sells_every_open_position(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    executor.enter(_ready(symbol="ETH/USDT"))
    results = executor.flatten()
    assert [item["status"] for item in results] == ["ok", "ok"]
    assert executor.store.open_positions() == []


def test_a_crash_after_the_entry_send_is_resolved_by_the_persisted_client_id(tmp_path, venue) -> None:
    from bot.exchange import service

    venue.fail_entry = ConnectionResetError("after send")
    executor = _executor(tmp_path)
    with pytest.raises(ConnectionResetError):
        executor.enter(_ready())
    pending = executor.store.live_positions()[0]
    sent_key = venue.calls[0]["idempotency_key"]
    assert pending["status"] == "pending"
    assert pending["entry_client_order_id"] == service.client_order_id(PROFILE, sent_key)
    venue.orders[pending["entry_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "closed", "filled": "1", "cost": "100", "order_id": "o1"},
    }
    executor.sync_positions()
    assert executor.store.open_positions()[0]["stop_order_id"]


def test_an_unreadable_fill_keeps_the_entry_pending(tmp_path, venue) -> None:
    venue.responses["buy:limit"] = [{"status": "ok", "idempotent_replay": True, "client_order_id": "cid"}]
    venue.orders["cid"] = {"status": "error", "error": "read_failed (Timeout)"}
    executor = _executor(tmp_path)
    assert executor.enter(_ready())["decision"] == "pendente"
    assert executor.store.live_positions()[0]["status"] == "pending"


def test_an_exit_the_gate_would_refuse_keeps_the_stop(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    venue.blocker = "halt ativo"
    result = executor.exit_position(position["id"], "fechamento manual")
    assert result["status"] == "blocked"
    assert venue.cancelled == []
    assert executor.store.position(position["id"])["stop_order_id"] == "stop2"  # type: ignore[index]


def test_flatten_reports_a_raising_position_and_continues(tmp_path, venue, monkeypatch) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    executor.enter(_ready(symbol="ETH/USDT"))

    def broken(order_id: str, profile_id: str, **_kwargs: Any) -> dict[str, Any]:
        if order_id == "stop2":
            raise RuntimeError("config")
        return {"status": "ok"}

    monkeypatch.setattr("bot.exchange.service.cancel_order", broken)
    results = executor.flatten()
    assert [item["status"] for item in results] == ["error", "ok"]
    assert results[0]["unprotected"] is True and "unprotected" not in results[1]


def test_sync_skips_the_costly_reconciliation_when_nothing_blocks(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    executor.sync_positions()
    assert "reconcile" not in venue.events



def _opened(tmp_path, venue) -> tuple[Executor, dict[str, Any]]:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    return executor, executor.store.open_positions()[0]


def test_the_stop_follows_the_peak(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    # The plan's stop distance, set on the fee-inclusive average price.
    assert position["trail_pct"] == pytest.approx(0.02)
    assert position["stop_price"] == pytest.approx(position["avg_price"] * 0.98)
    venue.bid = Decimal("110")
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "movido"
    assert venue.cancelled == ["stop2"]
    moved = executor.store.position(position["id"])
    assert moved is not None and moved["peak_price"] == 110.0
    new_stop = venue.calls[-1]
    assert new_stop["order_type"] == "stop_loss_limit" and Decimal(new_stop["stop_price"]) > Decimal("107")
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "stop já no nível"


def test_no_move_when_the_gate_would_refuse_the_new_stop(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    venue.blocker = "mandato expirado"
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "mandato expirado"
    assert venue.cancelled == []


def test_no_move_when_the_budget_is_reserved(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    venue.budget = 1
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "orçamento diário de ordens reservado"
    assert venue.cancelled == []


def test_no_move_when_the_new_stop_would_sit_at_the_bid(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("100")
    # Peak 112, new stop 109.76: the bid leaves no room under it (1% clearance, half the 2% trail).
    assert executor.trail(position["id"], Decimal("112"), reserve=2) == "novo stop sem folga de 1.0% do preço"
    assert venue.cancelled == []


def test_a_refused_replacement_exits_at_once(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    venue.responses["stop_loss_limit"] = [{"status": "blocked", "reason": "max_trades_per_day"}]
    executor.trail(position["id"], Decimal("110"), reserve=2)
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"


def test_an_entry_keeps_one_order_per_open_position_in_reserve(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    venue.budget = 3
    assert executor.enter(_ready(symbol="ETH/USDT"))["decision"] == "bloqueado"



def test_positions_record_the_exit_policy_they_were_opened_under(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE, exit_policy="target")
    executor.enter(_ready())
    assert executor.store.open_positions()[0]["exit_policy"] == "target"


def test_no_move_under_api_weight_pressure(tmp_path, venue, monkeypatch) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    monkeypatch.setattr("bot.executor.weight_pressure", lambda: True)
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "peso da API alto"
    assert venue.cancelled == []


def test_no_move_without_a_confirmed_resting_stop(tmp_path, venue) -> None:
    venue.responses["stop_loss_limit"] = [{"status": "unknown", "error": "timeout", "client_order_id": "s1"}]
    executor, position = _opened(tmp_path, venue)
    assert position["stop_client_order_id"] == "s1" and position["stop_order_id"] is None
    venue.bid = Decimal("110")
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "sem stop confirmado"
    assert venue.cancelled == []



def test_a_test_row_is_refused_outside_a_manual_spot_testnet_execution(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    assert executor.enter(_ready(test_only=True))["decision"] == "bloqueado"
    live = Executor(Store(tmp_path / "live.sqlite"), "binance-live-trade-bot")
    assert live.enter(_ready(test_only=True), manual=True)["decision"] == "bloqueado"
    assert venue.calls == []
    assert live.enter(_ready(test_only=True), allow_test=True)["decision"] == "bloqueado"
    assert executor.enter(_ready(test_only=True), manual=True)["decision"] == "operar"
    assert executor.store.open_positions()[0]["test_only"] == 1


def test_the_stop_stays_when_its_replacement_would_breach_the_order_cap(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    # About 0.999 BTC at 110 is worth 109.9, above 95% of a 112 cap: too close for the gate's own read.
    venue.cap = Decimal("112")
    assert "teto" in executor.trail(position["id"], Decimal("110"), reserve=2)
    assert venue.cancelled == []
    kept = executor.store.position(position["id"])
    assert kept is not None and kept["status"] == "open" and kept["stop_order_id"] == position["stop_order_id"]
    venue.cap = None
    assert "indisponível" in executor.trail(position["id"], Decimal("110"), reserve=2)
    assert venue.cancelled == []


def test_a_partly_filled_cancelled_stop_is_booked(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    client = str(position["stop_client_order_id"])
    venue.orders[client] = {"status": "ok", "order": {"status": "canceled", "filled": "0.4", "cost": "39.2", "order_id": "stop2"}}
    assert executor._take_stop_off(position) == "cancelled"
    after = executor.store.position(position["id"])
    assert after is not None
    assert after["qty"] == pytest.approx(position["qty"] - 0.4)
    assert after["stop_client_order_id"] is None and after["realized_pnl"] < 0


def test_an_unreadable_cancelled_stop_reports_the_position_unprotected(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    client = str(position["stop_client_order_id"])
    venue.orders[client] = {"status": "error", "error": "read_failed (TimeoutError)"}
    # The cancel went through: no stop is resting and the size is unknown. Callers must halt.
    with pytest.raises(UnprotectedPosition):
        executor.exit_position(position["id"], "teste")
    kept = executor.store.position(position["id"])
    assert kept is not None and kept["stop_client_order_id"] == client and kept["qty"] == position["qty"]
    venue.bid = Decimal("110")
    with pytest.raises(UnprotectedPosition):
        executor.trail(position["id"], Decimal("110"), reserve=2)
    # flatten reports it as unprotected instead of claiming the stop still protects it.
    rows = executor.flatten()
    assert rows and rows[0].get("unprotected") is True
    # Once the order can be read, the sync books it and puts a new stop on the book.
    venue.orders[client] = {"status": "ok", "order": {"status": "canceled", "filled": "0", "order_id": "stop2"}}
    executor.sync_positions()
    reprotected = executor.store.position(position["id"])
    assert reprotected is not None and reprotected["stop_client_order_id"] not in (None, client)


def test_a_chunk_sold_before_a_failure_is_already_booked(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    # A 60 USDT cap splits the ~100 USDT position into chunks; the second chunk raises.
    venue.cap = Decimal("60")
    venue.fail_limit_at = 2
    with pytest.raises(RuntimeError):
        executor.exit_position(position["id"], "teste")
    after = executor.store.position(position["id"])
    assert after is not None and after["status"] == "open"
    sold = position["qty"] - after["qty"]
    assert 0.5 < sold < 0.6
    assert after["realized_pnl"] != 0


def _pending(executor: Executor, client: str | None = "entry-1") -> int:
    position_id = executor.store.open_pending("BTC/USDT", "trend", stop_price=98.0)
    assert position_id is not None
    if client is not None:
        executor.store.update_position(position_id, entry_client_order_id=client)
    return position_id


def test_an_unregistered_entry_waits_out_the_acceptance_window_then_voids(tmp_path, venue, monkeypatch) -> None:
    from datetime import timedelta

    from bot import executor as module

    executor = _executor(tmp_path)
    position_id = _pending(executor)
    executor.sync_positions()
    pending = executor.store.position(position_id)
    assert pending is not None and pending["status"] == "pending"
    later = module._now() + timedelta(seconds=module._ACCEPTANCE_WINDOW_SECONDS + 1)
    monkeypatch.setattr(module, "_now", lambda: later)
    executor.sync_positions()
    voided = executor.store.position(position_id)
    assert voided is not None and voided["status"] == "void"


def test_a_dead_entry_that_never_filled_is_voided(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    position_id = _pending(executor)
    venue.orders["entry-1"] = {"status": "ok", "order": {"status": "expired", "filled": "0"}}
    executor.sync_positions()
    voided = executor.store.position(position_id)
    assert voided is not None and voided["status"] == "void" and "sem execução" in str(voided["reason"])


def test_a_missing_or_dead_stop_is_put_back_on_the_book(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    # Default lookup answer is not_found: the stop vanished from the exchange.
    before = sum(1 for call in venue.calls if call["order_type"] == "stop_loss_limit")
    executor.sync_positions()
    after = sum(1 for call in venue.calls if call["order_type"] == "stop_loss_limit")
    assert after == before + 1
    reprotected = executor.store.open_positions()[0]
    assert reprotected["stop_client_order_id"] not in (None, position["stop_client_order_id"])
    client = str(reprotected["stop_client_order_id"])
    venue.orders[client] = {"status": "ok", "order": {"status": "canceled", "filled": "0", "order_id": "gone"}}
    executor.sync_positions()
    assert sum(1 for call in venue.calls if call["order_type"] == "stop_loss_limit") == after + 1


def test_the_stop_is_priced_at_the_last_trade_when_it_is_above_the_bid(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    venue.cap = Decimal("130")
    venue.last = Decimal("140")
    # 0.999 BTC is 109.9 at the bid but 139.9 at the last trade the mandate gate uses: the stop stays.
    assert "teto" in executor.trail(position["id"], Decimal("110"), reserve=2)
    assert venue.cancelled == []
    venue.last = None
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "movido"



def test_a_position_keeps_the_hit_rate_it_was_entered_at(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    unrated = executor.enter(_ready())
    rated = executor.enter(_ready(symbol="ETH/USDT", win_rate=0.66))
    assert executor.store.position(unrated["position_id"])["entry_win_rate"] is None
    assert executor.store.position(rated["position_id"])["entry_win_rate"] == pytest.approx(0.66)


def test_each_cancel_and_replace_records_its_window(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("110")
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "movido"
    day_id = executor.store.open_pending("ETH/USDT", "intraday_momentum", stop_price=98.0, target_price=103.0, exit_policy="day")
    assert day_id is not None
    executor.store.update_position(day_id, status="open", qty=1.0, avg_price=100.0, stop_order_id="stop-day")
    venue.bid = Decimal("103.5")
    assert executor.take_profit(day_id, reserve=1) == "vendido"
    assert executor.open_instance(1, "grid", "SOL/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"
    grid = next(row for row in executor.store.open_positions() if row["symbol"] == "SOL/USDT")
    assert executor.scale_in(grid["id"], Decimal("10"), "grid-buy-1", Decimal("90"), reserve=2) == "comprado"
    # An exit the IOC fills only in part puts the stop back on the rest.
    venue.bid = Decimal("110")
    venue.responses["sell:limit"] = [{"status": "ok", "filled": "0.5", "cost": "55", "fees": []}]
    assert executor.exit_position(position["id"], "alvo atingido")["status"] == "error"
    stats = executor.store.stop_window_stats("2000-01-01T00:00:00+00:00")
    assert stats["count"] == 4
    assert set(stats["by_kind"]) == {"trail", "tp1", "restop", "exit_remainder"}
    assert all(item["count"] == 1 and item["max_ms"] >= 0 for item in stats["by_kind"].values())


def test_no_window_is_recorded_when_the_stop_never_comes_back(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.bid = Decimal("105")
    assert executor.exit_position(position["id"], "alvo atingido")["status"] == "ok"
    assert executor.store.stop_window_stats("2000-01-01T00:00:00+00:00")["count"] == 0

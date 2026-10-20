"""Executor fixes of the rigorous review: resting-buy branches, the carried stop sale, the scale-in
acceptance window and the exposure the gate sees (package P-D)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from bot.evaluator import SPOT_TAKER, Opportunity
from bot.exchange import sdk
from bot.exchange.service import submit_order as gate_submit_order
from bot.executor import REST_PLACED, Executor, UnprotectedPosition
from bot.mandate_io import commit_binance_mandate
from bot.store import Store

PROFILE = "binance-paper-trade"
#: The resting buy of ``_rest``: price, quantity and the stop over the whole after its fill.
REST_PRICE = Decimal("95")
REST_QTY = Decimal("0.1")
REST_STOP = Decimal("85")
#: The instance stop of ``_instance`` (client and exchange id of the first stop the venue places).
STOP_CLIENT_ID = "c2"
STOP_ORDER_ID = "stop2"


def _instance(tmp_path) -> tuple[Executor, dict[str, Any]]:
    """A grid inventory of 0.1996 BTC bought at about 100, its stop resting at 90."""
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE, ledger=lambda _kind, _payload: None)
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"
    position = executor.store.open_positions()[0]
    assert position["qty"] == pytest.approx(0.1996) and position["stop_order_id"] == STOP_ORDER_ID
    return executor, position


def _rest(executor: Executor, position: dict[str, Any]) -> str:
    return executor.rest_buy(position["id"], REST_PRICE, REST_QTY, "grid-rest-1-1", REST_STOP, level=1, reserve=0)


def _leg(executor: Executor, position_id: int) -> dict[str, Any] | None:
    raw = executor.store.position(position_id)["pending_leg"]
    return json.loads(raw) if raw else None


# -- item 1: resting-buy branches -----------------------------------------------------


def test_a_refused_resting_buy_clears_its_leg(tmp_path, venue) -> None:
    executor, position = _instance(tmp_path)
    venue.responses["buy:gtc"] = [{"status": "error", "error": "saldo insuficiente na corretora"}]
    assert _rest(executor, position) == "saldo insuficiente na corretora"
    # The leg recorded before the send is gone: nothing rests, so nothing blocks the next buy or exit.
    assert _leg(executor, position["id"]) is None
    decision = executor.store.decisions(limit=1)[0]
    assert decision["decision"] == "bloqueado" and "compra limitada recusada" in decision["reason"]


def test_a_resting_buy_filled_on_arrival_is_merged_and_restopped_at_once(tmp_path, venue) -> None:
    executor, position = _instance(tmp_path)
    venue.fill_rest_on_arrival = True
    assert _rest(executor, position) == "comprado"
    merged = executor.store.position(position["id"])
    # 0.1 BTC less the 0.0001 BTC fee, with the sell margin kept as dust: 0.0998 added.
    assert merged["pending_leg"] is None and merged["qty"] == pytest.approx(0.1996 + 0.0998)
    assert merged["stop_price"] == float(REST_STOP)
    # The old stop came off and one stop covers the whole; the filled buy needed no cancel.
    assert venue.cancelled == [STOP_ORDER_ID]
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    assert Decimal(stop["quantity"]) == Decimal(str(merged["qty"])) and merged["stop_order_id"] is not None


def test_cancel_resting_buys_marks_an_unprotected_position(tmp_path, venue, monkeypatch) -> None:
    executor, position = _instance(tmp_path)
    assert _rest(executor, position) == REST_PLACED

    def unprotected(_position_id: int) -> str:
        raise UnprotectedPosition("BTC/USDT: o stop executou durante o aumento")

    monkeypatch.setattr(executor, "cancel_rest", unprotected)
    assert executor.cancel_resting_buys() == [
        {
            "position_id": position["id"],
            "symbol": "BTC/USDT",
            "status": "error",
            "error": "BTC/USDT: o stop executou durante o aumento",
            "unprotected": True,
        }
    ]


def test_cancel_resting_buys_reports_a_failure_without_stopping(tmp_path, venue, monkeypatch) -> None:
    executor, position = _instance(tmp_path)
    assert _rest(executor, position) == REST_PLACED

    def broken(_position_id: int) -> str:
        raise RuntimeError("binance fora do ar")

    monkeypatch.setattr(executor, "cancel_rest", broken)
    assert executor.cancel_resting_buys() == [
        {"position_id": position["id"], "symbol": "BTC/USDT", "status": "error", "error": "RuntimeError: binance fora do ar"}
    ]


def test_a_dead_stop_that_sold_everything_settles_the_resting_buy_first(tmp_path, venue) -> None:
    executor, position = _instance(tmp_path)
    assert _rest(executor, position) == REST_PLACED
    rest_order = venue.resting_order()
    venue.orders[STOP_CLIENT_ID] = {
        "status": "ok",
        "order": {"status": "expired", "filled": "0.1996", "cost": "17.8642", "order_id": STOP_ORDER_ID},
    }
    executor.sync_positions()
    closed = executor.store.position(position["id"])
    assert closed["status"] == "closed" and closed["pending_leg"] is None
    # The buy came off the book before the row closed: no order left that could buy outside a row.
    assert rest_order["order_id"] in venue.cancelled and rest_order["status"] == "canceled"


# -- item 2: the carried stop sale is booked at the average before the buy ------------


def test_a_carried_stop_sale_is_booked_against_the_average_before_the_buy(tmp_path, venue) -> None:
    executor, position = _instance(tmp_path)
    assert _rest(executor, position) == REST_PLACED
    venue.fill_resting()
    sold = Decimal("0.1996")
    proceeds = sold * Decimal("85")
    venue.orders[STOP_CLIENT_ID] = {
        "status": "ok",
        "order": {"status": "closed", "filled": str(sold), "cost": str(proceeds), "average": "85", "order_id": STOP_ORDER_ID},
    }
    venue.bid = Decimal("90")
    executor.sync_positions()
    kept = executor.store.position(position["id"])
    assert kept["status"] == "open" and kept["qty"] == pytest.approx(0.0998)
    avg_before = Decimal(str(position["avg_price"]))
    fee = proceeds * Decimal(str(SPOT_TAKER))
    expected = proceeds - avg_before * sold - fee
    # Against the merged average (lower, it includes the buy at 95) the loss would be about 0.34 USD smaller.
    assert kept["realized_pnl"] == pytest.approx(float(expected), abs=1e-9)
    assert executor.store.pnl_days()[0]["net_pnl"] == pytest.approx(float(expected), abs=1e-9)


# -- item 4: a scale-in buy of unknown result times out from its own send -------------


def _clock(monkeypatch, start: datetime) -> dict[str, datetime]:
    clock = {"now": start}
    monkeypatch.setattr("bot.executor._now", lambda: clock["now"])
    return clock


def test_an_unknown_scale_in_buy_times_out_from_its_send_while_marks_move(tmp_path, venue, monkeypatch) -> None:
    executor, position = _instance(tmp_path)
    now = datetime.now(timezone.utc)
    clock = _clock(monkeypatch, now - timedelta(seconds=300))
    venue.responses["market"] = [{"status": "unknown", "error": "timeout"}]
    assert executor.scale_in(position["id"], Decimal("10"), "grid-buy-1", Decimal("90"), reserve=2) == "compra pendente"
    assert _leg(executor, position["id"])["placed_at"] == clock["now"].isoformat()
    clock["now"] = now
    # A new low moves ``updated_at`` (the price keeps falling); Binance never heard of the buy.
    executor.mark(position["id"], Decimal("92"))
    executor.sync_positions()
    assert _leg(executor, position["id"]) is None
    # So the exit is no longer blocked by the dead leg.
    assert executor.exit_position(position["id"], "stop do grid")["status"] == "ok"


def test_a_legacy_scale_in_leg_without_placed_at_times_out_from_updated_at(tmp_path, venue, monkeypatch) -> None:
    executor, position = _instance(tmp_path)
    leg = {"kind": "buy", "leg": "grid-buy-1", "client_order_id": "legacy", "stop": "90"}
    executor.store.update_position(position["id"], pending_leg=json.dumps(leg))
    updated = datetime.fromisoformat(executor.store.position(position["id"])["updated_at"])
    clock = _clock(monkeypatch, updated + timedelta(seconds=60))
    executor.sync_positions()
    assert _leg(executor, position["id"]) == leg
    clock["now"] = updated + timedelta(seconds=300)
    executor.sync_positions()
    assert _leg(executor, position["id"]) is None


# -- item 5: the gate counts a resting buy as held -----------------------------------------


def test_held_counts_a_resting_buy_on_its_symbol_for_buys_only(tmp_path, venue) -> None:
    executor, position = _instance(tmp_path)
    held = [{"symbol": "BTC/USDT", "quantity": position["qty"]}]
    assert executor._held_for_buy() == held and executor._held_for_sell() == held
    assert _rest(executor, position) == REST_PLACED
    # The resting buy's own send did not count it twice: held was read before its leg was written.
    assert venue.calls[-1]["held"] == held
    assert executor._held_for_buy() == [*held, {"symbol": "BTC/USDT", "quantity": float(REST_QTY)}]
    assert executor._held_for_sell() == held


def _real_gate(monkeypatch, tmp_path, *, cap: float, capital: float = 1000, max_order: float = 50):
    """Route ``service.submit_order`` through the real mandate gate; returns (placed, gate answers).
    Every symbol quotes 100; a placed order fills in full at its limit."""
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    commit_binance_mandate(capital_usd=capital, max_order_notional_usd=max_order, max_total_exposure_usd=cap)
    placed: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []

    def place_spot_order(_profile, _config, **kwargs):
        placed.append(kwargs)
        number = f"g{len(placed)}"
        result = {"status": "ok", "order_id": number, "client_order_id": number}
        if kwargs.get("order_type") == "limit" and kwargs.get("side") == "sell":
            qty = Decimal(str(kwargs["quantity"]))
            result.update(filled=str(qty), cost=str(qty * Decimal(str(kwargs["limit_price"]))), fees=[])
        return result

    def gate(symbol: str, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        answers.append(gate_submit_order(symbol, profile_id, **kwargs))
        return answers[-1]

    monkeypatch.setattr("bot.exchange.execution.orchestrate.place_spot_order", place_spot_order)
    monkeypatch.setattr(sdk, "build_config", lambda *_args, **_kwargs: sdk.BinanceConfig(profile="paper"))
    monkeypatch.setattr(sdk, "get_account_snapshot", lambda _config=None: {"status": "ok", "balances": []})
    monkeypatch.setattr(
        sdk, "get_quote", lambda symbol, **_: {"status": "ok", "symbol": symbol, "quote": {"last": 100.0, "bid": 100.0}}
    )
    monkeypatch.setattr("bot.exchange.service.submit_order", gate)
    return placed, answers


def test_a_stop_and_an_exit_on_another_pair_pass_the_gate_while_a_resting_buy_rests(tmp_path, venue, monkeypatch) -> None:
    monkeypatch.setattr("bot.exchange.service.free_balance", lambda *_a: Decimal("10000"))
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE, ledger=lambda _kind, _payload: None)
    ids: dict[str, int] = {}
    for number, (symbol, notional) in enumerate((("ETH/USDT", 550), ("BTC/USDT", 275), ("SOL/USDT", 275)), start=1):
        opened = executor.open_instance(number, "grid", symbol, Decimal(notional), Decimal("90"))
        assert opened["status"] == "ok"
        ids[symbol] = opened["position_id"]
    # 220 USD resting on ETH, filling later without the gate.
    eth = executor.store.position(ids["ETH/USDT"])
    rested = executor.rest_buy(ids["ETH/USDT"], Decimal("95"), Decimal("2.2"), "grid-rest-1-1", Decimal("85"), level=1, reserve=0)
    assert rested == REST_PLACED
    assert {"symbol": "ETH/USDT", "quantity": 2.2} in executor._held_for_buy() and eth["qty"] > 5
    placed, answers = _real_gate(monkeypatch, tmp_path, cap=1000, capital=2000, max_order=500)
    btc = ids["BTC/USDT"]
    # The traced case: selling BTC leaves ~823 of positions, but ~1043 with the resting buy counted.
    probe = gate_submit_order(
        "BTC/USDT", PROFILE, side="sell", order_type="limit", quantity=str(executor.store.position(btc)["qty"]),
        limit_price="99.5", time_in_force="ioc", idempotency_key="probe", held=executor._held_for_buy(),
    )
    assert probe["status"] == "blocked" and "max_total_exposure_usd" in str(probe.get("breach"))
    # The protective stop goes to the book...
    executor.store.update_position(btc, stop_order_id=None, stop_client_order_id=None)
    assert executor.protect(btc) is True
    assert answers[-1]["status"] == "ok" and placed[-1]["order_type"] == "stop_loss_limit"
    assert executor.store.position(btc)["stop_order_id"] is not None
    # ...and the IOC exit sells the whole pair.
    assert executor.exit_position(btc, "saída manual")["status"] == "ok"
    assert answers[-1]["status"] == "ok" and placed[-1]["side"] == "sell" and placed[-1]["order_type"] == "limit"
    assert executor.store.position(btc)["status"] == "closed"
    # The resting buy on ETH is still there, untouched.
    assert (_leg(executor, ids["ETH/USDT"]) or {}).get("kind") == "rest_buy"


def _entry(size_usd: float) -> Opportunity:
    return Opportunity(
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
        size_usd=size_usd,
        stop_price=98.0,
        target_price=104.0,
    )


@pytest.mark.parametrize("resting", [False, True])
def test_the_gate_refuses_an_entry_once_the_resting_buy_counts(tmp_path, venue, monkeypatch, resting) -> None:
    executor, position = _instance(tmp_path)
    if resting:
        assert _rest(executor, position) == REST_PLACED
    # The real mandate gate from here on: 20 USD held, 10 resting, an entry of 15 under a 40 USD cap.
    placed, answers = _real_gate(monkeypatch, tmp_path, cap=40)
    decision = executor.enter(_entry(15.0))
    if not resting:
        # Without the resting buy the same entry fits: 19.96 + 15 <= 40.
        assert len(placed) == 1 and answers[0]["status"] != "blocked"
        return
    # 19.96 held + 10 resting + 15 > 40: refused before anything reaches Binance.
    assert placed == [] and answers[0]["status"] == "blocked"
    assert "max_total_exposure_usd" in str(answers[0].get("breach"))
    assert decision["decision"] == "bloqueado"

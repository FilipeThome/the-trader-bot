"""Resting LIMIT GTC buys of DCA and grid robots (review item 6.10, D18)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from bot.backtest.instances import replay_dca, replay_grid
from bot.executor import REST_PLACED, REST_RESTOP_PENDING, UnprotectedPosition
from bot.instances import ORDERS_PER_RESTING_LEG, DcaParams, GridParams, grid_k_from_qty, grid_qty
from test_bot_instances import DCA, GRID, _live_runtime, _price, _step

RESTING_GRID = GRID.model_copy(update={"resting_buys": True})
RESTING_DCA = DCA.model_copy(update={"resting_buys": True})


def _gtc(venue) -> list[dict]:
    return [call for call in venue.calls if call["order_type"] == "limit" and call.get("time_in_force") == "gtc"]


def _leg(runtime) -> dict | None:
    raw = runtime.store.open_positions()[0]["pending_leg"]
    return json.loads(raw) if raw else None


def _resting(tmp_path, monkeypatch, venue, params=RESTING_GRID, kind: str = "grid"):
    """A robot created at 100 whose next cycle rests its next buy."""
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    assert runtime.create_instance(kind, "BTC/USDT", params)["status"] == "ok"
    _step(runtime)
    assert len(_gtc(venue)) == 1
    return runtime, runtime.store.open_positions()[0]


def _stop_filled(venue, position: dict, price: str = "85") -> None:
    qty = Decimal(str(position["qty"]))
    venue.orders[position["stop_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "closed", "filled": str(qty), "cost": str(qty * Decimal(price)), "average": price},
    }


# -- placement --------------------------------------------------------------------


def test_dca_places_one_resting_limit_buy_at_the_next_safety_price(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue, RESTING_DCA, "dca")
    call = _gtc(venue)[0]
    assert call["side"] == "buy" and Decimal(call["limit_price"]) == 98 and Decimal(call["quantity"]) == Decimal("0.102")
    leg = _leg(runtime)
    assert leg is not None and leg["kind"] == "rest_buy" and leg["level"] == 1
    assert Decimal(leg["stop"]) == pytest.approx(Decimal(str(96.0 * 0.95)))
    assert leg["client_order_id"] in venue.orders and leg["order_id"] == venue.resting_order()["order_id"]
    # The stop keeps resting: the buy locks USDT, the stop the base.
    assert position["stop_order_id"] is not None and venue.cancelled == []
    state = runtime.store.instances()[0]["state"]
    assert state["orders_today"] == 2 + ORDERS_PER_RESTING_LEG and state["rest_so"]["index"] == 1
    _step(runtime)
    _step(runtime)
    assert len(_gtc(venue)) == 1, "at most one resting buy per instance"


def test_grid_rests_its_buy_at_the_level_under_the_lowest_held(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    call = _gtc(venue)[0]
    assert Decimal(call["limit_price"]) == 95 and Decimal(call["quantity"]) == Decimal("0.1052")
    assert _leg(runtime)["level"] == 1


def test_resting_buys_off_keeps_the_market_flow(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    _step(runtime)
    assert _gtc(venue) == [] and runtime.store.open_positions()[0]["pending_leg"] is None
    _price(runtime, venue, 94.0)
    _step(runtime)
    assert _gtc(venue) == [] and [call["order_type"] for call in venue.calls].count("market") == 2
    assert grid_k_from_qty(grid_qty(GRID), runtime.store.open_positions()[0]["qty"]) == 1


def test_old_params_load_with_resting_buys_off() -> None:
    assert DcaParams.model_validate(DCA.model_dump(exclude={"resting_buys"})).resting_buys is False
    assert GridParams.model_validate(GRID.model_dump(exclude={"resting_buys"})).resting_buys is False


# -- budget -------------------------------------------------------------------------


def test_a_resting_buy_needs_three_orders_of_budget(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", RESTING_GRID)
    position = runtime.store.open_positions()[0]
    reserve = 2
    venue.budget = ORDERS_PER_RESTING_LEG + reserve - 1
    args = (position["id"], Decimal("95"), Decimal("0.1052"), "grid-rest-1-9", Decimal("85"))
    assert runtime.executor.rest_buy(*args, level=1, reserve=reserve) == "orçamento diário de ordens"
    assert _gtc(venue) == [] and runtime.store.position(position["id"])["pending_leg"] is None
    venue.budget = ORDERS_PER_RESTING_LEG + reserve
    assert runtime.executor.rest_buy(*args, level=1, reserve=reserve) == REST_PLACED


def test_a_resting_buy_must_fit_the_robots_daily_orders(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    # The first buy and its stop used 2 of 4: a resting leg needs 3.
    runtime.create_instance("grid", "BTC/USDT", RESTING_GRID.model_copy(update={"max_orders_per_day": 4}))
    _step(runtime)
    assert _gtc(venue) == []
    assert "limite diário de ordens do robô" in runtime.store.instances()[0]["reason"]


def test_a_resting_buy_whose_fill_would_pass_the_order_cap_is_not_sent(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", RESTING_GRID)
    venue.cap = Decimal("25")
    _step(runtime)
    assert _gtc(venue) == [] and "teto por ordem" in runtime.store.instances()[0]["reason"]


# -- reconciliation -------------------------------------------------------------------


def test_a_filled_resting_buy_is_merged_and_restopped(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    venue.fill_resting()
    venue.events.clear()
    runtime.executor.sync_positions()
    merged = runtime.store.open_positions()[0]
    assert merged["qty"] > position["qty"] and merged["pending_leg"] is None
    assert merged["avg_price"] < position["avg_price"] and merged["stop_price"] == 85.0
    assert venue.events.index(f"cancel:{position['stop_order_id']}") < venue.events.index("submit:stop_loss_limit")
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    assert Decimal(stop["quantity"]) == Decimal(str(merged["qty"]))
    assert grid_k_from_qty(grid_qty(GRID), merged["qty"]) == 1
    # The next cycle rests the level under it.
    _price(runtime, venue, 96.0)
    _step(runtime)
    assert len(_gtc(venue)) == 2 and Decimal(_gtc(venue)[-1]["limit_price"]) == 90


def test_a_partial_fill_cancels_the_rest_merges_and_restops(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue, RESTING_DCA, "dca")
    order = venue.resting_order()
    venue.fill_resting(Decimal("0.05"))
    runtime.executor.sync_positions()
    assert order["order_id"] in venue.cancelled and order["status"] == "canceled"
    merged = runtime.store.open_positions()[0]
    assert merged["qty"] == pytest.approx(position["qty"] + 0.0499, abs=1e-4) and merged["pending_leg"] is None
    assert merged["stop_order_id"] is not None
    # The safety order counts as filled: the next one rests at the following price, never the remainder.
    _price(runtime, venue, 99.0)
    _step(runtime)
    state = runtime.store.instances()[0]["state"]
    assert state["filled"] == 1 and Decimal(_gtc(venue)[-1]["limit_price"]) == 96


def test_an_unknown_resting_buy_is_read_back_by_its_key(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", RESTING_GRID)
    venue.responses["buy:gtc"] = [{"status": "unknown", "error": "timeout"}]
    _step(runtime)
    leg = _leg(runtime)
    assert leg["kind"] == "rest_buy" and "order_id" not in leg
    # Within the acceptance window an order Binance does not know yet is not forgotten.
    runtime.executor.sync_positions()
    assert _leg(runtime) is not None
    venue.orders[leg["client_order_id"]] = {
        "status": "ok",
        "order": {"status": "closed", "order_id": "r9", "filled": "0.1052", "cost": "9.994", "average": "95", "fees": []},
    }
    held = runtime.store.open_positions()[0]["qty"]
    runtime.executor.sync_positions()
    assert runtime.store.open_positions()[0]["qty"] > held and _leg(runtime) is None


def test_a_dead_resting_buy_with_no_fill_is_forgotten(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    venue.resting_order()["status"] = "expired"
    runtime.executor.sync_positions()
    assert _leg(runtime) is None and runtime.store.open_positions()[0]["qty"] == position["qty"]


def test_grid_moves_its_resting_buy_when_k_changes(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    first = venue.resting_order()
    _price(runtime, venue, 106.0)
    _step(runtime)
    # The sale of level 2 cancelled the resting buy of level 1 first.
    assert first["status"] == "canceled" and venue.events.index(f"cancel:{first['order_id']}") < venue.events.index("submit:limit")
    assert grid_k_from_qty(grid_qty(GRID), runtime.store.open_positions()[0]["qty"]) == 3
    _step(runtime)
    assert len(_gtc(venue)) == 2 and Decimal(_gtc(venue)[-1]["limit_price"]) == 100
    assert _leg(runtime)["level"] == 2


def test_a_level_reached_before_the_fill_cancels_and_buys_next_cycle(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    first = venue.resting_order()
    _price(runtime, venue, 94.0)
    _step(runtime)
    assert first["status"] == "canceled" and _leg(runtime) is None
    _step(runtime)
    assert grid_k_from_qty(grid_qty(GRID), runtime.store.open_positions()[0]["qty"]) == 1


# -- exits, stops and blocks ------------------------------------------------------------


def test_exit_cancels_the_resting_buy_and_keeps_bought_coins_in_the_row(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    order = venue.resting_order()
    venue.fill_resting(Decimal("0.05"))
    venue.events.clear()
    result = runtime.stop_instance(runtime.store.instances()[0]["id"])
    assert result["instance_status"] == "stopped"
    # The resting buy comes off before the stop, and the sale covers what it bought.
    assert venue.events.index(f"cancel:{order['order_id']}") < venue.events.index(f"cancel:{position['stop_order_id']}")
    sold = sum(Decimal(call["quantity"]) for call in venue.calls if call["order_type"] == "limit" and call["side"] == "sell")
    assert sold > Decimal(str(position["qty"]))
    closed = runtime.store.position(position["id"])
    assert closed["status"] == "closed"
    # No stop was re-placed in between: the exit took the merged quantity as it was.
    assert "submit:stop_loss_limit" not in venue.events


def test_a_fill_between_the_status_read_and_the_cancel_is_booked(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    order = venue.resting_order()

    def fills_first(order_id: str) -> dict | None:
        if order_id == order["order_id"]:
            venue.fill_resting()
            return {"status": "error", "error": "Unknown order sent."}
        return None

    venue.on_cancel = fills_first
    assert runtime.executor.cancel_rest(position["id"]) == "compra limitada executada antes do cancelamento"
    merged = runtime.store.position(position["id"])
    assert merged["qty"] > position["qty"] and merged["pending_leg"] is None and merged["stop_order_id"] is not None


def test_an_unconfirmed_cancel_blocks_the_exit_and_keeps_the_stop(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    venue.on_cancel = lambda _order_id: {"status": "error", "error": "timeout"}
    sent = len(venue.calls)
    result = runtime.executor.exit_position(position["id"], "teste")
    assert result["status"] == "blocked" and "compra limitada" in result["error"]
    after = runtime.store.position(position["id"])
    assert after["stop_order_id"] == position["stop_order_id"] and _leg(runtime)["kind"] == "rest_buy"
    assert len(venue.calls) == sent and venue.cancelled == [venue.resting_order()["order_id"]]


def test_stop_fill_with_a_filled_resting_buy_keeps_the_row_open_and_exits(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    venue.fill_resting()
    _stop_filled(venue, position)
    venue.bid = Decimal("84")
    runtime.executor.sync_positions()
    closed = runtime.store.position(position["id"])
    # The stop sold what it covered; the coins the buy added were sold by the emergency IOC.
    assert closed["status"] == "closed" and closed["realized_pnl"] < 0
    ioc = [call for call in venue.calls if call["order_type"] == "limit" and call["side"] == "sell"]
    assert len(ioc) == 1 and Decimal(ioc[0]["quantity"]) == pytest.approx(Decimal("0.1050"), abs=Decimal("0.0002"))
    decisions = [row["reason"] for row in runtime.store.decisions(limit=50)]
    assert any("segue na posição" in reason for reason in decisions)


def test_stop_fill_with_a_resting_buy_filled_at_the_cancel_restops_the_bought_coins(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    order = venue.resting_order()
    _stop_filled(venue, position)
    venue.bid = Decimal("90")

    def fills_first(order_id: str) -> dict | None:
        if order_id == order["order_id"]:
            venue.fill_resting()
            return {"status": "error", "error": "Unknown order sent."}
        return None

    venue.on_cancel = fills_first
    runtime.executor.sync_positions()
    kept = runtime.store.position(position["id"])
    assert kept["status"] == "open" and kept["pending_leg"] is None
    assert kept["qty"] == pytest.approx(0.1050, abs=2e-4) and kept["avg_price"] == pytest.approx(95.1, abs=0.2)
    assert kept["realized_pnl"] < 0 and kept["stop_order_id"] is not None and kept["stop_price"] == 85.0
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    assert Decimal(stop["quantity"]) == Decimal(str(kept["qty"]))


def test_a_stop_fill_with_an_unreadable_resting_buy_halts(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    _stop_filled(venue, position)
    venue.orders[_leg(runtime)["client_order_id"]] = {"status": "error", "error": "read_failed"}
    with pytest.raises(UnprotectedPosition):
        runtime.executor.sync_positions()
    # Nothing is booked or closed while the buy may hold coins outside the row.
    assert runtime.store.position(position["id"])["status"] == "open"


def test_halt_cancels_resting_buys_before_the_flag(tmp_path, monkeypatch, venue) -> None:
    import bot.runtime as runtime_module

    runtime, position = _resting(tmp_path, monkeypatch, venue)
    order = venue.resting_order()
    venue.fill_resting(Decimal("0.05"))
    real_trip = runtime_module.trip_halt

    def trip(*args, **kwargs):
        venue.events.append("trip_halt")
        return real_trip(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "trip_halt", trip)
    venue.events.clear()
    runtime.halt("teste", flatten=False)
    assert venue.events.index(f"cancel:{order['order_id']}") < venue.events.index("trip_halt")
    # What it bought is merged and re-stopped before the flag blocks every order.
    assert venue.events.index("submit:stop_loss_limit") < venue.events.index("trip_halt")
    after = runtime.store.position(position["id"])
    assert after["qty"] > position["qty"] and after["pending_leg"] is None and after["stop_order_id"] is not None


def test_a_halt_goes_on_when_a_resting_buy_cannot_be_cancelled(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    venue.on_cancel = lambda _order_id: {"status": "error", "error": "timeout"}
    result = runtime.halt("teste", flatten=False)
    assert result["halted"] and runtime.halted and _leg(runtime)["kind"] == "rest_buy"


def test_a_buy_block_takes_the_resting_buy_off_the_book(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    order = venue.resting_order()
    with runtime.lock:
        runtime._instances(runtime.collector.snapshot, datetime.now(timezone.utc), clock_blocked=True)
    assert order["status"] == "canceled" and _leg(runtime) is None
    assert "compra limitada cancelada" in runtime.store.instances()[0]["reason"]
    # REDUCING refuses the placement itself.
    runtime.store.set_state("risk_state", {"level": "reducing", "drawdown": 0.12})
    position = runtime.store.open_positions()[0]
    assert runtime.executor.rest_buy(
        position["id"], Decimal("95"), Decimal("0.1052"), "grid-rest-1-9", Decimal("85"), level=1, reserve=2
    ).startswith("modo só reduzir")
    # So does the daily-loss pause, which blocks buys only (review F2).
    runtime.store.set_state("risk_state", {"level": "normal", "drawdown": 0.0})
    runtime.store.set_state("paused_until", "2999-01-01T00:00:00+00:00")
    assert runtime.executor.rest_buy(
        position["id"], Decimal("95"), Decimal("0.1052"), "grid-rest-1-9", Decimal("85"), level=1, reserve=2
    ).startswith("pausado")


def test_stopping_the_cycle_cancels_resting_buys(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    order = venue.resting_order()
    assert runtime.stop()["running"] is False
    assert order["status"] == "canceled" and _leg(runtime) is None


# -- replay -------------------------------------------------------------------------------


def _bars(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """(open, low, close) per 1h bar."""
    index = pd.date_range("2026-01-01", periods=len(rows), freq="1h", tz="UTC")
    opens, lows, closes = zip(*rows)
    highs = [max(o, c) for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=index)


def test_replay_with_resting_buys_fills_at_the_level_on_the_low() -> None:
    # A wick to 94.5 that closes at 99, then a rise that sells level 1 at 100.
    grid_bars = _bars([(100, 100, 100), (100, 94.5, 99), (99, 99, 100.5)])
    off = replay_grid(GRID, grid_bars, taker=0.001)
    on = replay_grid(RESTING_GRID, grid_bars, taker=0.001)
    assert off["grid_trades"] == 0 and on["grid_trades"] == 1
    assert on["realized_usd"] == pytest.approx(10 / 95 * 100.5 * 0.999 - 10 * 1.001, rel=1e-9)
    # The levels rest above the stop: a bar that falls to it fills 95 and 90 on the way, then sells them at 85 too.
    gap = _bars([(100, 100, 100), (100, 84, 86)])
    stopped = replay_grid(RESTING_GRID, gap, taker=0.001)
    held = 10 / 90 + 10 / 95 + 10 / 100 + 10 / 105
    paid = (10 / 100 * 100 + 10 / 105 * 100) * 1.001 + 2 * 10 * 1.001
    assert stopped["ended"] == "stop" and stopped["pnl_usd"] == pytest.approx(held * 85 * 0.999 - paid, rel=1e-9)
    assert stopped["pnl_usd"] < replay_grid(GRID, gap, taker=0.001)["pnl_usd"]
    # DCA: the safety order fills at 98 on the wick, so the take-profit comes sooner.
    dca_bars = _bars([(100, 100, 100), (100, 97.9, 99.5), (99.5, 99.5, 102.5)])
    assert replay_dca(DCA, dca_bars, taker=0.001)["wins"] == 0
    assert replay_dca(RESTING_DCA, dca_bars, taker=0.001)["wins"] == 1


# -- final review: an unresolved resting buy is never left unattended ------------------------


def _open_stop(venue, position: dict) -> None:
    """The venue's view of the position's resting stop: open, nothing filled."""
    venue.orders[position["stop_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "open", "filled": "0", "cost": "0", "order_id": position["stop_order_id"], "price": "84.57"},
    }


def _fills_on_cancel(venue, order: dict, *, stop: dict | None = None):
    """The resting buy fills just before its cancel lands; the old stop's cancel (if given) never lands."""

    def hook(order_id: str) -> dict | None:
        if order_id == order["order_id"]:
            venue.fill_resting()
            return {"status": "error", "error": "exchange rejected the order (OrderNotFound)"}
        if stop is not None and order_id == stop["stop_order_id"]:
            return {"status": "unknown", "error": "timeout"}
        return None

    return hook


def test_stop_keeps_the_cycle_running_while_a_resting_buy_cancel_is_unconfirmed(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    runtime.store.set_state("running", True)
    venue.on_cancel = lambda _order_id: {"status": "error", "error": "timeout"}
    result = runtime.stop()
    assert result["status"] == "error" and result["error"] == "compra limitada ainda no livro: o ciclo continua para resolver"
    assert runtime.store.get_state("running") is True and _leg(runtime)["kind"] == "rest_buy"
    # Once the cancel lands, the stop is clean.
    venue.on_cancel = None
    assert runtime.stop() == {"status": "ok", "running": False}
    assert venue.resting_order()["status"] == "canceled" and _leg(runtime) is None


def test_stop_keeps_the_cycle_running_when_the_bought_coins_are_not_restopped(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    runtime.store.set_state("running", True)
    hook = _fills_on_cancel(venue, venue.resting_order())

    def blocks_after_the_fill(order_id: str) -> dict | None:
        answer = hook(order_id)
        venue.blocker = "sem mandato válido"
        return answer

    venue.on_cancel = blocks_after_the_fill
    result = runtime.stop()
    # The buy filled and merged, but the gate refused its re-stop: the new coins have no stop yet.
    assert result["status"] == "error" and result["running"] is True
    assert runtime.store.get_state("running") is True
    assert json.loads(runtime.store.position(position["id"])["pending_leg"])["kind"] == "restop"
    # The cycle resolves it once the gate allows writes again.
    venue.on_cancel = None
    venue.blocker = None
    runtime.executor.sync_positions()
    after = runtime.store.position(position["id"])
    assert after["pending_leg"] is None and after["stop_order_id"] is not None
    assert after["qty"] > position["qty"]


def test_a_halt_records_a_resting_buy_whose_restop_is_pending_as_failed(tmp_path, monkeypatch, venue) -> None:
    import bot.runtime as runtime_module

    records: list[dict[str, Any]] = []
    monkeypatch.setattr(runtime_module, "append_ledger", records.append)
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    _open_stop(venue, position)
    venue.on_cancel = _fills_on_cancel(venue, venue.resting_order(), stop=position)
    result = runtime.halt("teste", flatten=False)
    assert result["halted"] and runtime.halted
    halt = next(record for record in records if record["kind"] == "halt")
    assert halt["resting_buys"] == 1 and halt["resting_failed"] == 1
    cancel_all = next(record for record in records if record["kind"] == "resting_buy" and record.get("event") == "cancel_all")
    assert cancel_all["results"][0]["outcome"] == REST_RESTOP_PENDING


def test_a_gapped_stop_next_to_an_unreadable_resting_buy_halts_on_the_third_sync(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    _open_stop(venue, position)
    venue.orders[_leg(runtime)["client_order_id"]] = {"status": "error", "error": "read_failed"}
    venue.bid = Decimal("84")
    # The exit is blocked by the unknown buy, not by the gate: it counts toward the halt.
    for retries in (1, 2):
        runtime.executor.sync_positions()
        assert runtime.store.get_state("stop_gapped_retries") == {str(position["id"]): retries}
    with pytest.raises(UnprotectedPosition, match="3 ciclos"):
        runtime.executor.sync_positions()
    assert not any(call["order_type"] == "limit" and call["side"] == "sell" for call in venue.calls)


def test_an_acknowledged_resting_buy_is_not_forgotten_on_one_not_found_read(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    leg = _leg(runtime)
    assert leg["order_id"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    runtime.store.update_position(position["id"], pending_leg=json.dumps({**leg, "placed_at": old}))
    venue.orders[leg["client_order_id"]] = {"status": "not_found"}
    # The read says not found, but the cancel by its id is accepted: it may still rest.
    runtime.executor.sync_positions()
    assert leg["order_id"] in venue.cancelled and _leg(runtime)["kind"] == "rest_buy"
    _step(runtime)
    assert len(_gtc(venue)) == 1, "no second resting buy next to one that may still be live"
    assert runtime.executor.exit_position(position["id"], "teste")["status"] == "blocked"
    # Binance knows no such order either: only then is it forgotten.
    venue.on_cancel = lambda _order_id: {"status": "error", "error": "exchange rejected the order (OrderNotFound)"}
    runtime.executor.sync_positions()
    assert _leg(runtime) is None


def test_an_unacknowledged_resting_buy_is_forgotten_after_the_acceptance_window(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", RESTING_GRID)
    venue.responses["buy:gtc"] = [{"status": "unknown", "error": "timeout"}]
    _step(runtime)
    position = runtime.store.open_positions()[0]
    leg = _leg(runtime)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    runtime.store.update_position(position["id"], pending_leg=json.dumps({**leg, "placed_at": old}))
    cancels = len(venue.cancelled)
    runtime.executor.sync_positions()
    assert _leg(runtime) is None and len(venue.cancelled) == cancels


def test_a_restop_whose_old_stop_stays_still_checks_and_counts_the_stuck_stop(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    _open_stop(venue, position)
    venue.fill_resting()
    venue.on_cancel = lambda order_id: {"status": "unknown", "error": "timeout"} if order_id == position["stop_order_id"] else None
    runtime.executor.sync_positions()
    assert json.loads(runtime.store.position(position["id"])["pending_leg"])["kind"] == "restop"
    # The price gaps through the old stop while its cancel keeps failing: the gap check still runs.
    venue.bid = Decimal("84")
    runtime.executor.sync_positions()
    counts = runtime.store.get_state("stop_gapped_retries")
    assert counts == {f"{position['id']}:restop": 1, str(position["id"]): 1}
    runtime.executor.sync_positions()
    with pytest.raises(UnprotectedPosition, match="3 ciclos"):
        runtime.executor.sync_positions()
    assert not any(call["order_type"] == "limit" and call["side"] == "sell" for call in venue.calls)


def test_a_restop_that_finally_lands_clears_its_count(tmp_path, monkeypatch, venue) -> None:
    runtime, position = _resting(tmp_path, monkeypatch, venue)
    _open_stop(venue, position)
    venue.fill_resting()
    venue.on_cancel = lambda order_id: {"status": "unknown", "error": "timeout"} if order_id == position["stop_order_id"] else None
    runtime.executor.sync_positions()
    runtime.executor.sync_positions()
    assert runtime.store.get_state("stop_gapped_retries") == {f"{position['id']}:restop": 1}
    venue.on_cancel = None
    venue.orders[position["stop_client_order_id"]]["order"]["status"] = "canceled"
    runtime.executor.sync_positions()
    after = runtime.store.position(position["id"])
    assert after["pending_leg"] is None and runtime.store.get_state("stop_gapped_retries") == {}

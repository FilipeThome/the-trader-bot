from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from bot.collector import Series
from bot.config import BotConfig, RiskLimits
from bot.evaluator import Opportunity
from bot.exchange import service
from bot.executor import Executor
from bot.instances import (
    MIN_NET_MARGIN,
    DcaParams,
    GridParams,
    creation_problems,
    dca_ladder,
    dca_next,
    dca_plan,
    grid_k_from_qty,
    grid_levels,
    grid_next,
    grid_plan,
    grid_qty,
    grid_start_k,
)
from bot.risk import allocate, open_risk
from bot.runtime import Runtime
from bot.store import Store
from test_bot_executor import PROFILE, _ready
from test_bot_runtime import FakeCollector, _snapshot

DCA = DcaParams(base_usd=10, so_count=2, step_pct=0.02, take_profit_pct=0.03, stop_pct=0.05)
GRID = GridParams(lower=90, upper=110, grids=4, invest_usd=40, stop_price=85)


# -- plans ----------------------------------------------------------------------


def test_the_dca_ladder_grows_its_distance_and_size_by_the_multipliers() -> None:
    params = DcaParams(base_usd=10, so_count=3, step_pct=0.01, step_mult=2, size_mult=1.5, take_profit_pct=0.02, stop_pct=0.05)
    ladder = dca_ladder(params, 100.0)
    assert [round(row["price"], 6) for row in ladder] == [100.0, 99.0, 97.0, 93.0]
    assert [row["usd"] for row in ladder] == [10, 15, 22.5, 33.75]
    assert ladder[-1]["avg"] < ladder[1]["avg"] < 100.0
    plan = dca_plan(params, 100.0, taker=0.001)
    assert plan["stop_price"] == pytest.approx(93.0 * 0.95)
    assert plan["total_usd"] == pytest.approx(81.25)
    assert plan["loss_at_stop_usd"] == pytest.approx(81.25 - ladder[-1]["qty"] * 93.0 * 0.95 + (81.25 + ladder[-1]["qty"] * 93.0 * 0.95) * 0.001)
    assert plan["orders_per_round"] == 9


def test_the_dca_takes_profit_on_the_average_and_buys_the_next_level_only() -> None:
    assert dca_next(DCA, 100.0, 0, 103.0, 100.0) == ("take_profit", 0)
    assert dca_next(DCA, 100.0, 0, 98.5, 100.0) is None
    assert dca_next(DCA, 100.0, 0, 97.0, 100.0) == ("safety", 1)
    assert dca_next(DCA, 100.0, 2, 80.0, 97.0) is None


def test_the_grid_holds_the_top_levels_and_trades_the_ones_crossed() -> None:
    levels = grid_levels(GRID)
    assert levels == [90.0, 95.0, 100.0, 105.0, 110.0]
    geometric = grid_levels(GridParams(lower=100, upper=121, grids=2, mode="geometric", invest_usd=20, stop_price=90))
    assert geometric == pytest.approx([100.0, 110.0, 121.0])
    assert grid_start_k(levels, 100.0) == 2 and grid_start_k(levels, 99.0) == 1
    # Held from level 2 up: a drop to 94 buys level 1, a rise to 106 sells levels 2 and 3's partner... only 2.
    assert grid_next(levels, 2, 100.0) is None
    assert grid_next(levels, 2, 94.0) == ("buy", 1)
    assert grid_next(levels, 2, 89.0) == ("buy", 0)
    assert grid_next(levels, 2, 105.0) == ("sell", 3)
    assert grid_next(levels, 1, 111.0) == ("sell", 4)
    qtys = grid_qty(GRID)
    assert qtys[0] == pytest.approx(10 / 90)
    assert grid_k_from_qty(qtys, sum(qtys[2:]) * 0.998) == 2
    assert grid_k_from_qty(qtys, 0.0) == 4


def test_a_grid_too_tight_to_pay_fees_and_spread_is_refused() -> None:
    tight = GridParams(lower=99, upper=101, grids=10, invest_usd=100, stop_price=95)
    plan = grid_plan(tight, 100.0, taker=0.001, spread=0.0005)
    assert plan["profit_per_grid_net"] < MIN_NET_MARGIN
    problems = creation_problems(
        "grid", plan, min_notional=5, order_cap=1000, sizing_capital=10_000, exposure_room=1000,
        risk_per_trade_pct=0.02, taker=0.001, spread=0.0005, params=tight,
    )
    assert any("lucro por grade" in item for item in problems)


def test_a_plan_over_the_order_cap_the_risk_or_the_minimum_is_refused() -> None:
    plan = dca_plan(DCA, 100.0, taker=0.001)
    problems = creation_problems(
        "dca", plan, min_notional=12, order_cap=20, sizing_capital=100, exposure_room=1000,
        risk_per_trade_pct=0.01, taker=0.001, spread=0.0, params=DCA,
    )
    assert any("abaixo do mínimo" in item for item in problems)
    assert any("teto por ordem" in item for item in problems)
    assert any("risco por trade" in item for item in problems)


def test_params_are_checked_on_entry() -> None:
    with pytest.raises(ValueError):
        GridParams(lower=100, upper=90, grids=4, invest_usd=40, stop_price=80)
    with pytest.raises(ValueError):
        GridParams(lower=90, upper=110, grids=4, invest_usd=40, stop_price=95)
    with pytest.raises(ValueError):
        DcaParams(base_usd=10, so_count=8, step_pct=0.2, step_mult=2, take_profit_pct=0.02, stop_pct=0.05)


def test_a_reservation_counts_in_open_risk_and_holds_its_pair() -> None:
    reservation = {"symbol": "BTC/USDT", "strategy": "grid", "status": "reserved", "qty": 0.0, "avg_price": 0.0, "reserved_risk": 7.0}
    assert open_risk([reservation], pending_risk=10.0) == 7.0
    row = Opportunity(
        symbol="BTC/USDT", strategy="trend", action="Comprar", score=0.9, net_return=0.02, risk_pct=0.02,
        status="pronta", reason="r", timeframe="1h", direction=1, cost=0.002, gross_return=0.04, size_usd=50,
    )
    accepted = allocate([row], cash=1000, live=[reservation], capital=1000, limits=RiskLimits(), min_notional=lambda _s: 5.0)
    assert accepted == [] and row.reason == "ativo já em carteira: uma posição por par"


# -- executor -------------------------------------------------------------------


def _opened(tmp_path, venue) -> tuple[Executor, dict]:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    executor.enter(_ready(size_usd=20))
    return executor, executor.store.open_positions()[0]


def test_scale_in_buys_first_then_moves_the_stop_over_the_whole(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.events.clear()
    assert executor.scale_in(position["id"], Decimal("10"), "dca-1-1-1", Decimal("90"), reserve=2) == "comprado"
    assert venue.events[0] == "submit:market" and venue.events[1].startswith("cancel:")
    grown = executor.store.position(position["id"])
    assert grown["qty"] > position["qty"] and grown["pending_leg"] is None and grown["stop_price"] == 90.0
    stop = [call for call in venue.calls if call["order_type"] == "stop_loss_limit"][-1]
    assert Decimal(stop["quantity"]) == Decimal(str(grown["qty"])) and Decimal(stop["stop_price"]) == 90


def test_scale_in_over_the_order_cap_sends_nothing_and_keeps_the_stop(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.cap = Decimal("25")
    sent = len(venue.calls)
    outcome = executor.scale_in(position["id"], Decimal("10"), "dca-1-1-1", Decimal("90"), reserve=2)
    assert outcome.startswith("posição depois da compra acima do teto")
    assert len(venue.calls) == sent and venue.cancelled == []
    assert executor.store.position(position["id"])["stop_order_id"] == position["stop_order_id"]


def test_an_unknown_scale_in_is_merged_by_the_sync(tmp_path, venue) -> None:
    executor, position = _opened(tmp_path, venue)
    venue.responses["market"] = [{"status": "unknown", "error": "timeout"}]
    assert executor.scale_in(position["id"], Decimal("10"), "grid-buy-1", Decimal("90"), reserve=2) == "compra pendente"
    leg = json.loads(executor.store.position(position["id"])["pending_leg"])
    assert leg["kind"] == "buy" and venue.cancelled == []
    venue.orders[leg["client_order_id"]] = {"status": "ok", "order": {"status": "closed", "filled": "0.1", "cost": "10", "fees": []}}
    executor.sync_positions()
    merged = executor.store.position(position["id"])
    assert merged["pending_leg"] is None and merged["qty"] > position["qty"] and merged["stop_price"] == 90.0
    assert merged["stop_order_id"] is not None


def test_an_unknown_grid_sale_is_settled_by_its_own_leg_key(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"
    position = executor.store.open_positions()[0]
    keep = Decimal(str(position["qty"])) / 2
    venue.responses["limit"] = [{"status": "unknown", "error": "timeout"}]
    executor.exit_position(position["id"], "grid vendeu 1 nível", keep_qty=keep, leg="grid-sell-1", next_stop=Decimal("90"))
    pending = executor.store.position(position["id"])
    assert pending["stop_order_id"] is None and json.loads(pending["pending_leg"]) == {"kind": "sell", "leg": "grid-sell-1", "qty_before": position["qty"]}
    attempt = int(pending["exit_attempts"])
    client_id = service.client_order_id(PROFILE, executor._key(position["id"], "grid-sell-1", attempt))
    sold = Decimal(str(position["qty"])) - keep
    venue.orders[client_id] = {"status": "ok", "order": {"status": "closed", "filled": str(sold), "cost": str(sold * 100)}}
    executor.sync_positions()
    settled = executor.store.position(position["id"])
    assert settled["qty"] == pytest.approx(float(keep)) and settled["keep_qty"] is None and settled["pending_leg"] is None
    assert settled["tp1_done"] == 0 and settled["stop_order_id"] is not None


# -- runtime --------------------------------------------------------------------


def _live_runtime(tmp_path, monkeypatch, **overrides) -> Runtime:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    config = BotConfig(
        symbols=["BTC/USDT"], strategies={"trend": False}, limits=RiskLimits(instance_orders_per_day=40), **overrides
    )
    return Runtime(config, Store(tmp_path / "bot.sqlite"), FakeCollector(_snapshot(["BTC/USDT"])))  # type: ignore[arg-type]


def _price(runtime: Runtime, venue, close: float) -> None:
    venue.bid = Decimal(str(close))
    now = datetime.now(timezone.utc)
    index = pd.DatetimeIndex([pd.Timestamp(now).floor("h")])
    frame = pd.DataFrame({"open": [close], "high": [close], "low": [close], "close": [close], "volume": [1.0]}, index=index)
    for interval in ("1h", "15m"):
        runtime.collector.snapshot.candles[("BTC/USDT", interval)] = Series(frame, now, "test")


def _step(runtime: Runtime, moment: datetime | None = None) -> None:
    with runtime.lock:
        runtime._instances(runtime.collector.snapshot, moment or datetime.now(timezone.utc))


def test_an_instance_needs_orders_reserved_for_robots(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    runtime.config = runtime.config.model_copy(update={"limits": RiskLimits()})
    preview = runtime.preview_instance("grid", "BTC/USDT", GRID)
    assert any("instance_orders_per_day" in item for item in preview["problems"])
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["status"] == "error"
    assert runtime.store.instances() == []


def test_a_grid_buys_the_top_then_trades_the_levels_it_crosses(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    created = runtime.create_instance("grid", "BTC/USDT", GRID)
    assert created["status"] == "ok", created
    qtys = grid_qty(GRID)
    position = runtime.store.open_positions()[0]
    assert position["strategy"] == "grid" and position["stop_price"] == 85.0
    assert grid_k_from_qty(qtys, position["qty"]) == 2
    assert runtime.create_instance("dca", "BTC/USDT", DCA)["error"].endswith("um por par")
    _price(runtime, venue, 94.0)
    _step(runtime)
    position = runtime.store.open_positions()[0]
    assert grid_k_from_qty(qtys, position["qty"]) == 1 and position["stop_price"] == 85.0
    _price(runtime, venue, 106.0)
    _step(runtime)
    position = runtime.store.open_positions()[0]
    assert grid_k_from_qty(qtys, position["qty"]) == 3 and position["keep_qty"] is None
    assert position["realized_pnl"] > 0
    _price(runtime, venue, 84.0)
    _step(runtime)
    assert runtime.store.open_positions() == []
    assert runtime.store.instances()[0]["status"] == "stopped"


def test_a_dca_buys_safety_orders_then_takes_profit_on_the_average(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    assert runtime.create_instance("dca", "BTC/USDT", DCA)["status"] == "ok"
    base = runtime.store.open_positions()[0]
    assert base["strategy"] == "dca" and base["stop_price"] == pytest.approx(96.0 * 0.95)
    _price(runtime, venue, 97.9)
    _step(runtime)
    grown = runtime.store.open_positions()[0]
    assert grown["qty"] > base["qty"] and grown["avg_price"] < base["avg_price"]
    assert runtime.store.instances()[0]["state"]["filled"] == 1
    _price(runtime, venue, 102.5)
    _step(runtime)
    assert runtime.store.open_positions() == []
    _step(runtime)
    instance = runtime.store.instances()[0]
    assert instance["status"] == "stopped" and instance["reason"] == "alvo do DCA atingido"
    assert instance["realized_pnl"] > 0


def test_a_halt_stops_instances_and_leaves_their_stops(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    runtime.halt("teste", flatten=False)
    assert runtime.store.instances()[0]["status"] == "stopped"
    assert runtime.store.open_positions()[0]["stop_order_id"] is not None


def test_stopping_an_instance_sells_what_it_holds(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    instance_id = runtime.create_instance("grid", "BTC/USDT", GRID)["id"]
    result = runtime.stop_instance(instance_id)
    assert result["instance_status"] == "stopped" and runtime.store.open_positions() == []
    assert runtime.stop_instance(instance_id)["status"] == "error"


def test_an_instance_reserves_its_risk_and_pair_for_the_strategies(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    reserved = [row for row in runtime._book() if row["status"] == "reserved"]
    assert len(reserved) == 1 and reserved[0]["reserved_risk"] > 0 and reserved[0]["symbol"] == "BTC/USDT"


def test_old_databases_gain_the_instance_columns_and_tables(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    Store(path).close()
    connection = sqlite3.connect(path)
    connection.executescript(
        "ALTER TABLE positions DROP COLUMN instance_id; ALTER TABLE positions DROP COLUMN pending_leg;"
        "DROP TABLE armed_entries; DROP TABLE bot_instances;"
    )
    connection.commit()
    connection.close()
    store = Store(path)
    position_id = store.open_pending("BTC/USDT", "grid", exit_policy="grid", instance_id=7)
    assert position_id is not None and store.position(position_id)["instance_id"] == 7
    assert store.create_instance("grid", "BTC/USDT", {}, {}) is not None
    assert store.create_instance("grid", "BTC/USDT", {}, {}) is None
    assert store.arm_entry("BTC/USDT", "trend", 95.0, "below", "2099-01-01T00:00:00+00:00") is not None


# -- review regressions -----------------------------------------------------------


def test_an_exit_waits_for_an_unknown_scale_in_buy_instead_of_orphaning_it(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))
    position = executor.store.open_positions()[0]
    venue.responses["market"] = [{"status": "unknown", "error": "timeout"}]
    executor.scale_in(position["id"], Decimal("10"), "grid-buy-1", Decimal("90"), reserve=2)
    sent = len(venue.calls)
    blocked = executor.exit_position(position["id"], "stop do grid")
    assert blocked["status"] == "blocked" and len(venue.calls) == sent and venue.cancelled == []
    leg = json.loads(executor.store.position(position["id"])["pending_leg"])
    venue.orders[leg["client_order_id"]] = {"status": "ok", "order": {"status": "closed", "filled": "0.1", "cost": "10", "fees": []}}
    assert executor.exit_position(position["id"], "stop do grid")["status"] == "ok"
    closed = executor.store.position(position["id"])
    # The buy was merged before the exit, so the sale covered it too.
    assert closed["status"] == "closed"
    sold = sum(Decimal(call["quantity"]) for call in venue.calls if call["order_type"] == "limit")
    assert sold > Decimal(str(position["qty"]))


def test_a_partial_sale_that_sold_nothing_is_dropped(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))
    position = executor.store.open_positions()[0]
    venue.responses["limit"] = [{"status": "ok", "filled": "0", "cost": "0", "fees": []}]
    result = executor.exit_position(position["id"], "grid", keep_qty=Decimal(str(position["qty"])) / 2, leg="grid-sell-1", next_stop=Decimal("90"))
    after = executor.store.position(position["id"])
    assert result["status"] == "error" and after["keep_qty"] is None and after["pending_leg"] is None
    assert after["stop_order_id"] is not None and after["qty"] == position["qty"]


def test_instances_never_buy_with_the_clock_blocked(tmp_path, monkeypatch, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"), clock_blocked=True)["status"] == "blocked"
    assert venue.calls == []
    runtime = _live_runtime(tmp_path / "rt", monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.collector.snapshot.clock_blocked = True
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["error"].startswith("relógio")
    runtime.collector.snapshot.clock_blocked = False
    runtime.create_instance("grid", "BTC/USDT", GRID)
    held = runtime.store.open_positions()[0]["qty"]
    _price(runtime, venue, 94.0)
    with runtime.lock:
        runtime._instances(runtime.collector.snapshot, datetime.now(timezone.utc), clock_blocked=True)
    assert runtime.store.open_positions()[0]["qty"] == held


def test_a_grid_sale_left_midway_is_dropped_once_the_price_falls_back(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    position = runtime.store.open_positions()[0]
    runtime.store.update_position(position["id"], keep_qty=position["qty"] / 2, pending_leg=json.dumps({"kind": "sell", "leg": "grid-sell-9"}))
    _price(runtime, venue, 101.0)
    sent = len(venue.calls)
    _step(runtime)
    after = runtime.store.open_positions()[0]
    assert after["keep_qty"] is None and after["pending_leg"] is None
    assert not any(call["order_type"] == "limit" for call in venue.calls[sent:])


def test_a_retry_that_sells_nothing_keeps_the_quantity_an_earlier_attempt_sold_down_to(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    executor.open_instance(1, "grid", "BTC/USDT", Decimal("40"), Decimal("90"))
    position = executor.store.open_positions()[0]
    qty = Decimal(str(position["qty"]))
    keep = qty / 2
    part = (qty - keep) / 4
    venue.responses["limit"] = [{"status": "ok", "filled": str(part), "cost": str(part * 100), "fees": []}]
    executor.exit_position(position["id"], "venda", keep_qty=keep, leg="tp1", next_stop=Decimal("90"))
    assert executor.store.position(position["id"])["keep_qty"] == pytest.approx(float(keep))
    venue.responses["limit"] = [{"status": "ok", "filled": "0", "cost": "0", "fees": []}]
    executor.exit_position(position["id"], "venda", keep_qty=keep, leg="tp1", next_stop=Decimal("90"))
    after = executor.store.position(position["id"])
    # Part of the sale was sold earlier: the planned kept quantity survives the empty retry.
    assert after["keep_qty"] == pytest.approx(float(keep)) and after["stop_order_id"] is not None
    assert json.loads(after["pending_leg"])["qty_before"] == pytest.approx(float(qty))


# -- rigorous review: limits, manual close, untruncated live rows, untested branches ------


def test_the_mandate_adds_the_robots_daily_orders_to_the_budget(tmp_path, monkeypatch) -> None:
    from bot.live.mandate.store import load_mandate
    from bot.mandate_io import ORDERS_PER_ENTRY, commit_mandate_for
    from bot.paths import pin_mode_root

    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    pin_mode_root(tmp_path / "testnet")
    config = BotConfig(strategies={"trend": True}, limits=RiskLimits(instance_orders_per_day=40))
    commit_mandate_for(config)
    mandate = load_mandate("binance")
    assert mandate is not None
    assert mandate.hard_caps.max_trades_per_day == ORDERS_PER_ENTRY * config.limits.max_trades_per_day + 40


def test_a_daily_loss_pauses_the_day_and_stops_robot_buys(tmp_path, monkeypatch, venue) -> None:
    from bot.risk import utc_day

    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    runtime.store.set_state("running", True)
    held = runtime.store.open_positions()[0]["qty"]
    runtime.store.record_day(utc_day(), net_pnl=-31.0)
    _price(runtime, venue, 94.0)
    _step(runtime)
    assert runtime.store.open_positions()[0]["qty"] == held
    assert "perda diária" in runtime.store.instances()[0]["reason"]
    # The pause refuses buys only: the cycle keeps running (review F2).
    assert runtime.store.get_state("paused_until") and runtime.running
    assert runtime.create_instance("dca", "ETH/USDT", DCA)["error"].startswith("pausado")


def test_a_drawdown_halts_and_stops_every_robot(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    runtime.store.record_equity(2000.0, "2026-01-01T00:00:00+00:00", pnl=1000.0)
    _step(runtime)
    assert runtime.halted and runtime.store.instances()[0]["status"] == "stopped"


def test_a_dca_with_repeat_opens_the_next_round_after_its_take_profit(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("dca", "BTC/USDT", DCA.model_copy(update={"repeat": True, "cooldown_minutes": 0}))
    first = runtime.store.open_positions()[0]["id"]
    _price(runtime, venue, 103.5)
    _step(runtime)
    assert runtime.store.open_positions() == []
    _price(runtime, venue, 101.0)
    _step(runtime)
    instance = runtime.store.instances()[0]
    assert instance["status"] == "active" and instance["state"]["round"] == 2
    second = runtime.store.open_positions()[0]
    assert second["id"] != first and instance["state"]["base_price"] == 101.0


def test_dca_repeat_waits_the_cooldown_after_take_profit(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("dca", "BTC/USDT", DCA.model_copy(update={"repeat": True}))
    _price(runtime, venue, 103.5)
    _step(runtime)
    assert runtime.store.open_positions() == []
    closed_at = datetime.now(timezone.utc)
    _price(runtime, venue, 101.0)
    _step(runtime, closed_at)
    instance = runtime.store.instances()[0]
    # The default one-minute pause between rounds (Binance's 60 s).
    assert instance["state"]["round"] == 1 and instance["reason"].startswith("pausa entre rodadas até")
    assert runtime.store.open_positions() == []
    _step(runtime, closed_at + timedelta(seconds=61))
    assert runtime.store.instances()[0]["state"]["round"] == 2 and len(runtime.store.open_positions()) == 1


def test_grid_restart_waits_the_cooldown(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    _price(runtime, venue, 111.0)
    _step(runtime)
    # Sold out above the range floor: the grid stays active, with an empty inventory.
    assert runtime.store.open_positions() == [] and runtime.store.instances()[0]["status"] == "active"
    sold_at = datetime.now(timezone.utc)
    _price(runtime, venue, 100.0)
    _step(runtime, sold_at)
    assert runtime.store.open_positions() == []
    assert runtime.store.instances()[0]["reason"].startswith("pausa entre rodadas até")
    _step(runtime, sold_at + timedelta(seconds=61))
    assert len(runtime.store.open_positions()) == 1


def test_creation_refuses_a_robot_past_max_instances(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    runtime.config = runtime.config.model_copy(update={"limits": RiskLimits(instance_orders_per_day=40, max_instances=1)})
    _price(runtime, venue, 100.0)
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["status"] == "ok"
    preview = runtime.preview_instance("dca", "ETH/USDT", DCA)
    assert any("máximo de 1" in item for item in preview["problems"])


@pytest.mark.parametrize(("mandate", "expected"), [
    (False, "teto por ordem indisponível: confirme o mandato em Controles"),
    (True, "teto por ordem do testnet indisponível: defina testnet_max_order_notional em binance.json"),
])
def test_a_missing_order_cap_names_its_fix(tmp_path, monkeypatch, venue, mandate: bool, expected: str) -> None:
    from bot.mandate_io import commit_mandate_for

    runtime = _live_runtime(tmp_path, monkeypatch)
    if mandate:
        commit_mandate_for(runtime.config)
    venue.cap = None
    _price(runtime, venue, 100.0)
    problems = runtime.preview_instance("grid", "BTC/USDT", GRID)["problems"]
    assert expected in problems
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["status"] == "error"


def test_a_grid_ends_when_its_stop_filled_at_the_broker(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    runtime.create_instance("grid", "BTC/USDT", GRID)
    position = runtime.store.open_positions()[0]
    runtime.store.close_position(position["id"], exit_price=84.5, realized_pnl=-3.0, fees=0.1)
    _price(runtime, venue, 95.0)
    sent = len(venue.calls)
    _step(runtime)
    instance = runtime.store.instances()[0]
    assert instance["status"] == "stopped" and instance["reason"] == "o stop do grid executou"
    assert len(venue.calls) == sent


def test_a_robot_whose_sale_is_refused_stays_stopping_until_it_sells(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    instance_id = runtime.create_instance("grid", "BTC/USDT", GRID)["id"]
    venue.blocker = "mandato ausente"
    assert runtime.stop_instance(instance_id)["instance_status"] == "stopping"
    assert runtime.store.open_positions()[0]["stop_order_id"] is not None
    venue.blocker = None
    _step(runtime)
    assert runtime.store.instances()[0]["status"] == "stopped" and runtime.store.open_positions() == []


def test_closing_a_robot_position_by_hand_stops_the_robot(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    instance_id = runtime.create_instance("grid", "BTC/USDT", GRID)["id"]
    position = runtime.store.open_positions()[0]
    assert runtime.close_position(position["id"]) == {"status": "ok", "instance_stopped": instance_id}
    _price(runtime, venue, 94.0)
    sent = len(venue.calls)
    _step(runtime)
    assert runtime.store.instance(instance_id)["status"] == "stopped"
    assert runtime.store.open_positions() == [] and len(venue.calls) == sent


def test_live_rows_are_never_truncated(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for index in range(55):
        store.create_instance("grid", f"C{index:02d}/USDT", {}, {})
        store.arm_entry(f"C{index:02d}/USDT", "trend", 1.0, "below", "2099-01-01T00:00:00+00:00")
    assert len(store.instances(live_only=True)) == 55 and len(store.instances()) == 50
    assert len(store.armed_entries(live_only=True)) == 55 and len(store.armed_entries()) == 50

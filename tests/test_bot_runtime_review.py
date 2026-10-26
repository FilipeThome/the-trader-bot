"""Runtime fixes of the rigorous review: risk state outside the cycle, the market filter failing
closed, and resting buys across stop, halt, resume and start."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest

from bot.collector import Series
from bot.runtime import RESTING_UNRESOLVED, RESUME_REFUSED_RESTING, Runtime
from bot.strategies import validation_params
from test_bot_instances import GRID, _live_runtime, _price
from test_bot_resting_buys import RESTING_GRID, _leg, _resting
from test_bot_runtime import FakeExecutor, _ready_cycle, _snapshot
from test_bot_runtime_risk import RISING, _btc_4h


def _priced(runtime: Runtime, symbol: str, close: float) -> None:
    now = datetime.now(timezone.utc)
    index = pd.DatetimeIndex([pd.Timestamp(now).floor("h")])
    frame = pd.DataFrame({"open": [close], "high": [close], "low": [close], "close": [close], "volume": [1.0]}, index=index)
    for interval in ("1h", "15m"):
        runtime.collector.snapshot.candles[(symbol, interval)] = Series(frame, now, "test")  # type: ignore[attr-defined]


# -- item 1: only the cycle measures and stores the risk level ----------------------------------


def test_create_instance_after_a_restart_writes_no_risk_state(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    # A position on a pair the panel's view has no price for yet: worth 60 now, bought at 100.
    position_id = runtime.store.open_pending("ETH/USDT", "trend", exit_policy="trailing")
    assert position_id is not None
    runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=100.0, stop_price=55.0, stop_order_id="s")
    _price(runtime, venue, 100.0)
    created = runtime.create_instance("grid", "BTC/USDT", GRID)
    assert created["status"] == "ok", created
    # The blind view measured nothing and stored nothing.
    for key in ("day_start_equity", "risk_state", "paused_until"):
        assert runtime.store.get_state(key) is None, key
    # The first cycle prices the position: the loss is from before today, so no daily pause.
    _priced(runtime, "ETH/USDT", 60.0)
    with runtime.lock:
        state = runtime._risk_state(runtime.collector.snapshot, datetime.now(timezone.utc))
    assert state.pause_day is False and runtime.store.get_state("paused_until") is None
    # Its start counts the priced position (the grid's fill cost moves it by cents).
    assert runtime.store.get_state("day_start_equity")["equity"] == pytest.approx(960.0, abs=0.1)


def test_create_instance_obeys_the_stored_level_and_pause(tmp_path, monkeypatch, venue) -> None:
    runtime = _live_runtime(tmp_path, monkeypatch)
    _price(runtime, venue, 100.0)
    reducing = {"level": "reducing", "since": "2026-01-01T00:00:00+00:00", "drawdown": 0.11, "reason": "modo só reduzir: x"}
    runtime.store.set_state("risk_state", reducing)
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["error"] == "modo só reduzir: x"
    runtime.store.set_state("risk_state", {**reducing, "level": "normal", "reason": ""})
    runtime.store.set_state("paused_until", "2999-01-01T00:00:00+00:00")
    assert runtime.create_instance("grid", "BTC/USDT", GRID)["error"] == "pausado até o próximo dia UTC"
    assert runtime.store.get_state("risk_state")["level"] == "normal" and runtime.store.get_state("day_start_equity") is None
    assert runtime.store.instances() == []


# -- item 2: the market filter fails closed ------------------------------------------------------


def _filtered(tmp_path, monkeypatch, symbols: list[str]) -> tuple[Runtime, FakeExecutor]:
    runtime, executor = _ready_cycle(
        tmp_path, monkeypatch, symbols=symbols, entry_mode="auto", min_win_rate=0.70, market_filter="btc_ema40d"
    )
    params = validation_params(
        "trend", "trailing", timeframes={"1h": {"passed": True, "wins": 30, "trades": 40, "mc_p95_dd_r": 0.0}},
        symbols=symbols, market_filter="btc_ema40d", hysteresis=True,
    )
    runtime.store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": {**params, "wins": 30, "trades": 40}})
    snapshot = _snapshot(symbols)
    snapshot.candles[("BTC/USDT", "4h")] = _btc_4h(RISING)
    runtime.collector.snapshot = snapshot  # type: ignore[attr-defined]
    return runtime, executor


def test_the_market_filter_blocks_entries_when_btc_failed_to_read(tmp_path, monkeypatch) -> None:
    runtime, executor = _filtered(tmp_path, monkeypatch, ["ETH/USDT"])
    runtime.collector.snapshot.failed = frozenset({"BTC/USDT"})  # type: ignore[attr-defined]
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and row["reason"] == "filtro de mercado: BTC sem leitura neste ciclo"
    assert executor.events[-1] == "act:0"
    # The same cycle with BTC read and above its EMA enters.
    clean, clean_executor = _filtered(tmp_path / "clean", monkeypatch, ["ETH/USDT"])
    clean.tick()
    assert clean.store.opportunities()[0]["status"] == "pronta" and clean_executor.events[-1] == "act:1"


def test_the_market_filter_blocks_entries_without_a_btc_4h_frame(tmp_path, monkeypatch) -> None:
    runtime, executor = _filtered(tmp_path, monkeypatch, ["BTC/USDT"])
    del runtime.collector.snapshot.candles[("BTC/USDT", "4h")]  # type: ignore[attr-defined]
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and row["reason"] == "filtro de mercado: BTC sem leitura neste ciclo"
    assert executor.events[-1] == "act:0"


# -- items 3 and 4: an unresolved resting buy never starts or leaves the cycle unattended ------


def _cancel_fails(venue) -> None:
    venue.on_cancel = lambda _order_id: {"status": "error", "error": "timeout"}


def test_stop_of_a_stopped_bot_with_an_unresolved_resting_buy_never_starts_the_cycle(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    runtime.config = runtime.config.model_copy(update={"entry_mode": "auto"})
    runtime.store.set_state("running", False)
    _cancel_fails(venue)
    result = runtime.stop()
    assert result["status"] == "error" and result["error"] == RESTING_UNRESOLVED and result["running"] is False
    assert runtime.store.get_state("running") is False and runtime.running is False
    assert _leg(runtime)["kind"] == "rest_buy"


def test_halt_resume_then_stop_with_a_failed_cancel_keeps_the_bot_stopped(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    runtime.config = runtime.config.model_copy(update={"entry_mode": "auto"})
    events: list[dict[str, Any]] = []
    runtime.publish = events.append
    _cancel_fails(venue)
    halted = runtime.halt("teste", flatten=False)
    # The halt shows the buy it could not resolve, in its result and to the panel.
    assert halted["resting"][0]["status"] == "error" and _leg(runtime)["kind"] == "rest_buy"
    published = next(event for event in events if event["type"] == "halt")
    assert published["resting"] == halted["resting"]
    # Resume tries the cancel again; while it fails the halt stays.
    refused = runtime.resume()
    assert refused["status"] == "error" and refused["error"] == RESUME_REFUSED_RESTING and refused["halted"] is True
    assert runtime.halted and _leg(runtime)["kind"] == "rest_buy"
    # Once the cancel lands, resume resolves it and the cycle stays stopped.
    venue.on_cancel = None
    assert runtime.resume() == {"status": "ok", "halted": False}
    assert _leg(runtime) is None and venue.resting_order()["status"] == "canceled"
    assert runtime.store.get_state("running") is False
    # Stop of the stopped bot leaves it stopped.
    assert runtime.stop() == {"status": "ok", "running": False}
    assert runtime.running is False


def test_start_resolves_a_leftover_resting_buy_first(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue, RESTING_GRID)
    # Left on the book by a halt whose cancel failed: the stopped cycle watches over nothing.
    runtime.store.set_state("running", False)
    _cancel_fails(venue)
    refused = runtime.start()
    assert refused["status"] == "error" and refused["error"] == RESTING_UNRESOLVED
    assert runtime.store.get_state("running") is False and _leg(runtime)["kind"] == "rest_buy"
    venue.on_cancel = None
    assert runtime.start()["status"] == "ok" and runtime.running is True
    assert _leg(runtime) is None and venue.resting_order()["status"] == "canceled"


def test_resume_halts_again_when_a_resting_buy_leaves_a_position_unprotected(tmp_path, monkeypatch, venue) -> None:
    runtime, _position = _resting(tmp_path, monkeypatch, venue)
    _cancel_fails(venue)
    runtime.halt("teste", flatten=False)
    monkeypatch.setattr(
        runtime.executor,
        "cancel_resting_buys",
        lambda: [{"position_id": 1, "status": "error", "error": "sem stop", "unprotected": True}],
    )
    result = runtime.resume()
    assert result["status"] == "error" and result["error"].startswith("posição sem proteção: sem stop")
    assert result["halted"] is True and runtime.halted
    assert "posição sem proteção" in runtime.store.get_state("halt_reason")

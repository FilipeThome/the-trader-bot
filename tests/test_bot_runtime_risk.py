"""The runtime's risk level (6.7, D5, D20, D23): drawdown steps, daily loss on equity, market filter, halt flatten."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from bot.collector import Series
from bot.config import BotConfig, RiskLimits
from bot.executor import Executor
from bot.instances import DcaParams
from bot.runtime import Runtime, profile_for
from bot.strategies import validation_params
from test_bot_runtime import FakeExecutor, _open, _ready_cycle, _runtime, _snapshot, _validated

PAIRS = ["BTC/USDT", "ETH/USDT"]


def _ledger(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("bot.runtime.append_ledger", lambda payload: events.append(dict(payload)))
    return events


def _kinds(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["kind"] == kind]


def _two_pairs(tmp_path, monkeypatch, **overrides: Any) -> tuple[Runtime, FakeExecutor]:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, symbols=PAIRS, **overrides)
    runtime.collector.snapshot = _snapshot(PAIRS)  # type: ignore[attr-defined]
    runtime.store.save_strategy(
        {"strategy_id": "trend", "signal_only": False, "params": _validated("trend", "trailing", symbols=PAIRS, wins=30, trades=40)}
    )
    return runtime, executor


def _rows(runtime: Runtime) -> dict[str, dict[str, Any]]:
    return {row["symbol"]: row for row in runtime.store.opportunities()}


def test_5pct_drawdown_halves_risk_and_allows_one_entry_per_cycle(tmp_path, monkeypatch) -> None:
    limits = RiskLimits(max_order_notional_pct=1.0, max_entries_per_cycle=5)
    runtime, executor = _two_pairs(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70, limits=limits)
    # 50 lost on 1000: past the 5% step, short of the 10% one.
    runtime.store.record_day("2026-01-01", net_pnl=-50.0)
    runtime.tick()
    state = runtime.store.get_state("risk_state")
    assert state["level"] == "cautious" and state["drawdown"] == pytest.approx(0.05)
    rows = _rows(runtime)
    entered = [row for row in rows.values() if row["status"] == "pronta"]
    refused = [row for row in rows.values() if row["status"] == "bloqueada"]
    # Half the 1% risk at a 1% stop: 500 instead of the 1000 the full share would size (then capped at 600 exposure).
    assert len(entered) == 1 and entered[0]["size_usd"] == pytest.approx(500.0) and entered[0]["size_limit"] == "risk"
    assert len(refused) == 1 and "1 entrada por ciclo no modo cauteloso" in refused[0]["reason"]
    assert "act:1" in executor.events


def test_10pct_drawdown_enters_reducing_exits_still_run_no_entries(tmp_path, monkeypatch) -> None:
    events = _ledger(monkeypatch)
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    snapshot = _snapshot(PAIRS)
    snapshot.candles[("ETH/USDT", "15m")] = snapshot.candles[("ETH/USDT", "1h")]
    runtime.collector.snapshot = snapshot  # type: ignore[attr-defined]
    target_id = runtime.store.open_pending("ETH/USDT", "trend", exit_policy="target")
    assert target_id is not None
    runtime.store.update_position(target_id, status="open", qty=1.0, avg_price=100.0, target_price=95.0, stop_price=80.0)
    # Lost before today's first cycle: a drawdown, not a daily loss.
    runtime.store.record_day("2026-01-01", net_pnl=-100.0)
    runtime.tick()
    state = runtime.store.get_state("risk_state")
    assert state["level"] == "reducing" and state["since"] and "modo só reduzir" in state["reason"]
    assert runtime.halted is False and runtime.running is True
    # The target exit still runs; the ready row is refused and nothing is bought.
    assert f"exit:{target_id}" in executor.events
    row = _rows(runtime)["BTC/USDT"]
    assert row["status"] == "bloqueada" and "modo só reduzir" in row["reason"]
    assert executor.events[-1] == "act:0"
    changes = _kinds(events, "risk_state")
    assert [(event["from"], event["to"]) for event in changes] == [("normal", "reducing")]
    # The executor reads the same record: every buy is refused at the choke point.
    assert "modo só reduzir: drawdown de 10.0%" in str(Executor(runtime.store, profile_for(runtime.config))._reducing())


def test_15pct_drawdown_halts(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    runtime.store.record_day("2026-01-01", net_pnl=-150.0)
    runtime.tick()
    assert runtime.halted is True and "drawdown de 15%" in runtime.store.get_state("halt_reason")
    # Stored as reducing: after a manual resume buys stay refused until the drawdown recovers.
    assert runtime.store.get_state("risk_state")["level"] == "reducing"
    assert not any(event.startswith("act:") and event != "act:0" for event in executor.events)


def test_reducing_blocks_instance_buys_armed_and_manual_entries(tmp_path, monkeypatch, venue) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    runtime.executor = Executor(runtime.store, profile_for(runtime.config), ledger=runtime._ledger)
    snapshot = runtime.collector.snapshot  # type: ignore[attr-defined]
    snapshot.candles[("BTC/USDT", "15m")] = snapshot.candles[("BTC/USDT", "1h")]
    armed = runtime.arm_entry("BTC/USDT", "trend", 100.5, 2)
    assert armed["status"] == "ok"
    runtime.store.record_day("2026-01-01", net_pnl=-100.0)
    # The trigger is reached in the reducing cycle.
    now = datetime.now(timezone.utc)
    index = pd.DatetimeIndex([pd.Timestamp(now).floor("h")])
    bars = pd.DataFrame({"open": [101.0], "high": [101.0], "low": [101.0], "close": [101.0], "volume": [1.0]}, index=index)
    for interval in ("1h", "15m"):
        snapshot.candles[("BTC/USDT", interval)] = Series(bars, now, "test")
    runtime.tick()
    assert runtime.store.get_state("risk_state")["level"] == "reducing"
    assert runtime.store.armed_entries(live_only=True)[0]["id"] == armed["id"]
    manual = runtime.execute_manual(_ready_row())
    assert manual["results"][0]["decision"] == "bloqueado" and "modo só reduzir" in manual["results"][0]["reason"]
    params = DcaParams(base_usd=10, so_count=1, step_pct=0.02, take_profit_pct=0.03, stop_pct=0.05)
    robot = runtime.create_instance("dca", "BTC/USDT", params)
    assert robot["status"] == "error" and "modo só reduzir" in robot["error"]
    assert not any(event.startswith("submit") for event in venue.events)
    assert runtime.store.live_positions() == []


def _ready_row() -> Any:
    from bot.evaluator import Opportunity

    return Opportunity(
        symbol="BTC/USDT", strategy="trend", action="Comprar", score=1, net_return=0.02, risk_pct=0.01,
        status="pronta", reason="r", timeframe="1h", direction=1, cost=0.002, gross_return=0.03, size_usd=20,
    )


def _price(runtime: Runtime, symbols: list[str], close: float) -> None:
    now = datetime.now(timezone.utc)
    index = pd.DatetimeIndex([pd.Timestamp(now).floor("h")])
    bars = pd.DataFrame({"open": [close], "high": [close], "low": [close], "close": [close], "volume": [1.0]}, index=index)
    for symbol in symbols:
        for interval in ("1h", "15m"):
            runtime.collector.snapshot.candles[(symbol, interval)] = Series(bars, now, "test")  # type: ignore[attr-defined]


def test_daily_loss_counts_unrealized_and_pauses(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch)
    _open(runtime, "BTC/USDT", "mean_reversion", qty=1, avg=100.0, stop=50.0)
    _price(runtime, ["BTC/USDT"], 100.0)
    runtime.store.set_state("running", True)
    runtime.tick()
    start = runtime.store.get_state("day_start_equity")
    assert start["equity"] == pytest.approx(1000.0) and start["day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert runtime.running is True
    # 40 lost on paper, nothing realised: past the 3% daily limit (30), under the 5% drawdown step.
    _price(runtime, ["BTC/USDT"], 60.0)
    runtime.tick()
    assert all(float(day["net_pnl"]) == 0 for day in runtime.store.pnl_days())
    assert runtime.store.get_state("paused_until") and runtime.running is False
    assert runtime.store.get_state("risk_state")["level"] == "normal"
    assert "perda diária" in runtime.store.get_state("risk_state")["reason"]


@pytest.mark.parametrize("enabled", [True, False])
def test_daily_loss_flattens_only_day_trades_when_enabled(tmp_path, monkeypatch, enabled: bool) -> None:
    events = _ledger(monkeypatch)
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, PAIRS, flatten_day_on_daily_loss=enabled)
    _price(runtime, PAIRS, 100.0)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    day_id = runtime.store.open_pending("BTC/USDT", "intraday_momentum", exit_policy="day", exit_deadline=deadline)
    swing_id = runtime.store.open_pending("ETH/USDT", "trend", exit_policy="trailing")
    assert day_id is not None and swing_id is not None
    for position_id in (day_id, swing_id):
        runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=100.0, stop_price=90.0, stop_order_id="s")
    runtime.store.set_state("running", True)
    runtime.tick()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runtime.store.record_day(today, net_pnl=-40.0)
    runtime.tick()
    assert runtime.store.get_state("paused_until") and runtime.running is False
    assert (f"exit:{day_id}" in executor.events) is enabled
    assert f"exit:{swing_id}" not in executor.events
    flattened = _kinds(events, "daily_loss_flatten")
    assert (len(flattened) == 1 and flattened[0]["positions"] == 1) if enabled else flattened == []


def _btc_4h(closes: list[float]) -> Series:
    now = datetime.now(timezone.utc)
    index = pd.date_range(end=pd.Timestamp(now).floor("4h") - pd.Timedelta(hours=4), periods=len(closes), freq="4h")
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
    return Series(frame, now, "test")


def _trend(step: float) -> list[float]:
    """260 closed 4h closes moving ``step`` every other bar: a steady trend with a steady volatility."""
    closes = [100.0]
    for index in range(1, 260):
        closes.append(closes[-1] * (1 + step if index % 2 else 1.0))
    return closes


RISING = _trend(0.004)
FALLING = _trend(-0.004)


def _filtered(tmp_path, monkeypatch, market_filter: str, closes: list[float]) -> tuple[Runtime, FakeExecutor]:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70, market_filter=market_filter)
    params = validation_params(
        "trend", "trailing", timeframes={"1h": {"passed": True, "wins": 30, "trades": 40}}, symbols=["BTC/USDT"],
        market_filter=market_filter, hysteresis=True,
    )
    runtime.store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": {**params, "wins": 30, "trades": 40}})
    runtime.collector.snapshot.candles[("BTC/USDT", "4h")] = _btc_4h(closes)  # type: ignore[attr-defined]
    return runtime, executor


def test_market_filter_blocks_entries_when_btc_is_below_ema40d(tmp_path, monkeypatch) -> None:
    runtime, executor = _filtered(tmp_path, monkeypatch, "btc_ema40d", FALLING)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and row["reason"] == "filtro de mercado: BTC abaixo da EMA de 40 dias"
    assert "act:0" in executor.events
    rising, rising_executor = _filtered(tmp_path / "rising", monkeypatch, "btc_ema40d", RISING)
    rising.tick()
    assert rising.store.opportunities()[0]["status"] == "pronta" and "act:1" in rising_executor.events


def test_market_filter_off_by_default(tmp_path, monkeypatch) -> None:
    assert BotConfig().market_filter == "off"
    runtime, executor = _filtered(tmp_path, monkeypatch, "off", FALLING)
    runtime.tick()
    assert runtime.store.opportunities()[0]["status"] == "pronta" and "act:1" in executor.events


@pytest.mark.parametrize(
    ("mandate", "config", "flattens"),
    [(True, False, True), (False, True, False), (None, True, True), (None, False, False)],
)
def test_halt_flattens_per_the_mandate_choice(tmp_path, monkeypatch, mandate: bool | None, config: bool, flattens: bool) -> None:
    from bot.mandate_io import commit_mandate_for

    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, flatten_on_halt=config)
    if mandate is not None:
        commit_mandate_for(runtime.config.model_copy(update={"flatten_on_halt": mandate}))
    runtime.halt("Parar tudo", by="frontend")
    assert any(event.startswith("flatten") for event in executor.events) is flattens
    assert runtime.halted is True


def test_a_resumed_halt_keeps_buys_refused_until_the_drawdown_recovers(tmp_path, monkeypatch) -> None:
    from bot.live.halt import clear_halt

    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    runtime.store.record_day("2026-01-01", net_pnl=-150.0)
    runtime.tick()
    assert runtime.halted is True
    clear_halt("binance")
    clear_halt(None)
    # The operator resumes at a 7% drawdown: under the 10% step, still past the 5% release, so exits only.
    runtime.store.record_day("2026-01-02", net_pnl=80.0)
    assert runtime.start()["status"] == "ok"
    runtime.tick()
    state = runtime.store.get_state("risk_state")
    assert runtime.halted is False and state["level"] == "reducing" and "ainda acima de 5%" in state["reason"]
    assert executor.events[-1] == "act:0"
    # Below 5%, the level clears by itself.
    runtime.store.record_day("2026-01-03", net_pnl=30.0)
    runtime.tick()
    assert runtime.store.get_state("risk_state")["level"] == "normal" and executor.events[-1] == "act:1"


def test_the_risk_state_shape_is_what_the_executor_and_panel_read(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    runtime.tick()
    state = runtime.store.get_state("risk_state")
    assert set(state) == {"level", "since", "drawdown", "reason"}
    first = state["since"]
    runtime.tick()
    # Unchanged level: the time it started is kept.
    assert runtime.store.get_state("risk_state")["since"] == first
    assert state["level"] == "normal" and state["drawdown"] == 0 and state["reason"] == ""

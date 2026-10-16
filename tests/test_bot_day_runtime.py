from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from bot.collector import MarketSnapshot, Series
from bot.strategies.base import Setup
from test_bot_runtime import FakeExecutor, _runtime

NOW = datetime(2026, 10, 16, 12, 7, tzinfo=timezone.utc)


class DayExecutor(FakeExecutor):
    def take_profit(self, position_id: int, *, reserve: int) -> str:
        self.events.append(f"tp1:{position_id}")
        return "vendido"


def _snapshot(close: float, moment: datetime = NOW) -> MarketSnapshot:
    index = pd.date_range(end=pd.Timestamp(moment).floor("15min") - pd.Timedelta(minutes=15), periods=80, freq="15min")
    m15 = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 10.0}, index=index)
    hours = pd.date_range(end=pd.Timestamp(moment).floor("h") - pd.Timedelta(hours=1), periods=80, freq="h")
    h1 = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 10.0}, index=hours)
    return MarketSnapshot(
        candles={("BTC/USDT", "15m"): Series(m15, moment, "test"), ("BTC/USDT", "1h"): Series(h1, moment, "test")}
    )


def _day_runtime(tmp_path, monkeypatch, close: float, **overrides: Any):
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, **overrides)
    executor = DayExecutor()
    runtime.executor = executor  # type: ignore[assignment]
    collector.snapshot = _snapshot(close)
    runtime.store.set_state("running", True)
    return runtime, executor


def _open_day(runtime: Any, *, deadline: datetime, target: float = 103.0, tp1_done: int = 0) -> int:
    position_id = runtime.store.open_pending(
        "BTC/USDT", "intraday_momentum", stop_price=98.0, target_price=target, exit_policy="day", exit_deadline=deadline.isoformat()
    )
    runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=100.0, stop_order_id="s", tp1_done=tp1_done)
    return int(position_id)


def test_a_day_trade_leaves_at_its_time_limit(tmp_path, monkeypatch) -> None:
    runtime, executor = _day_runtime(tmp_path, monkeypatch, close=104.0)
    position_id = _open_day(runtime, deadline=NOW - timedelta(minutes=1))
    runtime.tick(now=NOW)
    assert f"exit:{position_id}" in executor.events and f"tp1:{position_id}" not in executor.events


def test_the_first_target_triggers_on_a_closed_15m_bar_and_only_once(tmp_path, monkeypatch) -> None:
    runtime, executor = _day_runtime(tmp_path, monkeypatch, close=103.2)
    position_id = _open_day(runtime, deadline=NOW + timedelta(hours=5))
    runtime.tick(now=NOW)
    assert f"tp1:{position_id}" in executor.events
    # Never the swing "target reached" exit, never the trailing stop.
    assert f"exit:{position_id}" not in executor.events
    assert not any(event.startswith("trail:") for event in executor.events)
    runtime.store.update_position(position_id, tp1_done=1)
    executor.events.clear()
    runtime.tick(now=NOW + timedelta(minutes=1))
    assert f"tp1:{position_id}" not in executor.events


def test_below_the_first_target_nothing_moves(tmp_path, monkeypatch) -> None:
    runtime, executor = _day_runtime(tmp_path, monkeypatch, close=102.0)
    position_id = _open_day(runtime, deadline=NOW + timedelta(hours=5))
    runtime.tick(now=NOW)
    assert f"tp1:{position_id}" not in executor.events and f"exit:{position_id}" not in executor.events


def _day_setup(*_args: Any) -> Setup:
    return Setup(
        "intraday_momentum", "BTC/USDT", "15m", "Comprar", 1.0, 0.015, 0.004, 0.01, 0.015, "rompimento", "trend_up",
        confidence=0.9,
    )


def test_no_day_entry_in_the_quiet_hours(tmp_path, monkeypatch) -> None:
    quiet = datetime(2026, 10, 16, 4, 7, tzinfo=timezone.utc)
    runtime, _executor = _day_runtime(tmp_path, monkeypatch, close=100.0)
    runtime.config.strategies.update(trend=False, intraday_momentum=True)
    runtime.collector.snapshot = _snapshot(100.0, quiet)  # type: ignore[attr-defined]
    monkeypatch.setattr("bot.runtime._setup", _day_setup)
    runtime.tick(now=quiet)
    rows = runtime.store.opportunities()
    assert rows and rows[0]["status"] == "bloqueada" and "hora sem liquidez" in rows[0]["reason"]
    runtime.collector.snapshot = _snapshot(100.0)  # type: ignore[attr-defined]
    runtime.tick(now=NOW)
    assert "hora sem liquidez" not in runtime.store.opportunities()[0]["reason"]


def test_a_day_setup_reads_the_closed_15m_and_1h_bars(tmp_path, monkeypatch) -> None:
    from bot.runtime import _setup
    from bot.strategies import STRATEGIES

    seen: dict[str, Any] = {}

    class Recorder(type(STRATEGIES["intraday_momentum"])):  # type: ignore[misc]
        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, *, hourly: pd.DataFrame | None = None) -> None:
            seen.update(timeframe=timeframe, last=frame.index[-1], hour=None if hourly is None else hourly.index[-1])
            return None

    _setup(Recorder(), "BTC/USDT", _snapshot(100.0), 0.1, NOW)
    assert seen["timeframe"] == "15m"
    assert seen["last"] + pd.Timedelta(minutes=15) <= pd.Timestamp(NOW)
    assert seen["hour"] + pd.Timedelta(hours=1) <= pd.Timestamp(NOW)


def test_a_mandate_with_a_day_strategy_budgets_its_extra_orders(tmp_path, monkeypatch) -> None:
    from bot.config import BotConfig
    from bot.live.mandate.store import load_mandate
    from bot.mandate_io import ORDERS_PER_DAY_ENTRY, ORDERS_PER_ENTRY, commit_mandate_for
    from bot.paths import pin_mode_root

    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    pin_mode_root(tmp_path / "testnet")
    swing = BotConfig(strategies={"trend": True})
    commit_mandate_for(swing)
    mandate = load_mandate("binance")
    assert mandate is not None and mandate.hard_caps.max_trades_per_day == ORDERS_PER_ENTRY * swing.limits.max_trades_per_day
    day = BotConfig(strategies={"trend": True, "vwap_pullback": True})
    commit_mandate_for(day)
    mandate = load_mandate("binance")
    assert mandate is not None and mandate.hard_caps.max_trades_per_day == ORDERS_PER_DAY_ENTRY * day.limits.max_trades_per_day


def test_a_day_row_prices_its_first_target_from_the_setup(tmp_path, monkeypatch) -> None:
    runtime, _executor = _day_runtime(tmp_path, monkeypatch, close=100.0)
    runtime.config.strategies.update(trend=False, intraday_momentum=True)
    monkeypatch.setattr("bot.runtime._setup", _day_setup)
    runtime.tick(now=NOW)
    row = runtime.store.opportunities()[0]
    setup = _day_setup()
    assert setup.expected_return == setup.target_pct
    assert abs(row["target_price"] - 100.0 * (1 + setup.target_pct)) < 1e-9

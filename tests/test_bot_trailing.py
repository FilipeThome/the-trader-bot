from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from bot.config import BotConfig
from bot.executor import Executor
from bot.store import Store
from bot.strategies import validated_for, validation_params
from bot.strategies.trailing import TRAIL, trailing_level
from test_bot_executor import PROFILE, _ready
from test_bot_runtime import _runtime, _snapshot


def test_the_activated_trail_waits_for_the_gain_then_locks_a_profit() -> None:
    assert trailing_level("trailing_tp", 100.0, 101.9, 0.02) is None
    level = trailing_level("trailing_tp", 100.0, 102.0, 0.02)
    assert level == pytest.approx(102.0 * (1 - TRAIL.callback_share * 0.02))
    assert level > 100.0
    assert trailing_level("trailing", 100.0, 101.0, 0.02) == pytest.approx(98.98)
    assert trailing_level("target", 100.0, 120.0, 0.02) is None
    exact = trailing_level("trailing_tp", Decimal("100"), Decimal("104"), Decimal("0.02"))
    assert isinstance(exact, Decimal) and exact == Decimal("102.96")


def test_the_config_maps_the_two_flags_to_one_exit_policy() -> None:
    assert BotConfig(trailing_stop=False, trailing_activation=True).exit_policy == "target"
    assert BotConfig(trailing_stop=True).exit_policy == "trailing"
    assert BotConfig(trailing_stop=True, trailing_activation=True).exit_policy == "trailing_tp"


def test_a_trailing_tp_validation_holds_only_for_the_rules_it_replayed() -> None:
    params = validation_params(
        "trend", "trailing_tp", timeframes={"1h": {"passed": True}}, symbols=["BTC/USDT"], market_filter="off", hysteresis=True
    )
    assert params["trailing"] == TRAIL.params
    assert validated_for(params, "trend", "trailing_tp")
    assert not validated_for({key: value for key, value in params.items() if key != "trailing"}, "trend", "trailing_tp")
    assert not validated_for({**params, "exit_policy": "trailing"}, "trend", "trailing_tp")
    assert not validated_for({**params, "trailing": {**TRAIL.params, "activation_r": 2.0}}, "trend", "trailing_tp")


def test_the_executor_keeps_the_entry_stop_until_activation(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE, exit_policy="trailing_tp")
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    assert position["exit_policy"] == "trailing_tp"
    venue.bid = Decimal("101")
    assert executor.trail(position["id"], Decimal("101"), reserve=2) == "aguardando ativação do stop móvel"
    assert venue.cancelled == []
    venue.bid = Decimal("110")
    assert executor.trail(position["id"], Decimal("110"), reserve=2) == "movido"
    moved = executor.store.position(position["id"])
    assert moved is not None
    expected = Decimal("110") * (1 - Decimal(str(TRAIL.callback_share)) * Decimal(str(position["trail_pct"])))
    assert Decimal(str(moved["stop_price"])) == pytest.approx(expected, abs=Decimal("0.01"))
    assert moved["stop_price"] > position["avg_price"]


def test_the_replay_holds_the_entry_stop_before_activation(monkeypatch) -> None:
    from bot.backtest import runner
    from bot.strategies.base import Setup

    class Once:
        fired = False

        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
            if self.fired:
                return None
            self.fired = True
            return Setup("trend", symbol, timeframe, "Comprar", 1.0, 0.05, 0.01, 0.03, 0.05, "t", "trend_up")

    def replay(closes: list[float], policy: str) -> float:
        index = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
        frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
        monkeypatch.setattr(runner, "_strategy", lambda _id: Once())
        return runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy=policy)[0].net_return

    never_activated = [100.0] * 61 + [102.0, 99.0, 98.0, 96.5]
    # The plain trail follows 102 at 3% and exits at 98; the activated one keeps the 97 stop.
    assert replay(never_activated, "trailing") == pytest.approx(-0.02 - runner.ROUND_TRIP_COST - 2 * runner.LEG_SLIPPAGE)
    assert replay(never_activated, "trailing_tp") == pytest.approx(-0.035 - runner.ROUND_TRIP_COST - 2 * runner.LEG_SLIPPAGE)
    # Activated at 103: the stop then sits 1.5% under the highest close, so the drop to 108 exits at 108.
    activated = [100.0] * 61 + [103.0, 106.0, 110.0, 112.0, 108.0]
    assert replay(activated, "trailing_tp") == pytest.approx(0.08 - runner.ROUND_TRIP_COST - 2 * runner.LEG_SLIPPAGE)
    one = [runner.Trade(pd.Timestamp("2026-01-02", tz="UTC"), 0.01)]
    record = runner.promotion_record(runner.evaluate("trend", one, 1.0, exit_policy="trailing_tp"))
    assert record["params"]["exit_policy"] == "trailing_tp" and record["params"]["trailing"] == TRAIL.params


def test_the_runtime_trails_activated_positions_and_never_sells_them_at_the_target(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    snapshot = _snapshot(["BTC/USDT"])
    snapshot.candles[("BTC/USDT", "15m")] = snapshot.candles[("BTC/USDT", "1h")]
    position_id = runtime.store.open_pending("BTC/USDT", "trend", exit_policy="trailing_tp")
    assert position_id is not None
    runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=90.0, target_price=95.0, stop_price=85.0)
    runtime._trail(snapshot)
    assert executor.events == [f"trail:{position_id}:2"]
    runtime._exits(snapshot, datetime.now(timezone.utc))
    assert executor.events == [f"trail:{position_id}:2"]

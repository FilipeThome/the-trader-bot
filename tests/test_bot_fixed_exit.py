"""The operator's fixed take-profit and stop-loss, and the young-pair relaxation."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from bot.backtest import runner
from bot.collector import Series
from bot.config import BotConfig
from bot.executor import Executor
from bot.store import Store
from bot.strategies import exit_policy_for, validated_for
from bot.strategies.base import Setup
from bot.strategies.day import DAY, MIN_HOURLY_BARS, hourly_trend
from bot.strategies.fixed import FixedExit, parse_fixed
from test_bot_executor import PROFILE, _ready
from test_bot_runtime import _ready_cycle, _runtime, _validated

POLICY = "fixed:300:150"


def _setup_row(expected: float = 0.05) -> Setup:
    return Setup("trend", "BTC/USDT", "1h", "Comprar", 1.0, expected, 0.01, 0.02, 0.06, "t", "trend_up", confidence=1)


# -- rule and config ---------------------------------------------------------


def test_the_policy_string_carries_the_exact_percentages() -> None:
    config = BotConfig(take_profit_pct=0.03, stop_loss_pct=0.015, trailing_stop=True)
    assert config.exit_policy == POLICY
    assert parse_fixed(POLICY) == FixedExit(0.03, 0.015)
    assert parse_fixed("trailing") is None and parse_fixed("fixed:x:1") is None
    assert BotConfig().exit_policy == "trailing"


@pytest.mark.parametrize(("take", "stop"), [(0.03, None), (0.005, 0.01), (0.03, 0.001)])
def test_a_half_set_or_out_of_range_rule_is_refused(take: float | None, stop: float | None) -> None:
    with pytest.raises(ValidationError):
        BotConfig(take_profit_pct=take, stop_loss_pct=stop)


def test_the_rule_replaces_stop_and_target_without_inflating_the_signal() -> None:
    applied = FixedExit(0.03, 0.015).apply(_setup_row(expected=0.05))
    assert (applied.stop_pct, applied.target_pct, applied.expected_return) == (0.015, 0.03, 0.03)
    weak = FixedExit(0.08, 0.015).apply(_setup_row(expected=0.02))
    assert weak.target_pct == 0.08 and weak.expected_return == 0.02


def test_the_rule_covers_day_strategies_and_their_validation_needs_the_day_rules() -> None:
    assert exit_policy_for("intraday_momentum", POLICY) == POLICY
    assert exit_policy_for("intraday_momentum", "trailing") == "day"
    assert validated_for(_validated("trend", POLICY), "trend", POLICY)
    assert not validated_for(_validated("trend", "fixed:300:100"), "trend", POLICY)
    day = _validated("intraday_momentum", POLICY, "15m")
    assert day["day"] == DAY.params and validated_for(day, "intraday_momentum", POLICY)
    assert not validated_for({key: value for key, value in day.items() if key != "day"}, "intraday_momentum", POLICY)
    # The fixed rule replaces the EMA exit, so the hysteresis flag cannot stale a fixed-rule validation.
    assert validated_for(_validated("trend", POLICY), "trend", POLICY, hysteresis=False)


# -- backtest ----------------------------------------------------------------


class _Once:
    def __init__(self, strategy_id: str = "trend") -> None:
        self.fired = False
        self.strategy_id = strategy_id

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, **_kwargs: Any) -> Setup | None:
        if self.fired:
            return None
        self.fired = True
        return Setup(self.strategy_id, symbol, timeframe, "Comprar", 1.0, 0.05, 0.01, 0.10, 0.20, "t", "trend_up")


def _bars(closes: list[float], lows: list[float] | None = None, highs: list[float] | None = None) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": highs or closes, "low": lows or closes, "close": closes, "volume": 1.0}, index=index
    )


def test_the_replay_sells_on_a_close_at_the_target_not_on_a_wick(monkeypatch) -> None:
    closes = [100.0] * 61 + [101.0, 103.5]
    highs = [100.0] * 61 + [104.0, 103.5]
    monkeypatch.setattr(runner, "_strategy", lambda _id: _Once())
    trades = runner.simulate("trend", "BTC/USDT", _bars(closes, highs=highs), "1h", exit_policy=POLICY)
    # The wick to 104 is not a sale: the cycle sells at the last price, at 103.5 on the next close.
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(0.035 - runner.ROUND_TRIP_COST - 2 * runner.LEG_SLIPPAGE)
    assert trades[0].exit_time == _bars(closes).index[-1]


def test_the_replay_stop_wins_when_one_bar_reaches_both(monkeypatch) -> None:
    closes = [100.0] * 61 + [104.0]
    lows = [100.0] * 61 + [98.0]
    monkeypatch.setattr(runner, "_strategy", lambda _id: _Once())
    trades = runner.simulate("trend", "BTC/USDT", _bars(closes, lows=lows), "1h", exit_policy=POLICY)
    # The strategy's own 10% stop and 20% target are replaced by 1.5% and 3%.
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(-0.015 - runner.ROUND_TRIP_COST - 2 * runner.LEG_SLIPPAGE)


def test_a_day_strategy_under_the_rule_keeps_the_day_gate_and_records_its_rule() -> None:
    stamps = pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC")
    returns = [0.01, 0.005] * 20
    trades = [runner.Trade(stamp, value) for stamp, value in zip(stamps, returns)]
    result = runner.evaluate("intraday_momentum", trades, 40 / 365, exit_policy=POLICY)
    # The day gate (daily Sharpe, 30 in-sample trades, mean return per trade) judged it, under the rule's name.
    assert result.exit_policy == POLICY and result.passed and result.gate_trades >= runner.DAY_MIN_TRADES
    record = runner.promotion_record(result)
    assert record["params"]["exit_policy"] == POLICY and record["params"]["day"] == DAY.params
    assert validated_for(record["params"], "intraday_momentum", POLICY)


def test_a_day_strategy_replays_the_rule_not_the_day_exits(monkeypatch) -> None:
    class DayOnce(runner.IntradayMomentum):
        fired = False

        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, *, hourly: pd.DataFrame | None = None) -> Setup | None:
            if self.fired:
                return None
            self.fired = True
            return Setup("intraday_momentum", symbol, timeframe, "Comprar", 1.0, 0.015, 0.004, 0.01, 0.015, "t", "trend_up")

    monkeypatch.setattr(runner, "_strategy", lambda _id: DayOnce())
    index = pd.date_range("2026-01-02 08:00", periods=63, freq="15min", tz="UTC")
    closes = [100.0] * 61 + [101.6, 103.2]
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
    hourly = _bars([100.0] * 200)
    trades = runner.simulate("intraday_momentum", "BTC/USDT", frame, "15m", exit_policy=POLICY, hourly=hourly)
    # No half sale at the 1.5% TP1: the whole position leaves at the 3% target, with the day slippage.
    assert len(trades) == 1
    assert trades[0].net_return == pytest.approx(0.032 - runner.ROUND_TRIP_COST - 2 * runner.DAY_SLIPPAGE)


# -- live cycle --------------------------------------------------------------


def _fixed_cycle(tmp_path, monkeypatch, wins: int):
    runtime, executor = _ready_cycle(
        tmp_path, monkeypatch, entry_mode="auto", take_profit_pct=0.03, stop_loss_pct=0.015, min_reward_risk=5.0
    )
    runtime.store.save_strategy(
        {"strategy_id": "trend", "signal_only": False, "params": _validated("trend", POLICY, wins=wins, trades=40)}
    )
    return runtime, executor


def test_a_fixed_row_enters_above_the_breakeven_of_its_payoff(tmp_path, monkeypatch) -> None:
    # Laplace 17/42 = 0.405 clears the break-even of 1.5% against about 2.75% (0.35) plus 0.03,
    # far below the 60% swing floor; the 5.0 reward/risk floor does not apply to the rule.
    runtime, executor = _fixed_cycle(tmp_path, monkeypatch, wins=16)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "pronta" and "act:1" in executor.events
    assert row["risk_pct"] == 0.015
    assert row["stop_price"] == pytest.approx(98.5) and row["target_price"] == pytest.approx(103.0)
    weak, weak_executor = _fixed_cycle(tmp_path / "weak", monkeypatch, wins=14)
    weak.tick()
    assert "act:0" in weak_executor.events


def test_a_validation_under_other_percentages_is_signal_only(tmp_path, monkeypatch) -> None:
    runtime, executor = _fixed_cycle(tmp_path, monkeypatch, wins=30)
    runtime.store.save_strategy(
        {"strategy_id": "trend", "signal_only": False, "params": _validated("trend", "fixed:500:150", wins=30, trades=40)}
    )
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and "act:0" in executor.events


def test_a_fixed_position_leaves_only_at_its_target(tmp_path, monkeypatch) -> None:
    runtime, collector, executor = _runtime(tmp_path, monkeypatch, take_profit_pct=0.03, stop_loss_pct=0.015)
    position_id = runtime.store.open_pending("BTC/USDT", "trend", stop_price=98.5, target_price=103.0, exit_policy=POLICY)
    assert position_id is not None
    runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
    # A strategy exit signal never closes a position under the operator's rule.
    monkeypatch.setattr("bot.cycle.exits._exit_reason", lambda *_a, **_k: "sinal da estratégia")
    now = datetime.now(timezone.utc)

    def price(value: float) -> None:
        index = pd.DatetimeIndex([pd.Timestamp(now).floor("15min")])
        bars = pd.DataFrame({"open": [value], "high": [value], "low": [value], "close": [value], "volume": [1.0]}, index=index)
        collector.snapshot.candles[("BTC/USDT", "15m")] = Series(bars, now, "test")

    price(102.9)
    runtime._exits(collector.snapshot, now)
    assert executor.events == []
    price(103.0)
    runtime._exits(collector.snapshot, now)
    assert executor.events == [f"exit:{position_id}"]


def test_the_rule_counts_from_the_average_fill(tmp_path, venue) -> None:
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE, exit_policy=POLICY)
    assert executor.enter(_ready(stop_price=90.0, target_price=200.0))["decision"] == "operar"
    position = executor.store.open_positions()[0]
    avg = position["avg_price"]
    assert position["exit_policy"] == POLICY
    assert position["stop_price"] == pytest.approx(avg * 0.985)
    assert position["target_price"] == pytest.approx(avg * 1.03)
    stop = next(call for call in venue.calls if call["order_type"] == "stop_loss_limit")
    assert float(stop["stop_price"]) == pytest.approx(avg * 0.985, abs=0.01)


# -- young pairs -------------------------------------------------------------


def _frame(closes: list[float], freq: str, start: str = "2026-10-17") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)


def test_a_young_pair_reads_its_day_and_only_below_60_hourly_bars() -> None:
    rising = _frame([100 * 1.002**i for i in range(80)], "15min")
    falling = _frame([100 * 0.998**i for i in range(80)], "15min")
    young = _frame([100.0] * 20, "h")
    assert hourly_trend(young, rising) is not None and hourly_trend(young, rising) > 0
    assert hourly_trend(young, falling) is None
    assert hourly_trend(young) is None
    # An established pair is judged on its 1h bars only, whatever its day does.
    flat = _frame([100.0] * MIN_HOURLY_BARS, "h")
    assert hourly_trend(flat, rising) is None


def test_a_young_pair_is_ranked_on_its_last_24_hours() -> None:
    from bot.markets import YOUNG_HOURS, _momentum_score

    def candle(close: float) -> list[Any]:
        return [0, close, close, close, close, 1.0]

    class Collector:
        def __init__(self, daily: int, hourly: list[float]) -> None:
            self.daily, self.hourly = daily, hourly

        def _fetch(self, url: str) -> tuple[Any, dict[str, str]]:
            if "interval=1d" in url:
                return [candle(100.0)] * self.daily, {}
            return [candle(value) for value in self.hourly] + [candle(0.0)], {}

    climbing = [100 * 1.01**i * (1.002 if i % 2 else 1.0) for i in range(YOUNG_HOURS)]
    score = _momentum_score(Collector(3, climbing), "NEW/USDT")  # type: ignore[arg-type]
    assert score is not None and score > 0
    assert _momentum_score(Collector(3, climbing[:5]), "NEW/USDT") is None  # type: ignore[arg-type]


# -- API ---------------------------------------------------------------------


def test_the_panel_sets_and_clears_the_rule_and_validation_starts_by_itself(api_client, monkeypatch) -> None:
    from bot.api import app as api

    started: list[str] = []
    gate = threading.Event()

    def run(key, strategy_id, config, root) -> None:
        started.append(strategy_id)
        api._update_job(key, status="done", stage="pronto")
        gate.set()

    monkeypatch.setattr(api, "_run_validation", run)
    api_client.put("/bot/config", json={"strategies": {"trend": False, "mean_reversion": True}})
    gate.wait(5)
    started.clear()
    gate.clear()
    # One strategy is already running: it is left alone; the other one is queued.
    with api.ctx.validation_lock:
        api.ctx.validations[("testnet", "momentum")] = {"status": "running", "stage": "baixando"}
    body = api_client.put(
        "/bot/config", json={"take_profit_pct": 0.03, "stop_loss_pct": 0.015, "strategies": {"momentum": True}}
    ).json()
    assert body["validating"] == ["mean_reversion"]
    gate.wait(5)
    assert started == ["mean_reversion"]
    status = api_client.get("/bot/status").json()
    assert (status["take_profit_pct"], status["stop_loss_pct"], status["exit_policy"]) == (0.03, 0.015, POLICY)
    assert api_client.put("/bot/config", json={"take_profit_pct": 0.03}).status_code == 422
    api_client.put("/bot/config", json={"take_profit_pct": None, "stop_loss_pct": None})
    status = api_client.get("/bot/status").json()
    assert status["take_profit_pct"] is None and status["exit_policy"] == "trailing"


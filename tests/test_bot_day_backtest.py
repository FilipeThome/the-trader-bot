from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from bot.backtest import runner
from bot.backtest.runner import DAY_SLIPPAGE, ROUND_TRIP_COST, Trade, simulate_day
from bot.strategies.base import Setup
from bot.strategies.day import DAY, DayRules

START = "2026-10-15T08:00:00"


class Stub:
    """Buys once, on the bar whose index is ``at``; records the 1h bars it was shown."""

    style = "day"

    def __init__(self, at: int, stop_pct: float = 0.01) -> None:
        self.at = at
        self.stop_pct = stop_pct
        self.calls = 0
        self.hourly_seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, *, hourly: pd.DataFrame | None = None) -> Setup | None:
        self.calls += 1
        if hourly is not None and not hourly.empty:
            self.hourly_seen.append((frame.index[-1], hourly.index[-1]))
        if len(frame) - 1 + max(0, self.offset(frame)) != self.at:
            return None
        return Setup("intraday_momentum", symbol, "15m", "Comprar", 1.0, 0.015, 0.01, self.stop_pct, 1.5 * self.stop_pct, "t", "trend_up")

    def offset(self, frame: pd.DataFrame) -> int:
        # Window position of the last bar inside the full frame.
        return int((frame.index[-1] - pd.Timestamp(START, tz="UTC")) / pd.Timedelta(minutes=15)) - (len(frame) - 1)


def _frame(closes: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    index = pd.date_range(START, periods=len(closes), freq="15min", tz="UTC")
    lows = lows or [price * 0.999 for price in closes]
    return pd.DataFrame(
        {"open": closes, "high": [price * 1.001 for price in closes], "low": lows, "close": closes, "volume": 10.0},
        index=index,
    )


def _hourly() -> pd.DataFrame:
    index = pd.date_range("2026-10-10T00:00:00", "2026-10-17T00:00:00", freq="h", tz="UTC")
    return pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}, index=index)


def _flat(count: int) -> list[float]:
    return [100.0] * count


def test_the_first_target_sells_half_and_the_rest_leaves_at_break_even() -> None:
    entry_at = 60
    closes = _flat(61) + [100.5, 101.6, 101.0, 100.5]
    lows = [price * 0.999 for price in closes]
    lows[-1] = 100.0  # touches the break-even stop (100.2)
    trades = simulate_day(Stub(entry_at), "BTC/USDT", _frame(closes, lows), _hourly(), DAY)
    assert len(trades) == 1
    first = (101.6 - 100) / 100
    rest = (min(100.5, 100.0 * (1 + DAY.breakeven_buffer)) - 100) / 100
    expected = 0.5 * first + 0.5 * rest - ROUND_TRIP_COST - 2 * DAY_SLIPPAGE
    assert trades[0].net_return == pytest.approx(expected)


def test_the_stop_wins_when_one_bar_reaches_both() -> None:
    closes = _flat(61) + [101.6]
    lows = [price * 0.999 for price in closes]
    lows[-1] = 98.5  # below the 99 stop on the same bar that closes above the target
    trades = simulate_day(Stub(60), "BTC/USDT", _frame(closes, lows), _hourly(), DAY)
    assert len(trades) == 1
    assert trades[0].net_return == pytest.approx((99.0 - 100) / 100 - ROUND_TRIP_COST - 2 * DAY_SLIPPAGE)


def test_the_rest_leaves_when_the_hold_time_ends() -> None:
    rules = DayRules(max_hold_minutes=60)
    closes = _flat(61) + [100.2, 100.3, 100.4, 100.6, 100.7]
    trades = simulate_day(Stub(60), "BTC/USDT", _frame(closes), _hourly(), rules)
    assert len(trades) == 1
    # Four 15m bars after the entry bar.
    assert trades[0].exit_time == _frame(closes).index[64]
    assert trades[0].net_return == pytest.approx((100.6 - 100) / 100 - ROUND_TRIP_COST - 2 * DAY_SLIPPAGE)


def test_no_entry_in_the_quiet_hours() -> None:
    # The entry bar opens 02:45 and closes 03:00: a quiet-hour decision.
    stub = Stub(at=10**6)
    frame = _frame(_flat(200))
    frame.index = pd.date_range("2026-10-15T00:00:00", periods=200, freq="15min", tz="UTC")
    simulate_day(stub, "BTC/USDT", frame, _hourly(), DAY)
    evaluated_at = {pd.Timestamp(seen[0]) for seen in stub.hourly_seen}
    assert not any(3 <= (moment + timedelta(minutes=15)).hour < 7 for moment in evaluated_at)


def test_the_replay_never_shows_the_hour_still_forming() -> None:
    stub = Stub(at=10**6)
    simulate_day(stub, "BTC/USDT", _frame(_flat(150)), _hourly(), DAY)
    assert stub.hourly_seen
    for bar_open, last_hour in stub.hourly_seen:
        assert last_hour + pd.Timedelta(hours=1) <= bar_open + pd.Timedelta(minutes=15)


def _trades(returns: list[float], start: str = "2026-07-01") -> list[Trade]:
    days = pd.date_range(start, periods=len(returns), freq="D", tz="UTC")
    return [Trade(day + pd.Timedelta(hours=12), value) for day, value in zip(days, returns)]


def test_the_day_gate_wants_a_sample_and_a_mean_return_per_trade() -> None:
    steady = [0.004, 0.003, -0.002, 0.005] * 10
    assert runner.evaluate("intraday_momentum", _trades(steady), 40 / 365, exit_policy="day").passed
    few = runner.evaluate("intraday_momentum", _trades(steady[:20]), 20 / 365, exit_policy="day")
    assert not few.passed and "menos que 30" in few.reason
    thin = runner.evaluate("intraday_momentum", _trades([0.0012, 0.0008, -0.0002, 0.0001] * 10), 40 / 365, exit_policy="day")
    assert not thin.passed and "retorno médio" in thin.reason


def test_a_day_validation_records_its_rules() -> None:
    result = runner.evaluate("intraday_momentum", _trades([0.004, 0.003, -0.002, 0.005] * 10), 40 / 365, exit_policy="day")
    params = runner.promotion_record(result)["params"]
    assert params["exit_policy"] == "day" and params["day"] == DAY.params


def test_a_day_strategy_always_validates_with_the_day_exits() -> None:
    from test_bot_strategy_golden import candles

    frames = {"BTC/USDT": candles(1500, 7, "15min")}
    hourly = {"BTC/USDT": candles(500, 7, "h")}
    result = runner.validate_strategy("intraday_momentum", frames, "15m", exit_policy="trailing", hourly_frames=hourly)
    assert result.exit_policy == "day" and result.trades > 0


def test_a_day_validation_holds_only_for_the_rules_it_replayed() -> None:
    from bot.strategies import exit_policy_for, validated_for, validation_params

    def key(strategy_id: str, policy: str, timeframe: str) -> dict:
        return validation_params(
            strategy_id, policy, timeframes={timeframe: {"passed": True}}, symbols=["BTC/USDT"], market_filter="off", hysteresis=True
        )

    assert exit_policy_for("intraday_momentum", "trailing") == "day"
    assert exit_policy_for("trend", "trailing") == "trailing"
    day = key("intraday_momentum", "trailing", "15m")
    assert day["exit_policy"] == "day" and day["day"] == DAY.params
    assert validated_for(day, "intraday_momentum", "trailing")
    other = {**DAY.params, "max_hold_minutes": 240}
    assert not validated_for({**day, "day": other}, "intraday_momentum", "trailing")
    assert not validated_for({**day, "exit_policy": "trailing"}, "intraday_momentum", "trailing")
    assert validated_for(key("trend", "trailing", "1h"), "trend", "trailing")


class Alternating:
    """Buys at every flat bar; every other setup has no confidence, so its score fails ``classify``."""

    style = "day"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, *, hourly: pd.DataFrame | None = None) -> Setup | None:
        self.calls += 1
        confidence = 1.0 if self.calls % 2 else 0.0
        return Setup("intraday_momentum", symbol, "15m", "Comprar", 1.0, 0.03, 0.01, 0.01, 0.015, "t", "trend_up", confidence=confidence)


def test_the_day_replay_marks_the_setups_classify_would_refuse() -> None:
    rules = DayRules(max_hold_minutes=30, quiet_start_hour=0, quiet_end_hour=0)
    trades = simulate_day(Alternating(), "BTC/USDT", _frame(_flat(80)), _hourly(), rules)  # type: ignore[arg-type]
    assert len(trades) >= 4
    assert [trade.accepted for trade in trades] == [index % 2 == 0 for index in range(len(trades))]
    # The cost filter refuses too: a looser score floor still refuses a setup that cannot cover twice its cost.
    cheap = runner.EntryFilter(score_threshold=-1.0, cost_multiple=50.0, min_reward_risk=0.0)
    costly = simulate_day(Alternating(), "BTC/USDT", _frame(_flat(80)), _hourly(), rules, entry_filter=cheap)  # type: ignore[arg-type]
    assert costly and not any(trade.accepted for trade in costly)

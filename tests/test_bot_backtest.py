from __future__ import annotations

import math

import pandas as pd
import pytest

from bot.backtest import runner
from bot.backtest.runner import MIN_TRADES, Trade, evaluate, promotion_record


def _trades(returns: list[float]) -> list[Trade]:
    start = pd.Timestamp("2026-01-01", tz="UTC")
    return [Trade(start + pd.Timedelta(days=i), value) for i, value in enumerate(returns)]


def test_sharpe_is_annualized_by_trades_per_year() -> None:
    returns = [0.02, -0.01, 0.015, 0.01, -0.005] * 4
    result = evaluate("trend", _trades(returns), years=2.0)
    series = pd.Series(returns)
    expected = series.mean() / series.std() * math.sqrt(len(returns) / 2.0)
    assert result.sharpe == pytest.approx(expected)
    assert result.trades == len(returns)


def test_too_few_trades_never_pass() -> None:
    steady = [0.05, 0.04] * MIN_TRADES
    assert evaluate("trend", _trades(steady[: MIN_TRADES - 1]), years=1.0).passed is False
    assert evaluate("trend", _trades(steady[:MIN_TRADES]), years=1.0).passed is True
    few = evaluate("trend", _trades([0.05, 0.04, 0.06]), years=1.0)
    assert few.passed is False
    assert promotion_record(few)["signal_only"] is True


def test_a_deep_drawdown_fails_the_gate() -> None:
    result = evaluate("trend", _trades([0.1, -0.15, -0.15, 0.2] * 5), years=1.0)
    assert result.max_drawdown > 0.2
    assert result.passed is False



def test_trailing_replay_lets_a_winner_run_past_the_target(monkeypatch) -> None:
    from bot.backtest import runner
    from bot.strategies.base import Setup

    class Once:
        fired = False

        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
            if self.fired:
                return None
            self.fired = True
            return Setup("trend", symbol, timeframe, "Comprar", 1.0, 0.05, 0.01, 0.03, 0.05, "t", "trend_up")

    closes = [100.0] * 61 + [103.0, 106.0, 110.0, 112.0, 108.0]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
    monkeypatch.setattr(runner, "_strategy", lambda _id: Once())
    target = runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy="target")
    monkeypatch.setattr(runner, "_strategy", lambda _id: Once())
    trailing = runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy="trailing")
    assert target[0].net_return == pytest.approx(0.06 - runner.ROUND_TRIP_COST)
    assert trailing[0].net_return == pytest.approx(0.08 - runner.ROUND_TRIP_COST)
    result = runner.evaluate("trend", trailing, 1.0, exit_policy="trailing")
    record = runner.promotion_record(result)
    assert {key: record["params"][key] for key in ("wins", "trades", "exit_policy")} == {
        "wins": 1, "trades": 1, "exit_policy": "trailing"
    }
    assert "menos que 10" in record["params"]["reason"]


def test_a_losing_half_fails_the_gate_even_with_a_good_sharpe() -> None:
    from bot.backtest import runner

    stamps = pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC")
    # The first half loses on balance, the second half makes it all back and more.
    returns = [-0.01, 0.002] * 10 + [0.05, 0.04] * 10
    trades = [runner.Trade(stamp, value) for stamp, value in zip(stamps, returns)]
    result = runner.evaluate("trend", trades, 1.0)
    assert result.sharpe > runner.MIN_SHARPE and result.max_drawdown < runner.MAX_DRAWDOWN
    assert result.passed is False and "instável" in result.reason
    steady = runner.evaluate("trend", [runner.Trade(stamp, 0.01 + 0.001 * (i % 3)) for i, stamp in enumerate(stamps)], 1.0)
    assert steady.passed is True and steady.reason == ""


def test_a_wick_through_the_stop_exits_at_the_stop_like_a_broker_order(monkeypatch) -> None:
    from bot.backtest import runner
    from bot.strategies.base import Setup

    class Once:
        fired = False

        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
            if self.fired:
                return None
            self.fired = True
            return Setup("trend", symbol, timeframe, "Comprar", 1.0, 0.05, 0.01, 0.03, 0.05, "t", "trend_up")

    closes = [100.0] * 61 + [101.0, 104.0]
    lows = [100.0] * 61 + [96.0, 103.0]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
    frame = pd.DataFrame({"open": closes, "high": closes, "low": lows, "close": closes, "volume": 1.0}, index=index)
    monkeypatch.setattr(runner, "_strategy", lambda _id: Once())
    trades = runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy="trailing")
    # The 61st bar closes at 101 but its low of 96 went through the 97 stop: out at 97, not held.
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(-0.03 - runner.ROUND_TRIP_COST)


def test_a_validation_in_worker_processes_matches_one_in_this_process(monkeypatch) -> None:
    from test_bot_strategy_golden import candles

    frames = {"BTC/USDT": candles(700, 7, "h"), "ETH/USDT": candles(700, 11, "h")}
    serial = runner.validate_strategy("trend", frames, "1h", exit_policy="trailing")
    monkeypatch.setattr(runner, "MAX_WORKERS", 2)
    try:
        pooled = runner.validate_strategy("trend", frames, "1h", exit_policy="trailing")
    finally:
        runner.shutdown_pool()
    assert serial.trades > 0
    assert pooled == serial


def test_a_broken_pool_finishes_the_validation_here(monkeypatch) -> None:
    from concurrent.futures.process import BrokenProcessPool

    from test_bot_strategy_golden import candles

    class Broken:
        def map(self, *_args):
            raise BrokenProcessPool("worker died")

        def shutdown(self, **_kwargs):
            pass

    frames = {"BTC/USDT": candles(400, 7, "h"), "ETH/USDT": candles(400, 11, "h")}
    expected = runner.validate_strategy("trend", frames, "1h")
    monkeypatch.setattr(runner, "MAX_WORKERS", 2)
    monkeypatch.setattr(runner, "_POOL", Broken())
    assert runner.validate_strategy("trend", frames, "1h") == expected
    assert runner._POOL is None


def test_the_promotion_records_the_window_the_replay_used() -> None:
    result = runner.evaluate("trend", [], 1.0)
    assert runner.promotion_record(result)["params"]["window"] == runner.LIVE_WINDOW == 299


def test_a_broken_pool_never_stops_a_newer_one(monkeypatch) -> None:
    class Pool:
        stopped = False

        def shutdown(self, **_kwargs):
            self.stopped = True

    old, current = Pool(), Pool()
    monkeypatch.setattr(runner, "_POOL", current)
    runner.shutdown_pool(old)  # type: ignore[arg-type]
    assert runner._POOL is current and not current.stopped
    runner.shutdown_pool(current)  # type: ignore[arg-type]
    assert runner._POOL is None and current.stopped

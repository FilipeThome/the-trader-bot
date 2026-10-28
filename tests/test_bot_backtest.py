from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from bot.backtest import runner, stats
from bot.backtest.runner import MIN_TRADES, BacktestResult, Trade, evaluate, promotion_record
from bot.indicators import btc_trend_ok
from bot.strategies import GATE_VERSION, timeframe_passed, timeframe_result, validated_for, validation_params
from bot.strategies.base import Setup

START = pd.Timestamp("2026-01-01", tz="UTC")
DAYS = 100
#: Eight trades that make money with little dispersion.
STEADY = [0.02, 0.01, 0.015, -0.005, 0.012, 0.018, 0.008, 0.01]


def _placed(windows: list[list[float]], holdout: list[float], days: int = DAYS) -> list[Trade]:
    """Trades spread evenly inside each in-sample window and inside the hold-out of a ``days`` span."""
    in_sample = days * (1 - runner.HOLDOUT_SHARE)
    width = in_sample / runner.WF_WINDOWS
    trades = []
    for k, returns in enumerate(windows):
        for i, value in enumerate(returns):
            trades.append(Trade(START + pd.Timedelta(days=width * (k + (i + 0.5) / len(returns))), value, stop_pct=0.02))
    held = days - in_sample
    for i, value in enumerate(holdout):
        trades.append(Trade(START + pd.Timedelta(days=in_sample + held * (i + 0.5) / len(holdout)), value, stop_pct=0.02))
    return trades


def _judge(trades: list[Trade], days: int = DAYS, **kwargs) -> BacktestResult:
    return evaluate("trend", trades, days / 365, span=(START, START + pd.Timedelta(days=days)), **kwargs)


# -- gate ----------------------------------------------------------------------


def test_sharpe_is_annualized_by_in_sample_trades_per_year() -> None:
    result = _judge(_placed([STEADY] * 4, [0.01, -0.02], days=730), days=730)
    sample = pd.Series(STEADY * 4)
    expected = sample.mean() / sample.std() * math.sqrt(len(sample) / (2.0 * (1 - runner.HOLDOUT_SHARE)))
    assert result.sharpe == pytest.approx(expected)
    assert result.trades == 34 and result.gate_trades == 32


def test_swing_gate_requires_30_in_sample_trades() -> None:
    hold = [0.01] * 10
    short = _judge(_placed([STEADY, STEADY, STEADY, STEADY[:5]], hold))
    assert short.trades == 39 and short.gate_trades == 29
    assert short.passed is False and short.reason == f"29 trades antes do hold-out, menos que {MIN_TRADES}"
    enough = _judge(_placed([STEADY, STEADY, STEADY, STEADY[:6]], hold))
    assert enough.passed is True and enough.reason == ""
    assert promotion_record(short)["signal_only"] is True


def test_a_deep_drawdown_fails_the_gate() -> None:
    result = _judge(_placed([[0.1, -0.15, -0.15, 0.2] * 2] * 4, [0.05]))
    assert result.max_drawdown > 0.2
    assert result.passed is False and "drawdown" in result.reason


def test_gate_fails_when_only_2_of_4_windows_profit() -> None:
    losing = [-value for value in STEADY]
    result = _judge(_placed([STEADY, losing, STEADY, losing], [0.01] * 8))
    assert [window["profitable"] for window in result.windows] == [True, False, True, False]
    assert result.passed is False and "walk-forward: 2 de 4 janelas lucram, menos que 3" in result.reason


def test_gate_fails_when_the_holdout_loses() -> None:
    losing = _judge(_placed([STEADY] * 4, [-0.02] * 5))
    assert losing.holdout["trades"] == 5 and losing.holdout["net"] == pytest.approx(-0.1)
    assert losing.passed is False and losing.reason.startswith("o hold-out (últimos 20% do período) perde")
    # Sharpe, drawdown and DSR judge the in-sample trades only: the hold-out is the only failure.
    assert ";" not in losing.reason
    # A quiet hold-out proves nothing: it fails with its own reason.
    quiet = _judge(_placed([STEADY] * 4, []))
    assert quiet.holdout == {**quiet.holdout, "trades": 0, "passed": False} and not quiet.passed
    assert quiet.reason == "o hold-out (últimos 20% do período) tem 0 trades, menos que 5"


def test_a_holdout_of_four_winning_trades_fails() -> None:
    four = _judge(_placed([STEADY] * 4, [0.02] * 4))
    assert four.holdout["trades"] == 4 and four.holdout["net"] > 0
    assert four.passed is False and four.reason == "o hold-out (últimos 20% do período) tem 4 trades, menos que 5"
    five = _judge(_placed([STEADY] * 4, [0.02] * 5))
    assert five.passed is True and five.holdout["passed"] is True


def test_a_holdout_slower_than_half_the_sample_fails() -> None:
    # In-sample: 0.352 over 80 days (0.44% a day). Hold-out: 0.03 over 20 days (0.15% a day) < 0.5 x 0.44%.
    slow = _judge(_placed([STEADY] * 4, [0.006] * 5))
    assert slow.holdout["net"] > 0 and slow.holdout["passed"] is False and slow.passed is False
    assert slow.reason.startswith("o hold-out (últimos 20% do período) ganha 0.15% por dia, menos que 0.5")


def test_gate_fails_on_low_dsr_after_many_trials() -> None:
    window = [0.03, -0.012, 0.025, -0.015, 0.02, -0.01, 0.03, -0.012]
    trades = _placed([window] * 4, [0.01] * 8)
    alone = _judge(trades)
    assert alone.passed and alone.trials == 1 and alone.dsr >= runner.MIN_DSR
    assert alone.sharpe_trade == pytest.approx(stats.per_trade_sharpe(window * 4))
    tried = _judge(trades, trials=20, trial_sharpes=[0.0, 0.5, -0.3, 0.4, 0.1, 0.25])
    assert tried.dsr < alone.dsr and tried.dsr < runner.MIN_DSR
    assert tried.passed is False and tried.reason == f"Sharpe deflacionado de {tried.dsr:.2f} em 20 testes, abaixo de 0.95"


def test_equal_stop_outs_record_a_zero_sharpe_and_cannot_poison_later_trials() -> None:
    # Equal 2% stop-outs entered at different prices differ only by float rounding.
    entries = [101.37, 98.12] * 4
    stop_outs = [(entry * (1 - 0.02) - entry) / entry - runner.ROUND_TRIP_COST for entry in entries]
    losing = _judge(_placed([stop_outs] * 4, stop_outs))
    assert losing.sharpe_trade == 0.0 and losing.sharpe == 0.0 and losing.passed is False
    window = [0.03, -0.012, 0.025, -0.015, 0.02, -0.01, 0.03, -0.012]
    trades = _placed([window] * 4, [0.01] * 8)
    # The Sharpe such a run stored before the dispersion tolerance (about -2e15) is left out of N's variance.
    poisoned = _judge(trades, trials=2, trial_sharpes=[-2e15])
    assert poisoned.passed and poisoned.dsr == pytest.approx(_judge(trades, trials=2).dsr)


def test_halves_rule_is_replaced_by_walk_forward() -> None:
    # The first half of the trades loses on balance (the old rule refused it); three of
    # four windows make money, the efficiency skips folds anchored on a loss, and it passes.
    windows = [[-0.0125] * 8, [0.00625] * 8, STEADY, STEADY]
    trades = _placed(windows, [0.01] * 8)
    first_half = sum(trade.net_return for trade in sorted(trades, key=lambda trade: trade.exit_time)[:20])
    assert first_half < 0
    result = _judge(trades)
    assert result.passed is True and "instável" not in result.reason
    assert result.wfe == pytest.approx(stats.walk_forward_efficiency(result.windows)) and result.wfe >= runner.MIN_WFE
    # Two losing windows of four fail, whatever the halves say.
    worse = _judge(_placed([[-0.0125] * 8, [-0.001] * 8, STEADY, STEADY], [0.01] * 8))
    assert worse.passed is False and "2 de 4 janelas" in worse.reason and "instável" not in worse.reason


def test_result_carries_r_pf_sortino_calmar_mae_mfe_turnover() -> None:
    held = pd.Timedelta(hours=10)
    returns = [0.04, -0.02, 0.06, -0.01]
    exits = [START + pd.Timedelta(days=10 * (i + 1)) for i in range(4)]
    trades = [
        Trade(at, value, entry_time=at - held, stop_pct=0.02, mae=-0.01 * (i + 1), mfe=0.03, bars=10)
        for i, (at, value) in enumerate(zip(exits, returns))
    ]
    years = 50 / 365
    result = evaluate("trend", trades, years, span=(START, START + pd.Timedelta(days=50)), pairs=2)
    r = np.array([2.0, -1.0, 3.0, -0.5])
    assert result.expectancy_r == pytest.approx(r.mean())
    assert result.profit_factor == pytest.approx(0.10 / 0.03)
    assert result.sortino == pytest.approx(stats.sortino(returns, 4 / years))
    equity = float(np.prod(1 + np.array(returns)))
    # The 2% loss after the first win is the deepest fall; the 1% one starts from a new peak.
    assert result.calmar == pytest.approx((equity ** (1 / years) - 1) / 0.02)
    assert (result.avg_win, result.avg_loss) == (pytest.approx(0.05), pytest.approx(-0.015))
    assert result.mae_r == pytest.approx(np.mean([-0.5, -1.0, -1.5, -2.0]))
    assert result.mfe_r == pytest.approx(1.5)
    assert result.turnover == pytest.approx(4 / years)
    assert result.avg_hold_hours == pytest.approx(10.0)
    assert result.exposure == pytest.approx(40 / (50 * 24 * 2))
    assert result.mc_p95_dd_r == pytest.approx(stats.mc_drawdown_r(r, runs=runner.MC_RUNS, seed=0))
    assert result.daily_sharpe != 0 and result.days == 50
    json.dumps(result.to_dict())


# -- validation record ---------------------------------------------------------


def _result(timeframe: str, passed: bool, daily_sharpe: float, wins: int, trades: int) -> BacktestResult:
    return BacktestResult(
        "trend", 1.5, 0.05, trades, 0.2, passed, wins, "trailing", "" if passed else "Sharpe 0.40 não passa de 1",
        timeframe=timeframe, daily_sharpe=daily_sharpe,
    )


def test_strategy_record_keeps_one_result_per_timeframe() -> None:
    results = {"1h": _result("1h", True, 1.1, 40, 61), "4h": _result("4h", False, 1.9, 10, 25)}
    record = runner.strategy_record(
        results, symbols=["ETH/USDT", "BTC/USDT"], exit_policy="trailing", market_filter="off", hysteresis=True
    )
    params = record["params"]
    assert record["signal_only"] is False and record["validated_at"] is not None
    # The best passed timeframe, not the best overall.
    assert record["backtest_sharpe"] == 1.1
    assert params["gate"] == GATE_VERSION and params["symbols"] == ["BTC/USDT", "ETH/USDT"] and len(params["pairset"]) == 12
    assert set(params["timeframes"]) == {"1h", "4h"}
    assert timeframe_passed(params, "1h") and not timeframe_passed(params, "4h")
    assert timeframe_result(params, "4h")["trades"] == 25 and timeframe_result(params, "15m") is None
    assert (params["wins"], params["trades"], params["window"]) == (50, 86, runner.LIVE_WINDOW)
    assert params["reason"] == "4h: Sharpe 0.40 não passa de 1"
    assert json.loads(json.dumps(params)) == params
    failed = runner.strategy_record(
        {"1h": _result("1h", False, 0.3, 1, 5), "4h": _result("4h", False, 0.7, 1, 5)},
        symbols=["BTC/USDT"], exit_policy="trailing", market_filter="off", hysteresis=True,
    )
    assert failed["signal_only"] is True and failed["validated_at"] is None and failed["backtest_sharpe"] == 0.7


def test_promotion_record_is_a_full_v3_record() -> None:
    result = evaluate("trend", [Trade(START, 0.01)], 1.0, exit_policy="trailing")
    record = promotion_record(result, {"timeframe": "4h", "symbols": ["BTC/USDT"], "days": 480})
    params = record["params"]
    assert (params["gate"], params["version"], params["market_filter"], params["hysteresis"]) == (3, 1, "off", True)
    assert list(params["timeframes"]) == ["4h"] and params["days"] == 480
    assert validated_for(params, "trend", "trailing", timeframe="4h", symbol="BTC/USDT")
    # Without a timeframe anywhere, the strategy's first one keys the result.
    assert list(promotion_record(result)["params"]["timeframes"]) == ["1h"]


def test_validated_for_checks_gate_version_strategy_version_market_filter_hysteresis_timeframe_and_symbol() -> None:
    params = validation_params(
        "trend", "trailing", timeframes={"1h": {"passed": False}}, symbols=["BTC/USDT"], market_filter="off", hysteresis=True
    )
    assert validated_for(params, "trend", "trailing", timeframe="1h", symbol="BTC/USDT")
    # Whether the gate passed is not part of the key: the timeframe was replayed under these rules.
    assert not timeframe_passed(params, "1h")
    assert not validated_for({**params, "gate": 1}, "trend", "trailing")
    assert not validated_for({"exit_policy": "trailing", "wins": 30, "trades": 40}, "trend", "trailing")
    assert not validated_for({**params, "version": 2}, "trend", "trailing")
    assert not validated_for(params, "trend", "trailing", market_filter="btc_ema40d")
    assert not validated_for(params, "trend", "trailing", hysteresis=False)
    assert not validated_for(params, "trend", "trailing", timeframe="4h")
    assert not validated_for(params, "trend", "trailing", symbol="ETH/USDT")
    assert not validated_for(params, "trend", "target")
    # Mean reversion has no EMA exit: the hysteresis switch neither stales nor is stored for it.
    reversion = validation_params(
        "mean_reversion", "trailing", timeframes={"15m": {}}, symbols=["BTC/USDT"], market_filter="off", hysteresis=True
    )
    assert reversion["hysteresis"] is False
    assert validated_for(reversion, "mean_reversion", "trailing", hysteresis=False)
    assert validated_for(reversion, "mean_reversion", "trailing", hysteresis=True)


def test_validation_budget_by_timeframe() -> None:
    assert runner.validation_days("trend", "1h") == 240
    assert runner.validation_days("trend", "4h") == runner.validation_days("momentum", "4h") == 480
    assert runner.validation_days("mean_reversion", "15m") == 120
    assert runner.validation_days("intraday_momentum", "15m") == runner.validation_days("vwap_pullback", "15m") == 120


# -- swing replay --------------------------------------------------------------


class Once:
    """Buys on the first bar it sees: a 3% stop and a 5% target."""

    def __init__(self) -> None:
        self.fired = False

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        if self.fired:
            return None
        self.fired = True
        return Setup("trend", symbol, timeframe, "Comprar", 1.0, 0.05, 0.01, 0.03, 0.05, "t", "trend_up")


def _bars(closes: list[float], *, opens=None, highs=None, lows=None) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": opens or closes, "high": highs or closes, "low": lows or closes, "close": closes, "volume": 1.0}, index=index
    )


def _replay_once(monkeypatch, frame: pd.DataFrame, policy: str = "target") -> list[Trade]:
    monkeypatch.setattr(runner, "_strategy", lambda _id: Once())
    return runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy=policy)


COSTS = runner.ROUND_TRIP_COST + 2 * runner.LEG_SLIPPAGE


def test_swing_replay_pays_slippage_on_both_legs(monkeypatch) -> None:
    assert runner.LEG_SLIPPAGE == runner.DAY_SLIPPAGE == 0.0003
    trades = _replay_once(monkeypatch, _bars([100.0] * 61 + [102.0, 106.0]))
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(0.06 - runner.ROUND_TRIP_COST - 0.0006)


def test_swing_target_fills_at_the_close_not_the_high(monkeypatch) -> None:
    closes = [100.0] * 61 + [103.0, 104.0, 106.0]
    highs = [100.0] * 61 + [107.0, 104.0, 106.0]
    trades = _replay_once(monkeypatch, _bars(closes, highs=highs))
    # The wick to 107 crossed the 105 target but closed at 103: the cycle sells on the close that reaches it.
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(0.06 - COSTS)
    assert trades[0].exit_time == _bars(closes).index[-1]


def test_a_gap_through_the_stop_fills_at_the_open(monkeypatch) -> None:
    closes = [100.0] * 61 + [95.0]
    opens = [100.0] * 61 + [94.0]
    trades = _replay_once(monkeypatch, _bars(closes, opens=opens, lows=[100.0] * 61 + [93.0]))
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(-0.06 - COSTS)


def test_a_wick_through_the_stop_exits_at_the_stop_like_a_broker_order(monkeypatch) -> None:
    closes = [100.0] * 61 + [101.0, 104.0]
    lows = [100.0] * 61 + [96.0, 103.0]
    trades = _replay_once(monkeypatch, _bars(closes, lows=lows), "trailing")
    # The 61st bar closes at 101 but its low of 96 went through the 97 stop: out at 97, not held.
    assert len(trades) == 1 and trades[0].net_return == pytest.approx(-0.03 - COSTS)


def test_trailing_replay_lets_a_winner_run_past_the_target(monkeypatch) -> None:
    closes = [100.0] * 61 + [103.0, 106.0, 110.0, 112.0, 108.0]
    target = _replay_once(monkeypatch, _bars(closes), "target")
    trailing = _replay_once(monkeypatch, _bars(closes), "trailing")
    assert target[0].net_return == pytest.approx(0.06 - COSTS)
    assert trailing[0].net_return == pytest.approx(0.08 - COSTS)
    record = promotion_record(evaluate("trend", trailing, 1.0, exit_policy="trailing"))
    assert {key: record["params"][key] for key in ("wins", "trades", "exit_policy")} == {
        "wins": 1, "trades": 1, "exit_policy": "trailing"
    }
    assert f"menos que {MIN_TRADES}" in record["params"]["reason"]


def test_trade_records_entry_time_stop_mae_mfe_without_changing_returns(monkeypatch) -> None:
    closes = [100.0] * 61 + [101.0, 99.0, 106.0]
    frame = _bars(closes, highs=[value + 1 for value in closes], lows=[value - 1 for value in closes])
    trades = _replay_once(monkeypatch, frame)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.net_return == pytest.approx(0.06 - COSTS)
    assert trade.entry_time == frame.index[60] and trade.exit_time == frame.index[-1] and trade.bars == 3
    assert trade.stop_pct == 0.03
    assert trade.mae == pytest.approx(-0.02) and trade.mfe == pytest.approx(0.07)


def test_hysteresis_does_not_raise_turnover_on_golden_candles() -> None:
    from test_bot_strategy_golden import candles

    for strategy_id, timeframe, freq in (("trend", "1h", "h"), ("momentum", "4h", "4h")):
        frame = candles(900, 7, freq)
        for policy in ("target", "trailing"):
            banded = runner.simulate(strategy_id, "BTC/USDT", frame, timeframe, exit_policy=policy, hysteresis=True)
            plain = runner.simulate(strategy_id, "BTC/USDT", frame, timeframe, exit_policy=policy, hysteresis=False)
            assert len(banded) <= len(plain)


def test_market_filter_replay_skips_entries_without_lookahead(monkeypatch) -> None:
    bars_4h = 320
    closes = [100 * 0.995**i for i in range(260)]
    closes += [closes[-1] * 1.02**i for i in range(1, bars_4h - 259)]
    market_index = pd.date_range("2026-01-01", periods=bars_4h, freq="4h", tz="UTC")
    market = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=market_index)
    seen: list[pd.Timestamp] = []

    class Watch:
        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
            seen.append(frame.index[-1] + pd.Timedelta(hours=1))
            return None

    frame = _bars([100.0] * (bars_4h * 4))
    monkeypatch.setattr(runner, "_strategy", lambda _id: Watch())
    runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy="trailing", market=market)

    def allowed(decided_at: pd.Timestamp) -> bool:
        # Only the 4h bars closed by the decision, the last 299 of them, as the live cycle sees them.
        closed = market[market.index + pd.Timedelta(hours=4) <= decided_at]["close"]
        return btc_trend_ok(closed.iloc[-runner.LIVE_WINDOW :])

    decisions = [stamp + pd.Timedelta(hours=1) for stamp in frame.index[60:]]
    expected = [moment for moment in decisions if allowed(moment)]
    assert seen == expected
    assert len(expected) < len(decisions)
    # The first entry after the block waits for a 4h bar to close, not for one to open.
    first_back = next(moment for moment in expected if moment > market_index[240])
    assert (first_back - market_index[0]) % pd.Timedelta(hours=4) == pd.Timedelta(0)
    seen.clear()
    runner.simulate("trend", "BTC/USDT", frame, "1h", exit_policy="trailing")
    assert seen == decisions


# -- process pool --------------------------------------------------------------


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
    assert result.reason == "nenhum trade"
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


def test_daily_sharpe_does_not_depend_on_the_order_of_the_trades() -> None:
    from bot.backtest.runner import Trade, _daily_sharpe

    start = pd.Timestamp("2026-01-01", tz="UTC")
    trades = [
        Trade(exit_time=start + pd.Timedelta(days=day, hours=3), net_return=ret)
        for day, ret in [(1, 0.01), (2, -0.004), (4, 0.006), (4, 0.002), (7, -0.001)]
    ]
    end = start + pd.Timedelta(days=9)
    ascending = _daily_sharpe(trades, start, end)
    assert ascending != 0
    assert _daily_sharpe(list(reversed(trades)), start, end) == pytest.approx(ascending)


# -- portfolio limits and the classify marks (gate v3) ---------------------------


def _at(hours: float) -> pd.Timestamp:
    return START + pd.Timedelta(hours=hours)


def _held(symbol: str, entered: float, exited: float, net: float, *, accepted: bool = True, stop_pct: float = 0.02) -> Trade:
    return Trade(_at(exited), net, _at(entered), stop_pct, symbol=symbol, accepted=accepted)


def test_overlapping_trades_beyond_the_position_slots_are_skipped() -> None:
    from bot.config import RiskLimits

    trades = [
        _held("ETH/USDT", 0, 10, 0.01),
        _held("BTC/USDT", 0, 10, 0.01),
        _held("SOL/USDT", 1, 5, 0.01),
        # classify would refuse it: it never trades live, so it neither takes a slot nor is refused.
        _held("XRP/USDT", 1, 5, 0.01, accepted=False),
        _held("SOL/USDT", 10, 12, 0.01),
    ]
    kept, skipped = runner.constrain_portfolio(trades, RiskLimits(max_positions=2, pair_cooldown_minutes=0))
    assert skipped == 1
    assert sorted((trade.symbol, trade.entry_time) for trade in kept) == [
        ("BTC/USDT", _at(0)), ("ETH/USDT", _at(0)), ("SOL/USDT", _at(10)), ("XRP/USDT", _at(1)),
    ]
    # A tie at the entry goes to the symbol first in order.
    one, skipped_one = runner.constrain_portfolio(trades[:2], RiskLimits(max_positions=1, pair_cooldown_minutes=0))
    assert [trade.symbol for trade in one] == ["BTC/USDT"] and skipped_one == 1


def test_the_loss_guard_counts_only_losses_that_exited_before_the_entry() -> None:
    from bot.config import RiskLimits

    limits = RiskLimits(max_positions=10, pair_cooldown_minutes=0, loss_guard_trades=2, loss_guard_r=0)
    trades = [
        _held("BTC/USDT", 0, 1, -0.01),
        _held("ETH/USDT", 0, 3, -0.01),
        # At 2h only BTC's loss has exited; ETH's exits an hour later and does not count yet.
        _held("SOL/USDT", 2, 4, 0.01),
        # At 3h30 both losses are in: paused.
        _held("XRP/USDT", 3.5, 5, 0.01),
        # At 7h30 BTC's loss is older than six hours.
        _held("ADA/USDT", 7.5, 9, 0.01),
    ]
    kept, skipped = runner.constrain_portfolio(trades, limits)
    assert [trade.symbol for trade in kept] == ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT"] and skipped == 1
    # The guard in R: two full stop-outs pause, whatever the count rule says.
    in_r = [_held("BTC/USDT", 0, 1, -0.02), _held("ETH/USDT", 0, 2, -0.02), _held("SOL/USDT", 3, 4, 0.01)]
    kept_r, skipped_r = runner.constrain_portfolio(in_r, RiskLimits(pair_cooldown_minutes=0, loss_guard_trades=3))
    assert [trade.symbol for trade in kept_r] == ["BTC/USDT", "ETH/USDT"] and skipped_r == 1
    # The replay and risk.protect decide through the same function.
    from bot import risk

    assert runner.loss_guard is risk.loss_guard


def test_a_pair_waits_out_its_cooldown_in_the_replay() -> None:
    from bot.config import RiskLimits

    trades = [_held("BTC/USDT", 0, 1, -0.01), _held("BTC/USDT", 2, 3, 0.01), _held("BTC/USDT", 3.1, 4, 0.01)]
    kept, skipped = runner.constrain_portfolio(trades, RiskLimits(pair_cooldown_minutes=120, loss_guard_trades=0, loss_guard_r=0))
    assert [trade.entry_time for trade in kept] == [_at(0), _at(3.1)] and skipped == 1


class Alternating:
    """Buys at every flat bar; every other setup expects too little to cover twice the replay's cost."""

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        self.calls += 1
        expected = 0.05 if self.calls % 2 else 0.006
        return Setup("trend", symbol, timeframe, "Comprar", 1.0, expected, 0.01, 0.03, 0.05, "t", "trend_up", confidence=1.0)


def _seesaw(pairs: int = 20) -> pd.DataFrame:
    """Flat warm-up, then 100 and 106 in turn: each entry at 100 exits at the 5% target on the next bar."""
    closes = [100.0] * 60 + [100.0, 106.0] * pairs
    return _bars(closes)


def test_the_replay_marks_the_setups_classify_would_refuse(monkeypatch) -> None:
    from bot.config import RiskLimits

    monkeypatch.setattr(runner, "_strategy", lambda _id: Alternating())
    result = runner.validate_strategy("trend", {"BTC/USDT": _seesaw()}, "1h", exit_policy="target")
    assert result.trades == 20 and result.filtered_trades == 10 and result.filtered_wins == 10
    assert (result.score_threshold, result.cost_multiple, result.min_reward_risk) == (0.3, 2.0, 1.5)
    assert result.skipped_by_limits == 0 and result.limits == {}
    record = runner.timeframe_record(result)
    assert record["filtered_trades"] == 10 and record["cost_multiple"] == 2.0 and "filtered_sharpe" in record
    # A looser filter lets every setup through, and the thresholds used are recorded.
    loose = runner.validate_strategy(
        "trend", {"BTC/USDT": _seesaw()}, "1h", exit_policy="target", entry_filter=runner.EntryFilter(0.3, 1.0, 0.0)
    )
    assert loose.filtered_trades == 20 and (loose.cost_multiple, loose.min_reward_risk) == (1.0, 0.0)
    # Under a fixed rule the reward-to-risk floor is 0, as live scoring has it.
    assert runner.filter_for(0.3, 2.0, 1.5, "fixed:300:150").min_reward_risk == 0.0
    # With limits, only accepted trades compete: the second pair's accepted trades find the one slot taken.
    monkeypatch.setattr(runner, "MAX_WORKERS", 1)
    limited = runner.validate_strategy(
        "trend",
        {"BTC/USDT": _seesaw(), "ETH/USDT": _seesaw()},
        "1h",
        exit_policy="target",
        limits=RiskLimits(max_positions=1, pair_cooldown_minutes=0),
    )
    assert limited.skipped_by_limits == 10 and limited.trades == 30 and limited.filtered_trades == 10
    assert limited.limits == {"max_positions": 1, "pair_cooldown_minutes": 0, "loss_guard_trades": 3, "loss_guard_r": 2.0}


class Rich:
    """Buys at every flat bar; the setup clears ``classify`` with its own 5% target."""

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        return Setup("trend", symbol, timeframe, "Comprar", 1.0, 0.05, 0.01, 0.03, 0.05, "t", "trend_up", confidence=1.0)


def test_a_fixed_replay_marks_each_trade_with_the_fixed_stop_and_target(monkeypatch) -> None:
    from bot.strategies.fixed import FixedExit

    marks = runner.EntryFilter(min_reward_risk=0.0)
    setup = Rich().evaluate("BTC/USDT", "1h", _seesaw())
    assert setup is not None and marks.accepts(setup)
    # A 0.5% take-profit caps the expected return below what the score and cost filters want.
    narrow, wide = FixedExit(take_profit_pct=0.005, stop_loss_pct=0.01), FixedExit(take_profit_pct=0.03, stop_loss_pct=0.01)
    assert not marks.accepts(narrow.apply(setup)) and marks.accepts(wide.apply(setup))
    refused = runner.simulate_fixed(Rich(), "BTC/USDT", _seesaw(), "1h", narrow)  # type: ignore[arg-type]
    assert refused and not any(trade.accepted for trade in refused)
    taken = runner.simulate_fixed(Rich(), "BTC/USDT", _seesaw(), "1h", wide)  # type: ignore[arg-type]
    assert taken and all(trade.accepted for trade in taken)
    monkeypatch.setattr(runner, "_strategy", lambda _id: Rich())
    result = runner.validate_strategy("trend", {"BTC/USDT": _seesaw()}, "1h", exit_policy=narrow.policy)
    assert result.trades == len(refused) and result.filtered_trades == 0 and result.min_reward_risk == 0.0

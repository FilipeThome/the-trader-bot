"""Backtest gate. A strategy is executable only after it passes here.

The live strategies are replayed on closed bars with their default
parameters; nothing is fitted, so the whole history is out of sample. Like the
live spot bot, the replay only buys: a sell setup is not a short. A position
leaves by the configured exit policy: ``target`` (stop or target) or
``trailing`` (a stop that follows the highest close at the entry's stop
distance, no target), and always at the strategy's own exit. Stops and
targets are checked against each bar's low and high, as broker orders fill
on wicks; the stop wins when one bar touches both. Each round trip
pays the spot taker fee twice. A pass also needs half-split stability: the
first and the second half of the trades, by exit time, must each make money. The result records its exit policy, wins and
trades, so the hit-rate estimate only uses trades of the policy being traded.

The Sharpe ratio is annualized by trades per year, because the sample is one
return per trade, not one per day.
"""

from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from bot.evaluator import SPOT_TAKER
from bot.indicators import ema
from bot.strategies.base import Setup
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trend import TrendBreakout

#: Gate thresholds (README: Sharpe above 1 and drawdown below 20%).
MIN_SHARPE = 1.0
MAX_DRAWDOWN = 0.20
MIN_TRADES = 10
ROUND_TRIP_COST = 2 * SPOT_TAKER
_WARMUP_BARS = 60
#: Closed candles a strategy sees per step: the live collector reads 300 and
#: the cycle drops the one still forming (``runtime._closed``).
LIVE_WINDOW = 299
#: Worker processes of a validation, one symbol per task. The replay is pure
#: Python and pandas, so threads would share one interpreter lock.
MAX_WORKERS = 4
VALIDATABLE = ("trend", "mean_reversion", "momentum")


@dataclass
class BacktestResult:
    strategy_id: str
    sharpe: float
    max_drawdown: float
    trades: int
    net_return: float
    passed: bool
    wins: int = 0
    exit_policy: str = "target"
    #: Why the gate refused it, empty when it passed.
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Trade:
    exit_time: pd.Timestamp
    net_return: float


def _strategy(strategy_id: str) -> TrendBreakout | MeanReversion | Momentum | None:
    if strategy_id == "trend":
        return TrendBreakout()
    if strategy_id == "mean_reversion":
        return MeanReversion()
    if strategy_id == "momentum":
        return Momentum()
    return None


def simulate(
    strategy_id: str,
    symbol: str,
    frame: pd.DataFrame,
    timeframe: str,
    *,
    exit_policy: str = "target",
) -> list[Trade]:
    """Replay one symbol. Returns one trade per closed long position."""
    strategy = _strategy(strategy_id)
    if strategy is None:
        return []
    trades: list[Trade] = []
    position: Setup | None = None
    entry = peak = 0.0
    for end in range(_WARMUP_BARS, len(frame)):
        window = frame.iloc[max(0, end + 1 - LIVE_WINDOW) : end + 1]
        price = float(window["close"].iloc[-1])
        if position is None:
            setup = strategy.evaluate(symbol, timeframe, window)
            if setup is not None and setup.direction > 0:
                position = setup
                entry = peak = price
            continue
        # Live stops and targets rest at the broker and fill inside the bar, so the
        # replay checks them against the bar's low and high: a wick through the stop
        # exits at the stop, or at the open when the bar gaps past it.
        opened = float(window["open"].iloc[-1])
        low = float(window["low"].iloc[-1])
        high = float(window["high"].iloc[-1])
        stop = (peak if exit_policy == "trailing" else entry) * (1 - position.stop_pct)
        target = entry * (1 + position.target_pct)
        if low <= stop:
            fill = min(opened, stop)
        elif exit_policy != "trailing" and high >= target:
            fill = max(opened, target)
        elif _strategy_exit(strategy, window):
            fill = price
        else:
            peak = max(peak, price)
            continue
        trades.append(Trade(window.index[-1], (fill - entry) / entry - ROUND_TRIP_COST))
        position = None
    return trades


def _strategy_exit(strategy: Any, window: pd.DataFrame) -> bool:
    """The same strategy exits the live runtime uses."""
    if isinstance(strategy, (TrendBreakout, Momentum)):
        return strategy.exit_reason(window, "buy") is not None
    if isinstance(strategy, MeanReversion):
        if len(window) < 20:
            return False
        return float(window["close"].iloc[-1]) >= float(ema(window["close"], 20).iloc[-1])
    return False


def evaluate(strategy_id: str, trades: list[Trade], years: float, *, exit_policy: str = "target") -> BacktestResult:
    wins = sum(1 for trade in trades if trade.net_return > 0)
    if not trades or years <= 0:
        return BacktestResult(strategy_id, 0.0, 0.0, len(trades), 0.0, False, wins, exit_policy)
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    returns = pd.Series([trade.net_return for trade in ordered])
    equity = (1 + returns).cumprod()
    drawdown = abs(float((equity / equity.cummax() - 1).min()))
    sigma = float(returns.std() or 0)
    per_year = len(ordered) / years
    sharpe = 0.0 if sigma == 0 else float(returns.mean() / sigma * np.sqrt(per_year))
    total = float(equity.iloc[-1] - 1)
    half = len(ordered) // 2
    stable = half > 0 and all(
        sum(trade.net_return for trade in part) > 0 for part in (ordered[:half], ordered[half:])
    )
    reasons = [
        text
        for failed, text in (
            (sharpe <= MIN_SHARPE, f"Sharpe {sharpe:.2f} não passa de {MIN_SHARPE:.0f}"),
            (drawdown >= MAX_DRAWDOWN, f"drawdown de {drawdown:.0%} não fica abaixo de {MAX_DRAWDOWN:.0%}"),
            (len(ordered) < MIN_TRADES, f"{len(ordered)} trades, menos que {MIN_TRADES}"),
            (not stable, "instável: uma das metades do histórico não lucra"),
        )
        if failed
    ]
    return BacktestResult(
        strategy_id, sharpe, drawdown, len(ordered), total, not reasons, wins, exit_policy, "; ".join(reasons)
    )


def validate_strategy(
    strategy_id: str,
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    *,
    exit_policy: str = "target",
) -> BacktestResult:
    """Replay every symbol, in parallel processes, and judge the pooled trades against the gate."""
    jobs = [(strategy_id, symbol, frame, timeframe, exit_policy) for symbol, frame in frames.items() if not frame.empty]
    trades: list[Trade] = []
    for replayed in _replay_all(jobs):
        trades.extend(replayed)
    years = max(
        ((frame.index[-1] - frame.index[0]).total_seconds() / (365 * 86400) for _s, _y, frame, _t, _p in jobs),
        default=0.0,
    )
    return evaluate(strategy_id, trades, years, exit_policy=exit_policy)


_POOL: ProcessPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _replay(job: tuple[str, str, pd.DataFrame, str, str]) -> list[Trade]:
    strategy_id, symbol, frame, timeframe, exit_policy = job
    return simulate(strategy_id, symbol, frame, timeframe, exit_policy=exit_policy)


def _replay_all(jobs: list[tuple[str, str, pd.DataFrame, str, str]]) -> list[list[Trade]]:
    """Each job's trades, in job order. One symbol runs here; more go to the process pool."""
    if len(jobs) < 2 or MAX_WORKERS < 2:
        return [_replay(job) for job in jobs]
    pool = _pool()
    try:
        return list(pool.map(_replay, jobs))
    except BrokenProcessPool:
        # A worker died (killed, out of memory): drop this pool, never a newer one, and finish here.
        shutdown_pool(pool)
        return [_replay(job) for job in jobs]


def _pool() -> ProcessPoolExecutor:
    """One pool per process, started on first use. ``spawn`` on every OS: forking the
    API process would copy its threads' locks in whatever state they hold."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=MAX_WORKERS, mp_context=multiprocessing.get_context("spawn"))
        return _POOL


def shutdown_pool(only: ProcessPoolExecutor | None = None) -> None:
    """Stop the worker processes. The API calls it on shutdown.

    ``only`` stops that pool if it is still the current one: a validation whose
    pool broke must not stop the newer pool another validation is using.
    """
    global _POOL
    with _POOL_LOCK:
        if only is not None and _POOL is not only:
            return
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def promotion_record(result: BacktestResult, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "strategy_id": result.strategy_id,
        "backtest_sharpe": result.sharpe,
        "backtest_drawdown": result.max_drawdown,
        "validated_at": datetime.now(timezone.utc).isoformat() if result.passed else None,
        "live_sharpe": None,
        "signal_only": not result.passed,
        "params": {
            **(params or {}),
            "wins": result.wins,
            "trades": result.trades,
            "exit_policy": result.exit_policy,
            "reason": result.reason,
            "window": LIVE_WINDOW,
        },
    }

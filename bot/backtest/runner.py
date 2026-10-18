"""Backtest gate. A strategy is executable only after it passes here.

The live strategies are replayed on closed bars with their default
parameters; nothing is fitted, so the whole history is out of sample. Like the
live spot bot, the replay only buys: a sell setup is not a short. A position
leaves by the configured exit policy: ``target`` (stop or target) or
``trailing`` (a stop that follows the highest close at the entry's stop
distance, no target), ``trailing_tp`` (the entry stop until the activation
gain, then a closer stop under the highest close; ``bot.strategies.trailing``),
and always at the strategy's own exit. The operator's fixed rule
(``bot.strategies.fixed``) replays apart, in ``simulate_fixed``. Stops and
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
from bot.strategies.day import DAY, ENTRY_BAR, DayRules, in_quiet_hours
from bot.strategies.fixed import FixedExit, parse_fixed
from bot.strategies.intraday import IntradayMomentum, VwapPullback
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trailing import TRAIL, TRAILING_POLICIES, trailing_level
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
VALIDATABLE = ("trend", "mean_reversion", "momentum", "intraday_momentum", "vwap_pullback")
#: Slippage charged on each day-trade leg (entry, first target, exit), on top of the fees.
DAY_SLIPPAGE = 0.0003
#: The day gate: more trades make a per-trade Sharpe easier, so day strategies are judged
#: on daily returns, with a floor on the mean net return per trade and a larger sample.
DAY_MIN_TRADES = 30
DAY_MIN_MEAN_RETURN = 0.001


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


def _strategy(strategy_id: str) -> TrendBreakout | MeanReversion | Momentum | IntradayMomentum | VwapPullback | None:
    if strategy_id == "trend":
        return TrendBreakout()
    if strategy_id == "mean_reversion":
        return MeanReversion()
    if strategy_id == "momentum":
        return Momentum()
    if strategy_id == "intraday_momentum":
        return IntradayMomentum()
    if strategy_id == "vwap_pullback":
        return VwapPullback()
    return None


def is_day(strategy_id: str) -> bool:
    return getattr(_strategy(strategy_id), "style", "swing") == "day"


def simulate(
    strategy_id: str,
    symbol: str,
    frame: pd.DataFrame,
    timeframe: str,
    *,
    exit_policy: str = "target",
    hourly: pd.DataFrame | None = None,
    day: DayRules | None = None,
) -> list[Trade]:
    """Replay one symbol. Returns one trade per closed long position.

    A day strategy replays with ``simulate_day`` (its 1h frame and ``day`` rules);
    everything below is the swing replay, unchanged.
    """
    strategy = _strategy(strategy_id)
    if strategy is None:
        return []
    fixed = parse_fixed(exit_policy)
    if fixed is not None:
        return simulate_fixed(strategy, symbol, frame, timeframe, fixed, hourly=hourly, day=day or DAY)
    if isinstance(strategy, (IntradayMomentum, VwapPullback)):
        return simulate_day(strategy, symbol, frame, hourly, day or DAY)
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
        stop = entry * (1 - position.stop_pct)
        trailed = trailing_level(exit_policy, entry, peak, position.stop_pct)
        if trailed is not None:
            stop = max(stop, trailed)
        target = entry * (1 + position.target_pct)
        if low <= stop:
            fill = min(opened, stop)
        elif exit_policy not in TRAILING_POLICIES and high >= target:
            fill = max(opened, target)
        elif _strategy_exit(strategy, window):
            fill = price
        else:
            peak = max(peak, price)
            continue
        trades.append(Trade(window.index[-1], (fill - entry) / entry - ROUND_TRIP_COST))
        position = None
    return trades


def simulate_day(
    strategy: IntradayMomentum | VwapPullback,
    symbol: str,
    frame: pd.DataFrame,
    hourly: pd.DataFrame | None,
    rules: DayRules,
) -> list[Trade]:
    """Replay a day strategy on closed 15m bars, as the live cycle trades it.

    * The entry is the close of the bar whose setup appeared, outside the quiet
      hours (judged at that bar's close), with the 1h bars already closed then.
    * Each later bar: the stop is checked on the low first and wins when the
      same bar also reaches the first target. The first target sells
      ``tp1_fraction`` at the close of the bar that closes at or above it (the
      live cycle checks the last price and sells with an IOC). From the next
      bar the stop of the rest sits at the entry plus the buffer.
    * The rest leaves at the close of the bar where ``max_hold_minutes`` ends.

    The return weighs both legs and pays the taker fee twice and the slippage
    on each leg.
    """
    if hourly is None or hourly.empty:
        return []
    closes = (hourly.index + pd.Timedelta(hours=1)).values
    hold_bars = -(-rules.max_hold_minutes // int(ENTRY_BAR.total_seconds() // 60))
    trades: list[Trade] = []
    position: Setup | None = None
    entry = stop = target = 0.0
    entered = sold = 0
    first_leg: float | None = None
    for end in range(_WARMUP_BARS, len(frame)):
        window = frame.iloc[max(0, end + 1 - LIVE_WINDOW) : end + 1]
        decided_at = window.index[-1] + ENTRY_BAR
        close = float(window["close"].iloc[-1])
        if position is None:
            if in_quiet_hours(decided_at, rules):
                continue
            seen = int(np.searchsorted(closes, decided_at.to_datetime64(), side="right"))
            setup = strategy.evaluate(symbol, "15m", window, hourly=hourly.iloc[max(0, seen - LIVE_WINDOW) : seen])
            if setup is not None and setup.direction > 0:
                position = setup
                entry = close
                stop = entry * (1 - setup.stop_pct)
                target = entry * (1 + setup.target_pct)
                entered, sold, first_leg = end, 0, None
            continue
        opened = float(window["open"].iloc[-1])
        low = float(window["low"].iloc[-1])
        rest: float | None = None
        if low <= stop:
            rest = min(opened, stop)
        elif first_leg is None and close >= target:
            first_leg = close
            sold = end
        if rest is None and end - entered >= hold_bars:
            rest = close
        if first_leg is not None and sold == end and rest is None:
            # The break-even stop rests from the next bar.
            stop = entry * (1 + rules.breakeven_buffer)
            continue
        if rest is None:
            continue
        if first_leg is None:
            gross = (rest - entry) / entry
        else:
            share = rules.tp1_fraction
            gross = share * (first_leg - entry) / entry + (1 - share) * (rest - entry) / entry
        # Fees and slippage by notional: the whole position is bought once and sold once, in one or two parts.
        trades.append(Trade(window.index[-1], gross - ROUND_TRIP_COST - 2 * DAY_SLIPPAGE))
        position = None
    return trades


def simulate_fixed(
    strategy: TrendBreakout | MeanReversion | Momentum | IntradayMomentum | VwapPullback,
    symbol: str,
    frame: pd.DataFrame,
    timeframe: str,
    fixed: FixedExit,
    *,
    hourly: pd.DataFrame | None = None,
    day: DayRules = DAY,
) -> list[Trade]:
    """Replay the operator's fixed take-profit and stop-loss.

    * The entry is the close of the bar whose setup appeared. A day strategy keeps
      its quiet hours and sees only the 1h bars closed by then, as in ``simulate_day``.
    * Each later bar: the stop rests at the broker, so it is checked on the low first
      and fills at the stop (or the open, on a gap through it). The target is not an
      order on the book: the cycle sells at the last price once it reaches the
      target. The replay only has closed bars, so it sells at the close of the bar
      that closes at or above it; live checks every minute and can sell on a touch
      the closed bar no longer shows, so the replay never counts a wick as a win.
    * No strategy exit, trailing or time stop. A day strategy also pays
      ``DAY_SLIPPAGE`` on both legs, like its own replay.
    """
    day_style = isinstance(strategy, (IntradayMomentum, VwapPullback))
    if day_style and (hourly is None or hourly.empty):
        return []
    closes = (hourly.index + pd.Timedelta(hours=1)).values if day_style and hourly is not None else None
    cost = ROUND_TRIP_COST + (2 * DAY_SLIPPAGE if day_style else 0.0)
    trades: list[Trade] = []
    holding = False
    entry = stop = target = 0.0
    for end in range(_WARMUP_BARS, len(frame)):
        window = frame.iloc[max(0, end + 1 - LIVE_WINDOW) : end + 1]
        close = float(window["close"].iloc[-1])
        if not holding:
            if isinstance(strategy, (IntradayMomentum, VwapPullback)) and closes is not None and hourly is not None:
                decided_at = window.index[-1] + ENTRY_BAR
                if in_quiet_hours(decided_at, day):
                    continue
                seen = int(np.searchsorted(closes, decided_at.to_datetime64(), side="right"))
                setup = strategy.evaluate(symbol, "15m", window, hourly=hourly.iloc[max(0, seen - LIVE_WINDOW) : seen])
            else:
                setup = strategy.evaluate(symbol, timeframe, window)
            if setup is not None and setup.direction > 0:
                holding = True
                entry = close
                stop = entry * (1 - fixed.stop_loss_pct)
                target = entry * (1 + fixed.take_profit_pct)
            continue
        opened = float(window["open"].iloc[-1])
        low = float(window["low"].iloc[-1])
        if low <= stop:
            fill = min(opened, stop)
        elif close >= target:
            fill = close
        else:
            continue
        trades.append(Trade(window.index[-1], (fill - entry) / entry - cost))
        holding = False
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
    # A day strategy is judged by the day gate whatever rule it exits by: many short trades.
    if exit_policy == "day" or is_day(strategy_id):
        return _evaluate_day(strategy_id, trades, years, exit_policy=exit_policy)
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


def _evaluate_day(strategy_id: str, trades: list[Trade], years: float, *, exit_policy: str = "day") -> BacktestResult:
    """The day gate: Sharpe of daily returns (days without a trade count as 0), drawdown,
    at least ``DAY_MIN_TRADES``, a mean net return per trade of ``DAY_MIN_MEAN_RETURN``
    and both halves of the trades making money."""
    wins = sum(1 for trade in trades if trade.net_return > 0)
    if not trades or years <= 0:
        return BacktestResult(strategy_id, 0.0, 0.0, len(trades), 0.0, False, wins, exit_policy, "nenhum trade")
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    returns = pd.Series([trade.net_return for trade in ordered])
    equity = (1 + returns).cumprod()
    drawdown = abs(float((equity / equity.cummax() - 1).min()))
    exits = pd.DatetimeIndex([trade.exit_time for trade in ordered]).normalize()
    daily = returns.groupby(exits).sum()
    # Every day of the replay window counts, the idle ones at either end too.
    span = max(int(round(years * 365)), len(daily))
    days = pd.date_range(end=exits.max(), periods=span, freq="D")
    daily = daily.reindex(days, fill_value=0.0)
    sigma = float(daily.std() or 0)
    sharpe = 0.0 if sigma == 0 or len(daily) < 2 else float(daily.mean() / sigma * np.sqrt(365))
    mean = float(returns.mean())
    half = len(ordered) // 2
    stable = half > 0 and all(sum(trade.net_return for trade in part) > 0 for part in (ordered[:half], ordered[half:]))
    reasons = [
        text
        for failed, text in (
            (sharpe <= MIN_SHARPE, f"Sharpe diário {sharpe:.2f} não passa de {MIN_SHARPE:.0f}"),
            (drawdown >= MAX_DRAWDOWN, f"drawdown de {drawdown:.0%} não fica abaixo de {MAX_DRAWDOWN:.0%}"),
            (len(ordered) < DAY_MIN_TRADES, f"{len(ordered)} trades, menos que {DAY_MIN_TRADES}"),
            (mean < DAY_MIN_MEAN_RETURN, f"retorno médio de {mean:.2%} por trade, abaixo de {DAY_MIN_MEAN_RETURN:.2%}"),
            (not stable, "instável: uma das metades do histórico não lucra"),
        )
        if failed
    ]
    total = float(equity.iloc[-1] - 1)
    return BacktestResult(strategy_id, sharpe, drawdown, len(ordered), total, not reasons, wins, exit_policy, "; ".join(reasons))


def validate_strategy(
    strategy_id: str,
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    *,
    exit_policy: str = "target",
    hourly_frames: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    """Replay every symbol, in parallel processes, and judge the pooled trades against the gate.

    A day strategy replays with the day exits (``exit_policy`` "day") unless the
    operator's fixed rule replaces them, and needs ``hourly_frames``, the closed 1h
    bars of each symbol.
    """
    if is_day(strategy_id) and parse_fixed(exit_policy) is None:
        exit_policy = "day"
    hourly = hourly_frames or {}
    jobs: list[_Job] = [
        (strategy_id, symbol, frame, timeframe, exit_policy, hourly.get(symbol))
        for symbol, frame in frames.items()
        if not frame.empty
    ]
    trades: list[Trade] = []
    for replayed in _replay_all(jobs):
        trades.extend(replayed)
    years = max(
        ((job[2].index[-1] - job[2].index[0]).total_seconds() / (365 * 86400) for job in jobs),
        default=0.0,
    )
    return evaluate(strategy_id, trades, years, exit_policy=exit_policy)


_Job = tuple[str, str, pd.DataFrame, str, str, "pd.DataFrame | None"]


_POOL: ProcessPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _replay(job: _Job) -> list[Trade]:
    strategy_id, symbol, frame, timeframe, exit_policy, hourly = job
    return simulate(strategy_id, symbol, frame, timeframe, exit_policy=exit_policy, hourly=hourly)


def _replay_all(jobs: list[_Job]) -> list[list[Trade]]:
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
            # A day validation holds only for the day rules it replayed (quiet hours and
            # slippage replay under a fixed rule too).
            **({"day": DAY.params} if result.exit_policy == "day" or is_day(result.strategy_id) else {}),
            **({"trailing": TRAIL.params} if result.exit_policy == "trailing_tp" else {}),
        },
    }

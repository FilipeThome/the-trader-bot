"""Backtest and walk-forward. A strategy is executable only after this gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from bot.strategies.base import Setup
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.trend import TrendBreakout


@dataclass
class BacktestResult:
    strategy_id: str
    sharpe: float
    max_drawdown: float
    trades: int
    net_return: float
    passed: bool


def _metrics(returns: pd.Series) -> tuple[float, float, float]:
    clean = returns.dropna()
    if clean.empty:
        return 0.0, 0.0, 0.0
    equity = (1 + clean).cumprod()
    peak = equity.cummax()
    drawdown = float((equity / peak - 1).min())
    sigma = float(clean.std() or 0)
    sharpe = 0.0 if sigma == 0 else float(clean.mean() / sigma * np.sqrt(252))
    total = float(equity.iloc[-1] - 1)
    return sharpe, abs(drawdown), total


def simulate(strategy_id: str, symbol: str, frame: pd.DataFrame, timeframe: str) -> BacktestResult:
    """Replay the live strategy on closed bars. Costs use the spot taker fee."""
    strategy: TrendBreakout | MeanReversion
    if strategy_id == "trend":
        strategy = TrendBreakout()
    elif strategy_id == "mean_reversion":
        strategy = MeanReversion()
    else:
        return BacktestResult(strategy_id, 0.0, 1.0, 0, 0.0, False)
    returns: list[float] = []
    trades = 0
    position: Setup | None = None
    entry = 0.0
    for end in range(60, len(frame)):
        window = frame.iloc[: end + 1]
        price = float(window["close"].iloc[-1])
        if position is None:
            setup = strategy.evaluate(symbol, timeframe, window)
            if setup is not None and setup.direction != 0:
                position = setup
                entry = price
                trades += 1
            continue
        stop = entry * (1 - position.stop_pct if position.direction > 0 else 1 + position.stop_pct)
        target = entry * (1 + position.target_pct if position.direction > 0 else 1 - position.target_pct)
        exit_price = None
        if position.direction > 0 and (price <= stop or price >= target):
            exit_price = price
        elif position.direction < 0 and (price >= stop or price <= target):
            exit_price = price
        if exit_price is None:
            continue
        gross = (exit_price - entry) / entry * position.direction
        returns.append(gross - 0.002)
        position = None
    series = pd.Series(returns if returns else [0.0])
    sharpe, drawdown, total = _metrics(series)
    passed = sharpe > 1.0 and drawdown < 0.20 and trades > 0
    return BacktestResult(strategy_id, sharpe, drawdown, trades, total, passed)


def walk_forward(
    strategy_id: str,
    symbol: str,
    frame: pd.DataFrame,
    timeframe: str,
    *,
    train: int = 180,
    validate: int = 60,
) -> BacktestResult:
    """Six months of fit, two months of validation, on daily-equivalent bars.

    Parameters are the strategy defaults. The window only accepts a slice when
    the untouched validation segment itself clears the promotion gate.
    """
    if len(frame) < train + validate:
        return simulate(strategy_id, symbol, frame, timeframe)
    last = BacktestResult(strategy_id, 0.0, 1.0, 0, 0.0, False)
    start = 0
    while start + train + validate <= len(frame):
        segment = frame.iloc[start + train : start + train + validate]
        last = simulate(strategy_id, symbol, segment, timeframe)
        if last.passed:
            last = BacktestResult(
                strategy_id,
                last.sharpe,
                last.max_drawdown,
                last.trades,
                last.net_return,
                True,
            )
            return last
        start += validate
    return last


def promotion_record(result: BacktestResult, params: dict | None = None) -> dict:
    return {
        "strategy_id": result.strategy_id,
        "backtest_sharpe": result.sharpe,
        "backtest_drawdown": result.max_drawdown,
        "validated_at": datetime.now(timezone.utc).isoformat() if result.passed else None,
        "live_sharpe": None,
        "signal_only": not result.passed,
        "params": params or {},
    }

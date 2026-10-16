"""Demote a strategy when live Sharpe falls more than 50% for 30 days."""

from __future__ import annotations

from bot.store import Store


def live_sharpe(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((value - mean) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
    sigma = float(var) ** 0.5
    if sigma == 0:
        return 0.0
    return float(mean / sigma * (365 ** 0.5))


def apply_decay(store: Store, strategy_id: str, daily_returns: list[float]) -> bool:
    """Return True when the strategy was demoted to signal-only."""
    row = store.strategy(strategy_id)
    if row is None or row.get("backtest_sharpe") in (None, 0):
        return False
    if len(daily_returns) < 30:
        return False
    window = daily_returns[-30:]
    current = live_sharpe(window)
    backtest = float(row["backtest_sharpe"])
    demoted = current < backtest * 0.5
    updated = {**row, "live_sharpe": current}
    if demoted:
        updated["signal_only"] = True
    store.save_strategy(updated)
    return demoted

"""Strategy families. Each one is code the backtest and the live engine share."""

from typing import Any

from bot.strategies.day import DAY

from bot.strategies.funding import FundingCarry
from bot.strategies.intraday import IntradayMomentum, VwapPullback
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trend import TrendBreakout

Strategy = TrendBreakout | MeanReversion | Momentum | FundingCarry | IntradayMomentum | VwapPullback

STRATEGIES: dict[str, Strategy] = {
    "trend": TrendBreakout(),
    "mean_reversion": MeanReversion(),
    "momentum": Momentum(),
    "funding_carry": FundingCarry(),
    "intraday_momentum": IntradayMomentum(),
    "vwap_pullback": VwapPullback(),
}
#: Strategies that day trade: 15m entries, first take-profit, break-even stop and a time stop.
DAY_STRATEGIES = frozenset(key for key, strategy in STRATEGIES.items() if strategy.style == "day")


def exit_policy_for(strategy_id: str, swing_policy: str) -> str:
    """The exit rule of a strategy's positions: ``day`` for day strategies, the operator's otherwise."""
    return "day" if strategy_id in DAY_STRATEGIES else swing_policy


def validated_for(params: dict[str, Any] | None, strategy_id: str, swing_policy: str) -> bool:
    """Whether a backtest's ``params`` replayed the exit rules this strategy trades with now."""
    if not params:
        return False
    policy = exit_policy_for(strategy_id, swing_policy)
    if params.get("exit_policy") != policy:
        return False
    return policy != "day" or params.get("day") == DAY.params

__all__ = [
    "DAY_STRATEGIES",
    "exit_policy_for",
    "validated_for",
    "STRATEGIES",
    "Strategy",
    "TrendBreakout",
    "MeanReversion",
    "Momentum",
    "FundingCarry",
    "IntradayMomentum",
    "VwapPullback",
]

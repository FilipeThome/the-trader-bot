"""Strategy families. Each one is code the backtest and the live engine share."""

from bot.strategies.funding import FundingCarry
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trend import TrendBreakout

Strategy = TrendBreakout | MeanReversion | Momentum | FundingCarry

STRATEGIES: dict[str, Strategy] = {
    "trend": TrendBreakout(),
    "mean_reversion": MeanReversion(),
    "momentum": Momentum(),
    "funding_carry": FundingCarry(),
}

__all__ = ["STRATEGIES", "Strategy", "TrendBreakout", "MeanReversion", "Momentum", "FundingCarry"]

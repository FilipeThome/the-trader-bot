"""Strategy families. Each one is code the backtest and the live engine share."""

from bot.strategies.funding import FundingCarry
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.trend import TrendBreakout

STRATEGIES = {
    "trend": TrendBreakout(),
    "mean_reversion": MeanReversion(),
    "funding_carry": FundingCarry(),
}

__all__ = ["STRATEGIES", "TrendBreakout", "MeanReversion", "FundingCarry"]

# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Spot execution policy for Binance.

The SDK submits an order that this package has already sized and authorized.
Live orders still pass through the existing mandate gate; this package does
not decide that a model request is permission to trade.
"""

from bot.exchange.execution.orchestrate import (
    cancel_spot_order,
    place_spot_order,
)

__all__ = ["cancel_spot_order", "place_spot_order"]

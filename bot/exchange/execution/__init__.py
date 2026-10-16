# First-party code of this repository. It was written in a local, unpublished
# Vibe-Trading checkout and is in no Vibe-Trading commit. See ORIGIN.md.
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

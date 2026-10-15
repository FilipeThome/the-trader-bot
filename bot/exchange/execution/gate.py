# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Read-only report of the Binance execution gate.

These fields describe the gate. They do not grant trading authority, and a
diagnostic caller cannot flip them. They are derived from
``controls.standing_blockers``, the same calculation the order path runs in
its final pre-submission check, so the report cannot say "open" while a
standing control blocks orders.
"""

from __future__ import annotations

from typing import Any

from bot.exchange.execution.controls import (
    LIVE_TRADE,
    PAPER_TRADE,
    standing_blockers,
)
from bot.exchange.types import TradingProfile


def execution_gate_status(profile: TradingProfile, config: Any) -> dict[str, Any]:
    """Return the enforced gate state for a profile.

    ``order_gate`` is ``open`` only when no standing control blocks a Spot
    Testnet order; ``mandate_required`` when the live trading profile's only
    standing blocker is the mandate checked at order time; otherwise
    ``blocked``. ``trading_authorized`` means "no standing blocker", never
    that a particular order will pass its order-specific checks.
    ``diagnostic_authorizes_trading`` is always false. ``live_trading_enabled``
    is always false: this report never enables live orders.
    """
    reasons = standing_blockers(profile, config)
    if not reasons and profile.id == PAPER_TRADE:
        gate = "open"
    elif profile.id == LIVE_TRADE and reasons == ["mandate_required"]:
        gate = "mandate_required"
    else:
        gate = "blocked"
    return {
        "trading_authorized": gate == "open",
        "order_gate": gate,
        "blocked_reasons": reasons,
        "diagnostic_authorizes_trading": False,
        "live_trading_enabled": False,
        "halted": "halted" in reasons,
        "audit_only": "audit_only" in reasons,
    }

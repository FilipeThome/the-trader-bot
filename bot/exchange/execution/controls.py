# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Standing execution blockers for a Binance trading profile. Read-only.

One calculation shared by the execution gate report, the safety status tool,
the order preview, and the final pre-submission check in ``orchestrate``.
It covers every control that does not depend on a particular order: profile
identity and host, read-only profiles, the halt flag, Spot Testnet audit-only
mode, the configured notional cap, the daily operation limit, credentials,
and journal state. Order-specific checks (size, filters, cap headroom,
balance, quote freshness) stay with the order.

An empty result means no standing control blocks a new order. It never
authorizes trading by itself: every order still passes the order-specific
checks, and live orders still need a valid mandate at order time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.exchange.types import TradingProfile

PAPER_TRADE = "binance-paper-trade"
LIVE_TRADE = "binance-live-trade"


def halted() -> bool:
    """Read the Binance halt flag. An unreadable flag reads as halted."""
    try:
        from bot.live.halt import halt_flag_set

        return bool(halt_flag_set("binance"))
    except Exception:  # noqa: BLE001 - fail closed
        return True


def standing_blockers(
    profile: TradingProfile,
    config: Any,
    *,
    quote_asset: str = "USDT",
    now: datetime | None = None,
) -> list[str]:
    """Return every non-order-specific reason a new order would be refused."""
    from bot.exchange import sdk
    from bot.exchange.execution import journal
    from bot.exchange.execution.orchestrate import _identity_error

    reasons: list[str] = []
    if config is None:
        reasons.append("config_error")
    else:
        identity = _identity_error(profile, config)
        if identity:
            reasons.extend(identity.get("reasons") or ["profile_not_tradable"])
    if bool(getattr(profile, "readonly", True)):
        reasons.append("profile_readonly")
    if halted():
        reasons.append("halted")
    moment = now or datetime.now(timezone.utc)
    if profile.id == PAPER_TRADE:
        if sdk.testnet_audit_only()[0]:
            reasons.append("audit_only")
        if sdk.paper_order_notional_limit(quote_asset) is None:
            reasons.append("notional_limit_unconfigured")
        limit = sdk.testnet_daily_operation_limit()
        if limit is None:
            reasons.append("daily_limit_invalid")
        else:
            try:
                if journal.daily_operation_count(profile.id, moment) >= limit:
                    reasons.append("daily_limit_reached")
            except Exception:  # noqa: BLE001 - an uncountable journal blocks
                reasons.append("journal_unavailable")
    if config is not None and not (getattr(config, "api_key", "") and getattr(config, "api_secret", "")):
        reasons.append("credentials_missing")
    try:
        if journal.has_unresolved():
            reasons.append("unresolved_execution")
    except Exception:  # noqa: BLE001 - an unreadable journal blocks
        reasons.append("journal_unavailable")
    if profile.id == LIVE_TRADE:
        reasons.append("mandate_required")
    return list(dict.fromkeys(reasons))

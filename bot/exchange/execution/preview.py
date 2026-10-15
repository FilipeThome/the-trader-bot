# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Read-only Binance spot preflight.

A passing preview is not an order. This module does not journal a submission
and does not call the exchange write adapter.

"Previewable" (``decision == "validation_passed"``) means the order passes the
same order-specific rules placement applies: exchange filters, the Spot
Testnet notional cap (including a limit sell's crossing-book estimate), and
the account checks. It is not authorization: ``execution_blocked`` and
``blocked_reasons`` report the standing controls (halt, audit-only, daily
limit, journal, ...) that would still refuse it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.exchange import sdk
from bot.exchange.execution.controls import PAPER_TRADE, standing_blockers
from bot.exchange.execution.orchestrate import (
    _deny,
    _guarded,
    _identity_error,
    _paper_notional_reasons,
    _structural_error,
)
from bot.exchange.execution.preflight import evaluate_account, reference_price
from bot.exchange.execution.rules import (
    SizeError,
    SizeOutOfRange,
    parse_market,
    parse_size,
    size_order,
)
from bot.exchange.types import TradingProfile

_BLOCKED = frozenset(
    {
        "halted",
        "unresolved_execution",
        "host_rejected",
        "profile_not_tradable",
        "journal_unavailable",
        "persist_failed",
    }
)
_UNAVAILABLE = frozenset(
    {
        "read_failed",
        "quote_unavailable",
        "quote_stale",
        "open_orders_unknown",
        "balance_unknown",
    }
)


def preview_spot_order(
    profile: TradingProfile,
    config: Any,
    *,
    symbol: str,
    side: str,
    quantity: Any = None,
    notional: Any = None,
    order_type: str = "market",
    limit_price: Any = None,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Validate one spot order and return without submitting it."""
    identity = _identity_error(profile, config)
    if identity:
        return _preview(identity)
    structural = _structural_error(
        side=side,
        order_type=order_type,
        quantity=quantity,
        notional=notional,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )
    if structural:
        return _preview(structural)
    try:
        from bot.live.halt import halt_flag_set

        if halt_flag_set("binance"):
            return _preview(_deny(("halted",)))
    except Exception as exc:  # noqa: BLE001
        return _preview(_deny(("halted",), detail=type(exc).__name__))
    canonical = sdk.normalize_symbol(symbol)
    if "/" not in canonical:
        return _preview(_deny(("symbol_unresolved",)))
    try:
        parsed_quantity = None if quantity is None else parse_size(quantity)
        parsed_notional = None if notional is None else parse_size(notional)
        parsed_price = None if limit_price is None else parse_size(limit_price)
    except SizeOutOfRange:
        return _preview(_deny(("size_out_of_range",)))
    except SizeError:
        return _preview(_deny(("size_not_positive",)))
    market_payload = _guarded(sdk.get_spot_market, canonical, config=config)
    snapshot = _guarded(sdk.get_account_snapshot, config)
    orders = _guarded(sdk.get_open_orders, config)
    quote = _guarded(sdk.get_quote, canonical, config=config)
    if any(
        isinstance(item, dict) and item.get("status") == "error" and item.get("error") == "read_failed"
        for item in (market_payload, snapshot, orders, quote)
    ):
        return _preview(_deny(("read_failed",)))
    market = market_payload.get("market") if isinstance(market_payload, dict) else None
    if str(market_payload.get("status", "")).lower() != "ok" or not isinstance(market, dict):
        return _preview(_deny(("symbol_unresolved",)))
    rules, rule_reasons = parse_market(market)
    if rules is None:
        return _preview(_deny(rule_reasons))
    side_clean = str(side).strip().lower()
    sized, size_reasons = size_order(
        order_type=str(order_type).strip().lower(),
        quantity=parsed_quantity,
        notional=parsed_notional,
        limit_price=parsed_price,
        rules=rules,
        reference_price=reference_price(quote, side_clean),
    )
    if sized is None:
        return _preview(_deny(size_reasons))
    if profile.id == PAPER_TRADE:
        cap_reasons = _paper_notional_reasons(
            canonical,
            sized,
            order_type=str(order_type).strip().lower(),
            side=side_clean,
            rules=rules,
            quote=quote,
        )
        if cap_reasons:
            return _preview(_deny(cap_reasons))
    account_reasons = evaluate_account(
        sized=sized,
        rules=rules,
        side=side_clean,
        snapshot=snapshot,
        quote=quote,
        open_orders=orders,
        canonical_symbol=canonical,
        client_order_id="preview",
        now=datetime.now(timezone.utc),
    )
    if account_reasons:
        return _preview(_deny(account_reasons))
    blockers = standing_blockers(profile, config, quote_asset=canonical.split("/", 1)[1])
    return {
        "execution_blocked": bool(blockers),
        "blocked_reasons": blockers,
        "status": "preview",
        "decision": "validation_passed",
        "submitted": False,
        "filled": False,
        "simulated": False,
        "order_id": None,
        "symbol": canonical,
        "side": side_clean,
        "quantity": sized.quantity_text(),
        "notional": sized.notional_text(),
        "limit_price": sized.limit_price_text(),
        "diagnostic_authorizes_trading": False,
    }


def _preview(denied: dict[str, Any]) -> dict[str, Any]:
    reasons = list(denied.get("reasons") or [])
    if any(reason in _BLOCKED for reason in reasons):
        decision = "execution_blocked"
    elif any(reason in _UNAVAILABLE for reason in reasons):
        decision = "data_unavailable"
    else:
        decision = "validation_rejected"
    return {
        "status": "preview",
        "decision": decision,
        "reasons": reasons,
        "submitted": False,
        "filled": False,
        "simulated": False,
        "order_id": None,
        "diagnostic_authorizes_trading": False,
    }

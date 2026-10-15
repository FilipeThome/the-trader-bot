# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""In-memory Binance spot simulation.

This module does not import or call an exchange write. A Production host is
refused before any simulated fill is applied.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlparse

from bot.exchange.execution.rules import (
    SizeError,
    parse_market,
    parse_size,
    size_order,
)
from bot.exchange.types import TradingProfile

_LIVE_HOSTS = frozenset({"api.binance.com", "fapi.binance.com"})


def simulate_spot_order(
    profile: TradingProfile,
    config: Any,
    *,
    symbol: str,
    side: str,
    market: Mapping[str, Any],
    quote: Mapping[str, Any],
    balances: dict[str, str],
    quantity: Any = None,
    notional: Any = None,
    order_type: str = "market",
    limit_price: Any = None,
    client_order_id: str = "",
    seen_client_ids: set[str] | None = None,
    outcome: str = "filled",
    max_notional: Decimal | None = None,
) -> dict[str, Any]:
    """Apply one synthetic spot order to ``balances`` and return a simulated result."""
    blocked = _production_block(profile, config)
    if blocked:
        return blocked
    if client_order_id and seen_client_ids is not None and client_order_id in seen_client_ids:
        return _simulated("execution_blocked", reasons=["duplicate_client_order"])
    if outcome == "timeout":
        return _simulated("unknown", reasons=["timeout"], ambiguous=True)
    if outcome == "rejected":
        return _simulated("validation_rejected", reasons=["exchange_rejected"])
    rules, reasons = parse_market(dict(market))
    if rules is None:
        return _simulated("validation_rejected", reasons=list(reasons))
    try:
        parsed_qty = None if quantity is None else parse_size(quantity)
        parsed_notional = None if notional is None else parse_size(notional)
        parsed_price = None if limit_price is None else parse_size(limit_price)
    except SizeError:
        return _simulated("validation_rejected", reasons=["size_not_positive"])
    reference = _reference(quote, side)
    sized, size_reasons = size_order(
        order_type=str(order_type or "").strip().lower(),
        quantity=parsed_qty,
        notional=parsed_notional,
        limit_price=parsed_price,
        rules=rules,
        reference_price=reference,
    )
    if sized is None:
        return _simulated("validation_rejected", reasons=list(size_reasons))
    spent = sized.notional if sized.notional is not None else (
        None if reference is None or sized.quantity is None else sized.quantity * reference
    )
    if max_notional is not None and spent is not None and spent > max_notional:
        return _simulated("validation_rejected", reasons=["risk_limit"])
    base, quote_asset = _split(symbol, market)
    if str(side).strip().lower() == "buy":
        if spent is None:
            return _simulated("data_unavailable", reasons=["quote_unavailable"])
        free = _balance(balances, quote_asset)
        if free is None:
            return _simulated("data_unavailable", reasons=["balance_unknown"])
        if free < spent:
            return _simulated("validation_rejected", reasons=["balance_insufficient"])
        fill_qty = sized.quantity if sized.quantity is not None else (spent / reference if reference else None)
        if outcome == "partial" and fill_qty is not None:
            fill_qty = fill_qty / Decimal(2)
            spent = spent / Decimal(2)
        if fill_qty is None:
            return _simulated("data_unavailable", reasons=["quote_unavailable"])
        balances[quote_asset] = str(free - spent)
        base_free = _balance(balances, base)
        if base_free is None:
            return _simulated("data_unavailable", reasons=["balance_unknown"])
        balances[base] = str(base_free + fill_qty)
        filled = "partial" if outcome == "partial" else "filled"
    elif str(side).strip().lower() == "sell":
        if sized.quantity is None:
            return _simulated("validation_rejected", reasons=["size_missing"])
        free = _balance(balances, base)
        fill_qty = sized.quantity if outcome != "partial" else sized.quantity / Decimal(2)
        if free is None:
            return _simulated("data_unavailable", reasons=["balance_unknown"])
        if free < fill_qty:
            return _simulated("validation_rejected", reasons=["balance_insufficient"])
        balances[base] = str(free - fill_qty)
        if spent is not None:
            quote_free = _balance(balances, quote_asset)
            if quote_free is None:
                return _simulated("data_unavailable", reasons=["balance_unknown"])
            credit = spent if outcome != "partial" else spent / Decimal(2)
            balances[quote_asset] = str(quote_free + credit)
        filled = "partial" if outcome == "partial" else "filled"
    else:
        return _simulated("validation_rejected", reasons=["invalid_side"])
    if seen_client_ids is not None and client_order_id:
        seen_client_ids.add(client_order_id)
    return _simulated(
        "validation_passed",
        fill=filled,
        quantity=str(fill_qty),
        notional=None if spent is None else str(spent),
    )


def _production_block(profile: TradingProfile, config: Any) -> dict[str, Any] | None:
    host = (urlparse(str(getattr(config, "host", "") or "")).hostname or "").lower()
    profile_name = str(getattr(config, "profile", "") or "")
    if host in _LIVE_HOSTS or profile_name in {"live", "live-readonly"} or profile.id != "binance-paper-trade":
        return _simulated("execution_blocked", reasons=["production_write_refused"])
    return None


def _split(symbol: str, market: Mapping[str, Any]) -> tuple[str, str]:
    raw = str(market.get("symbol") or symbol).upper().replace("-", "/")
    if "/" not in raw:
        return raw, "USDT"
    base, quote = raw.split("/", 1)
    return base, quote


def _balance(balances: dict[str, str], asset: str) -> Decimal | None:
    try:
        number = Decimal(str(balances.get(asset, "0")))
    except Exception:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _reference(quote: Mapping[str, Any], side: str) -> Decimal | None:
    body = quote.get("quote") if isinstance(quote.get("quote"), Mapping) else quote
    key = "ask" if str(side).lower() == "buy" else "bid"
    try:
        number = Decimal(str(body.get(key)))
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _simulated(
    decision: str,
    *,
    reasons: list[str] | None = None,
    fill: str | None = None,
    quantity: str | None = None,
    notional: str | None = None,
    ambiguous: bool = False,
) -> dict[str, Any]:
    return {
        "status": "simulated",
        "simulated": True,
        "submitted_to_exchange": False,
        "decision": decision,
        "reasons": list(reasons or []),
        "fill": fill,
        "quantity": quantity,
        "notional": notional,
        "ambiguous": ambiguous,
        "diagnostic_authorizes_trading": False,
    }

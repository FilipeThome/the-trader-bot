# First-party code of this repository. It was written in a local, unpublished
# Vibe-Trading checkout and is in no Vibe-Trading commit. See ORIGIN.md.
"""Account, quote, and journal checks that do not talk to the exchange."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from bot.exchange.execution.rules import (
    MarketRules,
    SizedOrder,
)

QUOTE_MAX_AGE = timedelta(seconds=15)
_FUTURE_SKEW = timedelta(seconds=5)


def _as_decimal(value: Any, *, high: bool) -> Decimal | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return None
            exact = Decimal(value)
            short = Decimal(str(value))
            number = max(exact, short) if high else min(exact, short)
        else:
            number = Decimal(str(value).strip())
    except Exception:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _quote_time(raw: Any, now: datetime) -> datetime | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if text.isdigit():
        millis = int(text)
        if millis > 10_000_000_000:
            return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(millis, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _balance_free(snapshot: Mapping[str, Any], asset: str) -> Decimal | None:
    if str(snapshot.get("status", "")).lower() != "ok":
        return None
    rows = snapshot.get("balances")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("asset", "")).upper() != asset.upper():
            continue
        free = row.get("free")
        if free is None or free == "":
            return None
        try:
            number = Decimal(str(free).strip()) if not isinstance(free, (int, float, Decimal)) else (
                Decimal(str(free)) if isinstance(free, float) else Decimal(free)
            )
        except Exception:
            return None
        if isinstance(free, float):
            number = _as_decimal(free, high=False) or number
        if not number.is_finite() or number < 0:
            return None
        return number
    return Decimal(0)


def quote_is_fresh(quote: Mapping[str, Any], *, now: datetime | None = None) -> tuple[bool, str | None]:
    """A missing or old ticker timestamp is stale. Local time is not a price time."""
    moment = now or datetime.now(timezone.utc)
    observed = _quote_time((quote.get("quote") or {}).get("time") if isinstance(quote.get("quote"), Mapping) else quote.get("time"), moment)
    if observed is None:
        return False, "quote_stale"
    if observed - moment > _FUTURE_SKEW or moment - observed > QUOTE_MAX_AGE:
        return False, "quote_stale"
    return True, None


def open_orders_are_complete(payload: Mapping[str, Any]) -> bool:
    """An empty list with a degrade note is not proof of zero open orders."""
    if str(payload.get("status", "")).lower() != "ok":
        return False
    if "open_orders_note" in payload:
        return False
    return isinstance(payload.get("open_orders"), list)


def reference_price(quote: Mapping[str, Any], side: str) -> Decimal | None:
    """Lower observed ask (buy) or bid (sell), so a minimum check cannot pass on a rounded-up price."""
    raw_body = quote.get("quote")
    body: Mapping[str, Any] = raw_body if isinstance(raw_body, Mapping) else {}
    key = "ask" if side == "buy" else "bid"
    return _as_decimal(body.get(key), high=False)


def evaluate_account(
    *,
    sized: SizedOrder,
    rules: MarketRules,
    side: str,
    snapshot: Mapping[str, Any],
    quote: Mapping[str, Any],
    open_orders: Mapping[str, Any],
    canonical_symbol: str,
    client_order_id: str,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Return deny reasons for balance, freshness, and duplicate identity."""
    reasons: list[str] = []
    if str(quote.get("status", "")).lower() != "ok":
        reasons.append("quote_unavailable")
    raw_body = quote.get("quote")
    body: Mapping[str, Any] = raw_body if isinstance(raw_body, Mapping) else {}
    fresh, fresh_reason = quote_is_fresh(quote, now=now)
    if not fresh and fresh_reason:
        reasons.append(fresh_reason)
    if not open_orders_are_complete(open_orders):
        reasons.append("open_orders_unknown")
    else:
        for row in open_orders.get("open_orders") or []:
            if isinstance(row, Mapping) and str(row.get("client_order_id") or "") == client_order_id:
                reasons.append("duplicate_client_order")
                break
    if quote.get("symbol") not in (None, "", canonical_symbol):
        reasons.append("quote_unavailable")
    reference = _as_decimal(body.get("ask" if side == "buy" else "bid"), high=side == "buy")
    if side == "buy":
        if sized.notional is not None:
            spent = sized.notional
        elif sized.quantity is not None and reference is not None:
            spent = sized.quantity * reference
        else:
            spent = None
            reasons.append("quote_unavailable")
        if spent is not None:
            required = spent * (Decimal(1) + rules.taker_fee)
            free = _balance_free(snapshot, canonical_symbol.split("/", 1)[1])
            if free is None:
                reasons.append("balance_unknown")
            elif free < required:
                reasons.append("balance_insufficient")
    else:
        reference = _as_decimal(body.get("bid"), high=False)
        if sized.quantity is not None:
            required_base = sized.quantity * (Decimal(1) + rules.taker_fee)
        elif sized.notional is not None and reference is not None and reference > 0:
            required_base = (sized.notional / reference) * (Decimal(1) + rules.taker_fee)
        else:
            required_base = None
            reasons.append("quote_unavailable")
        if required_base is not None:
            free = _balance_free(snapshot, canonical_symbol.split("/", 1)[0])
            if free is None:
                reasons.append("balance_unknown")
            elif free < required_base:
                reasons.append("balance_insufficient")
    return tuple(dict.fromkeys(reasons))

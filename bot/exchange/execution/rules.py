# First-party code of this repository. It was written in a local, unpublished
# Vibe-Trading checkout and is in no Vibe-Trading commit. See ORIGIN.md.
"""Pure Binance spot filter parsing and size flooring.

ccxt 4.5.76 truncates a spot market ``quoteOrderQty`` to ``precision.price``
in ``TICK_SIZE`` mode. The notional sent to the exchange is floored to that
tick. Base quantity is floored to the lot step. A limit price that is not
already on the tick is rejected rather than moved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

_REQUIRED_FILTERS = ("PRICE_FILTER", "LOT_SIZE")
_NOTIONAL_FILTERS = ("NOTIONAL", "MIN_NOTIONAL")


class SizeError(ValueError):
    """A size could not be read as a positive finite decimal."""


class SizeOutOfRange(SizeError):
    """A size is a positive finite decimal outside the range orders can use."""


#: Decimal exponent bounds for any caller size, limit price or notional.
#: Binance spot values sit far inside them (steps down to 1e-8, quantities and
#: prices below 1e10); outside them a value is refused before any arithmetic.
MAX_SIZE_EXPONENT = 15
MIN_SIZE_EXPONENT = -20


@dataclass(frozen=True)
class MarketRules:
    """Exchange filters needed before a spot order can be sized."""

    symbol: str
    price_tick: Decimal
    amount_tick: Decimal | None
    lot_step: Decimal
    market_step: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    market_min_qty: Decimal | None
    market_max_qty: Decimal | None
    min_notional: Decimal | None
    max_notional: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    limit_ticks: tuple[Decimal, ...]
    taker_fee: Decimal
    active: bool
    spot: bool


@dataclass(frozen=True)
class SizedOrder:
    """The only size that may be forwarded to the exchange."""

    quantity: Decimal | None
    notional: Decimal | None
    limit_price: Decimal | None

    def quantity_text(self) -> str | None:
        return None if self.quantity is None else decimal_text(self.quantity)

    def notional_text(self) -> str | None:
        return None if self.notional is None else decimal_text(self.notional)

    def limit_price_text(self) -> str | None:
        return None if self.limit_price is None else decimal_text(self.limit_price)


def decimal_text(value: Decimal) -> str:
    """Render ``value`` without an exponent so the wire value cannot grow."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_size(value: Any) -> Decimal:
    """Parse a caller size without rounding a float up to a coarser step.

    Floats use their exact binary value. The later floor-to-step can only
    make that value smaller. Strings are taken as written.
    """
    if isinstance(value, bool) or value is None:
        raise SizeError("size is missing")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        token = value.strip()
        if not token or token.lower() in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
            raise SizeError("size is not a positive finite decimal")
        try:
            number = Decimal(token)
        except InvalidOperation as exc:
            raise SizeError("size is not a positive finite decimal") from exc
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise SizeError("size is not a positive finite decimal")
        number = Decimal.from_float(value)
    else:
        raise SizeError("size is not a positive finite decimal")
    if not number.is_finite() or number <= 0:
        raise SizeError("size is not a positive finite decimal")
    if not MIN_SIZE_EXPONENT <= number.adjusted() <= MAX_SIZE_EXPONENT:
        raise SizeOutOfRange("size is outside the supported range")
    return number


def float_not_above(value: Decimal) -> float:
    """Return the greatest binary float that is still ``<= value``."""
    if not value.is_finite() or value <= 0:
        raise SizeError("size is not a positive finite decimal")
    candidate = float(value)
    if not math.isfinite(candidate) or candidate <= 0:
        raise SizeError("size is not representable")
    while Decimal(candidate) > value:
        nxt = math.nextafter(candidate, -math.inf)
        if nxt == candidate or not math.isfinite(nxt) or nxt <= 0:
            raise SizeError("size is not representable without increasing")
        candidate = nxt
    return candidate


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor ``value`` to a multiple of ``step``."""
    if step <= 0:
        raise SizeError("exchange step is not positive")
    return (value // step) * step


def _decimal_field(raw: Mapping[str, Any], key: str) -> Decimal | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise SizeError(f"filter {key} is not a decimal") from exc
    if not number.is_finite() or number <= 0:
        return None
    return number


def _filter_map(market: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    info = market.get("info")
    rows = info.get("filters") if isinstance(info, Mapping) else None
    found: dict[str, Mapping[str, Any]] = {}
    if not isinstance(rows, list):
        return found
    for row in rows:
        if isinstance(row, Mapping) and row.get("filterType"):
            found[str(row["filterType"])] = row
    return found


def _positive(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def parse_market(market: Mapping[str, Any]) -> tuple[MarketRules | None, tuple[str, ...]]:
    """Parse one ccxt market dict. Missing required filters deny."""
    reasons: list[str] = []
    if market.get("active") is not True:
        reasons.append("market_inactive")
    if market.get("spot") is not True:
        reasons.append("market_not_spot")
    filters = _filter_map(market)
    for name in _REQUIRED_FILTERS:
        if name not in filters:
            reasons.append(f"filter_missing:{name}")
    if not any(name in filters for name in _NOTIONAL_FILTERS):
        reasons.append("filter_missing:NOTIONAL")
    elif _decimal_field(filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}, "minNotional") is None:
        reasons.append("filter_missing:NOTIONAL.minNotional")
    raw_precision = market.get("precision")
    precision: Mapping[str, Any] = raw_precision if isinstance(raw_precision, Mapping) else {}
    price_tick = _positive(precision.get("price"))
    price_filter = filters.get("PRICE_FILTER", {})
    filter_tick = _decimal_field(price_filter, "tickSize") if price_filter else None
    if price_tick is None:
        reasons.append("filter_missing:precision.price")
    lot = filters.get("LOT_SIZE", {})
    lot_step = _decimal_field(lot, "stepSize") if lot else None
    if lot_step is None and "LOT_SIZE" in filters:
        reasons.append("filter_missing:LOT_SIZE.stepSize")
    if reasons or price_tick is None or lot_step is None:
        return None, tuple(dict.fromkeys(reasons))
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    notional_row = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    taker = market.get("taker")
    try:
        fee = Decimal(str(taker).strip()) if taker is not None and not isinstance(taker, bool) else None
    except InvalidOperation:
        fee = None
    if fee is None or not fee.is_finite() or fee < 0 or fee >= 1:
        reasons.append("fees_unknown")
        return None, tuple(dict.fromkeys(reasons))
    limit_ticks = tuple(dict.fromkeys(tick for tick in (price_tick, filter_tick) if tick is not None))
    return (
        MarketRules(
            symbol=str(market.get("symbol") or ""),
            price_tick=price_tick,
            amount_tick=_positive(precision.get("amount")),
            lot_step=lot_step,
            market_step=_decimal_field(market_lot, "stepSize") if market_lot else None,
            min_qty=_decimal_field(lot, "minQty"),
            max_qty=_decimal_field(lot, "maxQty"),
            market_min_qty=_decimal_field(market_lot, "minQty") if market_lot else None,
            market_max_qty=_decimal_field(market_lot, "maxQty") if market_lot else None,
            min_notional=_decimal_field(notional_row, "minNotional"),
            max_notional=_decimal_field(notional_row, "maxNotional"),
            min_price=_decimal_field(price_filter, "minPrice"),
            max_price=_decimal_field(price_filter, "maxPrice"),
            limit_ticks=limit_ticks,
            taker_fee=fee,
            active=True,
            spot=True,
        ),
        (),
    )


def _floor_all(value: Decimal, steps: list[Decimal]) -> Decimal | None:
    current = value
    for _ in range(len(steps) + 1):
        for step in steps:
            current = floor_to_step(current, step)
        if current > 0 and all(current % step == 0 for step in steps):
            return current
    return None


def _within(value: Decimal, low: Decimal | None, high: Decimal | None) -> str | None:
    if low is not None and value < low:
        return "below"
    if high is not None and value > high:
        return "above"
    return None


def size_order(
    *,
    order_type: str,
    quantity: Decimal | None,
    notional: Decimal | None,
    limit_price: Decimal | None,
    rules: MarketRules,
    reference_price: Decimal | None,
) -> tuple[SizedOrder | None, tuple[str, ...]]:
    """Floor the order to the filters ccxt will apply, or deny.

    Exchange filters are applied as Binance applies them: a limit order's
    NOTIONAL filter uses quantity x limit price, a market order's uses the
    observed reference price. Decimal arithmetic that cannot be carried out
    exactly (a value far outside any filter) denies with
    ``size_out_of_range`` instead of raising.
    """
    try:
        return _size_order(
            order_type=order_type,
            quantity=quantity,
            notional=notional,
            limit_price=limit_price,
            rules=rules,
            reference_price=reference_price,
        )
    except (ArithmeticError, SizeOutOfRange):
        return None, ("size_out_of_range",)


def _size_order(
    *,
    order_type: str,
    quantity: Decimal | None,
    notional: Decimal | None,
    limit_price: Decimal | None,
    rules: MarketRules,
    reference_price: Decimal | None,
) -> tuple[SizedOrder | None, tuple[str, ...]]:
    reasons: list[str] = []
    if order_type in {"limit", "stop_loss_limit", "take_profit_limit"}:
        if quantity is None or limit_price is None:
            return None, ("limit_price_required",)
        if notional is not None:
            return None, ("notional_on_limit",)
        if any(limit_price % tick != 0 for tick in rules.limit_ticks):
            reasons.append("price_off_tick")
        if rules.min_price is not None and limit_price < rules.min_price:
            reasons.append("price_out_of_range")
        if rules.max_price is not None and limit_price > rules.max_price:
            reasons.append("price_out_of_range")
        floored = _floor_all(quantity, [step for step in (rules.lot_step, rules.amount_tick) if step])
        if floored is None:
            reasons.append("below_min_qty")
        elif (band := _within(floored, rules.min_qty, rules.max_qty)) == "below":
            reasons.append("below_min_qty")
        elif band == "above":
            reasons.append("above_max_qty")
        if floored is not None:
            # Binance's NOTIONAL filter values a limit order at its own price.
            spent = floored * limit_price
            if (band := _within(spent, rules.min_notional, rules.max_notional)) == "below":
                reasons.append("below_min_notional")
            elif band == "above":
                reasons.append("above_max_notional")
        if reasons or floored is None:
            return None, tuple(dict.fromkeys(reasons))
        return SizedOrder(quantity=floored, notional=None, limit_price=limit_price), ()

    if order_type != "market":
        return None, ("invalid_order_type",)
    if notional is not None:
        floored_notional = floor_to_step(notional, rules.price_tick)
        if floored_notional <= 0 or (rules.min_notional is not None and floored_notional < rules.min_notional):
            reasons.append("below_min_notional")
        if rules.max_notional is not None and floored_notional > rules.max_notional:
            reasons.append("above_max_notional")
        if reasons:
            return None, tuple(dict.fromkeys(reasons))
        return SizedOrder(quantity=None, notional=floored_notional, limit_price=None), ()

    if quantity is None:
        return None, ("size_missing",)
    steps = [step for step in (rules.market_step or rules.lot_step, rules.lot_step, rules.amount_tick) if step]
    floored_qty = _floor_all(quantity, steps)
    if floored_qty is None:
        return None, ("below_min_qty",)
    min_qty = rules.market_min_qty or rules.min_qty
    max_qty = rules.market_max_qty or rules.max_qty
    if (band := _within(floored_qty, min_qty, max_qty)) == "below":
        reasons.append("below_min_qty")
    elif band == "above":
        reasons.append("above_max_qty")
    if reference_price is None:
        reasons.append("quote_unavailable")
    else:
        spent = floored_qty * reference_price
        if (band := _within(spent, rules.min_notional, rules.max_notional)) == "below":
            reasons.append("below_min_notional")
        elif band == "above":
            reasons.append("above_max_notional")
    if reasons:
        return None, tuple(dict.fromkeys(reasons))
    return SizedOrder(quantity=floored_qty, notional=None, limit_price=None), ()

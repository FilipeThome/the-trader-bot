"""Binance-only trading service.

The panel and the executor call this module. It never talks to ccxt itself:
entries go through ``execute_live_order``, which calls back into the ported
execution package. That package journals the order before the exchange write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bot.exchange import sdk
from bot.exchange.execution import orchestrate
from bot.exchange.execution.journal import daily_operation_count
from bot.exchange.execution.reconcile import reconcile_journal
from bot.exchange.execution.rules import MarketRules, parse_market
from bot.exchange.profiles import profile_by_id
from bot.exchange.types import TradingProfile
from bot.live.daily_count import read_daily_count
from bot.live.enforcement import OrderIntent
from bot.live.halt import halt_flag_set
from bot.live.mandate.model import MANDATE_SCHEMA_VERSION, AssetClass, InstrumentType
from bot.live.mandate.store import load_mandate
from bot.live.sdk_order_gate import execute_live_order

logger = logging.getLogger(__name__)
_PAPER = "binance-paper-trade"

_TRADABLE = frozenset({"binance-paper-trade", "binance-live-trade-bot"})
_CONFIG_KEYS = frozenset(
    {"profile", "testnet_host", "market_type", "api_key", "api_secret", "observation_absolute_tolerance"}
)


def _refusal() -> dict[str, Any]:
    return {
        "status": "error",
        "decision": "DENY",
        "reasons": ["production_writes_disabled"],
        "request_sent": False,
        "error": "order blocked: production_writes_disabled",
    }


def _profile(profile_id: str | None) -> TradingProfile | None:
    try:
        return profile_by_id(profile_id)
    except KeyError:
        return None


def _split(kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = {key: kwargs.pop(key) for key in list(kwargs) if key in _CONFIG_KEYS}
    return config, kwargs


def place_order(symbol: str, profile_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Place one order, or refuse it before the exchange exists.

    ``binance-live-trade`` and every other non-bot live profile are refused
    here, including a forged writable copy. The readonly tests depend on that.
    """
    profile = _profile(profile_id)
    if profile is None or profile.id not in _TRADABLE:
        return _refusal()
    return submit_order(symbol, profile.id, **kwargs)


def cancel_order(order_id: str, profile_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    profile = _profile(profile_id)
    if profile is None or profile.id not in _TRADABLE:
        return _refusal()
    config_overrides, rest = _split(dict(kwargs))
    config = sdk.build_config(dict(profile.config), config_overrides)
    symbol = rest.get("symbol")
    return orchestrate.cancel_spot_order(profile, config, str(order_id), symbol=symbol)


def submit_order(
    symbol: str,
    profile_id: str,
    *,
    held: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the mandate gate, then the journaled execution callback.

    ``held`` lists the bot's own open positions (``symbol``, ``quantity``). The
    mandate caps exposure of those positions, priced at the current quote; the
    rest of the account (cash, the operator's own coins) is not bot exposure.
    A position that cannot be priced is passed without a price, and the gate
    denies the order.
    """
    profile = profile_by_id(profile_id)
    if profile.id not in _TRADABLE:
        return _refusal()
    config_overrides, order = _split(dict(kwargs))
    config = sdk.build_config(dict(profile.config), config_overrides)
    side = str(order.get("side") or "buy")
    quantity = order.get("quantity")
    notional = order.get("notional")
    intent = OrderIntent(
        symbol=str(symbol).upper(),
        side=side,
        notional_usd=float(notional) if notional is not None else None,
        quantity=float(quantity) if quantity is not None else None,
        instrument_type=InstrumentType.CRYPTO,
        asset_class=AssetClass.CRYPTO,
        limit_price=float(order["limit_price"]) if order.get("limit_price") is not None else None,
    )

    def _place(config: Any = None, **place_kwargs: Any) -> dict[str, Any]:
        payload = dict(place_kwargs)
        payload.setdefault("symbol", symbol)
        payload.setdefault("side", side)
        return orchestrate.place_spot_order(profile, config, **payload)

    class _Module:
        def get_quote(self, quoted: str, *, config: Any = None, **_: Any) -> dict[str, Any]:
            return sdk.get_quote(quoted, config=config)

        def get_positions(self, config: Any = None) -> dict[str, Any]:
            return {"status": "ok", "positions": _priced_positions(held or [], config)}

        def get_account_snapshot(self, config: Any = None) -> dict[str, Any]:
            return sdk.get_account_snapshot(config)

        def place_order(self, config: Any = None, **place_kwargs: Any) -> dict[str, Any]:
            return _place(config, **place_kwargs)

    return execute_live_order(
        broker="binance",
        connector_module=_Module(),
        config=config,
        intent=intent,
        place_kwargs={
            "symbol": symbol,
            "side": side,
            "quantity": None if quantity is None else str(quantity),
            "notional": None if notional is None else str(notional),
            "order_type": order.get("order_type", "market"),
            "limit_price": order.get("limit_price"),
            "stop_price": order.get("stop_price"),
            "time_in_force": order.get("time_in_force", "gtc"),
            "idempotency_key": order.get("idempotency_key"),
            "session_id": order.get("session_id", "bot"),
        },
        session_id=str(order.get("session_id") or "bot"),
    )


def get_quote(symbol: str, profile_id: str) -> dict[str, Any]:
    profile = profile_by_id(profile_id)
    config = sdk.build_config(dict(profile.config))
    return sdk.get_quote(symbol, config=config)


#: Quote assets valued 1:1 against USDT.
_DOLLAR_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"})


def account_balances(profile_id: str) -> dict[str, Any]:
    """The account's non-zero balances, valued in USDT at the last trade price. Read-only.

    A stablecoin counts 1:1. An asset with no ``<ASSET>/USDT`` pair on the
    profile's host has no value and is counted in ``unpriced``. Errors come
    back as ``status: error`` with a short reason, never with credentials.
    """
    profile = profile_by_id(profile_id)
    try:
        config = sdk.build_config(dict(profile.config))
    except Exception as exc:  # noqa: BLE001 - an unreadable binance.json is reported, not raised
        return {"status": "error", "error": "config_invalid", "detail": type(exc).__name__}
    if not config.api_key or not config.api_secret:
        return {"status": "error", "error": "credentials_missing"}
    try:
        snapshot = sdk.get_account_snapshot(config)
    except Exception as exc:  # noqa: BLE001 - an auth or network failure is reported as such
        return {"status": "error", "error": "balance_unavailable", "detail": type(exc).__name__}
    rows = [row for row in snapshot.get("balances") or [] if row.get("asset")]
    others = [str(row["asset"]).upper() for row in rows if str(row["asset"]).upper() not in _DOLLAR_ASSETS]
    try:
        prices = sdk.get_last_prices([f"{asset}/USDT" for asset in others], config=config) if others else {}
    except Exception as exc:  # noqa: BLE001 - balances still show, without value
        logger.warning("last prices failed: %s", type(exc).__name__)
        prices = {}
    balances: list[dict[str, Any]] = []
    for row in rows:
        asset = str(row["asset"]).upper()
        total = float(row.get("total") or 0)
        price = 1.0 if asset in _DOLLAR_ASSETS else prices.get(f"{asset}/USDT")
        balances.append(
            {
                "asset": asset,
                "free": float(row.get("free") or 0),
                "used": float(row.get("used") or 0),
                "total": total,
                "price_usdt": price,
                "value_usdt": None if price is None else total * price,
            }
        )
    balances.sort(key=lambda item: (item["value_usdt"] is None, -(item["value_usdt"] or 0.0), item["asset"]))
    return {
        "status": "ok",
        "profile_id": profile.id,
        "is_testnet": config.is_testnet,
        "balances": balances,
        "total_usdt": sum(item["value_usdt"] or 0.0 for item in balances),
        "unpriced": sum(1 for item in balances if item["value_usdt"] is None),
    }


def journal_status() -> dict[str, Any]:
    """Whether the execution journal blocks new orders, and which keys do it. Read-only."""
    from bot.exchange.execution.journal import _latest_states, _records
    from bot.exchange.execution.lifecycle import BLOCKING_EVENTS

    try:
        states = _latest_states(_records())
    except Exception as exc:  # noqa: BLE001 - an unreadable journal blocks every order
        return {"blocked": True, "error": type(exc).__name__, "blocking": []}
    blocking = [{"state_key": key, "event": event} for key, event in states.items() if event in BLOCKING_EVENTS]
    return {"blocked": bool(blocking), "error": None, "blocking": blocking}


def reconcile(profile_id: str) -> dict[str, Any]:
    profile = profile_by_id(profile_id)
    config = sdk.build_config(dict(profile.config))
    return reconcile_journal(profile, config)


def check_status(profile_id: str) -> dict[str, Any]:
    """Validate that the selected profile has a key pair. No order is sent."""
    profile = profile_by_id(profile_id)
    try:
        config = sdk.build_config(dict(profile.config))
    except Exception as exc:  # noqa: BLE001 - an unreadable config is not ready
        return {"status": "error", "ok": False, "error": type(exc).__name__}
    if not config.api_key or not config.api_secret:
        return {"status": "error", "ok": False, "error": "credentials_missing"}
    return {"status": "ok", "ok": True, "host": config.host, "profile": config.profile}


def _quote_price(symbol: str, config: Any) -> float | None:
    """Last (or bid) price from the connector quote; ``None`` when unavailable."""
    try:
        payload = sdk.get_quote(symbol, config=config)
    except Exception as exc:  # noqa: BLE001 - an unpriced position makes the gate deny
        logger.warning("quote for %s failed: %s", symbol, type(exc).__name__)
        return None
    quote = payload.get("quote") if isinstance(payload, dict) else None
    if not isinstance(quote, dict):
        return None
    for key in ("last", "bid"):
        try:
            value = float(quote.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _priced_positions(held: list[dict[str, Any]], config: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in held:
        symbol = str(item.get("symbol") or "").upper()
        row: dict[str, Any] = {"symbol": symbol, "quantity": float(item.get("quantity") or 0)}
        price = _quote_price(symbol, config)
        if price is not None:
            row["price"] = price
        rows.append(row)
    return rows


def client_order_id(profile_id: str, idempotency_key: str) -> str:
    """The client order id an order keyed ``idempotency_key`` will carry on Binance."""
    return orchestrate.client_order_id_for(profile_by_id(profile_id).id, idempotency_key)


def write_blocker() -> str | None:
    """Why the gate would refuse every order now (halt, no mandate, expired mandate), or ``None``.

    Callers check this before taking a protective stop off the book, so a sell
    the gate will refuse never leaves a position without its stop.
    """
    try:
        if halt_flag_set("binance"):
            return "halt ativo"
    except Exception:  # noqa: BLE001 - an unreadable flag is a halt
        return "flag de halt ilegível"
    mandate = load_mandate("binance")
    if mandate is None or mandate.schema_version != MANDATE_SCHEMA_VERSION:
        return "sem mandato válido"
    try:
        expires = datetime.fromisoformat(mandate.consent.expires_at)
    except (TypeError, ValueError):
        return "validade do mandato ilegível"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires:
        return "mandato expirado"
    return None


def order_status(symbol: str, profile_id: str, client_order_id: str) -> dict[str, Any]:
    """Read one order by client order id. ``status`` is ok, not_found or error."""
    profile = profile_by_id(profile_id)
    try:
        config = sdk.build_config(dict(profile.config))
        return sdk.get_order(config, symbol=symbol, client_order_id=client_order_id)
    except Exception as exc:  # noqa: BLE001 - a failed read is not an absent order
        return {"status": "error", "error": f"read_failed ({type(exc).__name__})"}


def market_rules(symbol: str, profile_id: str) -> MarketRules | None:
    """Exchange filters (tick, lot, minimum notional, taker fee) for ``symbol``."""
    profile = profile_by_id(profile_id)
    try:
        config = sdk.build_config(dict(profile.config))
        payload = sdk.get_spot_market(symbol, config=config)
    except Exception as exc:  # noqa: BLE001 - callers treat missing rules as "cannot size"
        logger.warning("market rules for %s failed: %s", symbol, type(exc).__name__)
        return None
    market = payload.get("market") if payload.get("status") == "ok" else None
    if not isinstance(market, dict):
        return None
    rules, _reasons = parse_market(market)
    return rules


def free_balance(asset: str, profile_id: str) -> Decimal | None:
    """Free balance of ``asset``; ``None`` when the account cannot be read."""
    profile = profile_by_id(profile_id)
    try:
        config = sdk.build_config(dict(profile.config))
        snapshot = sdk.get_account_snapshot(config)
    except Exception as exc:  # noqa: BLE001 - unknown balance is reported as None
        logger.warning("balance read failed: %s", type(exc).__name__)
        return None
    for row in snapshot.get("balances") or []:
        if str(row.get("asset") or "").upper() == asset.upper():
            try:
                return Decimal(str(row.get("free") or 0))
            except InvalidOperation:
                return None
    return Decimal(0)


def orders_remaining(profile_id: str) -> int:
    """Orders the mandate (and, on the testnet, the journal limit) still allow today."""
    mandate = load_mandate("binance")
    if mandate is None:
        return 0
    remaining = mandate.hard_caps.max_trades_per_day - read_daily_count("binance")
    if profile_id == _PAPER:
        limit = sdk.testnet_daily_operation_limit()
        if limit is None:
            return 0
        try:
            used = daily_operation_count(profile_id)
        except Exception as exc:  # noqa: BLE001 - an uncountable journal allows nothing
            logger.warning("journal count failed: %s", type(exc).__name__)
            return 0
        remaining = min(remaining, limit - used)
    return max(0, remaining)


def order_notional_cap(symbol: str, profile_id: str) -> Decimal | None:
    """Largest notional one order may carry: the mandate cap, and the testnet cap."""
    mandate = load_mandate("binance")
    if mandate is None:
        return None
    cap = Decimal(str(mandate.hard_caps.max_order_notional_usd))
    if profile_id == _PAPER and "/" in symbol:
        paper = sdk.paper_order_notional_limit(symbol.split("/", 1)[1])
        if paper is None:
            return None
        cap = min(cap, paper)
    return cap

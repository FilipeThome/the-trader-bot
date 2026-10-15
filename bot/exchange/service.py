"""Binance-only trading service.

The panel and the executor call this module. It never talks to ccxt itself:
entries go through ``execute_live_order``, which calls back into the ported
execution package. That package journals the order before the exchange write.
"""

from __future__ import annotations

from typing import Any

from bot.exchange import sdk
from bot.exchange.execution import orchestrate
from bot.exchange.execution.reconcile import reconcile_journal
from bot.exchange.profiles import profile_by_id
from bot.exchange.types import TradingProfile
from bot.live.enforcement import OrderIntent
from bot.live.mandate.model import AssetClass, InstrumentType
from bot.live.sdk_order_gate import execute_live_order

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


def submit_order(symbol: str, profile_id: str, **kwargs: Any) -> dict[str, Any]:
    """Run the mandate gate, then the journaled execution callback."""
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
        return orchestrate.place_spot_order(profile, config, through_gate=True, **payload)

    class _Module:
        def get_quote(self, quoted: str, *, config: Any = None, **_: Any) -> dict[str, Any]:
            return sdk.get_quote(quoted, config=config)

        def get_positions(self, config: Any = None) -> dict[str, Any]:
            return sdk.get_positions(config)

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


def get_positions(profile_id: str) -> dict[str, Any]:
    profile = profile_by_id(profile_id)
    config = sdk.build_config(dict(profile.config))
    return sdk.get_positions(config)


def get_quote(symbol: str, profile_id: str) -> dict[str, Any]:
    profile = profile_by_id(profile_id)
    config = sdk.build_config(dict(profile.config))
    return sdk.get_quote(symbol, config=config)


def get_history(symbol: str, profile_id: str, **kwargs: Any) -> dict[str, Any]:
    profile = profile_by_id(profile_id)
    config = sdk.build_config(dict(profile.config))
    return sdk.get_historical_bars(symbol, config=config, **kwargs)


def get_safety_status(profile_id: str) -> dict[str, Any]:
    from bot.exchange.execution.controls import standing_blockers
    from bot.live.halt import halt_flag_set

    profile = profile_by_id(profile_id)
    config = sdk.build_config(dict(profile.config))
    return {
        "profile_id": profile.id,
        "halted": bool(halt_flag_set("binance")),
        "blockers": standing_blockers(profile, config),
        "latch": sdk.write_latch_open(),
    }


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

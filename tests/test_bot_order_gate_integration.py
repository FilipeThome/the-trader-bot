from __future__ import annotations

import hashlib
import json

from bot.exchange import sdk
from bot.exchange.execution.journal import append_event, new_event
from bot.exchange.execution.orchestrate import _request_fingerprint, place_spot_order
from bot.exchange.profiles import profile_by_id


def test_replayed_idempotency_key_does_not_send_again(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    (tmp_path / "binance.json").write_text(
        json.dumps(
            {
                "profile": "paper",
                "api_key": "k",
                "api_secret": "s",
                "testnet_audit_only": False,
                "testnet_max_order_notional": {"USDT": "1000"},
                "testnet_max_daily_operations": 5,
            }
        ),
        encoding="utf-8",
    )
    profile = profile_by_id("binance-paper-trade")
    config = sdk.BinanceConfig(profile="paper", api_key="k", api_secret="s")
    key = "same-key"
    client_order_id = hashlib.sha256(f"{profile.id}:{key}".encode()).hexdigest()[:32]
    fingerprint = _request_fingerprint("BTC/USDT", "buy", "market", "day", None, "25", None)
    state_key = f"order:{client_order_id}"
    common = {
        "profile_id": profile.id,
        "environment": "paper",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "notional": "25",
        "client_order_id": client_order_id,
        "request_fingerprint": fingerprint,
        "intent_id": key,
    }
    append_event(new_event("PREFLIGHT_ALLOWED", state_key, **common))
    append_event(new_event("SUBMISSION_PENDING", state_key, **common))
    append_event(new_event("ORDER_SUBMITTED", state_key, **common, exchange_order_id="99"))

    def boom(*_args, **_kwargs):
        raise AssertionError("exchange write on a replayed key")

    monkeypatch.setattr(sdk, "place_order", boom)
    result = place_spot_order(
        profile,
        config,
        symbol="BTC/USDT",
        side="buy",
        notional="25",
        idempotency_key=key,
    )
    assert result["status"] == "ok"
    assert result["order_id"] == "99"


def test_an_intent_without_quantity_has_no_implied_notional() -> None:
    from bot.live.enforcement import OrderIntent
    from bot.live.mandate.model import InstrumentType
    from bot.live.sdk_order_gate import _implied_notional

    class Quotes:
        @staticmethod
        def get_quote(symbol: str, config: object = None) -> dict[str, object]:
            return {"status": "ok", "quote": {"last": 100.0, "bid": 100.0, "ask": 100.0}}

    intent = OrderIntent(
        symbol="BTC-USDT", side="sell", notional_usd=None, quantity=None, instrument_type=InstrumentType.CRYPTO
    )
    # No quantity: sizing fails closed instead of raising on None * price.
    assert _implied_notional(intent, Quotes, None) is None

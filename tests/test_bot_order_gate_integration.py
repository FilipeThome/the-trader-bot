from __future__ import annotations

import hashlib
import json

from bot.exchange import sdk
from bot.exchange.execution.journal import append_event, new_event
from bot.exchange.execution.orchestrate import _request_fingerprint, place_spot_order
from bot.exchange.profiles import profile_by_id
from bot.config import BotConfig
from bot.runtime import Runtime
from bot.store import Store


def test_halt_cancels_open_orders_in_one_tick(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    cancelled: list[str] = []

    def cancel(order_id, profile_id=None, **kwargs):
        cancelled.append(order_id)
        return {"status": "ok", "request_sent": True}

    monkeypatch.setattr("bot.exchange.service.cancel_order", cancel)
    monkeypatch.setattr("bot.executor.service.cancel_order", cancel)
    config = BotConfig()
    store = Store(tmp_path / "testnet" / "bot.sqlite")
    store.upsert_position(
        {
            "symbol": "ETH/USDT",
            "strategy": "trend",
            "side": "buy",
            "qty": 0.2,
            "avg_price": 2000,
            "status": "open",
            "client_order_id": "order-1",
        }
    )
    runtime = Runtime(config, store, collector=None)
    result = runtime.halt("drawdown")
    assert result["halted"] is True
    assert cancelled == ["order-1"]
    assert runtime.halted is True
    assert runtime.start()["status"] == "error"


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
    assert Store is not None

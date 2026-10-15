from __future__ import annotations

import json

import pytest

from bot.exchange import sdk
from bot.exchange.profiles import BINANCE_PROFILES, profile_by_id
from bot.exchange.service import cancel_order, place_order
from bot.mandate_io import commit_binance_mandate


def test_only_paper_and_the_bot_profile_declare_writes() -> None:
    writable = [profile.id for profile in BINANCE_PROFILES if not profile.readonly]
    assert writable == ["binance-paper-trade", "binance-live-trade-bot"]
    for profile_id in ("binance-live-trade", "binance-live-sdk-readonly"):
        profile = profile_by_id(profile_id)
        assert profile.readonly is True
        assert profile.environment == "live"
        assert not any(cap.startswith(("orders.place", "orders.cancel")) for cap in profile.capabilities)


@pytest.mark.parametrize("profile_id", ["binance-live-trade", "binance-live-sdk-readonly"])
def test_service_still_refuses_every_other_live_profile(profile_id) -> None:
    placed = place_order("BTC/USDT", profile_id, side="buy", notional=5)
    cancelled = cancel_order("123", profile_id, symbol="BTC/USDT")
    assert placed["reasons"] == ["production_writes_disabled"]
    assert cancelled["reasons"] == ["production_writes_disabled"]
    assert placed["request_sent"] is False


def test_forged_writable_legacy_profile_is_still_refused(monkeypatch) -> None:
    forged = profile_by_id("binance-live-trade")
    monkeypatch.setattr(
        "bot.exchange.service.profile_by_id",
        lambda _pid=None: type(forged)(**{**forged.__dict__, "readonly": False}),
    )
    placed = place_order("BTC/USDT", "binance-live-trade", side="buy", notional=5)
    assert placed["reasons"] == ["production_writes_disabled"]


def test_bot_profile_writes_only_with_latch_and_mandate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    (tmp_path / "binance.json").write_text(
        json.dumps({"profile": "live-trade-bot", "api_key": "prod-key", "api_secret": "prod-secret"}),
        encoding="utf-8",
    )
    writes: list[str] = []

    class Exchange:
        def load_markets(self):
            return {}

        def create_order(self, *_args, **_kwargs):
            writes.append("create_order")
            return {"id": "1", "symbol": "BTC/USDT", "side": "buy", "type": "market", "status": "open"}

        def cancel_order(self, *_args, **_kwargs):
            writes.append("cancel_order")
            return {"id": "1"}

    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: Exchange())
    monkeypatch.setattr(sdk, "_assert_host", lambda _cfg: None)
    monkeypatch.setattr(sdk, "_assert_client_hosts", lambda *_args: None)
    config = sdk.BinanceConfig(profile="live-trade-bot", api_key="prod-key", api_secret="prod-secret")

    with sdk.authorize_exchange_write():
        blocked = sdk.place_order(config, symbol="BTC/USDT", side="buy", notional="25", client_order_id="botorder1")
    assert blocked["request_sent"] is False
    assert blocked["reasons"] == ["mandate_required"]
    assert writes == []

    commit_binance_mandate(capital_usd=1000, max_order_notional_usd=20, max_total_exposure_usd=600)
    denied = sdk.place_order(config, symbol="BTC/USDT", side="buy", notional="25", client_order_id="botorder1")
    assert denied["request_sent"] is False
    assert writes == []

    with sdk.authorize_exchange_write():
        placed = sdk.place_order(config, symbol="BTC/USDT", side="buy", notional="25", client_order_id="botorder1")
    assert placed["status"] == "ok"
    assert writes == ["create_order"]

    legacy = sdk.BinanceConfig(profile="live", api_key="prod-key", api_secret="prod-secret")
    with sdk.authorize_exchange_write():
        refused = sdk.place_order(legacy, symbol="BTC/USDT", side="buy", notional="25", client_order_id="legacy1")
    assert refused["reasons"] == ["production_writes_disabled"]
    assert writes == ["create_order"]

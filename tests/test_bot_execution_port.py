"""Fixes to the first-party Binance write path (see ORIGIN.md)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from bot.exchange import sdk
from bot.exchange.execution import orchestrate
from bot.exchange.execution.rules import MarketRules, SizedOrder
from bot.exchange.profiles import profile_by_id
from bot.live import sdk_order_gate


def _rules() -> MarketRules:
    return MarketRules(
        symbol="BTC/USDT", price_tick=Decimal("0.01"), amount_tick=None, lot_step=Decimal("0.0001"),
        market_step=None, min_qty=None, max_qty=None, market_min_qty=None, market_max_qty=None,
        min_notional=Decimal("5"), max_notional=None, min_price=None, max_price=None,
        limit_ticks=(Decimal("0.01"),), taker_fee=Decimal("0.001"), active=True, spot=True,
    )


def test_a_testnet_stop_sell_reads_the_bid_at_the_final_check(monkeypatch) -> None:
    monkeypatch.setattr(orchestrate, "standing_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sdk, "paper_order_notional_limit", lambda _quote: Decimal("1000"))
    quoted: list[str] = []

    def get_quote(symbol: str, *, config: Any = None, **_: Any) -> dict[str, Any]:
        quoted.append(symbol)
        return {"status": "ok", "symbol": symbol, "quote": {"bid": 100.0, "time": str(int(time.time() * 1000))}}

    monkeypatch.setattr(sdk, "get_quote", get_quote)
    sized = SizedOrder(quantity=Decimal("0.5"), notional=None, limit_price=Decimal("97.51"))
    reasons = orchestrate._final_reasons(
        profile_by_id("binance-paper-trade"), object(), "BTC/USDT", sized, "stop_loss_limit", "sell", _rules()
    )
    assert reasons == ()
    assert quoted == ["BTC/USDT"]


def _bot_config() -> sdk.BinanceConfig:
    return sdk.BinanceConfig(profile="live-trade-bot", api_key="k", api_secret="s")


def test_a_production_order_outside_the_mandate_gate_is_refused() -> None:
    result = orchestrate.place_spot_order(
        profile_by_id("binance-live-trade-bot"), _bot_config(), symbol="BTC/USDT", side="buy", notional="25"
    )
    assert result["reasons"] == ["mandate_gate_required"]


def test_inside_the_gate_the_production_order_reaches_its_own_checks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))

    def unreadable(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("offline")

    for name in ("get_spot_market", "get_account_snapshot", "get_open_orders", "get_quote"):
        monkeypatch.setattr(sdk, name, unreadable)
    token = sdk_order_gate._GATE_ALLOWED.set("binance")
    try:
        result = orchestrate.place_spot_order(
            profile_by_id("binance-live-trade-bot"), _bot_config(), symbol="BTC/USDT", side="buy", notional="25"
        )
    finally:
        sdk_order_gate._GATE_ALLOWED.reset(token)
    assert result["reasons"] == ["read_failed"]
    assert sdk_order_gate.gate_allowed("binance") is False


@pytest.mark.parametrize("order_type", ["limit", "stop_loss_limit"])
def test_a_reused_key_must_repeat_the_time_in_force(order_type: str) -> None:
    def fingerprint(tif: str) -> str:
        return orchestrate._request_fingerprint("BTC/USDT", "sell", order_type, tif, "0.5", None, "97.51", "98")

    assert fingerprint("gtc") != fingerprint("fok")

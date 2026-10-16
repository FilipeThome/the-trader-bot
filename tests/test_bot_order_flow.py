"""The mandate gate in front of the journaled order path, with the bot's priced positions."""

from __future__ import annotations

from typing import Any

import pytest

from bot.exchange import sdk, service
from bot.mandate_io import commit_binance_mandate


@pytest.fixture
def placed(tmp_path, monkeypatch) -> list[dict[str, Any]]:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    commit_binance_mandate(capital_usd=1000, max_order_notional_usd=50, max_total_exposure_usd=600)
    sent: list[dict[str, Any]] = []

    def place_spot_order(profile, config, **kwargs):
        from bot.live.sdk_order_gate import gate_allowed

        sent.append({**kwargs, "inside_gate": gate_allowed("binance")})
        return {"status": "ok", "order_id": "1", "client_order_id": "c1"}

    monkeypatch.setattr("bot.exchange.execution.orchestrate.place_spot_order", place_spot_order)
    monkeypatch.setattr(sdk, "build_config", lambda *_args, **_kwargs: sdk.BinanceConfig(profile="paper"))
    monkeypatch.setattr(sdk, "get_account_snapshot", lambda _config=None: {"status": "ok", "balances": []})
    return sent


def _quote(price: float | None):
    def get_quote(symbol: str, *, config: Any = None, **_: Any) -> dict[str, Any]:
        if price is None:
            raise TimeoutError("quote")
        return {"status": "ok", "symbol": symbol, "quote": {"last": price, "bid": price}}

    return get_quote


def test_an_order_passes_the_gate_with_priced_bot_positions(placed, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "get_quote", _quote(100.0))
    held = [{"symbol": "ETH/USDT", "quantity": 2.0}]
    result = service.submit_order(
        "BTC/USDT", "binance-paper-trade", side="buy", notional="25", order_type="market",
        idempotency_key="k1", held=held,
    )
    assert result["status"] == "ok"
    assert len(placed) == 1 and placed[0]["notional"] == "25"
    assert placed[0]["inside_gate"] is True


def test_an_unpriced_position_makes_the_gate_deny(placed, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "get_quote", _quote(None))
    result = service.submit_order(
        "BTC/USDT", "binance-paper-trade", side="buy", notional="25", order_type="market",
        idempotency_key="k2", held=[{"symbol": "ETH/USDT", "quantity": 2.0}],
    )
    assert result["status"] == "blocked"
    assert placed == []


def test_bot_exposure_above_the_mandate_is_denied(placed, monkeypatch) -> None:
    monkeypatch.setattr(sdk, "get_quote", _quote(100.0))
    result = service.submit_order(
        "BTC/USDT", "binance-paper-trade", side="buy", notional="25", order_type="market",
        idempotency_key="k3", held=[{"symbol": "ETH/USDT", "quantity": 6.0}],
    )
    assert result["status"] == "blocked"
    assert "max_total_exposure_usd" in str(result.get("breach"))
    assert placed == []


def test_write_blocker_names_halt_missing_and_expired_mandate(tmp_path, monkeypatch) -> None:
    from bot.live.halt import trip_halt

    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    assert service.write_blocker() == "sem mandato válido"
    commit_binance_mandate(capital_usd=1000, max_order_notional_usd=50, max_total_exposure_usd=600)
    assert service.write_blocker() is None
    commit_binance_mandate(capital_usd=1000, max_order_notional_usd=50, max_total_exposure_usd=600, days=-1)
    assert service.write_blocker() == "mandato expirado"
    trip_halt("file", "teste", broker="binance")
    assert service.write_blocker() == "halt ativo"

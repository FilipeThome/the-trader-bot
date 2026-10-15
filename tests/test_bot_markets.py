from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from bot.api.app import app, ctx
from bot.config import BotConfig
from bot.markets import parse_exchange_info


def test_parse_keeps_only_spot_markets_binance_allows() -> None:
    rows = parse_exchange_info(
        {
            "symbols": [
                {"status": "TRADING", "baseAsset": "ETH", "quoteAsset": "BTC", "isSpotTradingAllowed": True},
                {"status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
                {"status": "BREAK", "baseAsset": "FOO", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
                {"status": "TRADING", "baseAsset": "BAR", "quoteAsset": "USDT", "isSpotTradingAllowed": False},
                {"status": "TRADING", "baseAsset": "", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
            ]
        }
    )
    assert [row["symbol"] for row in rows] == ["BTC/USDT", "ETH/BTC"]


def test_markets_route_serves_the_cached_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    BotConfig().save()
    catalog = [
        {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"},
        {"symbol": "SOL/USDT", "base": "SOL", "quote": "USDT"},
        {"symbol": "ADA/BTC", "base": "ADA", "quote": "BTC"},
    ]
    with TestClient(app) as client:
        assert ctx.runtime is not None
        ctx.runtime.collector.market_cache = (datetime.now(timezone.utc), catalog)

        def boom(url: str):
            raise AssertionError(url)

        ctx.runtime.collector._fetch = boom
        response = client.get("/bot/markets")
    assert response.status_code == 200
    assert response.json()["symbols"] == catalog

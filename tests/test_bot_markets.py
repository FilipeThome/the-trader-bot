from __future__ import annotations

from datetime import datetime, timezone

from bot.api.app import ctx
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


def test_markets_route_serves_the_cached_catalog(api_client) -> None:
    catalog = [
        {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"},
        {"symbol": "SOL/USDT", "base": "SOL", "quote": "USDT"},
        {"symbol": "ADA/BTC", "base": "ADA", "quote": "BTC"},
    ]
    assert ctx.runtime is not None
    ctx.runtime.collector.market_cache = (datetime.now(timezone.utc), catalog)

    def boom(url: str):
        raise AssertionError(url)

    ctx.runtime.collector._fetch = boom
    response = api_client.get("/bot/markets")
    assert response.status_code == 200
    assert response.json()["symbols"] == catalog


def test_trade_charts_read_the_order_venue_with_a_fitting_interval() -> None:
    from datetime import datetime, timedelta, timezone

    from bot.exchange.weight import reset_weight, weight_snapshot
    from bot.collector import Collector
    from bot.markets import TESTNET_SPOT, chart_interval, venue_klines

    start = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert chart_interval(start, start + timedelta(hours=3))[0] == "1m"
    assert chart_interval(start, start + timedelta(hours=30))[0] == "5m"
    assert chart_interval(start, start + timedelta(days=9))[0] == "15m"
    urls: list[str] = []

    def fetch(url: str):
        urls.append(url)
        return [[int(start.timestamp() * 1000), "1", "2", "0.5", "1.5", "9"]], {"X-MBX-USED-WEIGHT-1M": "42"}

    collector = Collector(fetch)
    reset_weight()
    interval, candles = venue_klines(collector, "BTC/USDT", "testnet", start=start, end=start + timedelta(hours=1))
    assert urls[-1].startswith(TESTNET_SPOT) and "interval=1m" in urls[-1] and "startTime=" in urls[-1]
    assert candles == [{"t": start.isoformat(), "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}] and interval == "1m"
    assert weight_snapshot() == {}
    venue_klines(collector, "BTC/USDT", "production", start=start, end=start + timedelta(hours=1))
    assert urls[-1].startswith("https://api.binance.com") and weight_snapshot()["spot"]["used"] == 42

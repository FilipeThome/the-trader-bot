from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from bot.api.app import app, ctx
from bot.config import BotConfig
from bot.recommend import _fill_with_movers, rank_setups, shortlist
from bot.strategies.base import Setup


def _setup(symbol: str, expected: float, *, strategy: str = "trend") -> Setup:
    return Setup(
        strategy_id=strategy,
        symbol=symbol,
        timeframe="1h",
        action="Comprar",
        direction=1,
        expected_return=expected,
        sigma=0.01,
        stop_pct=0.01,
        target_pct=expected,
        reason="teste",
        regime="trend_up" if strategy == "trend" else "range",
        confidence=1,
    )


def test_shortlist_keeps_liquid_usdt_pairs() -> None:
    usdt = {"BTCUSDT": "BTC/USDT", "ETHUSDT": "ETH/USDT", "TINYUSDT": "TINY/USDT"}
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "20000000", "priceChangePercent": "1"},
        {"symbol": "ETHUSDT", "quoteVolume": "9000000", "priceChangePercent": "-8"},
        {"symbol": "TINYUSDT", "quoteVolume": "1000", "priceChangePercent": "40"},
        {"symbol": "FOOUSDT", "quoteVolume": "8000000", "priceChangePercent": "3"},
    ]
    chosen = shortlist(tickers, usdt, min_volume=5_000_000, limit=5)
    assert chosen[0] == "BTC/USDT"
    assert "ETH/USDT" in chosen
    assert "TINY/USDT" not in chosen
    assert "FOO/USDT" not in chosen


def test_rank_keeps_five_positive_scores_one_per_pair() -> None:
    setups = [
        _setup("A/USDT", 0.05),
        _setup("A/USDT", 0.03, strategy="mean_reversion"),
        _setup("B/USDT", 0.04),
        _setup("C/USDT", 0.001),
        _setup("D/USDT", 0.02),
        _setup("E/USDT", 0.015),
        _setup("F/USDT", 0.012),
        _setup("G/USDT", 0.011),
    ]
    rows = rank_setups(setups)
    assert [row["symbol"] for row in rows] == ["A/USDT", "B/USDT", "D/USDT", "E/USDT", "F/USDT"]
    assert rows[0]["score"] >= rows[-1]["score"]
    assert len(rows) == 5


def test_fill_completes_five_with_day_gainers_only() -> None:
    rows = [{"symbol": "BTC/USDT", "strategy": "trend", "action": "Comprar", "score": 1.0, "net_return": 0.02, "status": "pronta", "reason": "setup"}]
    usdt = {"BTCUSDT": "BTC/USDT", "ETHUSDT": "ETH/USDT", "SOLUSDT": "SOL/USDT", "BNBUSDT": "BNB/USDT", "XRPUSDT": "XRP/USDT", "ADAUSDT": "ADA/USDT"}
    tickers = [
        {"symbol": "ETHUSDT", "quoteVolume": "80000000", "priceChangePercent": "6"},
        {"symbol": "SOLUSDT", "quoteVolume": "60000000", "priceChangePercent": "-5"},
        {"symbol": "BNBUSDT", "quoteVolume": "40000000", "priceChangePercent": "4"},
        {"symbol": "XRPUSDT", "quoteVolume": "30000000", "priceChangePercent": "3"},
        {"symbol": "ADAUSDT", "quoteVolume": "20000000", "priceChangePercent": "2"},
    ]
    filled = _fill_with_movers(rows, tickers, usdt, min_volume=5_000_000)
    assert len(filled) == 5
    assert filled[0]["symbol"] == "BTC/USDT"
    assert [row["symbol"] for row in filled[1:]] == ["ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT"]
    assert all(float(row["net_return"]) > 0 for row in filled)
    assert "SOL/USDT" not in {row["symbol"] for row in filled}


def test_recommendations_route_uses_the_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    BotConfig().save()
    cached = [{"symbol": "ETH/USDT", "strategy": "trend", "action": "Comprar", "score": 1.2, "net_return": 0.02, "status": "pronta", "reason": "cache"}]
    with TestClient(app) as client:
        assert ctx.runtime is not None
        ctx.runtime.collector.recommendation_cache = (datetime.now(timezone.utc), cached)

        def boom(url: str):
            raise AssertionError(url)

        ctx.runtime.collector._fetch = boom
        response = client.get("/bot/recommendations")
    assert response.status_code == 200
    assert response.json()["rows"] == cached

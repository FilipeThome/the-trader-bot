from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from bot.api.app import ctx
from bot.collector import Series
from bot.features import build_features


def _frame(periods: int, freq: str) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    index = pd.date_range(end=end, periods=periods, freq=freq, tz="UTC")
    close = [100 + 5 * math.sin(i / 3) + (i % 7) * 0.4 for i in range(periods)]
    return pd.DataFrame(
        {
            "open": [price - 0.2 for price in close],
            "high": [price + 1 for price in close],
            "low": [price - 1 for price in close],
            "close": close,
            "volume": [10 + (i % 5) for i in range(periods)],
        },
        index=index,
    )


def test_feature_payload_matches_strategy_inputs() -> None:
    snapshot_frame = _frame(80, "h")
    mean_frame = _frame(80, "15min")
    from bot.collector import MarketSnapshot

    now = datetime.now(timezone.utc)
    snapshot = MarketSnapshot(
        candles={
            ("BTC/USDT", "1h"): Series(snapshot_frame, now, "test"),
            ("BTC/USDT", "15m"): Series(mean_frame, now, "test"),
        },
        funding={"BTC/USDT": [0.2, 0.21, 0.22]},
        funding_points={"BTC/USDT": [{"t": now.isoformat(), "v": 0.22}]},
    )
    body = build_features(snapshot, "BTC/USDT", funding_threshold=0.1)
    assert body["trend"]["interval"] == "1h"
    assert len(body["trend"]["candles"]) == 80
    assert len(body["trend"]["ema50"]) == 80
    assert any(value is not None for value in body["trend"]["breakout_high"])
    assert any(value is not None for value in body["trend"]["adx"])
    assert any(value is not None for value in body["mean_reversion"]["rsi"])
    assert any(value is not None for value in body["mean_reversion"]["upper"])
    assert body["funding_carry"]["threshold"] == 0.1
    assert body["funding_carry"]["latest"] == 0.22
    flat = str(body)
    assert "NaN" not in flat


def test_features_route_uses_the_cache(api_client) -> None:
    assert ctx.runtime is not None
    now = datetime.now(timezone.utc)
    collector = ctx.runtime.collector
    collector.snapshot.candles[("BTC/USDT", "1h")] = Series(_frame(80, "h"), now, "test")
    collector.snapshot.candles[("BTC/USDT", "15m")] = Series(_frame(80, "15min"), now, "test")
    collector.snapshot.funding_points["BTC/USDT"] = [{"t": now.isoformat(), "v": 0.11}]
    collector.snapshot.funding["BTC/USDT"] = [0.11]

    def boom(url: str):
        raise AssertionError(url)

    collector._fetch = boom
    ok = api_client.get("/bot/features", params={"symbol": "BTC/USDT"})
    missing = api_client.get("/bot/features", params={"symbol": "DOGE/USDT"})
    assert ok.status_code == 200
    assert ok.json()["symbol"] == "BTC/USDT"
    assert missing.status_code == 404

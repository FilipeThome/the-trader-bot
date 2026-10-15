from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from bot.collector import Collector
from bot.exchange.weight import reset_weight, weight_pressure


def _klines(open_ms: int) -> list:
    return [[open_ms, "100", "101", "99", "100.5", "10"]]


def test_stale_series_and_rest_fallback_when_the_stream_is_silent() -> None:
    reset_weight()
    now = datetime.now(timezone.utc)
    old = int((now - timedelta(hours=3)).timestamp() * 1000)

    def fetch(url: str):
        if url.endswith("/api/v3/time"):
            return {"serverTime": int((time.time() + 5) * 1000)}, {}
        if "klines" in url and "interval=1h" in url:
            return _klines(old), {"X-MBX-USED-WEIGHT-1M": "5000"}
        if "klines" in url:
            return _klines(int(now.timestamp() * 1000)), {}
        if "bookTicker" in url:
            return {"bidPrice": "100", "askPrice": "100.1", "bidQty": "2", "askQty": "2"}, {}
        if "fundingRate" in url:
            return [{"fundingRate": "0.0001"}, {"fundingRate": "0.0001"}, {"fundingRate": "0.0001"}], {}
        if "openInterest" in url:
            return {"openInterest": "10"}, {}
        raise AssertionError(url)

    collector = Collector(fetch)
    assert collector.stream_silent(now=now) is True
    assert collector.using_rest_fallback is True
    collector.refresh(["BTC/USDT"], intervals=("1h",))
    assert collector.snapshot.stale("BTC/USDT", "1h", now=now) is True
    assert weight_pressure() is True
    assert collector.sync_clock() is False
    collector.note_stream_message(now)
    assert collector.stream_silent(now=now) is False

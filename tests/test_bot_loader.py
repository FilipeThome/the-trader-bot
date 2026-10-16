from __future__ import annotations

import urllib.parse
from typing import Any

import pandas as pd
import pytest

from bot.backtest.loaders.binance_loader import BinanceLoader, WeightPressure
from bot.exchange.weight import note_weight, reset_weight


def _rows(start_ms: int, count: int) -> list[list[Any]]:
    return [[start_ms + i * 3_600_000, "1", "1", "1", "1", "1"] for i in range(count)]


def test_history_is_read_page_by_page_from_start() -> None:
    reset_weight()
    seen: list[dict[str, str]] = []
    start = pd.Timestamp("2026-01-01", tz="UTC")
    start_ms = int(start.timestamp() * 1000)

    def fetch(url: str):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen.append(query)
        cursor = int(query["startTime"])
        count = 1000 if len(seen) == 1 else 5
        return _rows(cursor, count), {"X-MBX-USED-WEIGHT-1M": "10"}

    progress: list[tuple[str, int]] = []
    frames = BinanceLoader(fetch).fetch(
        ["BTC/USDT"], start.isoformat(), (start + pd.Timedelta(days=60)).isoformat(), interval="1h",
        on_symbol=lambda symbol, done: progress.append((symbol, done)),
    )
    assert len(frames["BTC/USDT"]) == 1005
    assert int(seen[0]["startTime"]) == start_ms
    assert int(seen[1]["startTime"]) == start_ms + 999 * 3_600_000 + 1
    assert seen[0]["symbol"] == "BTCUSDT" and seen[0]["interval"] == "1h"
    assert progress == [("BTC/USDT", 1)]


def test_history_reads_stop_under_weight_pressure() -> None:
    reset_weight()
    note_weight(5500)

    def fetch(url: str):
        raise AssertionError("no request under pressure")

    with pytest.raises(WeightPressure):
        BinanceLoader(fetch).fetch(["BTC/USDT"], "2026-01-01", "2026-02-01", interval="1h")

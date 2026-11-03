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


def test_the_open_last_candle_and_duplicates_are_dropped(caplog) -> None:
    reset_weight()
    start = pd.Timestamp("2026-01-01", tz="UTC")
    start_ms = int(start.timestamp() * 1000)
    end = start + pd.Timedelta(hours=10)
    # Hours 0..10 with hour 6 missing; hour 10 is still forming at the end, and hour 3 comes twice.
    rows = [row for row in _rows(start_ms, 11) if row[0] != start_ms + 6 * 3_600_000]
    rows.append(_rows(start_ms + 3 * 3_600_000, 1)[0])

    def fetch(url: str):
        return rows, {"X-MBX-USED-WEIGHT-1M": "10"}

    with caplog.at_level("WARNING", logger="bot.backtest.loaders.binance_loader"):
        frame = BinanceLoader(fetch).fetch(["BTC/USDT"], start.isoformat(), end.isoformat(), interval="1h")["BTC/USDT"]
    assert list(frame.index) == [start + pd.Timedelta(hours=hour) for hour in (0, 1, 2, 3, 4, 5, 7, 8, 9)]
    assert frame.index.is_unique and frame.index.is_monotonic_increasing
    assert "1 of 10 candles missing" in caplog.text

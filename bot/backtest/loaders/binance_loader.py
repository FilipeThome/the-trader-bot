"""Public Binance OHLCV. No API key. Ported in role from binance_loader.py."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bot.backtest.loaders.base import DataLoader
from bot.backtest.loaders.registry import register
from bot.collector import _http_fetch


@register
class BinanceLoader(DataLoader):
    name = "binance"
    markets = {"crypto"}
    requires_auth = False

    def fetch(
        self,
        symbols: list[str],
        start: str,
        end: str,
        interval: str = "1d",
    ) -> dict[str, pd.DataFrame]:
        mapped = {"1D": "1d", "1d": "1d", "1h": "1h", "4h": "4h", "15m": "15m", "1m": "1m"}
        binance_interval = mapped.get(interval, "1d")
        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            pair = symbol.replace("/", "").replace("-", "")
            if not pair.endswith("USDT"):
                pair = f"{pair}USDT"
            url = (
                "https://api.binance.com/api/v3/klines"
                f"?symbol={pair}&interval={binance_interval}&limit=1000"
            )
            payload: Any
            payload, _headers = _http_fetch(url)
            rows = []
            index = []
            for item in payload:
                stamp = pd.Timestamp(item[0], unit="ms", tz="UTC")
                if stamp < pd.Timestamp(start, tz="UTC") or stamp > pd.Timestamp(end, tz="UTC"):
                    continue
                index.append(stamp)
                rows.append(
                    {
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                    }
                )
            frames[symbol] = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
        return frames

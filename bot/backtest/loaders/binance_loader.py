"""Public Binance OHLCV. No API key. Ported in role from binance_loader.py."""

from __future__ import annotations

import urllib.parse
from datetime import timezone
from typing import Any, Callable

import pandas as pd

from bot.backtest.loaders.base import DataLoader
from bot.backtest.loaders.registry import register
from bot.collector import _absorb_weight, _http_fetch
from bot.exchange.weight import weight_pressure

#: Binance returns at most this many candles per klines request (weight 2 each).
_PAGE = 1000
_INTERVALS = {"1D": "1d", "1d": "1d", "4h": "4h", "1h": "1h", "15m": "15m", "1m": "1m"}


class WeightPressure(RuntimeError):
    """The REST weight budget is under pressure; history reads stop."""


@register
class BinanceLoader(DataLoader):
    name = "binance"
    markets = {"crypto"}
    requires_auth = False

    def __init__(self, fetch: Callable[[str], tuple[Any, dict[str, str]]] = _http_fetch) -> None:
        self._fetch = fetch

    def fetch(
        self,
        symbols: list[str],
        start: str,
        end: str,
        interval: str = "1d",
        *,
        on_symbol: Callable[[str, int], None] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Candles from ``start`` to ``end`` (ISO dates or datetimes, UTC), page by page.

        Every page reports its weight; when the budget is under pressure the
        read stops with ``WeightPressure`` instead of pushing the account into a ban.
        """
        binance_interval = _INTERVALS.get(interval, "1d")
        start_ms = _ms(start)
        end_ms = _ms(end)
        frames: dict[str, pd.DataFrame] = {}
        for done, symbol in enumerate(symbols, start=1):
            pair = symbol.replace("/", "").replace("-", "")
            if not pair.endswith("USDT"):
                pair = f"{pair}USDT"
            rows: list[dict[str, float]] = []
            index: list[pd.Timestamp] = []
            cursor = start_ms
            while cursor < end_ms:
                if weight_pressure():
                    raise WeightPressure("peso da API da Binance acima de 80%; tente de novo em um minuto")
                query = urllib.parse.urlencode(
                    {"symbol": pair, "interval": binance_interval, "startTime": cursor, "endTime": end_ms, "limit": _PAGE}
                )
                payload, headers = self._fetch(f"https://api.binance.com/api/v3/klines?{query}")
                _absorb_weight(headers)
                if not payload:
                    break
                for item in payload:
                    index.append(pd.Timestamp(item[0], unit="ms", tz="UTC"))
                    rows.append(
                        {
                            "open": float(item[1]),
                            "high": float(item[2]),
                            "low": float(item[3]),
                            "close": float(item[4]),
                            "volume": float(item[5]),
                        }
                    )
                if len(payload) < _PAGE:
                    break
                cursor = int(payload[-1][0]) + 1
            frames[symbol] = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
            if on_symbol is not None:
                on_symbol(symbol, done)
        return frames


def _ms(value: str) -> int:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(timezone.utc)
    return int(stamp.timestamp() * 1000)

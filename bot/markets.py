"""Spot symbols Binance currently allows to trade."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.collector import SPOT, Collector
from bot.exchange.weight import note_weight

_QUOTE_RANK = {"USDT": 0, "USDC": 1, "FDUSD": 2, "BTC": 3, "ETH": 4, "BNB": 5}
_MAX_AGE_SECONDS = 3600


def parse_exchange_info(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Keep spot symbols whose status is TRADING and that Binance allows to trade."""
    rows: list[dict[str, str]] = []
    for item in payload.get("symbols") or []:
        if item.get("status") != "TRADING" or item.get("isSpotTradingAllowed") is False:
            continue
        base = str(item.get("baseAsset") or "")
        quote = str(item.get("quoteAsset") or "")
        if not base or not quote:
            continue
        rows.append({"symbol": f"{base}/{quote}", "base": base, "quote": quote})
    rows.sort(key=lambda row: (_QUOTE_RANK.get(row["quote"], 9), row["symbol"]))
    return rows


def tradable_symbols(collector: Collector) -> list[dict[str, str]]:
    """Return the cached catalog, refreshing from exchangeInfo at most once an hour."""
    now = datetime.now(timezone.utc)
    cached = getattr(collector, "market_cache", None)
    if cached is not None and (now - cached[0]).total_seconds() < _MAX_AGE_SECONDS:
        return cached[1]
    payload, headers = collector._fetch(f"{SPOT}/api/v3/exchangeInfo")
    raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
    if raw is not None and str(raw).isdigit():
        note_weight(int(raw))
    rows = parse_exchange_info(payload)
    collector.market_cache = (now, rows)
    return rows

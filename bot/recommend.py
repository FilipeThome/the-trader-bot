"""Top liquid Binance pairs by the same score the bot uses to judge a trade."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.collector import SPOT, Collector
from bot.evaluator import classify
from bot.exchange.weight import note_weight, weight_pressure
from bot.markets import tradable_symbols
from bot.strategies import STRATEGIES
from bot.strategies.base import Setup

_MAX_AGE_SECONDS = 60
_SCAN_LIMIT = 12


def shortlist(tickers: list[dict[str, Any]], usdt: dict[str, str], *, min_volume: float, limit: int = _SCAN_LIMIT) -> list[str]:
    """Liquid USDT pairs, busiest first, plus the most stretched so both strategies get a look."""
    liquid: list[tuple[str, float, float]] = []
    for item in tickers:
        pair = usdt.get(str(item.get("symbol") or ""))
        if pair is None:
            continue
        try:
            volume = float(item.get("quoteVolume") or 0)
            change = float(item.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if volume < min_volume:
            continue
        liquid.append((pair, volume, change))
    busiest = sorted(liquid, key=lambda row: row[1], reverse=True)
    stretched = sorted(liquid, key=lambda row: abs(row[2]), reverse=True)
    chosen: list[str] = []
    for bucket in (busiest[:8], stretched[:6]):
        for pair, _volume, _change in bucket:
            if pair not in chosen:
                chosen.append(pair)
            if len(chosen) >= limit:
                return chosen
    return chosen


def rank_setups(setups: list[Setup]) -> list[dict[str, Any]]:
    """Keep the best positive net return per pair, at most five."""
    ranked = []
    for setup in setups:
        item = classify(setup)
        if item.score <= 0 or item.net_return <= 0:
            continue
        ranked.append(
            {
                "symbol": item.symbol,
                "strategy": item.strategy,
                "action": item.action,
                "score": item.score,
                "net_return": round(item.net_return, 6),
                "status": item.status,
                "reason": item.reason,
            }
        )
    ranked.sort(key=lambda row: (float(row["score"]), float(row["net_return"])), reverse=True)
    best: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ranked:
        if row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        best.append(row)
        if len(best) == 5:
            break
    return best


def recommendations(collector: Collector, *, min_volume: float) -> list[dict[str, Any]]:
    """Scan a small liquid slice at most once a minute."""
    now = datetime.now(timezone.utc)
    cached = getattr(collector, "recommendation_cache", None)
    if cached is not None and (now - cached[0]).total_seconds() < _MAX_AGE_SECONDS:
        return cached[1]
    if weight_pressure():
        return cached[1] if cached is not None else []
    catalog = tradable_symbols(collector)
    usdt = {f"{row['base']}{row['quote']}": row["symbol"] for row in catalog if row["quote"] == "USDT"}
    payload, headers = collector._fetch(f"{SPOT}/api/v3/ticker/24hr")
    raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
    if raw is not None and str(raw).isdigit():
        note_weight(int(raw))
    tickers = payload if isinstance(payload, list) else []
    chosen = shortlist(tickers, usdt, min_volume=min_volume)
    rows = rank_setups(_setups(collector, chosen))
    if len(rows) < 5:
        rows = _fill_with_movers(rows, tickers, usdt, min_volume=min_volume)
        rows.sort(key=lambda row: (float(row["score"]), float(row["net_return"])), reverse=True)
        rows = rows[:5]
    collector.recommendation_cache = (now, rows)
    return rows


def _fill_with_movers(
    rows: list[dict[str, Any]],
    tickers: list[dict[str, Any]],
    usdt: dict[str, str],
    *,
    min_volume: float,
) -> list[dict[str, Any]]:
    """Complete the top 5 with liquid USDT pairs that rose today. Losers stay out."""
    seen = {row["symbol"] for row in rows}
    movers: list[tuple[float, dict[str, Any]]] = []
    for item in tickers:
        pair = usdt.get(str(item.get("symbol") or ""))
        if pair is None or pair in seen:
            continue
        try:
            volume = float(item.get("quoteVolume") or 0)
            change = float(item.get("priceChangePercent") or 0) / 100
        except (TypeError, ValueError):
            continue
        if volume < min_volume or change <= 0:
            continue
        score = change * min(volume / 50_000_000, 3.0)
        movers.append(
            (
                score,
                {
                    "symbol": pair,
                    "strategy": "trend" if change >= 0.03 else "mean_reversion",
                    "action": "Comprar",
                    "score": round(score, 4),
                    "net_return": round(change, 6),
                    "status": "potencial",
                    "reason": f"Alta de {change:+.1%} no dia com volume alto",
                },
            )
        )
    movers.sort(key=lambda item: item[0], reverse=True)
    filled = list(rows)
    for _score, row in movers:
        filled.append(row)
        if len(filled) == 5:
            break
    return filled


def _setups(collector: Collector, symbols: list[str]) -> list[Setup]:
    trend = STRATEGIES["trend"]
    mean = STRATEGIES["mean_reversion"]
    found: list[Setup] = []
    for symbol in symbols:
        if weight_pressure():
            break
        try:
            collector._load_klines(symbol, "1h")
            collector._load_klines(symbol, "15m")
        except Exception:
            continue
        hourly = collector.snapshot.candles.get((symbol, "1h"))
        quarter = collector.snapshot.candles.get((symbol, "15m"))
        if hourly is not None and not hourly.frame.empty:
            setup = trend.evaluate(symbol, "1h", hourly.frame)
            if setup is not None:
                found.append(setup)
            setup = mean.evaluate(symbol, "1h", hourly.frame)
            if setup is not None:
                found.append(setup)
        if quarter is not None and not quarter.frame.empty:
            setup = mean.evaluate(symbol, "15m", quarter.frame)
            if setup is not None:
                found.append(setup)
    return found

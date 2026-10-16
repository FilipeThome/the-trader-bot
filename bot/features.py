"""Series the live strategies actually read, shaped for the panel charts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from bot.collector import Collector, MarketSnapshot
from bot.exchange.weight import weight_pressure
from bot.indicators import adx, bollinger, ema, rsi

_CHART_BARS = 120
_MAX_AGE_SECONDS = 8


def ensure_chart_data(collector: Collector, symbol: str) -> None:
    """Refresh the two chart intervals when the cache is older than a few seconds."""
    if weight_pressure():
        return
    now = datetime.now(timezone.utc)
    refreshed = False
    for interval in ("15m", "1h"):
        series = collector.snapshot.candles.get((symbol, interval))
        age = None if series is None else (now - series.fetched_at).total_seconds()
        if series is None or series.frame.empty or age is None or age > _MAX_AGE_SECONDS:
            collector._load_klines(symbol, interval)
            refreshed = True
    if refreshed or symbol not in collector.snapshot.funding_points:
        collector._load_funding(symbol)


def build_features(snapshot: MarketSnapshot, symbol: str, *, funding_threshold: float = 0.10) -> dict[str, Any]:
    trend = snapshot.candles[(symbol, "1h")].frame
    mean = snapshot.candles[(symbol, "15m")].frame
    latest = snapshot.funding.get(symbol) or []
    return {
        "symbol": symbol,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "trend": _trend_block(trend),
        "mean_reversion": _mean_block(mean),
        "funding_carry": {
            "threshold": funding_threshold,
            "points": list(snapshot.funding_points.get(symbol) or []),
            "latest": None if not latest else float(latest[-1]),
        },
    }


def _trend_block(frame: pd.DataFrame) -> dict[str, Any]:
    average_volume = frame["volume"].shift(1).rolling(20).mean()
    return {
        "interval": "1h",
        **_price_block(frame),
        "ema50": _aligned(ema(frame["close"], 50), frame),
        "breakout_high": _aligned(frame["high"].shift(1).rolling(20).max(), frame),
        "breakout_low": _aligned(frame["low"].shift(1).rolling(20).min(), frame),
        "volume": _aligned(frame["volume"], frame),
        "volume_avg": _aligned(average_volume, frame),
        "adx": _aligned(adx(frame), frame),
    }


def _mean_block(frame: pd.DataFrame) -> dict[str, Any]:
    mid, upper, lower = bollinger(frame["close"])
    return {
        "interval": "15m",
        **_price_block(frame),
        "mid": _aligned(mid, frame),
        "upper": _aligned(upper, frame),
        "lower": _aligned(lower, frame),
        "rsi": _aligned(rsi(frame["close"]), frame),
    }


def _price_block(frame: pd.DataFrame) -> dict[str, Any]:
    tail = frame.tail(_CHART_BARS)
    times: list[str] = []
    candles: list[list[float]] = []
    for stamp, row in tail.iterrows():
        times.append(_iso(stamp))
        candles.append(
            [
                round(float(row["open"]), 8),
                round(float(row["close"]), 8),
                round(float(row["low"]), 8),
                round(float(row["high"]), 8),
            ]
        )
    return {"t": times, "candles": candles}


def _aligned(series: pd.Series, frame: pd.DataFrame) -> list[float | None]:
    tail_index = frame.tail(_CHART_BARS).index
    values = series.reindex(tail_index)
    out: list[float | None] = []
    for value in values:
        if pd.isna(value):
            out.append(None)
        else:
            out.append(round(float(value), 8))
    return out


def _iso(stamp: Any) -> str:
    moment = pd.Timestamp(stamp)
    if moment.tzinfo is None:
        moment = moment.tz_localize(timezone.utc)
    return str(moment.isoformat())

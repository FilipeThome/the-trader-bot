"""Small Alpha Zoo subset. Each alpha returns a vote in [-1, 1]."""

from __future__ import annotations

import pandas as pd

from bot.indicators import bollinger, rsi


def momentum_20(frame: pd.DataFrame) -> float:
    close = frame["close"]
    if len(close) < 21:
        return 0.0
    change = float(close.iloc[-1] / close.iloc[-21] - 1)
    return max(-1.0, min(1.0, change / 0.05))


def reversal_rsi(frame: pd.DataFrame) -> float:
    value = rsi(frame["close"])
    if value.dropna().empty:
        return 0.0
    last = float(value.iloc[-1])
    if last < 30:
        return (30 - last) / 30
    if last > 70:
        return -((last - 70) / 30)
    return 0.0


def volume_spike(frame: pd.DataFrame) -> float:
    volume = frame["volume"]
    if len(volume) < 21:
        return 0.0
    avg = float(volume.iloc[-21:-1].mean() or 0)
    if avg <= 0:
        return 0.0
    ratio = float(volume.iloc[-1] / avg)
    direction = 1.0 if frame["close"].iloc[-1] >= frame["open"].iloc[-1] else -1.0
    if ratio < 1:
        return 0.0
    return max(-1.0, min(1.0, direction * min(ratio / 2, 1)))


def bollinger_zscore(frame: pd.DataFrame) -> float:
    mid, upper, lower = bollinger(frame["close"])
    if pd.isna(mid.iloc[-1]) or float(upper.iloc[-1]) == float(lower.iloc[-1]):
        return 0.0
    span = float(upper.iloc[-1] - lower.iloc[-1])
    z = (float(frame["close"].iloc[-1]) - float(mid.iloc[-1])) / (span / 2)
    return max(-1.0, min(1.0, -z))


ALPHAS = {
    "momentum_20": momentum_20,
    "reversal_rsi": reversal_rsi,
    "volume_spike": volume_spike,
    "bollinger_zscore": bollinger_zscore,
}


def votes(frame: pd.DataFrame) -> dict[str, float]:
    return {name: float(fn(frame)) for name, fn in ALPHAS.items()}


def agreement(direction: float, frame: pd.DataFrame) -> tuple[float, dict[str, float]]:
    """Share of alphas that vote with ``direction``. Returns confidence in [0, 1]."""
    cast = votes(frame)
    if direction == 0 or not cast:
        return 0.0, cast
    sign = 1 if direction > 0 else -1
    aligned = sum(1 for value in cast.values() if value * sign > 0.05)
    return aligned / len(cast), cast

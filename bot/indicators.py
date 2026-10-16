"""Indicators shared by the live signal engine and the backtest."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["high"]
    low = frame["low"]
    prev = frame["close"].shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["high"]
    low = frame["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    true_range = atr(frame, period)
    plus_di = 100 * (plus_dm.rolling(period).mean() / true_range.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).mean() / true_range.replace(0, np.nan))
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.rolling(period).mean()


def bollinger(close: pd.Series, period: int = 20, width: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return mid, mid + width * sigma, mid - width * sigma


def regime(frame: pd.DataFrame, strength: pd.Series | None = None) -> str:
    """Return ``trend_up``, ``trend_down`` or ``range`` from the last closed bar.

    ``strength`` is ``adx(frame)`` when the caller already computed it.
    """
    if len(frame) < 60:
        return "range"
    if strength is None:
        strength = adx(frame)
    slow = ema(frame["close"], 50)
    last_adx = float(strength.iloc[-1]) if pd.notna(strength.iloc[-1]) else 0.0
    if last_adx < 25:
        return "range"
    if float(frame["close"].iloc[-1]) >= float(slow.iloc[-1]):
        return "trend_up"
    return "trend_down"


def session_vwap(frame: pd.DataFrame, min_bars: int = 4) -> pd.Series | None:
    """VWAP anchored at 00:00 UTC of each bar's day: typical price times volume.

    Returns the series for every bar, or None when the last bar's day has
    fewer than ``min_bars`` bars (right after midnight the VWAP is one bar).
    The index must be timezone-aware.
    """
    if frame.empty:
        return None
    day = frame.index.normalize()
    if int((day == day[-1]).sum()) < min_bars:
        return None
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    weighted = (typical * frame["volume"]).groupby(day).cumsum()
    volume = frame["volume"].groupby(day).cumsum()
    return weighted / volume.replace(0, np.nan)

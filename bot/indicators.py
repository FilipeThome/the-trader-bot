"""Indicators shared by the live signal engine and the backtest.

ATR, ADX and RSI average with a simple moving average, not Wilder's smoothing.
That is a decision: live and replay compute the same numbers, the pinned
strategy digests stay valid and so does every stored validation. Wilder's
smoothing would change all three for no evidence that it trades better.
"""

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


#: 4h bars in a UTC day.
DAY_BARS_4H = 6


def atr_pct_daily(frame_4h: pd.DataFrame, period: int = 14) -> float | None:
    """ATR of UTC days as a share of the last daily close, from closed 4h bars.

    The bars are resampled to whole UTC days (open first, high max, low min,
    close last); a last day with fewer than six bars is still forming and is
    dropped. Returns None without ``period + 1`` whole days or a usable close.
    """
    if frame_4h is None or frame_4h.empty:
        return None
    grouped = frame_4h.groupby(frame_4h.index.floor("D"))
    daily = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    if int(grouped.size().iloc[-1]) < DAY_BARS_4H:
        daily = daily.iloc[:-1]
    if len(daily) < period + 1:
        return None
    value = atr(daily, period).iloc[-1]
    close = float(daily["close"].iloc[-1])
    if pd.isna(value) or close <= 0:
        return None
    return float(value) / close


#: EMA length of the market filter: 240 bars of 4h are 40 days. The 300-candle
#: collect cannot warm a 50-day EMA of daily closes.
MARKET_EMA_BARS = 240


def btc_trend_ok(closes_4h: pd.Series | np.ndarray | list[float]) -> bool:
    """The market filter: the last closed BTC 4h close at or above its EMA of 240 bars.

    With fewer than 240 closed bars there is no 40-day trend to judge, so the
    filter lets entries through (a young history must not block every pair).
    """
    closes = pd.Series(np.asarray(closes_4h, dtype=float))
    if len(closes) < MARKET_EMA_BARS:
        return True
    return bool(closes.iloc[-1] >= ema(closes, MARKET_EMA_BARS).iloc[-1])


def btc_trend_ok_series(closes_4h: pd.Series | np.ndarray, window: int) -> np.ndarray:
    """``btc_trend_ok`` of the last ``window`` closes at every bar, in one pass.

    Entry ``i`` equals ``btc_trend_ok(closes[max(0, i + 1 - window) : i + 1])``.
    An EMA without adjustment that starts at ``x[s]`` differs from the one that
    starts at ``x[0]`` by ``(1 - a)^(t - s) * (E[s] - x[s])``, so each window's
    EMA is the full EMA minus that decayed term.
    """
    values = np.asarray(closes_4h, dtype=float)
    count = len(values)
    flags = np.ones(count, dtype=bool)
    if count < MARKET_EMA_BARS or window < MARKET_EMA_BARS:
        return flags
    full = ema(pd.Series(values), MARKET_EMA_BARS).to_numpy()
    decay = 1 - 2 / (MARKET_EMA_BARS + 1)
    windowed = full.copy()
    if count > window:
        starts = np.arange(1, count - window + 1)
        windowed[window:] = full[window:] - decay ** (window - 1) * (full[starts] - values[starts])
    judged = np.arange(count) >= MARKET_EMA_BARS - 1
    flags[judged] = values[judged] >= windowed[judged]
    return flags

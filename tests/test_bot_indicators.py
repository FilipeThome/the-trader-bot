from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot import indicators
from bot.indicators import MARKET_EMA_BARS, atr_pct_daily, btc_trend_ok, btc_trend_ok_series


def _four_hour(days: int, *, extra_bars: int = 0, spread: float = 1.0) -> pd.DataFrame:
    bars = days * 6 + extra_bars
    index = pd.date_range("2026-01-01", periods=bars, freq="4h", tz="UTC")
    close = np.full(bars, 100.0)
    return pd.DataFrame({"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1.0}, index=index)


def test_daily_atr_resamples_closed_4h_bars_to_utc_days() -> None:
    # Each day ranges 99-101 around a 100 close: the daily true range is 2, so 2% of the close.
    assert atr_pct_daily(_four_hour(20)) == pytest.approx(0.02)
    # A forming day (two bars) with a wide range is dropped, not read as a whole day.
    forming = _four_hour(20, extra_bars=2)
    forming.iloc[-2:, forming.columns.get_loc("high")] = 150.0
    assert atr_pct_daily(forming) == pytest.approx(0.02)
    assert atr_pct_daily(_four_hour(14)) is None
    assert atr_pct_daily(_four_hour(15)) == pytest.approx(0.02)
    assert atr_pct_daily(_four_hour(0)) is None


def test_the_daily_atr_is_wider_than_the_4h_atr_on_a_trending_day() -> None:
    frame = _four_hour(20)
    steps = np.arange(len(frame), dtype=float)
    frame["close"] = 100 + steps * 0.5
    frame["open"] = frame["close"] - 0.5
    frame["high"] = frame["close"] + 0.2
    frame["low"] = frame["open"] - 0.2
    daily = atr_pct_daily(frame)
    four_hour = float(indicators.atr(frame).iloc[-1]) / float(frame["close"].iloc[-1])
    assert daily is not None and daily > 3 * four_hour


def test_btc_trend_ok_needs_240_bars_and_a_close_above_the_ema() -> None:
    assert btc_trend_ok([100.0] * (MARKET_EMA_BARS - 2) + [1.0]) is True
    assert btc_trend_ok([100.0] * (MARKET_EMA_BARS - 1) + [1.0]) is False
    rising = [100 * 1.001**i for i in range(MARKET_EMA_BARS)]
    falling = [100 * 0.999**i for i in range(MARKET_EMA_BARS)]
    assert btc_trend_ok(rising) is True
    assert btc_trend_ok(falling) is False
    assert btc_trend_ok(pd.Series(rising)) is True


def test_the_vectorised_market_filter_matches_each_window() -> None:
    rng = np.random.default_rng(3)
    closes = 100 * np.exp(np.cumsum(np.repeat(rng.choice([-0.004, 0.004], 9), 80) + rng.normal(0, 0.01, 720)))
    window = 299
    flags = btc_trend_ok_series(closes, window)
    expected = [btc_trend_ok(closes[max(0, i + 1 - window) : i + 1]) for i in range(len(closes))]
    assert flags.tolist() == expected
    assert flags[: MARKET_EMA_BARS - 1].all() and not flags.all() and flags[MARKET_EMA_BARS:].any()
    assert btc_trend_ok_series(closes[:100], window).all()

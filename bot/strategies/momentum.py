"""Time-series momentum on 4h candles, long only.

Trend following is the best-documented edge in crypto, and the evidence holds
for long-only portfolios (Rozario et al. 2020; Liu, Tsyvinski and Wu 2022).
A buy needs a positive return over the last 120 bars (about 20 days), EMA 20
above EMA 50, the close above EMA 50 and ADX of at least 20. The confidence is
that return divided by the volatility over the same bars, so a steady rise
counts more than a jump. The stop is 2.5 ATR; the position leaves on the
trailing stop or when EMA 20 falls under EMA 50.
"""

from __future__ import annotations

import math

import pandas as pd

from bot.indicators import adx, atr, ema, regime
from bot.strategies.base import Setup

#: Bars of the momentum return: 120 bars of 4h are 20 days.
LOOKBACK = 120
#: The lookback plus the EMA 50 warm-up.
MIN_BARS = LOOKBACK + 50
#: A return of this many standard deviations over the lookback gives full confidence.
FULL_CONFIDENCE = 2.0
STOP_ATR = 2.5
MIN_ADX = 20.0


class Momentum:
    id = "momentum"
    timeframes = ("4h",)
    markets = ("spot",)
    style = "swing"
    history_days = 365

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        if timeframe not in self.timeframes or len(frame) < MIN_BARS:
            return None
        close = frame["close"]
        last = float(close.iloc[-1])
        start = float(close.iloc[-1 - LOOKBACK])
        if last <= 0 or start <= 0:
            return None
        fast = float(ema(close, 20).iloc[-1])
        slow = float(ema(close, 50).iloc[-1])
        momentum = last / start - 1
        # The trend checks are cheap; the ADX and ATR only run on bars that pass them.
        if momentum <= 0 or fast <= slow or last <= slow:
            return None
        strength_series = adx(frame)
        strength_value = strength_series.iloc[-1]
        range_value = atr(frame).iloc[-1]
        if pd.isna(strength_value) or pd.isna(range_value):
            return None
        strength = float(strength_value)
        true_range = float(range_value)
        if true_range <= 0 or strength < MIN_ADX:
            return None
        bar_sigma = float(close.pct_change().tail(LOOKBACK).std() or 0)
        if bar_sigma <= 0:
            return None
        strength_ratio = momentum / (bar_sigma * math.sqrt(LOOKBACK))
        stop_pct = STOP_ATR * true_range / last
        target_pct = 2 * stop_pct
        return Setup(
            strategy_id=self.id,
            symbol=symbol,
            timeframe=timeframe,
            action=f"Comprar · momento {timeframe}",
            direction=1.0,
            expected_return=target_pct,
            sigma=max(float(close.pct_change().tail(20).std() or 0.01), 0.005),
            stop_pct=stop_pct,
            target_pct=target_pct,
            reason=(
                f"Alta de {momentum:.1%} em 20 dias ({strength_ratio:.1f} desvios), "
                f"EMA 20 acima da 50 e ADX {strength:.0f}"
            ),
            regime=regime(frame, strength_series),
            market="spot",
            confidence=max(0.0, min(1.0, strength_ratio / FULL_CONFIDENCE)),
            alphas={"momentum": round(momentum, 4), "vol_adjusted": round(strength_ratio, 2)},
        )

    def exit_reason(self, frame: pd.DataFrame, side: str) -> str | None:
        if side != "buy" or len(frame) < 50:
            return None
        if float(ema(frame["close"], 20).iloc[-1]) < float(ema(frame["close"], 50).iloc[-1]):
            return "momento virou: EMA 20 abaixo da 50"
        return None

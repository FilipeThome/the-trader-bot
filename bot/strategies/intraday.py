"""Day-trade strategies: 15m entries while the 1h trend is up, long only.

Evidence (see docs/plano-day-trade.md): intraday momentum is strongest on
high-volume bars (Gao, Han, Li and Zhou 2018; Shen, Urquhart and Wang 2022
for Bitcoin), and short pullbacks revert inside a trend (De Nicola 2021). No
study shows either one profitable after costs on crypto spot, so both trade
only after the backtest gate, like every other strategy.

The stop is the larger of ``stop_atr`` ATR(14) and ``min_stop_pct``; the
target is the first take-profit at ``tp1_r`` times that risk. The expected
return is that first target (architect decision, plan v3): the score, the cost
and reward/risk filters and the Kelly payoff measure the TP1 leg, the trade's
R target, while the backtest gate alone judges the blended expectancy of both
legs.
"""

from __future__ import annotations

import pandas as pd

from bot.indicators import atr, ema, regime, rsi, session_vwap
from bot.strategies.base import Setup
from bot.strategies.day import DAY, ENTRY_TIMEFRAME, hourly_trend

#: Bars of the 15m high the momentum close must break.
BREAKOUT_BARS = 8
#: Volume of the breakout bar against the median of the last 20 bars.
VOLUME_SURGE = 1.5
#: RSI band of a pullback: weak enough to be a dip, not a collapse.
PULLBACK_RSI = (35.0, 55.0)
MIN_BARS = 60
#: EMA 20 this far above EMA 50 on the 1h counts as a full-strength trend.
FULL_TREND = 0.01


def _sigma(close: pd.Series) -> float:
    return max(float(close.pct_change().tail(20).std() or 0.002), 0.002)


def _stop_pct(frame: pd.DataFrame, last: float, floor: float = 0.0) -> float | None:
    value = atr(frame).iloc[-1]
    if pd.isna(value) or float(value) <= 0 or last <= 0:
        return None
    return max(DAY.stop_atr * float(value) / last, DAY.min_stop_pct, floor)


def _setup(
    strategy_id: str, symbol: str, action: str, frame: pd.DataFrame, hourly: pd.DataFrame,
    stop_pct: float, confidence: float, reason: str, alphas: dict[str, float],
) -> Setup:
    target = DAY.tp1_r * stop_pct
    return Setup(
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=ENTRY_TIMEFRAME,
        action=action,
        direction=1.0,
        expected_return=target,
        sigma=_sigma(frame["close"]),
        stop_pct=stop_pct,
        target_pct=target,
        reason=reason,
        regime=regime(hourly),
        market="spot",
        confidence=max(0.0, min(1.0, confidence)),
        alphas=alphas,
    )


class IntradayMomentum:
    """Breakout of the 8-bar 15m high on a volume surge, above the day's VWAP, in a 1h uptrend."""

    id = "intraday_momentum"
    timeframes = (ENTRY_TIMEFRAME,)
    markets = ("spot",)
    style = "day"
    history_days = 90

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, *, hourly: pd.DataFrame | None = None) -> Setup | None:
        if timeframe != ENTRY_TIMEFRAME or len(frame) < MIN_BARS:
            return None
        last = float(frame["close"].iloc[-1])
        # Cheap rejections first: most bars break nothing.
        if last <= float(frame["high"].iloc[-1 - BREAKOUT_BARS : -1].max()):
            return None
        volume = float(frame["volume"].iloc[-1])
        median = float(frame["volume"].iloc[-21:-1].median())
        if median <= 0 or volume <= VOLUME_SURGE * median:
            return None
        trend = hourly_trend(hourly, frame)
        if trend is None or hourly is None:
            return None
        vwap = session_vwap(frame)
        if vwap is None or pd.isna(vwap.iloc[-1]) or last <= float(vwap.iloc[-1]):
            return None
        stop_pct = _stop_pct(frame, last)
        if stop_pct is None:
            return None
        surge = volume / median
        confidence = 0.4 + 0.3 * min(surge / (2 * VOLUME_SURGE), 1.0) + 0.3 * min(trend / FULL_TREND, 1.0)
        return _setup(
            self.id, symbol, "Comprar · day trade momento 15m", frame, hourly, stop_pct, confidence,
            f"Rompeu a máxima de {BREAKOUT_BARS} velas de 15m com volume {surge:.1f}x a mediana, acima da VWAP do dia, "
            "com o 1h em alta",
            {"volume_surge": round(surge, 2), "hourly_trend": round(trend, 4)},
        )


class VwapPullback:
    """A dip under the day's VWAP or EMA 20 that closes back above it, in a 1h uptrend."""

    id = "vwap_pullback"
    timeframes = (ENTRY_TIMEFRAME,)
    markets = ("spot",)
    style = "day"
    history_days = 90

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame, *, hourly: pd.DataFrame | None = None) -> Setup | None:
        if timeframe != ENTRY_TIMEFRAME or len(frame) < MIN_BARS:
            return None
        close = frame["close"]
        last = float(close.iloc[-1])
        previous = float(close.iloc[-2])
        if last <= previous:
            return None
        trend = hourly_trend(hourly, frame)
        if trend is None or hourly is None:
            return None
        fast = ema(close, 20)
        vwap = session_vwap(frame)
        reclaimed = previous < float(fast.iloc[-2]) and last > float(fast.iloc[-1])
        if vwap is not None and not pd.isna(vwap.iloc[-1]) and not pd.isna(vwap.iloc[-2]):
            reclaimed = reclaimed or (previous < float(vwap.iloc[-2]) and last > float(vwap.iloc[-1]))
        if not reclaimed:
            return None
        value = rsi(close).iloc[-1]
        if pd.isna(value) or not PULLBACK_RSI[0] <= float(value) <= PULLBACK_RSI[1]:
            return None
        swing_low = float(frame["low"].iloc[-2:].min())
        floor = (last - swing_low) / last if 0 < swing_low < last else 0.0
        stop_pct = _stop_pct(frame, last, floor)
        if stop_pct is None:
            return None
        confidence = 0.5 + 0.5 * min(trend / FULL_TREND, 1.0)
        return _setup(
            self.id, symbol, "Comprar · day trade pullback 15m", frame, hourly, stop_pct, confidence,
            f"Recuou e voltou acima da VWAP/EMA 20 no 15m com RSI {float(value):.0f}, com o 1h em alta",
            {"rsi": round(float(value), 1), "hourly_trend": round(trend, 4)},
        )

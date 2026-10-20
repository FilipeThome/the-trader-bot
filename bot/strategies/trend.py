"""Breakout: close above the 20-bar high, volume above average, ADX above 25.

The position leaves when EMA 20 crosses back under EMA 50. With hysteresis (the
default, ``exit_hysteresis`` in the config) one bar under the slow EMA is not
enough: the fast EMA must sit ``EXIT_BAND`` below it, or under it on the last
``EXIT_CONFIRM_BARS`` closed bars, so a flat market does not churn exits.
"""

from __future__ import annotations

import pandas as pd

from bot.alphas import agreement
from bot.indicators import adx, atr, ema, regime
from bot.strategies.base import Setup

#: How far EMA 20 must fall under EMA 50 for an exit on a single bar.
EXIT_BAND = 0.005
#: Closed bars in a row with EMA 20 under EMA 50 that exit without the band.
EXIT_CONFIRM_BARS = 2


def ema_crossed(close: pd.Series, side: str, *, hysteresis: bool = True) -> bool:
    """Whether EMA 20 crossed EMA 50 against a ``side`` position on the last closed bar.

    Without hysteresis any bar on the wrong side exits. With it, the cross needs
    the ``EXIT_BAND`` margin or ``EXIT_CONFIRM_BARS`` bars in a row.
    """
    fast = ema(close, 20)
    slow = ema(close, 50)
    if len(close) == 0 or pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]):
        return False
    sign = 1.0 if side == "buy" else -1.0
    # Positive when the fast EMA sits on the losing side of the slow one.
    against = sign * (slow - fast)
    if float(against.iloc[-1]) <= 0:
        return False
    if not hysteresis:
        return True
    if float(against.iloc[-1]) > EXIT_BAND * float(slow.iloc[-1]):
        return True
    recent = against.iloc[-EXIT_CONFIRM_BARS:]
    return len(recent) == EXIT_CONFIRM_BARS and bool((recent > 0).all())


class TrendBreakout:
    id = "trend"
    timeframes = ("1h", "4h")
    markets = ("spot", "usdm")
    style = "swing"
    history_days = 180

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        if timeframe not in self.timeframes or len(frame) < 60:
            return None
        close = float(frame["close"].iloc[-1])
        prior_high = float(frame["high"].iloc[-21:-1].max())
        prior_low = float(frame["low"].iloc[-21:-1].min())
        avg_volume = float(frame["volume"].iloc[-21:-1].mean() or 0)
        volume = float(frame["volume"].iloc[-1])
        # Most bars break nothing: refuse them before the ADX and ATR, which
        # cost most of a backtest step.
        if not (volume > avg_volume and (close > prior_high or close < prior_low)):
            return None
        strength_series = adx(frame)
        state = regime(frame, strength_series)
        strength = float(strength_series.iloc[-1]) if pd.notna(strength_series.iloc[-1]) else 0.0
        range_series = atr(frame)
        true_range = float(range_series.iloc[-1]) if pd.notna(range_series.iloc[-1]) else 0.0
        if true_range <= 0 or close <= 0:
            return None
        broke = close > prior_high and volume > avg_volume and strength > 25 and state == "trend_up"
        broke_down = close < prior_low and volume > avg_volume and strength > 25 and state == "trend_down"
        if not broke and not broke_down:
            return None
        direction = 1.0 if broke else -1.0
        stop_pct = (2 * true_range) / close
        target_pct = stop_pct * 2
        sigma = max(float(frame["close"].pct_change().tail(20).std() or 0.01), 0.005)
        conf, votes = agreement(direction, frame)
        side = "Comprar" if direction > 0 else "Vender (futuro)"
        return Setup(
            strategy_id=self.id,
            symbol=symbol,
            timeframe=timeframe,
            action=f"{side} · tendência {timeframe}",
            direction=direction,
            expected_return=target_pct,
            sigma=sigma,
            stop_pct=stop_pct,
            target_pct=target_pct,
            reason=(
                f"Rompimento da {'máxima' if direction > 0 else 'mínima'} de 20 períodos "
                f"com volume {volume / avg_volume:.1f}x a média e ADX {strength:.0f}"
            ),
            regime=state,
            market="spot" if direction > 0 else "usdm",
            confidence=conf,
            alphas=votes,
        )

    def exit_reason(self, frame: pd.DataFrame, side: str, *, hysteresis: bool = True) -> str | None:
        if side not in ("buy", "sell"):
            return None
        if ema_crossed(frame["close"], side, hysteresis=hysteresis):
            return "cruzamento contrário da EMA 20/50"
        return None

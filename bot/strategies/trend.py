"""Breakout: close above the 20-bar high, volume above average, ADX above 25."""

from __future__ import annotations

import pandas as pd

from bot.alphas import agreement
from bot.indicators import adx, atr, ema, regime
from bot.strategies.base import Setup


class TrendBreakout:
    id = "trend"
    timeframes = ("1h", "4h")
    markets = ("spot", "usdm")

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        if timeframe not in self.timeframes or len(frame) < 60:
            return None
        state = regime(frame)
        close = float(frame["close"].iloc[-1])
        prior_high = float(frame["high"].iloc[-21:-1].max())
        avg_volume = float(frame["volume"].iloc[-21:-1].mean() or 0)
        volume = float(frame["volume"].iloc[-1])
        strength = float(adx(frame).iloc[-1]) if pd.notna(adx(frame).iloc[-1]) else 0.0
        true_range = float(atr(frame).iloc[-1]) if pd.notna(atr(frame).iloc[-1]) else 0.0
        if true_range <= 0 or close <= 0:
            return None
        broke = close > prior_high and volume > avg_volume and strength > 25 and state == "trend_up"
        broke_down = (
            close < float(frame["low"].iloc[-21:-1].min())
            and volume > avg_volume
            and strength > 25
            and state == "trend_down"
        )
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

    def exit_reason(self, frame: pd.DataFrame, side: str) -> str | None:
        fast = ema(frame["close"], 20)
        slow = ema(frame["close"], 50)
        if pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]):
            return None
        if side == "buy" and float(fast.iloc[-1]) < float(slow.iloc[-1]):
            return "cruzamento contrário da EMA 20/50"
        if side == "sell" and float(fast.iloc[-1]) > float(slow.iloc[-1]):
            return "cruzamento contrário da EMA 20/50"
        return None

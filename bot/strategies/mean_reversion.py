"""Mean reversion on 15m and 1h, spot only, and only in a sideways regime."""

from __future__ import annotations

import pandas as pd

from bot.alphas import agreement
from bot.indicators import atr, bollinger, ema, regime, rsi
from bot.strategies.base import Setup


class MeanReversion:
    id = "mean_reversion"
    timeframes = ("15m", "1h")
    markets = ("spot",)

    def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup | None:
        if timeframe not in self.timeframes or len(frame) < 40:
            return None
        close = float(frame["close"].iloc[-1])
        value = rsi(frame["close"])
        if value.dropna().empty or close <= 0:
            return None
        last_rsi = float(value.iloc[-1])
        # RSI inside 30-70 is most bars: refuse them before the bands, the ATR and the regime.
        if not (last_rsi < 30 or last_rsi > 70):
            return None
        mid, upper, lower = bollinger(frame["close"])
        if pd.isna(lower.iloc[-1]):
            return None
        buy = last_rsi < 30 and close < float(lower.iloc[-1])
        sell = last_rsi > 70 and close > float(upper.iloc[-1])
        if not buy and not sell:
            return None
        range_series = atr(frame)
        true_range = float(range_series.iloc[-1]) if pd.notna(range_series.iloc[-1]) else 0.0
        if true_range <= 0:
            return None
        state = regime(frame)
        direction = 1.0 if buy else -1.0
        mean = float(ema(frame["close"], 20).iloc[-1])
        target_pct = abs(mean - close) / close
        stop_pct = (1.5 * true_range) / close
        sigma = max(float(frame["close"].pct_change().tail(20).std() or 0.01), 0.004)
        conf, votes = agreement(direction, frame)
        return Setup(
            strategy_id=self.id,
            symbol=symbol,
            timeframe=timeframe,
            action=("Comprar" if direction > 0 else "Vender") + f" · reversão {timeframe}",
            direction=direction,
            expected_return=max(target_pct, stop_pct),
            sigma=sigma,
            stop_pct=stop_pct,
            target_pct=max(target_pct, stop_pct),
            reason=f"RSI {last_rsi:.0f} com preço fora das bandas de Bollinger",
            regime=state,
            market="spot",
            confidence=conf,
            alphas=votes,
            extra={"mean": mean},
        )

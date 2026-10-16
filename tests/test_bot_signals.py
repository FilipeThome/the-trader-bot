from __future__ import annotations

import pandas as pd

from bot.indicators import ema, rsi
from bot.strategies.base import Setup
from bot.evaluator import regime_factor, round_trip_cost, score_setup


def _frame() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = [100 + (step % 5) - 2 for step in range(40)]
    return pd.DataFrame(
        {
            "open": [value - 0.4 for value in close],
            "high": [value + 0.6 for value in close],
            "low": [value - 0.6 for value in close],
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def test_indicators_are_deterministic_for_fixed_candles() -> None:
    frame = _frame()
    first = rsi(frame["close"])
    second = ema(frame["close"], 20)
    assert first.equals(rsi(frame["close"]))
    assert second.equals(ema(frame["close"], 20))
    assert first.notna().any()
    assert 0 <= float(first.dropna().iloc[-1]) <= 100


def test_score_matches_the_closed_form() -> None:
    setup = Setup(
        strategy_id="trend",
        symbol="BTC/USDT",
        timeframe="4h",
        action="Comprar",
        direction=1,
        expected_return=0.03,
        sigma=0.02,
        stop_pct=0.011,
        target_pct=0.03,
        reason="rompimento",
        regime="trend_up",
        confidence=0.75,
    )
    cost = round_trip_cost(setup, slippage=0.0005)
    expected = ((setup.expected_return - cost) / setup.sigma) * setup.confidence * regime_factor(
        "trend", "trend_up", 1
    )
    assert score_setup(setup, slippage=0.0005) == expected
    assert score_setup(setup, slippage=0.0005) == score_setup(setup, slippage=0.0005)

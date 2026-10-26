from __future__ import annotations

import pandas as pd
import pytest

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


def _closes(*tail: float, flat: int = 80) -> pd.DataFrame:
    """A flat history at 100 (EMA 20 equals EMA 50), then ``tail``."""
    return pd.DataFrame({"close": [100.0] * flat + list(tail)})


def test_trend_exit_needs_the_band_or_two_closed_bars() -> None:
    from bot.strategies.trend import EXIT_BAND, EXIT_CONFIRM_BARS, TrendBreakout

    strategy = TrendBreakout()
    assert (EXIT_BAND, EXIT_CONFIRM_BARS) == (0.005, 2)
    # One bar a little lower: EMA 20 dips under EMA 50 by far less than the 0.5% band.
    dip = _closes(99.9)
    assert strategy.exit_reason(dip, "buy", hysteresis=False) == "cruzamento contrário da EMA 20/50"
    assert strategy.exit_reason(dip, "buy") is None
    # The second closed bar under it confirms the cross.
    assert strategy.exit_reason(_closes(99.9, 99.9), "buy") == "cruzamento contrário da EMA 20/50"
    # A drop that opens the band exits on its first bar.
    assert strategy.exit_reason(_closes(80.0), "buy") == "cruzamento contrário da EMA 20/50"
    assert strategy.exit_reason(_closes(), "buy") is None and strategy.exit_reason(_closes(), "buy", hysteresis=False) is None
    # The sell side mirrors it.
    assert strategy.exit_reason(_closes(100.1), "sell") is None
    assert strategy.exit_reason(_closes(100.1), "sell", hysteresis=False) is not None


def test_momentum_exit_hysteresis() -> None:
    from bot.strategies.momentum import Momentum

    strategy = Momentum()
    assert strategy.exit_reason(_closes(99.9), "buy", hysteresis=False) == "momento virou: EMA 20 abaixo da 50"
    assert strategy.exit_reason(_closes(99.9), "buy") is None
    assert strategy.exit_reason(_closes(99.9, 99.9), "buy") == "momento virou: EMA 20 abaixo da 50"
    assert strategy.exit_reason(_closes(80.0), "buy") is not None
    assert strategy.exit_reason(_closes(80.0, flat=40), "buy") is None  # fewer than 50 bars
    assert strategy.exit_reason(_closes(80.0), "sell") is None


def test_funding_setup_labels_a_daily_return() -> None:
    from bot.strategies.funding import FundingCarry

    setup = FundingCarry().evaluate("BTC/USDT", "8h", pd.DataFrame(), funding_rates=[0.05, 0.73, 0.73, 0.73])
    assert setup is not None and setup.timeframe == "8h"
    # Annualised 73% is 0.2% a day: the estimate is per day, not per 8h period.
    assert setup.expected_return == setup.target_pct == pytest.approx(0.002)
    assert setup.extra["return_horizon"] == "1d"
    assert setup.reason == "Funding anualizado acima do limiar há 3 períodos; retorno estimado de 0.200% ao dia"

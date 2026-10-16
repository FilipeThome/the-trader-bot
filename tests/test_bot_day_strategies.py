from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from bot.evaluator import classify
from bot.indicators import session_vwap
from bot.strategies import DAY_STRATEGIES, STRATEGIES
from bot.strategies.day import DAY, closed_hourly, hourly_trend, in_quiet_hours
from test_bot_strategy_golden import candles


def _bars(prices: list[float], start: str, freq: str = "15min", volume: float = 10.0) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    close = np.array(prices, dtype=float)
    return pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": volume}, index=index
    )


def _scan(strategy_id: str) -> list:
    m15 = candles(3000, 7, "15min")
    hourly = candles(800, 7, "h")
    found = []
    for end in range(60, len(m15)):
        window = m15.iloc[max(0, end - 299) : end]
        setup = STRATEGIES[strategy_id].evaluate(
            "BTC/USDT", "15m", window, hourly=closed_hourly(hourly, window.index[-1] + pd.Timedelta(minutes=15), 299)
        )
        if setup is not None:
            found.append(setup)
    return found


def test_the_vwap_restarts_at_utc_midnight_and_waits_for_a_few_bars() -> None:
    frame = _bars([100.0] * 92 + [200.0] * 6, "2026-10-14T01:00:00")
    vwap = session_vwap(frame)
    assert vwap is not None
    # The new day's first bar is weighted alone: the old day's prices are gone.
    assert vwap.iloc[-6] == pytest.approx(200.0)
    assert vwap.iloc[-7] == pytest.approx(100.0)
    assert session_vwap(frame.iloc[:-3]) is None


def test_quiet_hours_are_judged_at_the_close_of_the_entry_bar() -> None:
    assert in_quiet_hours(datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc))
    assert in_quiet_hours(datetime(2026, 10, 16, 6, 59, tzinfo=timezone.utc))
    assert not in_quiet_hours(datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc))
    assert not in_quiet_hours(datetime(2026, 10, 16, 2, 59, tzinfo=timezone.utc))


def test_the_1h_filter_never_sees_the_hour_still_forming() -> None:
    hourly = _bars([100.0] * 10, "2026-10-16T00:00:00", freq="h")
    # A 15m bar closing at 05:45 sees the hours that closed by then: up to the 04:00 bar.
    seen = closed_hourly(hourly, pd.Timestamp("2026-10-16T05:45:00Z"), 299)
    assert seen.index[-1] == pd.Timestamp("2026-10-16T04:00:00Z")
    seen = closed_hourly(hourly, pd.Timestamp("2026-10-16T06:00:00Z"), 299)
    assert seen.index[-1] == pd.Timestamp("2026-10-16T05:00:00Z")


def test_the_1h_trend_must_be_up() -> None:
    rising = _bars(list(np.linspace(100, 130, 80)), "2026-10-10T00:00:00", freq="h")
    falling = _bars(list(np.linspace(130, 100, 80)), "2026-10-10T00:00:00", freq="h")
    assert hourly_trend(rising) is not None and hourly_trend(rising) > 0
    assert hourly_trend(falling) is None
    assert hourly_trend(rising.iloc[:30]) is None


@pytest.mark.parametrize("strategy_id", sorted(DAY_STRATEGIES))
def test_a_day_setup_respects_the_stop_floor_and_the_first_target(strategy_id: str) -> None:
    setups = _scan(strategy_id)
    assert setups
    for setup in setups:
        assert setup.direction > 0 and setup.timeframe == "15m" and setup.market == "spot"
        assert setup.stop_pct >= DAY.min_stop_pct
        assert setup.target_pct == pytest.approx(DAY.tp1_r * setup.stop_pct)
        # The setup filters measure the TP1 leg (plan v3).
        assert setup.expected_return == setup.target_pct
        assert 0 < setup.confidence <= 1


@pytest.mark.parametrize("strategy_id", sorted(DAY_STRATEGIES))
def test_a_day_setup_reaches_ready_under_the_day_preset(strategy_id: str) -> None:
    setup = _scan(strategy_id)[0]
    row = classify(setup, score_threshold=0.3, cost_multiple=3.0, min_reward_risk=1.2)
    assert row.status == "pronta", row.reason


@pytest.mark.parametrize("strategy_id", sorted(DAY_STRATEGIES))
def test_a_day_strategy_needs_the_1h_frame(strategy_id: str) -> None:
    m15 = candles(400, 7, "15min")
    assert all(
        STRATEGIES[strategy_id].evaluate("BTC/USDT", "15m", m15.iloc[: end], hourly=None) is None
        for end in range(60, 400)
    )

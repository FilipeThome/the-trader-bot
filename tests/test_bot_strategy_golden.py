"""The strategies' setups are pinned bit for bit on synthetic candles.

The digests were taken before the cheap rejections were moved ahead of the
expensive indicators: a change of order must not change a single setup.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trend import TrendBreakout

WINDOW = 300


def candles(bars: int, seed: int, freq: str) -> pd.DataFrame:
    """Random-walk candles that alternate trending and sideways stretches."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.004, 0.0, 0.004], size=bars // 100 + 1), 100)[:bars]
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, bars)))
    opened = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.006, bars)) * close
    index = pd.date_range("2025-01-01", periods=bars, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": opened,
            "high": np.maximum(opened, close) + spread,
            "low": np.minimum(opened, close) - spread,
            "close": close,
            "volume": rng.lognormal(10, 0.5, bars),
        },
        index=index,
    )


def digest(strategy, timeframe: str, frame: pd.DataFrame) -> tuple[int, str]:
    setups = []
    for end in range(60, len(frame) + 1):
        setup = strategy.evaluate("BTC/USDT", timeframe, frame.iloc[max(0, end - WINDOW) : end])
        setups.append(None if setup is None else asdict(setup))
    found = sum(1 for setup in setups if setup is not None)
    return found, hashlib.sha256(repr(setups).encode()).hexdigest()


CASES = {
    "trend": (TrendBreakout(), "1h", "h"),
    "mean_reversion": (MeanReversion(), "1h", "h"),
    "momentum": (Momentum(), "4h", "4h"),
}
GOLDEN = {
    "mean_reversion": (92, "e25be2e5f27411f84ac4682ee81e8f408b10bd95070f565f6903506579df13ea"),
    "momentum": (313, "f705a4f20cda0c38ad723d616043b9c0d1826ad314670c4f893d21ec00ece3ae"),
    "trend": (65, "d5bb395d072c7f12f83d270b2bffd4df8e1bbb4caab92a4bd1db1bc360f45bf6"),
}


@pytest.mark.parametrize("strategy_id", sorted(CASES))
def test_a_strategy_finds_the_same_setups_as_before(strategy_id: str) -> None:
    strategy, timeframe, freq = CASES[strategy_id]
    assert digest(strategy, timeframe, candles(900, 7, freq)) == GOLDEN[strategy_id]

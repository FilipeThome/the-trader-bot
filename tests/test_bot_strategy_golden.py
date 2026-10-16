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


def trades_digest(strategy_id: str, timeframe: str, freq: str, policy: str) -> tuple[int, str]:
    from bot.backtest.runner import simulate

    trades = simulate(strategy_id, "BTC/USDT", candles(900, 7, freq), timeframe, exit_policy=policy)
    return len(trades), hashlib.sha256(repr([(t.exit_time, t.net_return) for t in trades]).encode()).hexdigest()


TRADES_GOLDEN = {
    ("mean_reversion", "target"): (14, "eac8bd6df4e9b4237d2a436df469cc1eab570a61030ad7254fe54f223d3a72cb"),
    ("mean_reversion", "trailing"): (16, "78e792d4ce1489e51200cc5e56ef9c96e0cf262007473c374c7f52edf4084bd1"),
    ("momentum", "target"): (16, "18e5da71cbdf4711a1d92ddb1467a3dafd41c846b16be8ab80b167cacc928121"),
    ("momentum", "trailing"): (7, "a3557ba41f16bda0cdb113acce3cc69749698cf35e77e6672c3da4bf9c293f0f"),
    ("trend", "target"): (13, "e8715555ac0876c58cf488e8faf9844c2f9fd6c153148235c8f12be0efa22b28"),
    ("trend", "trailing"): (8, "c404e774bf4450954d4dfabf8ab171b4fdbe51d58b850b1f52abda4bde8a2bec"),
}


@pytest.mark.parametrize(("strategy_id", "policy"), sorted(TRADES_GOLDEN))
def test_a_replay_without_day_rules_trades_as_before(strategy_id: str, policy: str) -> None:
    _strategy, timeframe, freq = CASES[strategy_id]
    assert trades_digest(strategy_id, timeframe, freq, policy) == TRADES_GOLDEN[(strategy_id, policy)]


def day_digests(strategy_id: str) -> tuple[tuple[int, str], tuple[int, str]]:
    """Setups on every closed 15m window (with the closed 1h bars) and the day replay's trades."""
    from bot.backtest.runner import simulate
    from bot.strategies import STRATEGIES
    from bot.strategies.day import closed_hourly

    m15 = candles(1500, 7, "15min")
    hourly = candles(500, 7, "h")
    setups = []
    for end in range(60, len(m15) + 1):
        window = m15.iloc[max(0, end - WINDOW) : end]
        found = STRATEGIES[strategy_id].evaluate(
            "BTC/USDT", "15m", window, hourly=closed_hourly(hourly, window.index[-1] + pd.Timedelta(minutes=15), 299)
        )
        setups.append(None if found is None else asdict(found))
    trades = simulate(strategy_id, "BTC/USDT", m15, "15m", hourly=hourly)
    return (
        (sum(1 for setup in setups if setup is not None), hashlib.sha256(repr(setups).encode()).hexdigest()),
        (len(trades), hashlib.sha256(repr([(t.exit_time, t.net_return) for t in trades]).encode()).hexdigest()),
    )


DAY_GOLDEN = {
    "intraday_momentum": (
        (25, "44ffdc94773c51dd9ca7593747c56db5dabb8247213bfda1fe10847413ba9900"),
        (10, "3e65dc1795059ada3182dbed622f3f62b4b7951c7499c87701aea04f96e67d9b"),
    ),
    "vwap_pullback": (
        (27, "fa0da2f17c1aa0921a2129604737096589ef4ad89ebcc5b7f28bf93c37465d98"),
        (14, "00a0df9be35430fb32dcccd7401344209a4292ef6d604cd426390773ab3004ef"),
    ),
}


@pytest.mark.parametrize("strategy_id", sorted(DAY_GOLDEN))
def test_a_day_strategy_finds_and_trades_the_same_as_before(strategy_id: str) -> None:
    assert day_digests(strategy_id) == DAY_GOLDEN[strategy_id]

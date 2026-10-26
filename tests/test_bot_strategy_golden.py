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


# Every swing replay changed with the replay-fidelity step (6.6): each leg now pays LEG_SLIPPAGE
# (0.03%), so every return moves even where the trades do not. The "target" keys also sell a reached
# target at the close of the bar that closes at or above it, not on the wick: momentum and trend
# each lose one trade (12 of 13, 15 of 16) because a wick-only target no longer ends a position.
# Exit hysteresis (6.9) then changed one key: momentum "trailing" (same 7 trades, later EMA exits).
# Its 6.6-only digest was d10ab8625c9f62440f234d8c8055a51f1a7f08610a2a3e41a8bd4a606112b18a.
TRADES_GOLDEN = {
    # 6.6: slippage on both legs; target filled at the close.
    ("mean_reversion", "target"): (14, "63df5e9ccff6af0344abfca9fa3f6af7b6e36999b6674d1a9d34288c86aafb80"),
    # 6.6: slippage on both legs.
    ("mean_reversion", "trailing"): (16, "b2849a26a7e0703a75f2ed1c19906848b9d118be9dec609a9c7d6e2136eabf49"),
    # 6.6: slippage on both legs; target filled at the close (16 -> 15 trades).
    ("momentum", "target"): (15, "b2689770cacbe15e91095f606656943d8b3c09ad6fe853e2eb640e403647c2eb"),
    # 6.6: slippage on both legs; 6.9: hysteresis on the EMA 20/50 exit.
    ("momentum", "trailing"): (7, "5e72d9f1a532bc74ed9ef11eab88fef0868815b9c189e620238ab8a6c1ae0d13"),
    # 6.6: slippage on both legs; target filled at the close (13 -> 12 trades).
    ("trend", "target"): (12, "f484f644f2d51e580830301f27a96b18900e25c712b96b07baadb7bcabde4237"),
    # 6.6: slippage on both legs (hysteresis leaves these trades unchanged).
    ("trend", "trailing"): (8, "65460032927286b4bddb6842fa14af482ae0e8b750ab955830e800c0eab1a5b2"),
}


@pytest.mark.parametrize(("strategy_id", "policy"), sorted(TRADES_GOLDEN))
def test_a_replay_without_day_rules_trades_as_before(strategy_id: str, policy: str) -> None:
    _strategy, timeframe, freq = CASES[strategy_id]
    assert trades_digest(strategy_id, timeframe, freq, policy) == TRADES_GOLDEN[(strategy_id, policy)]


def day_digests(strategy_id: str) -> tuple[tuple[int, str], tuple[int, str]]:
    """Setups on every closed 15m window (with the closed 1h bars) and the day replay's trades."""
    from bot.backtest.runner import simulate
    from bot.strategies import STRATEGIES
    from bot.strategies.day import MIN_HOURLY_BARS, closed_hourly

    m15 = candles(1500, 7, "15min")
    hourly = candles(500, 7, "h")
    setups = []
    for end in range(60, len(m15) + 1):
        window = m15.iloc[max(0, end - WINDOW) : end]
        closed = closed_hourly(hourly, window.index[-1] + pd.Timedelta(minutes=15), 299)
        found = STRATEGIES[strategy_id].evaluate("BTC/USDT", "15m", window, hourly=closed)
        # Both series start together, so the first 60 hours read as a young pair, which now
        # trades on its day (``session_trend``). Every established window must stay as before.
        setups.append(None if found is None or len(closed) < MIN_HOURLY_BARS else asdict(found))
    trades = simulate(strategy_id, "BTC/USDT", m15, "15m", hourly=hourly)
    return (
        (sum(1 for setup in setups if setup is not None), hashlib.sha256(repr(setups).encode()).hexdigest()),
        (len(trades), hashlib.sha256(repr([(t.exit_time, t.net_return) for t in trades]).encode()).hexdigest()),
    )


DAY_GOLDEN = {
    "intraday_momentum": (
        (25, "44ffdc94773c51dd9ca7593747c56db5dabb8247213bfda1fe10847413ba9900"),
        # 10 trades before young pairs traded their day; the 2 new ones open in the first 60 hours.
        (12, "0abb89d4958ee4692253b1ff5614798f9fff2ebedaf48a4b5c45ff0c5f5d25f9"),
    ),
    "vwap_pullback": (
        (27, "fa0da2f17c1aa0921a2129604737096589ef4ad89ebcc5b7f28bf93c37465d98"),
        (14, "00a0df9be35430fb32dcccd7401344209a4292ef6d604cd426390773ab3004ef"),
    ),
}


@pytest.mark.parametrize("strategy_id", sorted(DAY_GOLDEN))
def test_a_day_strategy_finds_and_trades_the_same_as_before(strategy_id: str) -> None:
    assert day_digests(strategy_id) == DAY_GOLDEN[strategy_id]

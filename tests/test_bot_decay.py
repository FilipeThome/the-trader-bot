from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from bot.backtest.runner import Trade, _daily_sharpe
from bot.decay import (
    PAIR_DEMOTED,
    SLIPPAGE_BLOCKED,
    apply_decay,
    live_sharpe,
    pair_demoted,
    pair_slippage_blocked,
    review_strategy,
)
from bot.store import Store

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
VALIDATED = "2026-08-01T00:00:00+00:00"


def _trade(
    store: Store,
    closed_at: datetime,
    *,
    pnl: float = 1.0,
    r: float | None = None,
    symbol: str = "BTC/USDT",
    strategy: str = "trend",
    entry_slippage: float | None = None,
    exit_slippage: float | None = None,
) -> int:
    position_id = store.open_pending(symbol, strategy)
    assert position_id is not None
    store.update_position(
        position_id,
        status="open",
        qty=1.0,
        avg_price=100.0,
        r_multiple=r,
        entry_slippage=entry_slippage,
        exit_slippage=exit_slippage,
    )
    store.close_position(position_id, exit_price=100.0 + pnl, realized_pnl=pnl, fees=0.0)
    with store._conn:
        store._conn.execute("UPDATE positions SET closed_at=? WHERE id=?", (closed_at.isoformat(), position_id))
    return position_id


def _validated(store: Store, strategy_id: str, timeframes: dict[str, Any], **fields: Any) -> None:
    store.save_strategy(
        {
            "strategy_id": strategy_id,
            "backtest_sharpe": 0.0,
            "backtest_drawdown": 0.1,
            "validated_at": VALIDATED,
            "signal_only": False,
            "params": {"timeframes": timeframes},
            **fields,
        }
    )


def test_sharpe_drop_above_half_demotes_the_strategy(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    store.save_strategy(
        {
            "strategy_id": "trend",
            "backtest_sharpe": 2.0,
            "backtest_drawdown": 0.1,
            "validated_at": "2026-01-01T00:00:00+00:00",
            "signal_only": False,
            "params": {},
        }
    )
    assert apply_decay(store, "trend", [-0.02] * 30) is True
    assert store.strategy("trend")["signal_only"] is True

    store.save_strategy(
        {
            "strategy_id": "mean_reversion",
            "backtest_sharpe": 1.1,
            "backtest_drawdown": 0.08,
            "validated_at": "2026-01-01T00:00:00+00:00",
            "signal_only": False,
            "params": {},
        }
    )
    strong = [0.02, 0.015, 0.025, 0.01] * 8
    assert apply_decay(store, "mean_reversion", strong) is False
    assert store.strategy("mean_reversion")["signal_only"] is False


def test_decay_compares_daily_sharpe_with_zero_days_on_both_sides(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    # Ten winning days out of 30: only the idle days give the series a spread.
    closes = [NOW - timedelta(days=day) for day in range(10)]
    for closed_at in closes:
        _trade(store, closed_at, pnl=1.0, r=0.5)
    expected = live_sharpe([0.0] * 20 + [0.001] * 10)
    assert live_sharpe([0.001] * 10) == 0.0  # the trading days alone would read as no edge
    # The backtest measures the same trades the same way: 30 UTC days, idle days as 0, √365.
    start = pd.Timestamp(NOW - timedelta(days=29))
    replay = [Trade(exit_time=pd.Timestamp(closed_at), net_return=0.001) for closed_at in sorted(closes)]
    assert _daily_sharpe(replay, start, pd.Timestamp(NOW)) == pytest.approx(expected)

    # The best passed timeframe is the benchmark; a failed one is not, however high.
    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 2 * expected - 0.01}, "1h": {"passed": False, "daily_sharpe": 99.0}})
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None
    row = store.strategy("trend")
    assert row["signal_only"] is False and row["live_sharpe"] == pytest.approx(expected)

    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 2 * expected + 0.01}})
    reason = review_strategy(store, "trend", capital=1000.0, now=NOW)
    assert reason is not None and "Sharpe diário dos últimos 30 dias" in reason
    row = store.strategy("trend")
    assert row["signal_only"] is True
    assert row["params"]["demoted"] == {"reason": reason, "at": NOW.isoformat()}
    assert row["params"]["timeframes"]["4h"]["passed"] is True
    # Already signal-only: nothing new to report.
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None


def test_decay_needs_10_trades_in_the_window_since_the_validation(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for day in range(9):
        _trade(store, NOW - timedelta(days=day), pnl=-1.0)
    _trade(store, NOW - timedelta(days=31), pnl=-1.0)  # outside the 30 calendar days
    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 1.0}})
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None
    assert store.strategy("trend")["live_sharpe"] is None

    _trade(store, NOW - timedelta(days=12), pnl=-1.0)
    # Validated after that trade: the window holds 9 trades of the current validation.
    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 1.0}}, validated_at=(NOW - timedelta(days=10)).isoformat())
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None

    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 1.0}})
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is not None


def test_decay_demotes_after_20_trades_with_negative_r(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    # Older than the 30-day window, so the Sharpe check has nothing to judge.
    for day in range(19):
        _trade(store, NOW - timedelta(days=40 + day), pnl=-1.0, r=-0.5)
    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 1.0}})
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None  # 19 trades

    _trade(store, NOW - timedelta(days=60), pnl=2.0, r=None)
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None  # one of the 20 has no R

    _trade(store, NOW - timedelta(days=34), pnl=-1.0, r=-0.5)
    reason = review_strategy(store, "trend", capital=1000.0, now=NOW)
    assert reason == "média de R dos últimos 20 trades negativa (-0.50 R)"
    assert store.strategy("trend")["signal_only"] is True

    # A revalidation starts the evidence over: the trades that demoted it no longer count.
    _validated(store, "trend", {"4h": {"passed": True, "daily_sharpe": 1.0}}, validated_at=(NOW - timedelta(days=1)).isoformat())
    assert review_strategy(store, "trend", capital=1000.0, now=NOW) is None
    assert store.strategy("trend")["signal_only"] is False


def test_a_pair_with_10_losing_r_trades_is_demoted_for_that_strategy_only(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for day in range(10):
        moment = NOW - timedelta(days=day)
        _trade(store, moment, pnl=-1.0, r=-1.0)
        _trade(store, moment, pnl=1.0, r=1.0, strategy="momentum")
        _trade(store, moment, pnl=1.0, r=1.0, symbol="ETH/USDT")
    assert pair_demoted(store, "trend", "BTC/USDT", now=NOW) == PAIR_DEMOTED
    assert PAIR_DEMOTED == "rebaixada neste par: expectativa negativa em 10 trades"
    assert pair_demoted(store, "momentum", "BTC/USDT", now=NOW) is None
    assert pair_demoted(store, "trend", "ETH/USDT", now=NOW) is None
    assert pair_demoted(store, "trend", "SOL/USDT", now=NOW) is None

    # Only the last 10 count: one recent winner breaks the losing run.
    _trade(store, NOW, pnl=20.0, r=10.0)
    assert pair_demoted(store, "trend", "BTC/USDT", now=NOW) is None


def test_pair_demotion_ages_out_after_90_days(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for hour in range(10):
        _trade(store, NOW - timedelta(days=85, hours=hour), pnl=-1.0, r=-1.0)
    assert pair_demoted(store, "trend", "BTC/USDT", now=NOW) == PAIR_DEMOTED
    assert pair_demoted(store, "trend", "BTC/USDT", now=NOW + timedelta(days=5)) is None


def test_a_pair_whose_last_20_trades_slipped_over_budget_is_blocked(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for index in range(20):
        moment = NOW - timedelta(hours=index)
        strategy = "trend" if index % 2 else "momentum"  # the block is the pair's, whatever the strategy
        _trade(store, moment, strategy=strategy, entry_slippage=0.002, exit_slippage=0.0015)
        _trade(store, moment, symbol="ETH/USDT", entry_slippage=0.001, exit_slippage=0.001)
    assert pair_slippage_blocked(store, "BTC/USDT", 0.003, now=NOW) == SLIPPAGE_BLOCKED
    assert SLIPPAGE_BLOCKED == "slippage realizado acima do orçamento"
    assert pair_slippage_blocked(store, "BTC/USDT", 0.004, now=NOW) is None
    assert pair_slippage_blocked(store, "BTC/USDT", 0.0, now=NOW) is None  # budget 0 is off
    assert pair_slippage_blocked(store, "ETH/USDT", 0.003, now=NOW) is None

    # Only the last 20 count: 20 clean fills push the slipped ones out.
    for index in range(20):
        _trade(store, NOW + timedelta(minutes=index + 1), entry_slippage=0.0, exit_slippage=0.0)
    assert pair_slippage_blocked(store, "BTC/USDT", 0.003, now=NOW + timedelta(hours=1)) is None


def test_the_slippage_block_needs_10_measured_trades(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for index in range(9):
        _trade(store, NOW - timedelta(hours=index), entry_slippage=0.01, exit_slippage=0.01)
    _trade(store, NOW - timedelta(hours=10), entry_slippage=0.01, exit_slippage=None)
    assert pair_slippage_blocked(store, "BTC/USDT", 0.003, now=NOW) is None
    _trade(store, NOW - timedelta(hours=11), entry_slippage=0.01, exit_slippage=0.01)
    assert pair_slippage_blocked(store, "BTC/USDT", 0.003, now=NOW) == SLIPPAGE_BLOCKED


def test_the_slippage_block_ages_out_after_30_days(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for index in range(20):
        _trade(store, NOW - timedelta(days=25, hours=index), entry_slippage=0.003, exit_slippage=0.003)
    assert pair_slippage_blocked(store, "BTC/USDT", 0.003, now=NOW) == SLIPPAGE_BLOCKED
    assert pair_slippage_blocked(store, "BTC/USDT", 0.003, now=NOW + timedelta(days=6)) is None

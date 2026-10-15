from __future__ import annotations

from bot.decay import apply_decay
from bot.store import Store


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

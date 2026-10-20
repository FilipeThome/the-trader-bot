from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import pytest

from bot.store import Store


def test_store_survives_restart(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    store = Store(path)
    store.set_state("running", True)
    store.replace_opportunities(
        [
            {
                "symbol": "BTC/USDT",
                "strategy": "trend",
                "action": "Comprar",
                "score": 0.82,
                "net_return": 0.024,
                "risk_pct": 0.011,
                "status": "pronta",
                "reason": "rompimento",
            }
        ]
    )
    position_id = store.open_pending("BTC/USDT", "trend")
    assert position_id is not None
    store.update_position(position_id, status="open", qty=0.01, avg_price=100.0)
    store.close()

    restored = Store(path)
    assert restored.get_state("running") is True
    assert restored.opportunities()[0]["symbol"] == "BTC/USDT"
    assert restored.open_positions()[0]["qty"] == 0.01
    restored.close()


def test_one_live_position_per_pair_and_many_closed(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for _ in range(2):
        position_id = store.open_pending("ETH/USDT", "trend")
        assert position_id is not None
        assert store.open_pending("ETH/USDT", "trend") is None
        store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
        store.close_position(position_id, exit_price=110.0, realized_pnl=10.0, fees=0.2)
    assert store.open_positions() == []
    assert store.strategy_daily_returns("trend", 1000.0) == [0.02]


def test_v1_positions_table_is_migrated_with_its_rows(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            avg_price REAL NOT NULL,
            stop_price REAL,
            target_price REAL,
            trailing INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            client_order_id TEXT,
            UNIQUE(symbol, strategy, status)
        );
        INSERT INTO positions VALUES (7, 'BTC/USDT', 'trend', 'buy', 0.5, 100, 95, 110, 0, 't', 't', 'open', 0, 'abc');
        INSERT INTO positions VALUES (8, 'BTC/USDT', 'trend', 'buy', 0.5, 90, 85, 99, 1, 't', 't', 'closed', 3, 'def');
        """
    )
    connection.commit()
    connection.close()

    store = Store(path)
    rows = {row["id"]: row for row in (store.position(7), store.position(8)) if row}
    # The v1 trailing flag survives as the exit policy.
    assert rows[7]["exit_policy"] == "target" and rows[8]["exit_policy"] == "trailing"
    assert rows[7]["entry_client_order_id"] == "abc" and rows[7]["status"] == "open"
    assert rows[8]["realized_pnl"] == 3
    # The live-slot index now holds: a second open position for the pair is refused.
    assert store.open_pending("BTC/USDT", "trend") is None
    store.close()
    Store(path).close()


def test_a_v2_table_gains_the_trailing_columns(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    store = Store(path)
    store.close()
    connection = sqlite3.connect(path)
    connection.executescript(
        "ALTER TABLE positions DROP COLUMN peak_price; ALTER TABLE positions DROP COLUMN trail_pct;"
        "ALTER TABLE positions DROP COLUMN exit_policy; ALTER TABLE positions DROP COLUMN entry_win_rate;"
        "INSERT INTO positions (symbol, strategy, side, qty, avg_price, stop_price, opened_at, updated_at, status)"
        " VALUES ('BTC/USDT', 'trend', 'buy', 1, 100, 98, 't', 't', 'open');"
    )
    connection.commit()
    connection.close()
    migrated = Store(path)
    row = migrated.open_positions()[0]
    assert row["peak_price"] is None and row["trail_pct"] is None and row["exit_policy"] is None
    assert row["entry_win_rate"] is None and row["stop_price"] == 98
    migrated.update_position(row["id"], peak_price=101.0, trail_pct=0.02)
    migrated.close()
    Store(path).close()


def test_strategy_outcomes_count_only_closes_of_the_same_exit_policy(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for pnl, policy in ((5.0, "trailing"), (-2.0, "trailing"), (1.0, "trailing"), (9.0, "target"), (4.0, None)):
        position_id = store.open_pending("BTC/USDT", "trend", exit_policy=policy)
        assert position_id is not None
        store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
        store.close_position(position_id, exit_price=100.0, realized_pnl=pnl, fees=0.0)
    assert store.strategy_outcomes("trend", "trailing") == (2, 3)
    assert store.strategy_outcomes("trend", "target") == (1, 1)
    assert store.strategy_outcomes("mean_reversion", "trailing") == (0, 0)



def test_test_positions_stay_out_of_hit_rate_and_decay(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for pnl, test in ((5.0, False), (7.0, True)):
        position_id = store.open_pending("BTC/USDT", "trend", exit_policy="trailing", test_only=test)
        assert position_id is not None
        store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
        store.close_position(position_id, exit_price=100.0, realized_pnl=pnl, fees=0.0)
    assert store.strategy_outcomes("trend", "trailing") == (1, 1)
    assert store.strategy_daily_returns("trend", 1000.0) == [0.005]


def test_recent_losses_lists_only_closed_losing_positions(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    loser = store.open_pending("BTC/USDT", "trend")
    winner = store.open_pending("ETH/USDT", "trend")
    store.open_pending("SOL/USDT", "trend")
    voided = store.open_pending("XRP/USDT", "trend")
    assert loser and winner and voided
    for opened in (loser, winner):
        store.update_position(opened, status="open", qty=1.0, avg_price=100.0)
    store.close_position(loser, exit_price=99.0, realized_pnl=-1.0, fees=0.0)
    store.close_position(winner, exit_price=101.0, realized_pnl=1.0, fees=0.0)
    store.void_position(voided, "recusada")
    rows = store.recent_losses("2000-01-01T00:00:00+00:00")
    assert [row["symbol"] for row in rows] == ["BTC/USDT"]
    assert store.recent_losses("2999-01-01T00:00:00+00:00") == []



def test_calibration_compares_the_estimate_at_entry_with_the_outcome(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    cases = [("A/USDT", 0.65, 1.0), ("B/USDT", 0.62, -1.0), ("C/USDT", 0.45, -2.0), ("D/USDT", None, 5.0)]
    for symbol, rate, pnl in cases:
        position_id = store.open_pending(symbol, "trend", entry_win_rate=rate)
        assert position_id is not None
        store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
        store.close_position(position_id, exit_price=100.0 + pnl, realized_pnl=pnl, fees=0.0)
    test_row = store.open_pending("E/USDT", "trend", entry_win_rate=0.65, test_only=True)
    assert test_row is not None
    store.update_position(test_row, status="open", qty=1.0, avg_price=100.0)
    store.close_position(test_row, exit_price=110.0, realized_pnl=10.0, fees=0.0)
    rows = {row["bucket"]: row for row in store.calibration()}
    assert set(rows) == {"abaixo de 50%", "60–70%"}
    assert rows["60–70%"]["trades"] == 2 and rows["60–70%"]["realized"] == 0.5
    assert rows["60–70%"]["predicted"] == pytest.approx(0.635)
    assert rows["abaixo de 50%"]["realized"] == 0.0


_ROW = {"strategy": "trend", "decision": "pronta"}


def test_prune_drops_old_history_but_keeps_the_pnl_peak(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    store.record_equity(1500.0, "2026-08-01T00:00:00+00:00", pnl=500.0)
    store.record_equity(1000.0, "2026-10-15T00:00:00+00:00", pnl=0.0)
    store.add_decision({"ts": "2026-08-01T00:00:00+00:00", "symbol": "BTC/USDT", **_ROW, "reason": "old"})
    store.add_decision({"ts": "2026-10-15T00:00:00+00:00", "symbol": "BTC/USDT", **_ROW, "reason": "new"})
    assert store.prune("2026-09-15T00:00:00+00:00") == {"decisions": 1, "signals": 0, "equity": 1, "stop_windows": 0}
    assert [row["reason"] for row in store.decisions()] == ["new"]
    # The drawdown still measures from the old peak.
    assert store.peak_pnl() == 500.0


def test_the_pnl_peak_of_an_upgraded_store_is_seeded_from_the_daily_pnl(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    store = Store(path)
    # The absolute equity peak of older versions includes a capital that may have changed since.
    store.set_state("equity_peak", 1000.0)
    for day, pnl in (("2026-10-01", 30.0), ("2026-10-02", 20.0), ("2026-10-03", -40.0)):
        store.record_day(day, net_pnl=pnl)
    store.close()
    assert Store(path).peak_pnl() == 50.0


def test_a_day_report_reads_every_decision_of_that_day(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for minute in range(30):
        store.add_decision({"ts": f"2026-10-14T10:{minute:02d}:00+00:00", "symbol": "BTC/USDT", **_ROW, "reason": "r"})
    for minute in range(10):
        store.add_decision({"ts": f"2026-10-15T10:{minute:02d}:00+00:00", "symbol": "BTC/USDT", **_ROW, "reason": "r"})
    assert len(store.decisions_on("2026-10-14")) == 30 and len(store.decisions_on("2026-10-15")) == 10


def test_a_table_from_before_day_trades_gains_their_columns(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    Store(path).close()
    connection = sqlite3.connect(path)
    connection.executescript(
        "ALTER TABLE positions DROP COLUMN tp1_done; ALTER TABLE positions DROP COLUMN keep_qty;"
        "ALTER TABLE positions DROP COLUMN exit_deadline;"
        "INSERT INTO positions (symbol, strategy, side, qty, avg_price, stop_price, opened_at, updated_at, status)"
        " VALUES ('BTC/USDT', 'trend', 'buy', 1, 100, 98, 't', 't', 'open');"
    )
    connection.commit()
    connection.close()
    migrated = Store(path)
    row = migrated.open_positions()[0]
    assert row["tp1_done"] == 0 and row["keep_qty"] is None and row["exit_deadline"] is None
    migrated.update_position(row["id"], tp1_done=1, keep_qty=0.5, exit_deadline="2026-10-16T18:00:00+00:00")
    assert migrated.position(row["id"])["keep_qty"] == 0.5


_PLAN_COLUMNS = (
    "timeframe", "plan_stop_pct", "plan_target_pct", "exit_hysteresis", "entry_ref_price", "initial_stop",
    "initial_risk_usd", "mae_price", "mfe_price", "entry_slippage", "exit_slippage", "r_multiple",
)


def test_an_older_table_gains_the_plan_and_execution_columns_and_keeps_its_rows(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    Store(path).close()
    connection = sqlite3.connect(path)
    connection.executescript(
        "".join(f"ALTER TABLE positions DROP COLUMN {name};" for name in _PLAN_COLUMNS)
        + "INSERT INTO positions (symbol, strategy, side, qty, avg_price, stop_price, opened_at, updated_at, status,"
        " exit_policy, realized_pnl) VALUES ('BTC/USDT', 'trend', 'buy', 1, 100, 98, 't', 't', 'open', 'trailing', 0);"
    )
    connection.commit()
    connection.close()
    migrated = Store(path)
    row = migrated.open_positions()[0]
    assert all(row[name] is None for name in _PLAN_COLUMNS)
    assert (row["qty"], row["avg_price"], row["stop_price"], row["exit_policy"]) == (1, 100, 98, "trailing")
    migrated.update_position(row["id"], mae_price=97.5, mfe_price=104.0, entry_slippage=0.001, r_multiple=1.5)
    updated = migrated.position(row["id"])
    assert updated is not None and (updated["mae_price"], updated["r_multiple"]) == (97.5, 1.5)
    migrated.close()
    Store(path).close()


def test_open_pending_records_the_entry_plan(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    planned = store.open_pending(
        "BTC/USDT", "trend", timeframe="4h", plan_stop_pct=0.02, plan_target_pct=0.05, exit_hysteresis=True
    )
    legacy = store.open_pending("ETH/USDT", "trend")
    off = store.open_pending("SOL/USDT", "trend", exit_hysteresis=False)
    assert planned and legacy and off
    row, old, without = store.position(planned), store.position(legacy), store.position(off)
    assert row is not None and old is not None and without is not None
    assert (row["timeframe"], row["plan_stop_pct"], row["plan_target_pct"], row["exit_hysteresis"]) == ("4h", 0.02, 0.05, 1)
    assert (old["timeframe"], old["plan_stop_pct"], old["plan_target_pct"], old["exit_hysteresis"]) == (None,) * 4
    assert without["exit_hysteresis"] == 0


def _trial_row(passed: bool, sharpe: float, **overrides: object) -> dict[str, object]:
    return {
        "strategy_id": "trend", "timeframe": "1h", "exit_policy": "trailing", "pairset": "a1b2c3d4e5f6",
        "market_filter": "off", "hysteresis": True, "sharpe_trade": sharpe, "trades": 40, "passed": passed,
        **overrides,
    }


def test_trials_since_pass_lists_the_runs_after_the_last_pass(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    store.add_trial(_trial_row(False, 0.1))
    assert [row["sharpe_trade"] for row in store.trials_since_pass("trend")] == [0.1]
    store.add_trial(_trial_row(True, 0.3))
    assert store.trials_since_pass("trend") == []
    store.add_trial(_trial_row(False, 0.2, timeframe="4h", payload={"dsr": 0.4}))
    store.add_trial(_trial_row(False, float("nan"), hysteresis=False))
    store.add_trial(_trial_row(True, 0.9, strategy_id="momentum"))
    rows = store.trials_since_pass("trend")
    assert [row["timeframe"] for row in rows] == ["4h", "1h"]
    # A Sharpe that could not be measured is stored as 0.
    assert [row["sharpe_trade"] for row in rows] == [0.2, 0.0]
    assert [row["hysteresis"] for row in rows] == [True, False] and not any(row["passed"] for row in rows)
    assert rows[0]["payload"] == {"dsr": 0.4} and rows[1]["payload"]["pairset"] == "a1b2c3d4e5f6"
    assert store.trials_since_pass("momentum") == []
    with pytest.raises(ValueError):
        store.add_trial({"strategy_id": "trend"})


def test_trials_survive_prune(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    store.add_trial(_trial_row(False, 0.1, ts="2020-01-01T00:00:00+00:00"))
    store.prune("2026-01-01T00:00:00+00:00")
    assert len(store.trials_since_pass("trend")) == 1


def test_stop_window_stats_mean_p95_max_by_kind(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    empty = store.stop_window_stats("2000-01-01T00:00:00+00:00")
    assert empty == {"count": 0, "mean_ms": None, "p95_ms": None, "max_ms": None, "by_kind": {}}
    for ms in (100.0, 200.0, 300.0, 400.0):
        store.record_stop_window("trail", "BTC/USDT", 1, ms)
    store.record_stop_window("tp1", "ETH/USDT", 2, 1000.0)
    store.record_stop_window("restop", "ETH/USDT", None, 50.0, ts="2026-01-01T00:00:00+00:00")
    stats = store.stop_window_stats("2026-06-01T00:00:00+00:00")
    assert stats["count"] == 5 and stats["max_ms"] == 1000.0
    assert stats["mean_ms"] == pytest.approx(400.0)
    # Linear interpolation over [100, 200, 300, 400, 1000] at position 3.8.
    assert stats["p95_ms"] == pytest.approx(880.0)
    assert stats["by_kind"] == {
        "tp1": {"count": 1, "mean_ms": 1000.0, "max_ms": 1000.0},
        "trail": {"count": 4, "mean_ms": 250.0, "max_ms": 400.0},
    }
    assert store.stop_window_stats("2000-01-01T00:00:00+00:00")["by_kind"]["restop"]["count"] == 1
    assert store.prune("2026-06-01T00:00:00+00:00")["stop_windows"] == 1
    assert store.stop_window_stats("2000-01-01T00:00:00+00:00")["count"] == 5


def _closed(store: Store, symbol: str, pnl: float, *, strategy: str = "trend", **kwargs: Any) -> int:
    position_id = store.open_pending(symbol, strategy, **kwargs)
    assert position_id is not None
    store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
    store.close_position(position_id, exit_price=100.0 + pnl, realized_pnl=pnl, fees=0.0)
    return position_id


def test_closed_trades_filter_and_leave_out_test_and_instance_rows(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    first = _closed(store, "BTC/USDT", 1.0, exit_policy="trailing")
    second = _closed(store, "ETH/USDT", -1.0, exit_policy="target")
    third = _closed(store, "BTC/USDT", 2.0, exit_policy="trailing")
    _closed(store, "SOL/USDT", 5.0, strategy="momentum", exit_policy="trailing")
    _closed(store, "BTC/USDT", 9.0, exit_policy="trailing", test_only=True)
    _closed(store, "XRP/USDT", 9.0, strategy="dca", instance_id=1)
    store.open_pending("BNB/USDT", "trend")
    assert [row["id"] for row in store.closed_trades(strategy="trend")] == [third, second, first]
    assert [row["id"] for row in store.closed_trades(strategy="trend", symbol="BTC/USDT")] == [third, first]
    assert [row["id"] for row in store.closed_trades(exit_policy="target")] == [second]
    assert [row["id"] for row in store.closed_trades(strategy="trend", limit=2)] == [third, second]
    assert len(store.closed_trades()) == 4
    assert store.closed_trades(since="2999-01-01T00:00:00+00:00") == []
    assert len(store.closed_trades(since="2000-01-01T00:00:00+00:00")) == 4


def test_calendar_daily_returns_are_zero_filled_utc_days_ending_today(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    closes = (
        (50.0, "2026-10-10T12:00:00+00:00"),
        (10.0, "2026-10-18T09:00:00+00:00"),
        (6.0, "2026-10-20T01:00:00+00:00"),
        (-4.0, "2026-10-20T23:59:00+00:00"),
        (70.0, "2026-10-21T00:30:00+00:00"),
    )
    for pnl, closed_at in closes:
        position_id = _closed(store, "BTC/USDT", pnl)
        store._conn.execute("UPDATE positions SET closed_at=? WHERE id=?", (closed_at, position_id))
    store._conn.commit()
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    assert store.strategy_daily_returns("trend", 1000.0, calendar_days=4, now=now) == pytest.approx(
        [0.0, 0.01, 0.0, 0.002]
    )
    assert len(store.strategy_daily_returns("trend", 1000.0, calendar_days=30, now=now)) == 30
    assert store.strategy_daily_returns("momentum", 1000.0, calendar_days=3, now=now) == [0.0, 0.0, 0.0]
    # Without calendar days: only the days that closed a trade, as before.
    assert store.strategy_daily_returns("trend", 1000.0) == pytest.approx([0.05, 0.01, 0.002, 0.07])
    with pytest.raises(ValueError):
        store.strategy_daily_returns("trend", 1000.0, calendar_days=0)

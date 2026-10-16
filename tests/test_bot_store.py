from __future__ import annotations

import sqlite3

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


def test_prune_drops_old_history_but_keeps_the_equity_peak(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    store.record_equity(1500.0, "2026-08-01T00:00:00+00:00")
    store.record_equity(1000.0, "2026-10-15T00:00:00+00:00")
    store.add_decision({"ts": "2026-08-01T00:00:00+00:00", "symbol": "BTC/USDT", **_ROW, "reason": "old"})
    store.add_decision({"ts": "2026-10-15T00:00:00+00:00", "symbol": "BTC/USDT", **_ROW, "reason": "new"})
    assert store.prune("2026-09-15T00:00:00+00:00") == {"decisions": 1, "signals": 0, "equity": 1}
    assert [row["reason"] for row in store.decisions()] == ["new"]
    # The drawdown still measures from the old peak.
    assert store.peak_equity() == 1500.0


def test_the_peak_of_an_upgraded_store_is_seeded_from_its_equity_table(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    store = Store(path)
    store._conn.execute("INSERT INTO equity(ts, equity) VALUES('2026-10-01T00:00:00+00:00', 1234.0)")
    store._conn.commit()
    store.close()
    assert Store(path).peak_equity() == 1234.0


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

"""SQLite store. One file per environment, under that environment's runtime root.

The tick thread and the API threads share one connection, so every method
holds the store lock. A position moves ``pending -> open -> closed`` or
``pending -> void``; at most one live (``pending`` or ``open``) position exists
per pair and strategy.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from bot.paths import get_runtime_root

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction REAL NOT NULL,
    confidence REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    score REAL NOT NULL,
    net_return REAL NOT NULL,
    risk_pct REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    data_age_seconds REAL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,
    stop_price REAL,
    target_price REAL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    entry_client_order_id TEXT,
    stop_order_id TEXT,
    stop_client_order_id TEXT,
    stop_attempts INTEGER NOT NULL DEFAULT 0,
    exit_attempts INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    exit_price REAL,
    fees REAL NOT NULL DEFAULT 0,
    reason TEXT,
    peak_price REAL,
    trail_pct REAL,
    exit_policy TEXT,
    test_only INTEGER NOT NULL DEFAULT 0,
    entry_win_rate REAL,
    tp1_done INTEGER NOT NULL DEFAULT 0,
    keep_qty REAL,
    exit_deadline TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS positions_live ON positions(symbol, strategy)
    WHERE status IN ('pending', 'open');
CREATE TABLE IF NOT EXISTS pnl_daily (
    day TEXT PRIMARY KEY,
    net_pnl REAL NOT NULL,
    gross_pnl REAL NOT NULL,
    fees REAL NOT NULL,
    slippage REAL NOT NULL,
    funding REAL NOT NULL,
    trades INTEGER NOT NULL,
    violations_avoided INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS ignored (
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    until TEXT NOT NULL,
    PRIMARY KEY(symbol, strategy)
);
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_stats (
    strategy_id TEXT PRIMARY KEY,
    backtest_sharpe REAL,
    backtest_drawdown REAL,
    validated_at TEXT,
    live_sharpe REAL,
    signal_only INTEGER NOT NULL DEFAULT 1,
    params TEXT NOT NULL
);
"""

#: Columns ``update_position`` may change. Identity and timestamps stay with the store.
_POSITION_FIELDS = frozenset(
    {
        "side",
        "qty",
        "avg_price",
        "stop_price",
        "target_price",
        "status",
        "realized_pnl",
        "entry_client_order_id",
        "stop_order_id",
        "stop_client_order_id",
        "stop_attempts",
        "exit_attempts",
        "exit_price",
        "fees",
        "reason",
        "peak_price",
        "trail_pct",
        "tp1_done",
        "keep_qty",
        "exit_deadline",
    }
)

#: Columns added after v2. ``_add_missing_columns`` adds them to an older table.
_ADDED_POSITION_COLUMNS = (
    ("peak_price", "REAL"),
    ("trail_pct", "REAL"),
    ("exit_policy", "TEXT"),
    ("test_only", "INTEGER NOT NULL DEFAULT 0"),
    ("entry_win_rate", "REAL"),
    # Day trades: the first target was sold, the quantity a partial sale keeps, and the time limit.
    ("tp1_done", "INTEGER NOT NULL DEFAULT 0"),
    ("keep_qty", "REAL"),
    ("exit_deadline", "TEXT"),
)

#: Buckets of the estimated hit rate at entry; manual entries have no floor, hence below 50%.
_CALIBRATION_BUCKETS = (
    ("abaixo de 50%", 0.0, 0.5),
    ("50–60%", 0.5, 0.6),
    ("60–70%", 0.6, 0.7),
    ("70–80%", 0.7, 0.8),
    ("80–100%", 0.8, 1.01),
)

_F = TypeVar("_F", bound=Callable[..., Any])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _locked(method: _F) -> _F:
    @functools.wraps(method)
    def wrapper(self: "Store", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(_F, wrapper)


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_runtime_root() / "bot.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # The validation thread opens a second connection; a writer waits for it instead of failing.
        # Rollback journal on purpose: WAL needs shared memory a Docker Desktop bind mount does not
        # guarantee, and a copy of the database alone would miss the -wal file.
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._migrate_positions()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Add nullable columns a v2 ``positions`` table lacks. Additive and idempotent."""
        present = {row["name"] for row in self._conn.execute("PRAGMA table_info(positions)").fetchall()}
        missing = [(name, kind) for name, kind in _ADDED_POSITION_COLUMNS if name not in present]
        if not missing:
            return
        previous = self._conn.isolation_level
        self._conn.isolation_level = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for name, kind in missing:
                    self._conn.execute(f"ALTER TABLE positions ADD COLUMN {name} {kind}")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            self._conn.isolation_level = previous

    def _migrate_positions(self) -> None:
        """Rebuild a v1 ``positions`` table (one row per pair, strategy and status).

        v1 forbade a second closed position for the same pair and strategy. The
        rebuild keeps every row and its ``id``, maps ``client_order_id`` to
        ``entry_client_order_id`` and the v1 ``trailing`` flag to ``exit_policy``
        (``trailing`` or ``target``), so no column's information is lost. It runs
        in one transaction; a failure leaves the v1 table untouched.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
        ).fetchone()
        if row is None or "entry_client_order_id" in str(row["sql"]):
            return
        previous = self._conn.isolation_level
        self._conn.isolation_level = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("ALTER TABLE positions RENAME TO positions_v1")
                for statement in _position_ddl():
                    self._conn.execute(statement)
                present = {row["name"] for row in self._conn.execute("PRAGMA table_info(positions)").fetchall()}
                for name, kind in _ADDED_POSITION_COLUMNS:
                    if name not in present:
                        self._conn.execute(f"ALTER TABLE positions ADD COLUMN {name} {kind}")
                self._conn.execute(
                    """INSERT INTO positions
                    (id, symbol, strategy, side, qty, avg_price, stop_price, target_price,
                     opened_at, updated_at, status, realized_pnl, entry_client_order_id, exit_policy)
                    SELECT id, symbol, strategy, side, qty, avg_price, stop_price, target_price,
                           opened_at, updated_at, status, realized_pnl, client_order_id,
                           CASE WHEN trailing = 1 THEN 'trailing' ELSE 'target' END
                    FROM positions_v1"""
                )
                self._conn.execute("DROP TABLE positions_v1")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            self._conn.isolation_level = previous

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @_locked
    def set_state(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    @_locked
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    @_locked
    def replace_opportunities(self, rows: list[dict[str, Any]]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM opportunities")
            for row in rows:
                self._conn.execute(
                    """INSERT INTO opportunities
                    (ts, symbol, strategy, action, score, net_return, risk_pct, status, reason, data_age_seconds, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row.get("ts") or _now(),
                        row["symbol"],
                        row["strategy"],
                        row["action"],
                        row["score"],
                        row["net_return"],
                        row["risk_pct"],
                        row["status"],
                        row["reason"],
                        row.get("data_age_seconds"),
                        json.dumps(row),
                    ),
                )

    @_locked
    def opportunities(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM opportunities ORDER BY score DESC"
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    @_locked
    def add_signal(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO signals(ts, symbol, strategy, timeframe, direction, confidence, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("ts") or _now(),
                row["symbol"],
                row["strategy"],
                row.get("timeframe", ""),
                row.get("direction", 0),
                row.get("confidence", 0),
                json.dumps(row),
            ),
        )
        self._conn.commit()

    @_locked
    def add_decision(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO decisions(ts, symbol, strategy, decision, reason, manual, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("ts") or _now(),
                row.get("symbol", ""),
                row.get("strategy", ""),
                row["decision"],
                row.get("reason", ""),
                1 if row.get("manual") else 0,
                json.dumps(row, default=str),
            ),
        )
        self._conn.commit()

    @_locked
    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    @_locked
    def decisions_on(self, day: str) -> list[dict[str, Any]]:
        """Every decision recorded on a UTC day (``YYYY-MM-DD``), oldest first."""
        rows = self._conn.execute(
            "SELECT payload FROM decisions WHERE ts >= ? AND ts < ? ORDER BY id", (day, day + "T99")
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    @_locked
    def prune(self, before: str) -> dict[str, int]:
        """Delete decisions, signals and equity points older than ``before`` (ISO time).

        The equity peak lives in ``bot_state``, so old equity points are not needed to
        measure the drawdown. Positions and daily PnL are kept.
        """
        self._peak_from_table()
        with self._conn:
            decisions = self._conn.execute("DELETE FROM decisions WHERE ts < ?", (before,)).rowcount
            signals = self._conn.execute("DELETE FROM signals WHERE ts < ?", (before,)).rowcount
            equity = self._conn.execute("DELETE FROM equity WHERE ts < ?", (before,)).rowcount
        return {"decisions": decisions, "signals": signals, "equity": equity}

    @_locked
    def ignore(self, symbol: str, strategy: str, until: str) -> None:
        self._conn.execute(
            """INSERT INTO ignored(symbol, strategy, until) VALUES(?, ?, ?)
            ON CONFLICT(symbol, strategy) DO UPDATE SET until=excluded.until""",
            (symbol, strategy, until),
        )
        self._conn.commit()

    @_locked
    def ignored_keys(self, now: datetime | None = None) -> set[tuple[str, str]]:
        moment = (now or datetime.now(timezone.utc)).isoformat()
        rows = self._conn.execute(
            "SELECT symbol, strategy FROM ignored WHERE until > ?", (moment,)
        ).fetchall()
        return {(row["symbol"], row["strategy"]) for row in rows}

    # -- positions ---------------------------------------------------------

    @_locked
    def open_pending(
        self,
        symbol: str,
        strategy: str,
        *,
        stop_price: float | None = None,
        target_price: float | None = None,
        exit_policy: str | None = None,
        test_only: bool = False,
        entry_win_rate: float | None = None,
        exit_deadline: str | None = None,
    ) -> int | None:
        """Reserve a live slot before the entry order. ``None`` when one already exists.

        ``entry_win_rate`` is the hit rate the strategy was estimated at when it
        entered; ``calibration`` compares it with how the trades ended.
        """
        stamp = _now()
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """INSERT INTO positions
                    (symbol, strategy, side, qty, avg_price, stop_price, target_price,
                     opened_at, updated_at, status, exit_policy, test_only, entry_win_rate, exit_deadline)
                    VALUES (?, ?, 'buy', 0, 0, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                    (
                        symbol, strategy, stop_price, target_price, stamp, stamp, exit_policy,
                        1 if test_only else 0, entry_win_rate, exit_deadline,
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return int(cursor.lastrowid or 0) or None

    @_locked
    def update_position(self, position_id: int, **fields: Any) -> None:
        unknown = set(fields) - _POSITION_FIELDS
        if unknown:
            raise ValueError(f"unknown position fields: {sorted(unknown)}")
        if not fields:
            return
        columns = ", ".join(f"{name}=?" for name in fields)
        with self._conn:
            self._conn.execute(
                f"UPDATE positions SET {columns}, updated_at=? WHERE id=?",
                (*fields.values(), _now(), position_id),
            )

    @_locked
    def position(self, position_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        return dict(row) if row is not None else None

    @_locked
    def open_positions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at, id"
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def calibration(self) -> list[dict[str, Any]]:
        """Estimated hit rate at entry against how the trades ended, by bucket.

        Closed, non-test positions that recorded an estimate. A bucket whose
        realized win share sits well below its predicted rate says the estimate
        is too optimistic for automatic entries.
        """
        rows = self._conn.execute(
            """SELECT entry_win_rate, realized_pnl FROM positions
            WHERE status='closed' AND test_only=0 AND entry_win_rate IS NOT NULL"""
        ).fetchall()
        out: list[dict[str, Any]] = []
        for label, low, high in _CALIBRATION_BUCKETS:
            inside = [row for row in rows if low <= float(row["entry_win_rate"]) < high]
            if not inside:
                continue
            wins = sum(1 for row in inside if float(row["realized_pnl"]) > 0)
            out.append(
                {
                    "bucket": label,
                    "trades": len(inside),
                    "predicted": sum(float(row["entry_win_rate"]) for row in inside) / len(inside),
                    "realized": wins / len(inside),
                }
            )
        return out

    @_locked
    def recent_losses(self, since: str) -> list[dict[str, Any]]:
        """Closed positions with a realized loss, closed at or after ``since`` (ISO time)."""
        rows = self._conn.execute(
            """SELECT symbol, closed_at, realized_pnl FROM positions
            WHERE status='closed' AND realized_pnl < 0 AND closed_at IS NOT NULL AND closed_at >= ?
            ORDER BY closed_at""",
            (since,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def live_positions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status IN ('pending', 'open') ORDER BY opened_at, id"
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def close_position(self, position_id: int, *, exit_price: float, realized_pnl: float, fees: float) -> None:
        stamp = _now()
        with self._conn:
            self._conn.execute(
                """UPDATE positions SET status='closed', exit_price=?, realized_pnl=?, fees=?,
                closed_at=?, updated_at=?, stop_order_id=NULL, stop_client_order_id=NULL
                WHERE id=? AND status='open'""",
                (exit_price, realized_pnl, fees, stamp, stamp, position_id),
            )

    @_locked
    def void_position(self, position_id: int, reason: str) -> None:
        stamp = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE positions SET status='void', reason=?, closed_at=?, updated_at=? WHERE id=? AND status='pending'",
                (reason[:500], stamp, stamp, position_id),
            )

    @_locked
    def strategy_outcomes(self, strategy_id: str, exit_policy: str) -> tuple[int, int]:
        """(winning, total) closed positions of ``strategy_id`` opened under ``exit_policy``.

        A win has positive realized PnL. Positions without a recorded policy
        (opened before it was recorded) and test-mode positions are left out.
        """
        row = self._conn.execute(
            """SELECT SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins, COUNT(*) AS total
            FROM positions WHERE strategy=? AND status='closed' AND exit_policy=? AND test_only=0""",
            (strategy_id, exit_policy),
        ).fetchone()
        return int(row["wins"] or 0), int(row["total"] or 0)

    @_locked
    def strategy_daily_returns(self, strategy_id: str, capital: float) -> list[float]:
        """Realized PnL per UTC close day as a fraction of ``capital``, oldest first."""
        if capital <= 0:
            return []
        rows = self._conn.execute(
            """SELECT substr(closed_at, 1, 10) AS day, SUM(realized_pnl) AS pnl
            FROM positions WHERE strategy=? AND status='closed' AND closed_at IS NOT NULL AND test_only=0
            GROUP BY day ORDER BY day""",
            (strategy_id,),
        ).fetchall()
        return [float(row["pnl"]) / capital for row in rows]

    # -- daily PnL and equity ----------------------------------------------

    @_locked
    def record_day(self, day: str, **fields: float) -> None:
        with self._conn:
            current = self._conn.execute("SELECT * FROM pnl_daily WHERE day=?", (day,)).fetchone()
            base = {
                "net_pnl": 0.0,
                "gross_pnl": 0.0,
                "fees": 0.0,
                "slippage": 0.0,
                "funding": 0.0,
                "trades": 0,
                "violations_avoided": 0,
            }
            unknown = set(fields) - set(base)
            if unknown:
                raise ValueError(f"unknown pnl fields: {sorted(unknown)}")
            if current is not None:
                base.update({key: current[key] for key in base})
            for key, value in fields.items():
                base[key] = base[key] + value
            self._conn.execute(
                """INSERT INTO pnl_daily(day, net_pnl, gross_pnl, fees, slippage, funding, trades, violations_avoided)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    net_pnl=excluded.net_pnl, gross_pnl=excluded.gross_pnl, fees=excluded.fees,
                    slippage=excluded.slippage, funding=excluded.funding, trades=excluded.trades,
                    violations_avoided=excluded.violations_avoided""",
                (
                    day,
                    base["net_pnl"],
                    base["gross_pnl"],
                    base["fees"],
                    base["slippage"],
                    base["funding"],
                    int(base["trades"]),
                    int(base["violations_avoided"]),
                ),
            )

    @_locked
    def pnl_days(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM pnl_daily ORDER BY day").fetchall()
        return [dict(row) for row in rows]

    @_locked
    def record_equity(self, equity: float, ts: str | None = None) -> None:
        peak = self._peak_from_table()
        with self._conn:
            self._conn.execute(
                "INSERT INTO equity(ts, equity) VALUES(?, ?) ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity",
                (ts or _now(), equity),
            )
            if equity > peak:
                self._conn.execute(
                    "INSERT INTO bot_state(key, value) VALUES('equity_peak', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(equity),),
                )

    @_locked
    def peak_equity(self) -> float:
        """The highest equity ever recorded: kept in ``bot_state``, so pruning old points keeps it."""
        return self._peak_from_table()

    def _peak_from_table(self) -> float:
        """The stored peak; the first call after an upgrade seeds it from ``MAX(equity)``. Caller holds the lock."""
        row = self._conn.execute("SELECT value FROM bot_state WHERE key='equity_peak'").fetchone()
        if row is not None:
            return float(json.loads(row["value"]))
        found = self._conn.execute("SELECT MAX(equity) AS peak FROM equity").fetchone()
        peak = float(found["peak"]) if found is not None and found["peak"] is not None else 0.0
        with self._conn:
            self._conn.execute(
                "INSERT INTO bot_state(key, value) VALUES('equity_peak', ?) ON CONFLICT(key) DO NOTHING",
                (json.dumps(peak),),
            )
        return peak

    @_locked
    def trades_today(self, day: str) -> int:
        row = self._conn.execute("SELECT trades FROM pnl_daily WHERE day=?", (day,)).fetchone()
        return int(row["trades"]) if row else 0

    # -- strategy validation -------------------------------------------------

    @_locked
    def save_strategy(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO strategy_stats
            (strategy_id, backtest_sharpe, backtest_drawdown, validated_at, live_sharpe, signal_only, params)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id) DO UPDATE SET
                backtest_sharpe=excluded.backtest_sharpe,
                backtest_drawdown=excluded.backtest_drawdown,
                validated_at=excluded.validated_at,
                live_sharpe=excluded.live_sharpe,
                signal_only=excluded.signal_only,
                params=excluded.params""",
            (
                row["strategy_id"],
                row.get("backtest_sharpe"),
                row.get("backtest_drawdown"),
                row.get("validated_at"),
                row.get("live_sharpe"),
                1 if row.get("signal_only", True) else 0,
                json.dumps(row.get("params") or {}),
            ),
        )
        self._conn.commit()

    @_locked
    def strategies(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM strategy_stats").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["signal_only"] = bool(item["signal_only"])
            item["params"] = json.loads(item["params"])
            out.append(item)
        return out

    def strategy(self, strategy_id: str) -> dict[str, Any] | None:
        for row in self.strategies():
            if row["strategy_id"] == strategy_id:
                return row
        return None


def _position_ddl() -> list[str]:
    """The ``positions`` table and its live-slot index, as separate statements."""
    start = _SCHEMA.index("CREATE TABLE IF NOT EXISTS positions")
    end = _SCHEMA.index("CREATE TABLE IF NOT EXISTS pnl_daily")
    block = _SCHEMA[start:end]
    return [statement.strip() for statement in block.split(";") if statement.strip()]

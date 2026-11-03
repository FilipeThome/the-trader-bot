"""SQLite store. One file per environment, under that environment's runtime root.

The tick thread and the API threads share one connection, so every method
holds the store lock. A position moves ``pending -> open -> closed`` or
``pending -> void``; at most one live (``pending`` or ``open``) position exists
per pair and strategy.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
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
CREATE TABLE IF NOT EXISTS armed_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    condition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    position_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS armed_live ON armed_entries(symbol, strategy) WHERE status = 'armed';
CREATE TABLE IF NOT EXISTS bot_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    params TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stopped_at TEXT,
    realized_pnl REAL NOT NULL DEFAULT 0,
    reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS instances_live ON bot_instances(kind, symbol) WHERE status IN ('active', 'stopping');
CREATE TABLE IF NOT EXISTS validation_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    exit_policy TEXT NOT NULL,
    pairset TEXT NOT NULL,
    market_filter TEXT NOT NULL,
    hysteresis INTEGER NOT NULL,
    sharpe_trade REAL NOT NULL,
    trades INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS validation_trials_strategy ON validation_trials(strategy_id, id);
CREATE TABLE IF NOT EXISTS stop_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    position_id INTEGER,
    ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stop_windows_ts ON stop_windows(ts);
CREATE TABLE IF NOT EXISTS advice_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    signal_close_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL,
    size_factor REAL NOT NULL,
    regime TEXT,
    risk_flags TEXT NOT NULL DEFAULT '[]',
    rationale TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms REAL,
    entry_price REAL NOT NULL,
    stop_pct REAL NOT NULL,
    target_pct REAL NOT NULL,
    taken INTEGER NOT NULL DEFAULT 0,
    position_id INTEGER,
    counterfactual_r REAL,
    resolved_at TEXT,
    expired_at TEXT,
    fetch_failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS advice_log_open ON advice_log(resolved_at);
CREATE TABLE IF NOT EXISTS advisor_usage_daily (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    confirm INTEGER NOT NULL DEFAULT 0,
    reduce INTEGER NOT NULL DEFAULT 0,
    veto INTEGER NOT NULL DEFAULT 0,
    abstain INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0
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
        "instance_id",
        "pending_leg",
        "timeframe",
        "plan_stop_pct",
        "plan_target_pct",
        "exit_hysteresis",
        "entry_ref_price",
        "initial_stop",
        "initial_risk_usd",
        "mae_price",
        "mfe_price",
        "entry_slippage",
        "exit_slippage",
        "r_multiple",
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
    # DCA and grid inventory: the bot instance it belongs to, and the order in flight, resolved
    # by the sync. JSON with "kind": "buy" (leg, client_order_id, stop), "restop" (stop) or
    # "sell" (leg, qty_before).
    ("instance_id", "INTEGER"),
    ("pending_leg", "TEXT"),
    # The entry plan: the setup's timeframe, its stop and target distances (recomputed on the
    # average fill) and whether the exit rule uses hysteresis. NULL on rows from before them.
    ("timeframe", "TEXT"),
    ("plan_stop_pct", "REAL"),
    ("plan_target_pct", "REAL"),
    ("exit_hysteresis", "INTEGER"),
    # Execution quality: the quote the entry was priced on, the stop and the risk in USD at the
    # fill, the lowest and highest price while open, slippage per leg and the result in R.
    ("entry_ref_price", "REAL"),
    ("initial_stop", "REAL"),
    ("initial_risk_usd", "REAL"),
    ("mae_price", "REAL"),
    ("mfe_price", "REAL"),
    ("entry_slippage", "REAL"),
    ("exit_slippage", "REAL"),
    ("r_multiple", "REAL"),
    # The DeepSeek advisor's verdict and confidence on the row the position entered from.
    ("entry_advice_verdict", "TEXT"),
    ("entry_advice_confidence", "REAL"),
)

#: Columns added to ``advice_log`` after it shipped. ``expired_at``: the row's window and its
#: grace passed without the candles to label it (``calibration.resolve_pending``).
#: ``fetch_failures``: klines reads that failed after that grace.
_ADDED_ADVICE_COLUMNS = (("expired_at", "TEXT"), ("fetch_failures", "INTEGER NOT NULL DEFAULT 0"))
#: One verdict per signal: a restart that asks again finds the logged row instead.
_ADVICE_SIGNAL_KEY = ("symbol", "strategy", "timeframe", "signal_close_at", "prompt_version", "model")

#: Counters of ``advisor_usage_daily`` that ``record_advisor_usage`` adds to.
_USAGE_COLUMNS = (
    "calls", "prompt_tokens", "cache_hit_tokens", "completion_tokens", "cost_usd",
    "confirm", "reduce", "veto", "abstain", "errors",
)
#: Columns of ``advice_log`` that ``add_advice`` writes; the rest start empty.
_ADVICE_COLUMNS = (
    "ts", "mode", "model", "prompt_version", "prompt_sha256", "symbol", "strategy", "timeframe",
    "signal_close_at", "verdict", "confidence", "size_factor", "regime", "risk_flags", "rationale",
    "prompt_tokens", "cache_hit_tokens", "completion_tokens", "cost_usd", "latency_ms",
    "entry_price", "stop_pct", "target_pct",
)

#: Columns of ``validation_trials`` a trial row must give; ``ts`` and ``payload`` are optional.
_TRIAL_COLUMNS = (
    "strategy_id",
    "timeframe",
    "exit_policy",
    "pairset",
    "market_filter",
    "hysteresis",
    "sharpe_trade",
    "trades",
    "passed",
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


def _usage(conn: sqlite3.Connection, since: str, until: str | None) -> dict[str, Any]:
    """``Store.advisor_usage`` on ``conn``."""
    sums = ", ".join(f"COALESCE(SUM({name}), 0) AS {name}" for name in _USAGE_COLUMNS)
    query = f"SELECT {sums} FROM advisor_usage_daily WHERE day >= ?"
    values: list[Any] = [since]
    if until is not None:
        query += " AND day <= ?"
        values.append(until)
    row = conn.execute(query, values).fetchone()
    return {name: (float(row[name]) if name == "cost_usd" else int(row[name])) for name in _USAGE_COLUMNS}


def advisor_usage_at(path: Path, since: str, until: str | None = None) -> dict[str, Any] | None:
    """``Store.advisor_usage`` of the database at ``path``, read only: another mode's store is
    summed without migrating or locking it. None when the file or its usage table is missing."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='advisor_usage_daily'").fetchone() is None:
            return None
        return _usage(conn, since, until)
    finally:
        conn.close()


def _advice_item(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["risk_flags"] = json.loads(item["risk_flags"] or "[]")
    item["taken"] = bool(item["taken"])
    return item


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
        self._add_missing_columns("positions", _ADDED_POSITION_COLUMNS)
        self._add_missing_columns("advice_log", _ADDED_ADVICE_COLUMNS)
        self._unique_advice()

    @functools.cached_property
    def identity(self) -> str:
        """A short, stable name of this database file. The advisor keys its verdicts by it, so a
        verdict asked by one mode's store never gates or links a row of the other mode's."""
        return hashlib.sha256(str(self.path.resolve()).encode("utf-8")).hexdigest()[:16]

    def _add_missing_columns(self, table: str, added: tuple[tuple[str, str], ...]) -> None:
        """Add nullable columns an older ``table`` lacks. Additive and idempotent."""
        present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = [(name, kind) for name, kind in added if name not in present]
        if not missing:
            return
        previous = self._conn.isolation_level
        self._conn.isolation_level = None
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for name, kind in missing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            self._conn.isolation_level = previous

    def _unique_advice(self) -> None:
        """Give ``advice_log`` its unique signal key (``_ADVICE_SIGNAL_KEY``).

        A table from before the key may hold the same signal twice (a restart asked again). The
        oldest row of each signal stays; it takes over a later duplicate's link to a position, so
        the realized column keeps it. Runs once: afterwards the index exists.
        """
        exists = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='advice_log_signal'").fetchone()
        if exists is not None:
            return
        key = ", ".join(_ADVICE_SIGNAL_KEY)
        with self._conn:
            groups = self._conn.execute(
                f"SELECT {key}, MIN(id) AS keep FROM advice_log GROUP BY {key} HAVING COUNT(*) > 1"
            ).fetchall()
            match = " AND ".join(f"{name}=?" for name in _ADVICE_SIGNAL_KEY)
            for group in groups:
                values = [group[name] for name in _ADVICE_SIGNAL_KEY]
                keep = int(group["keep"])
                linked = self._conn.execute(
                    f"SELECT position_id FROM advice_log WHERE {match} AND id<>? AND taken=1 ORDER BY id LIMIT 1",
                    (*values, keep),
                ).fetchone()
                if linked is not None:
                    self._conn.execute(
                        "UPDATE advice_log SET taken=1, position_id=? WHERE id=? AND taken=0", (linked["position_id"], keep)
                    )
                self._conn.execute(f"DELETE FROM advice_log WHERE {match} AND id<>?", (*values, keep))
            self._conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS advice_log_signal ON advice_log({key})")

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
        """Delete decisions, signals, equity points and stop windows older than ``before`` (ISO time).

        The PnL peak lives in ``bot_state``, so old equity points are not needed to
        measure the drawdown. Positions, daily PnL and validation trials are kept: the
        deflated Sharpe counts every trial since the last pass, however old.
        """
        with self._conn:
            decisions = self._conn.execute("DELETE FROM decisions WHERE ts < ?", (before,)).rowcount
            signals = self._conn.execute("DELETE FROM signals WHERE ts < ?", (before,)).rowcount
            equity = self._conn.execute("DELETE FROM equity WHERE ts < ?", (before,)).rowcount
            windows = self._conn.execute("DELETE FROM stop_windows WHERE ts < ?", (before,)).rowcount
        return {"decisions": decisions, "signals": signals, "equity": equity, "stop_windows": windows}

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
        instance_id: int | None = None,
        timeframe: str | None = None,
        plan_stop_pct: float | None = None,
        plan_target_pct: float | None = None,
        exit_hysteresis: bool | None = None,
        entry_advice_verdict: str | None = None,
        entry_advice_confidence: float | None = None,
    ) -> int | None:
        """Reserve a live slot before the entry order. ``None`` when one already exists.

        ``entry_win_rate`` is the hit rate the strategy was estimated at when it
        entered; ``calibration`` compares it with how the trades ended.
        ``plan_stop_pct`` and ``plan_target_pct`` are the setup's distances from the
        entry, for the stop and target set on the average fill. ``entry_advice_*`` is the
        DeepSeek advisor's verdict on the row, when it had one.
        """
        stamp = _now()
        hysteresis = None if exit_hysteresis is None else int(bool(exit_hysteresis))
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """INSERT INTO positions
                    (symbol, strategy, side, qty, avg_price, stop_price, target_price,
                     opened_at, updated_at, status, exit_policy, test_only, entry_win_rate, exit_deadline, instance_id,
                     timeframe, plan_stop_pct, plan_target_pct, exit_hysteresis,
                     entry_advice_verdict, entry_advice_confidence)
                    VALUES (?, ?, 'buy', 0, 0, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        symbol, strategy, stop_price, target_price, stamp, stamp, exit_policy,
                        1 if test_only else 0, entry_win_rate, exit_deadline, instance_id,
                        timeframe, plan_stop_pct, plan_target_pct, hysteresis,
                        entry_advice_verdict, entry_advice_confidence,
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
        """Closed positions with a realized loss, closed at or after ``since`` (ISO time), with the
        entry risk that turns the loss into R for the loss guard."""
        rows = self._conn.execute(
            """SELECT symbol, closed_at, realized_pnl, initial_risk_usd FROM positions
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
    def closed_trades(
        self,
        *,
        strategy: str | None = None,
        symbol: str | None = None,
        exit_policy: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Closed strategy trades, newest first: test-mode and DCA/grid rows are left out.

        ``since`` (ISO time) keeps the trades closed at or after it; ``limit`` keeps the newest ones.
        """
        clauses = ["status='closed'", "test_only=0", "instance_id IS NULL", "closed_at IS NOT NULL"]
        values: list[Any] = []
        for column, value in (("strategy", strategy), ("symbol", symbol), ("exit_policy", exit_policy)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        if since is not None:
            clauses.append("closed_at >= ?")
            values.append(since)
        query = f"SELECT * FROM positions WHERE {' AND '.join(clauses)} ORDER BY closed_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            values.append(max(0, int(limit)))
        return [dict(row) for row in self._conn.execute(query, values).fetchall()]

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
    def record_equity(self, equity: float, ts: str | None = None, *, pnl: float | None = None) -> None:
        """Record an equity point for the chart and, with ``pnl``, raise the PnL peak.

        The drawdown is measured on the cumulative PnL (closed days plus open
        positions), not on the equity: the equity includes the configured capital,
        so lowering the capital would read as a loss and raising it would hide one.
        """
        peak = self._pnl_peak()
        with self._conn:
            self._conn.execute(
                "INSERT INTO equity(ts, equity) VALUES(?, ?) ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity",
                (ts or _now(), equity),
            )
            if pnl is not None and pnl > peak:
                self._conn.execute(
                    "INSERT INTO bot_state(key, value) VALUES('pnl_peak', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(pnl),),
                )

    @_locked
    def peak_pnl(self) -> float:
        """The highest cumulative PnL ever recorded, at least 0: kept in ``bot_state``, so pruning keeps it."""
        return self._pnl_peak()

    def _pnl_peak(self) -> float:
        """The stored PnL peak. The first call seeds it from the running sum of the daily PnL,
        because the absolute equity peak of older versions mixed in the capital. Caller holds the lock."""
        row = self._conn.execute("SELECT value FROM bot_state WHERE key='pnl_peak'").fetchone()
        if row is not None:
            return float(json.loads(row["value"]))
        peak = running = 0.0
        for day in self._conn.execute("SELECT net_pnl FROM pnl_daily ORDER BY day").fetchall():
            running += float(day["net_pnl"] or 0)
            peak = max(peak, running)
        with self._conn:
            self._conn.execute(
                "INSERT INTO bot_state(key, value) VALUES('pnl_peak', ?) ON CONFLICT(key) DO NOTHING",
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

    @_locked
    def add_trial(self, row: dict[str, Any]) -> int:
        """Log one validation run of a strategy on one timeframe; returns its id.

        ``row`` gives every column of ``validation_trials`` but ``id``; ``ts`` defaults to
        now and ``payload`` (any JSON value) to the row itself. A non-finite
        ``sharpe_trade`` is stored as 0, a run without a measurable Sharpe.
        """
        missing = [name for name in _TRIAL_COLUMNS if name not in row]
        if missing:
            raise ValueError(f"missing trial fields: {missing}")
        sharpe = float(row["sharpe_trade"])
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO validation_trials
                (ts, strategy_id, timeframe, exit_policy, pairset, market_filter, hysteresis,
                 sharpe_trade, trades, passed, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.get("ts") or _now(),
                    row["strategy_id"],
                    row["timeframe"],
                    row["exit_policy"],
                    row["pairset"],
                    row["market_filter"],
                    1 if row["hysteresis"] else 0,
                    sharpe if math.isfinite(sharpe) else 0.0,
                    int(row["trades"]),
                    1 if row["passed"] else 0,
                    json.dumps(row["payload"] if "payload" in row else row, default=str),
                ),
            )
        return int(cursor.lastrowid or 0)

    @_locked
    def trials_since_pass(self, strategy_id: str) -> list[dict[str, Any]]:
        """The strategy's trials after its most recent passing one, oldest first.

        Every trial when none passed. The passing trial itself is not included.
        """
        rows = self._conn.execute(
            """SELECT * FROM validation_trials
            WHERE strategy_id=? AND id > COALESCE(
                (SELECT MAX(id) FROM validation_trials WHERE strategy_id=? AND passed=1), 0)
            ORDER BY id""",
            (strategy_id, strategy_id),
        ).fetchall()
        return [_trial(row) for row in rows]

    # -- unprotected windows -------------------------------------------------

    @_locked
    def record_stop_window(
        self, kind: str, symbol: str, position_id: int | None, ms: float, *, ts: str | None = None
    ) -> None:
        """Record how long a position stood without a stop while it was replaced (``kind``:
        trail, tp1, partial, restop or exit_remainder)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO stop_windows(ts, kind, symbol, position_id, ms) VALUES (?, ?, ?, ?, ?)",
                (ts or _now(), kind, symbol, position_id, float(ms)),
            )

    @_locked
    def stop_window_stats(self, since: str) -> dict[str, Any]:
        """Count, mean, p95 and max of the windows recorded at or after ``since`` (ISO time),
        overall and by kind. The statistics are None when there is no window."""
        rows = self._conn.execute(
            "SELECT kind, ms FROM stop_windows WHERE ts >= ? ORDER BY id", (since,)
        ).fetchall()
        values = [float(row["ms"]) for row in rows]
        by_kind: dict[str, list[float]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(float(row["ms"]))
        return {
            "count": len(values),
            "mean_ms": sum(values) / len(values) if values else None,
            "p95_ms": _percentile(values, 0.95) if values else None,
            "max_ms": max(values) if values else None,
            "by_kind": {
                kind: {"count": len(items), "mean_ms": sum(items) / len(items), "max_ms": max(items)}
                for kind, items in sorted(by_kind.items())
            },
        }

    # -- DeepSeek advisor ----------------------------------------------------

    @_locked
    def add_advice(self, row: dict[str, Any]) -> int:
        """Log one verdict (``_ADVICE_COLUMNS``; ``latency_ms`` optional); returns its id. A signal
        already logged (``_ADVICE_SIGNAL_KEY``) keeps its row, whose id is returned."""
        missing = [name for name in _ADVICE_COLUMNS if name not in row and name != "latency_ms"]
        if missing:
            raise ValueError(f"missing advice fields: {missing}")
        values = [json.dumps(list(row["risk_flags"])) if name == "risk_flags" else row.get(name) for name in _ADVICE_COLUMNS]
        columns = ", ".join(_ADVICE_COLUMNS)
        marks = ", ".join("?" for _ in _ADVICE_COLUMNS)
        with self._conn:
            cursor = self._conn.execute(f"INSERT OR IGNORE INTO advice_log ({columns}) VALUES ({marks})", values)
        if cursor.rowcount:
            return int(cursor.lastrowid or 0)
        # Ignored: lastrowid would be the connection's previous insert, not this signal's row.
        kept = self.advice_for(*(str(row[name]) for name in _ADVICE_SIGNAL_KEY))
        return int(kept["id"]) if kept is not None else 0

    @_locked
    def advice_for(
        self, symbol: str, strategy: str, timeframe: str, signal_close_at: str, prompt_version: str, model: str
    ) -> dict[str, Any] | None:
        """The logged verdict of one signal (``_ADVICE_SIGNAL_KEY``), or None."""
        match = " AND ".join(f"{name}=?" for name in _ADVICE_SIGNAL_KEY)
        row = self._conn.execute(
            f"SELECT * FROM advice_log WHERE {match} ORDER BY id LIMIT 1",
            (symbol, strategy, timeframe, signal_close_at, prompt_version, model),
        ).fetchone()
        return _advice_item(row) if row is not None else None

    @_locked
    def advice_since(self, ts: str) -> list[dict[str, Any]]:
        """The verdicts logged at or after ``ts`` (an ISO time in UTC), oldest first."""
        rows = self._conn.execute("SELECT * FROM advice_log WHERE ts >= ? ORDER BY id", (ts,)).fetchall()
        return [_advice_item(row) for row in rows]

    @_locked
    def advice_rows(self, *, unresolved: bool = False) -> list[dict[str, Any]]:
        """Logged verdicts, oldest first, with the closed position's result in R (``realized_r``)
        when the row entered one. ``unresolved`` keeps the rows without a counterfactual yet that
        did not expire."""
        query = """SELECT a.*, p.r_multiple AS realized_r FROM advice_log a
            LEFT JOIN positions p ON p.id = a.position_id AND p.status = 'closed'"""
        if unresolved:
            query += " WHERE a.resolved_at IS NULL AND a.expired_at IS NULL"
        return [_advice_item(row) for row in self._conn.execute(query + " ORDER BY a.id").fetchall()]

    @_locked
    def expire_advice(self, advice_id: int, expired_at: str) -> None:
        """Mark an unresolved row as expired: its window and grace passed without the candles to
        label it. It is never tried again and counts apart in the calibration."""
        with self._conn:
            self._conn.execute(
                "UPDATE advice_log SET expired_at=? WHERE id=? AND resolved_at IS NULL AND expired_at IS NULL",
                (expired_at, advice_id),
            )

    @_locked
    def note_advice_fetch_failure(self, advice_id: int) -> int:
        """Count one failed klines read of an unresolved row; returns its count so far."""
        with self._conn:
            self._conn.execute("UPDATE advice_log SET fetch_failures = fetch_failures + 1 WHERE id=?", (advice_id,))
        row = self._conn.execute("SELECT fetch_failures FROM advice_log WHERE id=?", (advice_id,)).fetchone()
        return int(row["fetch_failures"]) if row is not None else 0

    @_locked
    def resolve_advice(self, advice_id: int, counterfactual_r: float, resolved_at: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE advice_log SET counterfactual_r=?, resolved_at=? WHERE id=? AND resolved_at IS NULL",
                (counterfactual_r, resolved_at, advice_id),
            )

    @_locked
    def link_advice(self, advice_id: int, position_id: int) -> None:
        """Record that the row a verdict judged was sent as ``position_id``."""
        with self._conn:
            self._conn.execute("UPDATE advice_log SET taken=1, position_id=? WHERE id=?", (position_id, advice_id))

    @_locked
    def record_advisor_usage(self, day: str, **counts: float) -> None:
        """Add to the day's advisor counters (``_USAGE_COLUMNS``): calls, tokens, cost, verdicts, errors."""
        unknown = set(counts) - set(_USAGE_COLUMNS)
        if unknown:
            raise ValueError(f"unknown usage fields: {sorted(unknown)}")
        if not counts:
            return
        names = list(counts)
        columns = ", ".join(names)
        marks = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name}={name}+excluded.{name}" for name in names)
        with self._conn:
            self._conn.execute(
                f"INSERT INTO advisor_usage_daily(day, {columns}) VALUES (?, {marks}) ON CONFLICT(day) DO UPDATE SET {updates}",
                (day, *counts.values()),
            )

    @_locked
    def advisor_usage(self, since: str, until: str | None = None) -> dict[str, Any]:
        """The advisor counters summed over the UTC days ``since`` to ``until``, both included
        (``until`` None: every day from ``since``). Zeros without a row."""
        return _usage(self._conn, since, until)

    # -- armed entries -----------------------------------------------------

    @_locked
    def arm_entry(self, symbol: str, strategy: str, trigger_price: float, condition: str, expires_at: str) -> int | None:
        """Arm one entry. ``None`` when the pair and strategy already have an armed one."""
        stamp = _now()
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """INSERT INTO armed_entries
                    (symbol, strategy, trigger_price, condition, created_at, updated_at, expires_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'armed')""",
                    (symbol, strategy, trigger_price, condition, stamp, stamp, expires_at),
                )
        except sqlite3.IntegrityError:
            return None
        return int(cursor.lastrowid or 0) or None

    @_locked
    def armed_entries(self, *, live_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """Armed entries, newest first. ``live_only`` returns every armed one; ``limit`` caps only the history."""
        if live_only:
            rows = self._conn.execute("SELECT * FROM armed_entries WHERE status = 'armed' ORDER BY id DESC").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM armed_entries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def settle_armed(self, armed_id: int, status: str, reason: str, *, position_id: int | None = None) -> bool:
        """Leave the ``armed`` state, or only record ``reason`` when ``status`` is ``armed``.

        False when the entry was no longer armed (a concurrent cancel or fire).
        """
        with self._conn:
            cursor = self._conn.execute(
                """UPDATE armed_entries SET status=?, reason=?, position_id=COALESCE(?, position_id), updated_at=?
                WHERE id=? AND status='armed'""",
                (status, reason, position_id, _now(), armed_id),
            )
        return cursor.rowcount == 1

    # -- bot instances (DCA, grid) -----------------------------------------

    @_locked
    def create_instance(self, kind: str, symbol: str, params: dict[str, Any], state: dict[str, Any]) -> int | None:
        """Start one DCA or grid instance. ``None`` when that kind already runs on the pair."""
        stamp = _now()
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """INSERT INTO bot_instances(kind, symbol, params, state, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                    (kind, symbol, json.dumps(params), json.dumps(state), stamp, stamp),
                )
        except sqlite3.IntegrityError:
            return None
        return int(cursor.lastrowid or 0) or None

    @_locked
    def instances(self, *, live_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """Instances, newest first. ``live_only`` returns every live one (risk, halt and the cycle need
        all of them); ``limit`` caps only the history."""
        if live_only:
            rows = self._conn.execute(
                "SELECT * FROM bot_instances WHERE status IN ('active', 'stopping') ORDER BY id DESC"
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM bot_instances ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_instance(row) for row in rows]

    @_locked
    def instance(self, instance_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM bot_instances WHERE id=?", (instance_id,)).fetchone()
        return None if row is None else _instance(row)

    @_locked
    def update_instance(self, instance_id: int, **fields: Any) -> None:
        unknown = set(fields) - {"state", "status", "stopped_at", "realized_pnl", "reason"}
        if unknown:
            raise ValueError(f"unknown instance fields: {sorted(unknown)}")
        if "state" in fields:
            fields["state"] = json.dumps(fields["state"])
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self._conn:
            self._conn.execute(
                f"UPDATE bot_instances SET {assignments}, updated_at=? WHERE id=?",
                (*fields.values(), _now(), instance_id),
            )

    @_locked
    def instance_positions(self, instance_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM positions WHERE instance_id=? ORDER BY id", (instance_id,)).fetchall()
        return [dict(row) for row in rows]


def _instance(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["params"] = json.loads(item["params"])
    item["state"] = json.loads(item["state"])
    return item


def _trial(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["hysteresis"] = bool(item["hysteresis"])
    item["passed"] = bool(item["passed"])
    item["payload"] = json.loads(item["payload"])
    return item


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile of a non-empty list (numpy's default method)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _position_ddl() -> list[str]:
    """The ``positions`` table and its live-slot index, as separate statements."""
    start = _SCHEMA.index("CREATE TABLE IF NOT EXISTS positions")
    end = _SCHEMA.index("CREATE TABLE IF NOT EXISTS pnl_daily")
    block = _SCHEMA[start:end]
    return [statement.strip() for statement in block.split(";") if statement.strip()]

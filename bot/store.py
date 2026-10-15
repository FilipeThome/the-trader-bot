"""SQLite store. One file per environment, under that environment's runtime root."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    trailing INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    client_order_id TEXT,
    UNIQUE(symbol, strategy, status)
);
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_runtime_root() / "bot.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def set_state(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def replace_opportunities(self, rows: list[dict[str, Any]]) -> None:
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
        self._conn.commit()

    def opportunities(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM opportunities ORDER BY score DESC"
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

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
                json.dumps(row),
            ),
        )
        self._conn.commit()

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def ignore(self, symbol: str, strategy: str, until: str) -> None:
        self._conn.execute(
            """INSERT INTO ignored(symbol, strategy, until) VALUES(?, ?, ?)
            ON CONFLICT(symbol, strategy) DO UPDATE SET until=excluded.until""",
            (symbol, strategy, until),
        )
        self._conn.commit()

    def ignored_keys(self, now: datetime | None = None) -> set[tuple[str, str]]:
        moment = (now or datetime.now(timezone.utc)).isoformat()
        rows = self._conn.execute(
            "SELECT symbol, strategy FROM ignored WHERE until > ?", (moment,)
        ).fetchall()
        return {(row["symbol"], row["strategy"]) for row in rows}

    def upsert_position(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO positions
            (symbol, strategy, side, qty, avg_price, stop_price, target_price, trailing,
             opened_at, updated_at, status, realized_pnl, client_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, strategy, status) DO UPDATE SET
                qty=excluded.qty, avg_price=excluded.avg_price, stop_price=excluded.stop_price,
                target_price=excluded.target_price, trailing=excluded.trailing,
                updated_at=excluded.updated_at, realized_pnl=excluded.realized_pnl,
                client_order_id=excluded.client_order_id""",
            (
                row["symbol"],
                row["strategy"],
                row["side"],
                row["qty"],
                row["avg_price"],
                row.get("stop_price"),
                row.get("target_price"),
                1 if row.get("trailing") else 0,
                row.get("opened_at") or _now(),
                _now(),
                row.get("status", "open"),
                row.get("realized_pnl", 0),
                row.get("client_order_id"),
            ),
        )
        self._conn.commit()

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def close_position(self, symbol: str, strategy: str, realized_pnl: float) -> None:
        self._conn.execute(
            """UPDATE positions SET status='closed', realized_pnl=?, updated_at=?
            WHERE symbol=? AND strategy=? AND status='open'""",
            (realized_pnl, _now(), symbol, strategy),
        )
        self._conn.commit()

    def record_day(self, day: str, **fields: float) -> None:
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
        if current is not None:
            base.update({key: current[key] for key in base})
        for key, value in fields.items():
            base[key] = base.get(key, 0) + value
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
        self._conn.commit()

    def pnl_days(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM pnl_daily ORDER BY day").fetchall()
        return [dict(row) for row in rows]

    def record_equity(self, equity: float, ts: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO equity(ts, equity) VALUES(?, ?) ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity",
            (ts or _now(), equity),
        )
        self._conn.commit()

    def peak_equity(self) -> float:
        row = self._conn.execute("SELECT MAX(equity) AS peak FROM equity").fetchone()
        if row is None or row["peak"] is None:
            return 0.0
        return float(row["peak"])

    def trades_today(self, day: str) -> int:
        row = self._conn.execute("SELECT trades FROM pnl_daily WHERE day=?", (day,)).fetchone()
        return int(row["trades"]) if row else 0

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

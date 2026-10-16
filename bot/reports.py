"""Daily report. The exported chain must verify, and it must match the store."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from bot.governance.ledger import append_record, export_chain_to_file, rotate_if_needed, verify_chain_with_archives
from bot.paths import get_runtime_root
from bot.store import Store

#: Size at which the bot ledger is sealed into a numbered segment. Every append
#: re-verifies the active file, so this bounds an append to about 2,600 records
#: (~40 ms) instead of growing with the whole history.
LEDGER_ROTATE_BYTES = 1024 * 1024
#: Rotation and append must not interleave in this process: the cycle and the
#: validation thread both write the ledger.
_LEDGER_LOCK = threading.Lock()


def ledger_path() -> Path:
    return get_runtime_root() / "bot-ledger.jsonl"


def append_ledger(payload: Mapping[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    """Append one record to the bot ledger, sealing the active file first when it is full."""
    target = path or ledger_path()
    with _LEDGER_LOCK:
        rotate_if_needed(target, LEDGER_ROTATE_BYTES)
        return append_record(target, payload)


def verify_ledger(path: Path | None = None) -> dict[str, Any]:
    """Verify the whole bot ledger history, sealed segments included. Read-only."""
    target = path or ledger_path()
    with _LEDGER_LOCK:
        result = verify_chain_with_archives(target)
    return {"ok": bool(result.ok), "records": result.record_count, "break": None if result.ok else str(result.first_break)}


def build_daily_report(store: Store, *, day: str | None = None) -> dict[str, Any]:
    path = ledger_path()
    with _LEDGER_LOCK:
        chain = verify_chain_with_archives(path) if path.is_file() else None
    # A day's decisions come from SQL, so a busy day is counted whole (a fixed tail of rows was not).
    decisions = store.decisions_on(day) if day is not None else store.decisions(limit=5000)
    days = store.pnl_days()
    if day is not None:
        days = [row for row in days if row["day"] == day]
    export = None
    if path.is_file():
        name = "latest" if day is None else date.fromisoformat(day).isoformat()
        folder = (get_runtime_root() / "reports").resolve()
        target = (folder / f"{name}.json").resolve()
        if target.parent != folder:
            raise ValueError("report outside the reports folder")
        folder.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK:
            export_chain_to_file(path, target)
        # Only the file name: the absolute path of the operator's folder stays on the server.
        export = target.name
    return {
        "day": day,
        "decisions": len(decisions),
        "pnl": days,
        "chain_ok": bool(chain.ok) if chain is not None else True,
        "chain_records": chain.record_count if chain is not None else 0,
        "export": export,
    }

"""Daily report. The exported chain must verify, and it must match the store."""

from __future__ import annotations

from pathlib import Path

from bot.governance.ledger import export_chain_to_file, verify_chain
from bot.paths import get_runtime_root
from bot.store import Store


def ledger_path() -> Path:
    return get_runtime_root() / "bot-ledger.jsonl"


def build_daily_report(store: Store, *, day: str | None = None) -> dict:
    path = ledger_path()
    chain = verify_chain(path) if path.is_file() else None
    decisions = store.decisions(limit=5000)
    if day is not None:
        decisions = [row for row in decisions if str(row.get("ts", "")).startswith(day)]
    days = store.pnl_days()
    if day is not None:
        days = [row for row in days if row["day"] == day]
    export = None
    if path.is_file():
        target = get_runtime_root() / "reports" / f"{day or 'latest'}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        export = str(export_chain_to_file(path, target))
    return {
        "day": day,
        "decisions": len(decisions),
        "pnl": days,
        "chain_ok": bool(chain.ok) if chain is not None else True,
        "chain_records": chain.record_count if chain is not None else 0,
        "export": export,
    }

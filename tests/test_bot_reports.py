from __future__ import annotations

from bot.governance.ledger import append_record, verify_chain
from bot.reports import build_daily_report, ledger_path
from bot.store import Store


def test_daily_report_matches_the_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    path = ledger_path()
    append_record(path, {"kind": "tick", "decision": "pronta", "symbol": "BTC/USDT"})
    append_record(path, {"kind": "tick", "decision": "nao_operar", "symbol": "ETH/USDT"})
    store = Store(tmp_path / "bot.sqlite")
    store.add_decision(
        {
            "ts": "2026-10-14T12:00:00+00:00",
            "symbol": "BTC/USDT",
            "strategy": "trend",
            "decision": "pronta",
            "reason": "rompimento",
        }
    )
    store.record_day("2026-10-14", net_pnl=12.5, trades=1)
    report = build_daily_report(store, day="2026-10-14")
    chain = verify_chain(path)
    assert chain.ok
    assert report["chain_ok"] is True
    assert report["chain_records"] == chain.record_count == 2
    assert report["decisions"] == 1
    assert report["pnl"][0]["net_pnl"] == 12.5


def test_a_rotated_ledger_still_verifies_reports_and_exports_whole(tmp_path, monkeypatch) -> None:
    import json

    from bot import reports
    from bot.governance.ledger import archive_segments, verify_export
    from bot.paths import pin_mode_root
    from bot.store import Store

    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    pin_mode_root(tmp_path / "testnet")
    monkeypatch.setattr(reports, "LEDGER_ROTATE_BYTES", 2_000)
    for index in range(60):
        reports.append_ledger({"kind": "tick", "n": index})
    path = reports.ledger_path()
    assert len(archive_segments(path)) >= 2
    # Each append verified only the active segment; the whole history still holds.
    assert reports.verify_ledger() == {"ok": True, "records": 60, "break": None}
    report = reports.build_daily_report(Store(tmp_path / "testnet" / "bot.sqlite"))
    assert report["chain_ok"] is True and report["chain_records"] == 60
    exported = json.loads((tmp_path / "testnet" / "reports" / "latest.json").read_text(encoding="utf-8"))
    assert exported["record_count"] == 60 and verify_export(exported).ok


def test_rotation_never_renames_over_an_existing_segment(tmp_path) -> None:
    from bot.governance.ledger import append_record, archive_segments, rotate_if_needed

    path = tmp_path / "bot-ledger.jsonl"
    for round_ in range(3):
        for index in range(20):
            append_record(path, {"kind": "tick", "round": round_, "n": index}, fsync=False)
        rotate_if_needed(path, 500, fsync=False)
    first, second, third = archive_segments(path)
    second.unlink()
    for index in range(20):
        append_record(path, {"kind": "tick", "round": 9, "n": index}, fsync=False)
    rotate_if_needed(path, 500, fsync=False)
    names = [segment.name for segment in archive_segments(path)]
    # The next segment is numbered past the newest one; the newest is never overwritten.
    assert names == [first.name, third.name, "bot-ledger.0004.jsonl"]

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

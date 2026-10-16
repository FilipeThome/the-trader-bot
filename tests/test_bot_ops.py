from __future__ import annotations

from bot.exchange.execution.journal import append_event, new_event
from bot.ops import run


def test_status_reports_a_clean_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    code, result = run(["status"])
    assert code == 0
    assert result["journal"] == {"blocked": False, "error": None, "blocking": []}
    assert result["halted"] is False


def test_status_names_a_stuck_submission_and_confirm_absent_needs_a_pending_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    from bot.paths import pin_mode_root
    from bot.runtime import mode_root
    from bot.config import BotConfig

    pin_mode_root(mode_root(BotConfig()))
    append_event(new_event("SUBMISSION_PENDING", "order:abc", profile_id="binance-paper-trade", symbol="BTC/USDT"))
    code, result = run(["status"])
    assert code == 0
    assert result["journal"]["blocked"] is True
    assert result["journal"]["blocking"] == [{"state_key": "order:abc", "event": "SUBMISSION_PENDING"}]
    code, refused = run(["confirm-absent", "order:missing", "--reason", "processo morto"])
    assert code == 1 and "not a pending submission" in refused["error"]


def test_adopt_checkpoint_requires_a_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    code, result = run(["adopt-checkpoint", "--reason", " "])
    assert code == 1 and "reason" in result["error"]

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bot.exchange.execution import journal
from bot.governance.ledger import GENESIS_PREV_HASH, LedgerCorruptionError, compute_record_hash
from bot.paths import pin_mode_root


def _write(path: Path, count: int) -> None:
    previous = GENESIS_PREV_HASH
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for seq in range(1, count + 1):
            payload = {"event": "ORDER_SUBMITTED", "state_key": f"order:{seq}", "recorded_at": "2026-10-16T10:00:00+00:00"}
            record_hash = compute_record_hash(seq, previous, payload)
            handle.write(json.dumps({**payload, "seq": seq, "prev_record_hash": previous, "record_hash": record_hash}) + "\n")
            previous = record_hash


def _append(path: Path, seq: int) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    previous = rows[-1]["record_hash"]
    payload = {"event": "ORDER_SUBMITTED", "state_key": f"order:{seq}", "recorded_at": "2026-10-16T10:00:00+00:00"}
    record_hash = compute_record_hash(seq, previous, payload)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({**payload, "seq": seq, "prev_record_hash": previous, "record_hash": record_hash}) + "\n")


@pytest.fixture
def journal_file(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    pin_mode_root(tmp_path / "testnet")
    path = journal.journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, 50)
    journal._write_checkpoint(journal._read_verified(path, full=True)[0])
    journal.reset_journal_cache()
    return path


def _count_walked(monkeypatch) -> list[int]:
    walked: list[int] = []
    inner = journal._walk_chain

    def counting(source: Any, *args: Any, **kwargs: Any) -> Any:
        items = list(source)
        walked.append(len(items))
        return inner(iter(items), *args, **kwargs)

    monkeypatch.setattr(journal, "_walk_chain", counting)
    return walked


def test_an_unchanged_journal_is_read_from_memory_and_an_append_walks_only_the_new_line(journal_file, monkeypatch) -> None:
    walked = _count_walked(monkeypatch)
    assert len(journal._records()) == 50
    assert len(journal._records()) == 50
    _append(journal_file, 51)
    journal._write_checkpoint(journal._read_verified(journal_file)[0])
    assert len(journal._records()) == 51
    # One full walk of 50, then only the appended record.
    assert walked == [50, 1]


def test_a_rewrite_of_the_same_size_is_caught(journal_file) -> None:
    assert len(journal._records()) == 50
    text = journal_file.read_text(encoding="utf-8")
    journal_file.write_text(text.replace("order:7", "order:8", 1), encoding="utf-8", newline="\n")
    with pytest.raises(LedgerCorruptionError):
        journal._records()


def test_a_deep_edit_followed_by_an_append_is_caught_by_the_full_walk_of_a_new_order(journal_file) -> None:
    assert len(journal._records()) == 50
    text = journal_file.read_text(encoding="utf-8")
    journal_file.write_text(text.replace("order:3", "order:4", 1), encoding="utf-8", newline="\n")
    _append(journal_file, 51)
    # Before an order is recorded, the whole chain is walked again.
    with pytest.raises(LedgerCorruptionError):
        journal._records(full=True)
    with pytest.raises(LedgerCorruptionError):
        journal.begin_submission({"event": "SUBMISSION_PENDING", "state_key": "order:new"})


def test_the_cache_expires_into_a_full_walk(journal_file, monkeypatch) -> None:
    walked = _count_walked(monkeypatch)
    journal._records()
    monkeypatch.setattr(journal, "FULL_VERIFY_SECONDS", 0.0)
    journal._records()
    assert walked == [50, 50]


def test_rows_returned_are_copies(journal_file) -> None:
    rows = journal._records()
    rows[0]["event"] = "tampered"
    assert journal._records()[0]["event"] == "ORDER_SUBMITTED"

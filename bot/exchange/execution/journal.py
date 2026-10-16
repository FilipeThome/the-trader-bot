# First-party code of this repository. It was written in a local, unpublished
# Vibe-Trading checkout and is in no Vibe-Trading commit. See ORIGIN.md.
"""Append-only Binance execution journal.

Records are written with ``append_record`` so a broken chain cannot grow.
The lock around the read and the append is what stops two submissions from
both observing a clear journal. API secrets are not fields of these events.

Truncation detection
--------------------
A hash chain proves that the records it holds were not edited, but not that
none were removed from its end. After every append a checkpoint file
(``binance-execution-journal.checkpoint.json``) records the number of records
and the last record hash, written atomically under the journal lock. Every
read compares the journal with it and fails closed when:

* the checkpoint exists but the journal does not (journal deleted);
* the journal has fewer records than the checkpoint (tail removed);
* the record at the checkpoint position has a different hash (rollback);
* the journal holds records but the checkpoint is missing or unreadable.

The journal may hold *more* records than the checkpoint: that is a crash
between the journal append and the checkpoint write, and the next append
brings the checkpoint up to date.

Threat model: this detects accidental deletion, truncation and rollback of
checkpointed records, for example by a tool or a person editing one file. It does not protect against
someone who rewrites or deletes both files; both live under the same runtime
root. Fsync failures on the journal or the checkpoint (including the
directory entry of the checkpoint rename) are persistence errors, not
warnings. Records appended after the last checkpoint (a crash between the two
writes) are not anchored: removing only those goes undetected. Deleting both
files is indistinguishable from a first run.

Recovery and migration: a failed check blocks every new order
(``journal_unavailable``). Restore the journal from a backup that matches the
checkpoint. A journal written before checkpoints existed has none and also
blocks; that is the one-time migration case. If the state is understood, an
operator (never the agent: no tool, MCP or API route calls it) runs
``adopt_journal_checkpoint(reason=...)`` from a Python shell outside the
agent. It re-anchors the checkpoint to the journal as it now is and records a
``CHECKPOINT_ADOPTED`` event; unresolved records still in the journal keep
blocking. It refuses while the checkpoint anchors records the journal has
lost, so it cannot be used to forget a possibly live order.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from bot.paths import get_runtime_root
from bot.governance.ledger import (
    LedgerCorruptionError,
    _fsync_dir,
    append_record,
    _iter_parsed_lines,
    _walk_chain,
)
from bot.exchange._filelock import LockUnavailable, exclusive_file_lock
from bot.exchange.execution.lifecycle import (
    BLOCKING_EVENTS,
    transition_allowed,
)

JOURNAL_FILENAME = "binance-execution-journal.jsonl"
LOCK_FILENAME = "binance-execution-journal.lock"
CHECKPOINT_FILENAME = "binance-execution-journal.checkpoint.json"

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "signature",
        "authorization",
        "password",
        "token",
    }
)


class PersistError(RuntimeError):
    """The journal did not durably accept the event."""


class UnresolvedExecution(RuntimeError):
    """An in-flight or unknown write still blocks a new submission."""


class InvalidTransition(RuntimeError):
    """The event would reopen or skip a lifecycle state."""


class DailyLimitReached(RuntimeError):
    """The profile has used every operation allowed for this UTC day."""


def _now() -> datetime:
    """Current UTC time; one seam so tests can cross a UTC midnight."""
    return datetime.now(timezone.utc)


def _utc_day(recorded_at: Any) -> str | None:
    try:
        stamp = datetime.fromisoformat(str(recorded_at or ""))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).date().isoformat()


def _operations_on(rows: list[dict[str, Any]], profile_id: str, day: str) -> int:
    """Count ``profile_id``'s operations on UTC ``day``.

    One operation is one ``SUBMISSION_PENDING``: an order that may have been
    handed to the exchange, whatever its outcome (rejected, timed out, or not
    journaled afterwards all count). A pending record later closed by
    ``SUBMISSION_ABORTED`` is not counted: the adapter proved that request was
    never sent. Cancels, previews, and denied orders are not operations.
    """
    pending_day: dict[str, str | None] = {}
    count = 0
    for row in rows:
        if row.get("profile_id") != profile_id:
            continue
        key = str(row.get("state_key") or "")
        event = row.get("event")
        if event == "SUBMISSION_PENDING":
            stamp = _utc_day(row.get("recorded_at"))
            pending_day[key] = stamp
            if stamp == day:
                count += 1
        elif event == "SUBMISSION_ABORTED" and key in pending_day:
            if pending_day.pop(key) == day:
                count -= 1
    return count


def daily_operation_count(profile_id: str, now: datetime | None = None) -> int:
    """Return ``profile_id``'s operations on the UTC day of ``now``. Read-only.

    Raises the journal's own errors when it cannot be read, so callers deny.
    """
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    return _operations_on(_records(), profile_id, day)


def journal_path() -> Path:
    """Return the execution journal path under the runtime root."""
    return get_runtime_root() / JOURNAL_FILENAME


def _lock_path() -> Path:
    return get_runtime_root() / LOCK_FILENAME


def checkpoint_path() -> Path:
    """Return the truncation-detection checkpoint path."""
    return get_runtime_root() / CHECKPOINT_FILENAME


def _reject_secrets(payload: Mapping[str, Any]) -> None:
    for key in payload:
        lowered = str(key).lower()
        if lowered in _SECRET_KEYS or "secret" in lowered or "api_key" in lowered:
            raise PersistError("execution journal refused a secret field")


def _read_checkpoint() -> dict[str, Any] | None:
    path = checkpoint_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PersistError("execution journal checkpoint is unreadable") from exc
    records = data.get("records") if isinstance(data, dict) else None
    last_hash = data.get("last_hash") if isinstance(data, dict) else None
    if isinstance(records, bool) or not isinstance(records, int) or records < 0:
        raise PersistError("execution journal checkpoint is malformed")
    if records and (not isinstance(last_hash, str) or not last_hash):
        raise PersistError("execution journal checkpoint is malformed")
    return {"records": records, "last_hash": last_hash}


def _check_checkpoint(rows: list[dict[str, Any]], journal_exists: bool, checkpoint: Mapping[str, Any] | None) -> None:
    if checkpoint is None:
        if journal_exists:
            raise PersistError("execution journal checkpoint is missing")
        return
    expected = int(checkpoint["records"])
    if expected and not journal_exists:
        raise PersistError("execution journal is missing but its checkpoint is not")
    if len(rows) < expected:
        raise PersistError("execution journal is shorter than its checkpoint")
    if expected and rows[expected - 1].get("record_hash") != checkpoint["last_hash"]:
        raise PersistError("execution journal does not match its checkpoint")


def _write_checkpoint(rows: list[dict[str, Any]]) -> None:
    """Atomically record the journal length and last hash. Caller holds the lock."""
    path = checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "records": len(rows),
            "last_hash": rows[-1].get("record_hash") if rows else None,
            "updated_at": _now().isoformat(),
        }
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise PersistError("execution journal checkpoint was not written") from exc
        raise
    try:
        # Make the rename itself durable, or a crash can bring back the older
        # checkpoint. A no-op on Windows, which cannot fsync a directory.
        _fsync_dir(path.parent, strict=True)
    except OSError as exc:
        raise PersistError("execution journal checkpoint was not made durable") from exc


#: Seconds a verified read is trusted before the whole journal is verified again.
FULL_VERIFY_SECONDS = 300.0
#: Bytes before the cached end that must still match for an append-only read.
_TAIL_BYTES = 4096


@dataclass
class _Verified:
    """A journal already verified up to ``size`` bytes, in this process."""

    identity: tuple[int, int]
    size: int
    mtime_ns: int
    tail: bytes
    rows: list[dict[str, Any]]
    last_seq: int
    last_hash: str
    verified_at: float


_CACHE: dict[str, _Verified] = {}
_CACHE_LOCK = threading.Lock()


def reset_journal_cache() -> None:
    """Forget every verified read. Tests and the operator CLI call it."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _read_verified(path: Path, *, full: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """The journal's rows, verified against the hash chain.

    The chain is walked in full once, then only for what was appended since:
    while the file is the same inode, its size and mtime are unchanged or it
    only grew, and the last cached bytes still match, the new lines are
    verified as a continuation of the cached last record. Anything else (a
    shrink, a rewrite, a replaced file), ``full=True``, a failed incremental
    read, or a cache older than ``FULL_VERIFY_SECONDS`` walks the whole file
    again. Reads run under the journal lock, so no writer moves the file
    between ``stat`` and read.
    """
    key = str(path)
    try:
        status = path.stat()
    except FileNotFoundError:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return [], False
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    identity = (status.st_dev, status.st_ino)
    fresh = cached is not None and time.monotonic() - cached.verified_at < FULL_VERIFY_SECONDS
    if not full and cached is not None and fresh and cached.identity == identity:
        if status.st_size == cached.size and status.st_mtime_ns == cached.mtime_ns:
            return [dict(row) for row in cached.rows], True
        if status.st_size > cached.size:
            grown = _verify_appended(path, cached, status.st_mtime_ns)
            if grown is not None:
                with _CACHE_LOCK:
                    _CACHE[key] = grown
                return [dict(row) for row in grown.rows], True
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
    data = path.read_bytes()
    result, last_seq, last_hash = _walk_chain(_iter_parsed_lines(data.decode("utf-8").splitlines()))
    if not result.ok:
        if result.first_break is None:
            raise PersistError("execution journal failed verification")
        raise LedgerCorruptionError(result.first_break)
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    entry = _Verified(identity, len(data), status.st_mtime_ns, data[-_TAIL_BYTES:], rows, last_seq, last_hash, time.monotonic())
    with _CACHE_LOCK:
        _CACHE[key] = entry
    return [dict(row) for row in rows], True


def _verify_appended(path: Path, cached: _Verified, mtime_ns: int) -> _Verified | None:
    """Verify only the bytes appended after ``cached``; None sends the caller to a full read."""
    try:
        with path.open("rb") as handle:
            handle.seek(cached.size - len(cached.tail))
            if handle.read(len(cached.tail)) != cached.tail:
                return None
            appended = handle.read()
        if not appended.endswith(b"\n"):
            return None
        lines = appended.decode("utf-8").splitlines()
        result, last_seq, last_hash = _walk_chain(
            _iter_parsed_lines(lines), start_seq=cached.last_seq + 1, start_prev_hash=cached.last_hash
        )
        if not result.ok:
            return None
        rows = cached.rows + [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError):
        return None
    size = cached.size + len(appended)
    tail = (cached.tail + appended)[-_TAIL_BYTES:]
    return _Verified(cached.identity, size, mtime_ns, tail, rows, last_seq, last_hash, cached.verified_at)


def _records(*, full: bool = False) -> list[dict[str, Any]]:
    """Read the whole journal, verified against its chain and its checkpoint.

    Takes the journal lock (re-entrant per thread), so a read never overlaps
    an append and never compares a journal with a newer checkpoint. ``full``
    walks the whole chain instead of trusting the verified read in memory.
    """
    with _exclusive_lock():
        try:
            checkpoint = _read_checkpoint()
            rows, exists = _read_verified(journal_path(), full=full)
        except OSError as exc:
            raise PersistError(f"execution journal could not be read ({type(exc).__name__})") from exc
        _check_checkpoint(rows, exists, checkpoint)
        return rows


def _latest_states(rows: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for row in rows:
        event = str(row.get("event") or "")
        key = str(row.get("state_key") or "")
        if key and event:
            latest[key] = event
    return latest


def has_unresolved(rows: list[dict[str, Any]] | None = None) -> bool:
    """Return whether any key is pending or unknown."""
    states = _latest_states(_records() if rows is None else rows)
    return any(event in BLOCKING_EVENTS for event in states.values())


@contextmanager
def _exclusive_lock() -> Iterator[None]:
    """Serialize journal reads and writes across threads and processes.

    Waiting is bounded: a lock not acquired in time is a ``PersistError`` and
    callers deny. Only acquisition errors are rewritten; an error raised
    inside the block propagates unchanged.
    """
    lock = exclusive_file_lock(_lock_path())
    try:
        lock.__enter__()
    except (LockUnavailable, OSError) as exc:
        raise PersistError("execution journal lock failed") from exc
    try:
        yield
    except BaseException:
        if not lock.__exit__(*sys.exc_info()):
            raise
    else:
        lock.__exit__(None, None, None)


def _append_locked(body: dict[str, Any]) -> dict[str, Any]:
    """Append, confirm, then advance the checkpoint. Caller holds the lock."""
    if not checkpoint_path().exists() and not journal_path().exists():
        # Anchor an empty journal first, so a crash after the first append
        # leaves "journal ahead of checkpoint" (tolerated), not "no checkpoint".
        _write_checkpoint([])
    try:
        written = append_record(journal_path(), body, strict_fsync=True)
    except OSError as exc:
        raise PersistError(f"execution journal write was not durable ({type(exc).__name__})") from exc
    rows, _ = _read_verified(journal_path())
    if not rows or rows[-1].get("record_hash") != written.get("record_hash"):
        raise PersistError("execution journal did not retain the event")
    _write_checkpoint(rows)
    return dict(written)


def adopt_journal_checkpoint(*, reason: str) -> dict[str, Any]:
    """Operator recovery and migration only. Never called by the agent.

    Re-anchors the checkpoint to the journal as it now is (its chain must
    still verify) and records a ``CHECKPOINT_ADOPTED`` event carrying
    ``reason``. Unresolved records left in the journal keep blocking orders.

    Refused while an existing checkpoint anchors records the journal no
    longer holds (journal deleted, shortened or rolled back): adopting then
    would forget whatever those records said, including an order that may be
    live. Restore the journal from a backup instead.
    """
    text = str(reason or "").strip()
    if not text:
        raise ValueError("an operator reason is required")
    with _exclusive_lock():
        previous = _read_checkpoint()
        rows, exists = _read_verified(journal_path())
        if previous is not None:
            _check_checkpoint(rows, exists, previous)
        _write_checkpoint(rows)
        _append_locked(
            new_event(
                "CHECKPOINT_ADOPTED",
                f"admin:{uuid.uuid4()}",
                reason=text,
                records=len(rows),
                previous_checkpoint_records=None if previous is None else previous["records"],
            )
        )
        return {"records": len(rows), "unresolved": has_unresolved(rows)}


def append_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Append one event if the chain and the lifecycle transition allow it."""
    _reject_secrets(payload)
    event = str(payload.get("event") or "")
    state_key = str(payload.get("state_key") or "")
    if not event or not state_key:
        raise InvalidTransition("journal event requires an event name and a state key")
    with _exclusive_lock():
        rows = _records()
        previous = _latest_states(rows).get(state_key)
        if not transition_allowed(previous, event):
            raise InvalidTransition(f"cannot record {event} after {previous}")
        return _append_locked(dict(payload))


def begin_submission(
    payload: Mapping[str, Any],
    *,
    daily_limit: tuple[str, int] | None = None,
) -> dict[str, Any]:
    """Record ``SUBMISSION_PENDING`` only while every prior write is resolved.

    With ``daily_limit=(profile_id, limit)`` the event is time-stamped inside
    the lock and refused with ``DailyLimitReached`` when ``profile_id`` already
    has ``limit`` operations on that UTC day (reset at 00:00 UTC). The check
    and the append share the lock, so concurrent submissions cannot both pass.
    """
    if str(payload.get("event") or "") != "SUBMISSION_PENDING":
        raise InvalidTransition("begin_submission only records SUBMISSION_PENDING")
    _reject_secrets(payload)
    state_key = str(payload.get("state_key") or "")
    body = dict(payload)
    with _exclusive_lock():
        # A new order trusts nothing cached: the whole chain is walked before it is recorded.
        rows = _records(full=True)
        if has_unresolved(rows):
            raise UnresolvedExecution("an unresolved Binance submission blocks a new order")
        previous = _latest_states(rows).get(state_key)
        if not transition_allowed(previous, "SUBMISSION_PENDING"):
            raise InvalidTransition(f"cannot record SUBMISSION_PENDING after {previous}")
        if daily_limit is not None:
            profile_id, limit = daily_limit
            body["recorded_at"] = _now().isoformat()
            day = _utc_day(body["recorded_at"])
            if day is None or _operations_on(rows, profile_id, day) >= limit:
                raise DailyLimitReached(f"{profile_id} reached {limit} operations for {day} UTC")
        return _append_locked(body)


def new_event(event: str, state_key: str, **fields: Any) -> dict[str, Any]:
    """Build a secret-free journal body."""
    body = {
        "event": event,
        "event_id": str(uuid.uuid4()),
        "state_key": state_key,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    body.update(fields)
    _reject_secrets(body)
    return body


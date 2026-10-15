# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Cross-process exclusive file lock with bounded waiting.

Used by the execution journal and by ``binance.json`` persistence. The lock
is released only when it was acquired, an unlock failure never replaces the
exception that is already propagating, and a lock that cannot be taken in
``timeout`` seconds raises ``LockUnavailable`` instead of continuing unlocked.

On POSIX this is ``flock``. On Windows it is ``msvcrt.locking`` on byte 0 of
a dedicated lock file; that region is mandatory, so data files are never
locked this way (a reader of the data file would otherwise fail).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 10.0
_POLL = 0.05
_held = threading.local()


class LockUnavailable(OSError):
    """The lock could not be taken; the caller must not proceed unlocked."""


def _try_lock(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if msvcrt is not None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    raise LockUnavailable("no file lock backend is available")


def _unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if missing).

    Re-entrant per thread for the same path, so a reader called while the
    writer already holds the lock does not deadlock itself. Another thread or
    process waits up to ``timeout`` seconds, then gets ``LockUnavailable``.
    """
    key = str(Path(path).resolve())
    depth = getattr(_held, "paths", None)
    if depth is None:
        depth = _held.paths = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    acquired = False
    try:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                acquired = _try_lock(handle)
            except LockUnavailable:
                raise
            except OSError as exc:
                raise LockUnavailable(f"lock on {path.name} failed: {type(exc).__name__}") from exc
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(_POLL)
        if not acquired:
            raise LockUnavailable(f"lock on {path.name} was not acquired within {timeout:g}s")
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
    finally:
        try:
            if acquired:
                _unlock(handle)
        except OSError:
            # Closing the handle releases the lock; never mask the original error.
            pass
        finally:
            handle.close()

"""REST weight budget read from ``X-MBX-USED-WEIGHT-1M``.

Above 80% of the venue limit the collector slows down and the executor sends
only cancels and exits.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_USED = 0
_LIMIT = 6000
_PRESSURE = False


def note_weight(used: int, *, limit: int = 6000) -> bool:
    """Record the latest used weight. Return whether the bot is under pressure."""
    global _USED, _LIMIT, _PRESSURE
    with _LOCK:
        _USED = max(0, int(used))
        _LIMIT = max(1, int(limit))
        _PRESSURE = _USED / _LIMIT >= 0.80
        return _PRESSURE


def weight_pressure() -> bool:
    with _LOCK:
        return _PRESSURE


def weight_snapshot() -> dict[str, int | bool]:
    with _LOCK:
        return {"used": _USED, "limit": _LIMIT, "pressure": _PRESSURE}


def reset_weight() -> None:
    note_weight(0, limit=6000)

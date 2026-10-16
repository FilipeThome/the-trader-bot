"""REST weight budgets read from ``X-MBX-USED-WEIGHT-1M``.

Binance counts spot (``api.binance.com``, 6000 per minute) and USD-M futures
(``fapi.binance.com``, 1200 per minute) separately, so each venue keeps its own
counter. Above 80% of any venue's limit the collector slows down and the
executor sends only cancels and exits.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

Venue = Literal["spot", "usdm"]
_DEFAULT_LIMITS: dict[str, int] = {"spot": 6000, "usdm": 1200}

_LOCK = threading.Lock()
#: Wall clock in seconds; tests replace it to cross or stay in a minute.
_clock: Callable[[], float] = time.time


@dataclass
class _Budget:
    used: int = 0
    limit: int = 6000
    minute: int = -1

    def pressure(self, minute: int) -> bool:
        """Above 80% in the current minute; Binance resets the count every minute."""
        return self.minute == minute and self.used / self.limit >= 0.80


_BUDGETS: dict[str, _Budget] = {}


def note_weight(used: int, *, venue: Venue = "spot", limit: int | None = None) -> bool:
    """Record the weight one venue reported. Return whether any venue is under pressure.

    Responses read in parallel can arrive out of order, so within one
    wall-clock minute the highest report of a venue wins; its next minute
    starts from its own reports.
    """
    minute = int(_clock() // 60)
    with _LOCK:
        budget = _BUDGETS.setdefault(venue, _Budget(limit=_DEFAULT_LIMITS[venue]))
        value = max(0, int(used))
        budget.used = max(budget.used, value) if minute == budget.minute else value
        budget.minute = minute
        budget.limit = max(1, int(limit if limit is not None else _DEFAULT_LIMITS[venue]))
        return any(item.pressure(minute) for item in _BUDGETS.values())


def note_rate_limited(*, venue: Venue = "spot") -> None:
    """Binance answered 429 or 418: the venue counts as full for the rest of the minute."""
    minute = int(_clock() // 60)
    with _LOCK:
        budget = _BUDGETS.setdefault(venue, _Budget(limit=_DEFAULT_LIMITS[venue]))
        budget.used = budget.limit
        budget.minute = minute


def weight_pressure() -> bool:
    minute = int(_clock() // 60)
    with _LOCK:
        return any(item.pressure(minute) for item in _BUDGETS.values())


def weight_snapshot() -> dict[str, dict[str, int | bool]]:
    minute = int(_clock() // 60)
    with _LOCK:
        return {
            venue: {"used": item.used, "limit": item.limit, "pressure": item.pressure(minute)}
            for venue, item in _BUDGETS.items()
        }


def reset_weight() -> None:
    with _LOCK:
        _BUDGETS.clear()

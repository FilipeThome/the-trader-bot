"""Trailing stop rules shared by the live executor and the backtest.

``trailing`` follows the highest price at the entry's stop distance from the
first cycle. ``trailing_tp`` works like a trailing take-profit with an
activation price: the entry stop stays where it is until the price has gained
``activation_r`` times the stop distance, and from then on the stop follows
the highest price at ``callback_share`` of that distance, which locks a profit
at activation. Like ``DAY``, the values are constants: a validation holds only
for the rules it replayed, and they are recorded with it (``TRAIL.params``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, TypeVar

#: Exit policies whose stop follows the highest price.
TRAILING_POLICIES = frozenset({"trailing", "trailing_tp"})

_N = TypeVar("_N", float, Decimal)


@dataclass(frozen=True)
class TrailRules:
    #: Gain, in multiples of the entry stop distance, that starts the trailing.
    activation_r: float = 1.0
    #: Distance from the highest price once active, as a share of the entry stop distance.
    callback_share: float = 0.5

    @property
    def params(self) -> dict[str, Any]:
        return asdict(self)


TRAIL = TrailRules()


def trailing_level(policy: str, entry: _N, peak: _N, trail_pct: _N, rules: TrailRules = TRAIL) -> _N | None:
    """Where the stop of ``policy`` sits for ``peak``, or None while it does not trail yet.

    ``entry`` is the average entry price and ``trail_pct`` the entry stop distance as a
    share of it. Works on floats (backtest) and on Decimals (executor) alike.
    """
    number = type(entry)
    one = number(1)
    if policy == "trailing":
        return peak * (one - trail_pct)
    if policy != "trailing_tp":
        return None
    if peak < entry * (one + number(str(rules.activation_r)) * trail_pct):
        return None
    return peak * (one - number(str(rules.callback_share)) * trail_pct)

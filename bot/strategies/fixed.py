"""Fixed take-profit and stop-loss, in percent of the entry, set by the operator.

The operator's two percentages replace every strategy's own stop and target:
the stop rests at the broker at ``stop_loss_pct`` below the average fill, and
the cycle sells when the last 15m price reaches ``take_profit_pct`` above it.
No strategy exit, no trailing, no time stop.

The values are encoded in the policy string (``fixed:<tp_bps>:<sl_bps>``), so
the per-policy invariants hold without extra plumbing: positions record the
rule they opened under, the hit-rate sample only mixes trades of one rule, and
a validation is valid only for the exact percentages it replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from bot.strategies.base import Setup

PREFIX = "fixed:"
#: The cost filter wants a net return above twice the round trip (about 0.25%),
#: so a target under 1% would never be ready to trade.
MIN_TAKE_PROFIT = 0.01
MAX_TAKE_PROFIT = 1.0
MIN_STOP_LOSS = 0.005
MAX_STOP_LOSS = 0.5
#: Automatic entries need an estimated hit rate this far above the break-even of the
#: operator's payoff (``bot.risk.breakeven_win_rate``), like day rows.
FIXED_WIN_RATE_MARGIN = 0.03


def _bps(value: float) -> int:
    return int(round(value * 10_000))


@dataclass(frozen=True)
class FixedExit:
    take_profit_pct: float
    stop_loss_pct: float

    @property
    def policy(self) -> str:
        return f"{PREFIX}{_bps(self.take_profit_pct)}:{_bps(self.stop_loss_pct)}"

    def apply(self, setup: Setup) -> Setup:
        """The setup with the operator's stop and target.

        The expected return never rises above the strategy's own estimate: a wide
        target does not make a weak signal look strong to the score and cost filters.
        """
        return replace(
            setup,
            stop_pct=self.stop_loss_pct,
            target_pct=self.take_profit_pct,
            expected_return=min(setup.expected_return, self.take_profit_pct),
        )


def is_fixed(policy: str | None) -> bool:
    return bool(policy) and str(policy).startswith(PREFIX)


def parse_fixed(policy: str | None) -> FixedExit | None:
    """The rule a ``fixed:<tp_bps>:<sl_bps>`` policy names, or None for any other policy."""
    if not is_fixed(policy):
        return None
    try:
        take, stop = str(policy)[len(PREFIX) :].split(":")
        return FixedExit(int(take) / 10_000, int(stop) / 10_000)
    except ValueError:
        return None

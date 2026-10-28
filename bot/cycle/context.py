"""What one cycle decides on (``CycleContext``) and the writes scoring hands back (``ScoreEffect``).

They live apart from ``bot.runtime`` so the cycle modules and the ``Runtime`` can share them
without a module of ``bot.cycle`` importing the runtime. ``profile_for`` lives here for the
same reason; ``bot.runtime`` still exposes it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from bot.collector import MarketSnapshot
from bot.config import BotConfig, RiskLimits
from bot.risk import RiskState

if TYPE_CHECKING:
    # Types only: ``bot.advisor.service`` reaches ``bot.cycle.market``, which imports this module.
    from bot.advisor.service import AdviceKey, AdviceState

#: How a cycle module writes the ledger: the ``Runtime._ledger`` it is given, never ``append_ledger``.
Ledger = Callable[[str, dict[str, Any]], None]
#: How an event reaches the panel (``Runtime.publish``).
Publish = Callable[[dict[str, Any]], None]


def profile_for(config: BotConfig) -> str:
    if config.mode == "production":
        return "binance-live-trade-bot"
    return "binance-paper-trade"


@dataclass(frozen=True)
class CycleContext:
    """What one cycle decides on, read once after the robots acted and the daily review ran.

    ``_score``, ``_protect`` and ``_allocate`` read it instead of the store, so scoring and
    allocation see the same book, equity and mandate. ``limits`` is the mandate-tightened
    limits read at the start of the cycle. ``state`` is the cycle's risk level; None outside
    the cycle (a manual or armed entry), where no level is measured and the executor refuses
    buys on the stored one.
    """

    moment: datetime
    snapshot: MarketSnapshot
    #: Open positions (``Store.open_positions``).
    positions: list[dict[str, Any]]
    #: (symbol, strategy) of every live position, pending ones included.
    live: frozenset[tuple[str, str]]
    #: Live positions plus what each DCA or grid instance reserved (``Runtime._book``).
    book: list[dict[str, Any]]
    #: What each held pair is worth at the last price (``_held_usd``).
    held_usd: dict[str, float]
    equity: float
    #: The configured capital: equity, open risk and drawdown are measured on it.
    capital: float
    #: The capital orders are sized on (``Runtime._sizing_capital``).
    sizing_capital: float
    cash: float | None
    returns: dict[str, list[float]]
    state: RiskState | None
    takers: Mapping[str, float]
    limits: RiskLimits
    #: A copy of the advisor's verdict cache taken for this cycle (``Advisor.snapshot``); empty
    #: with the advisor off. Scoring reads it, so it stays free of side effects.
    advice: Mapping[AdviceKey, AdviceState] = field(default_factory=dict)
    #: The runtime store's ``Store.identity``: the advice keys of this cycle carry it, so a
    #: verdict another mode's store asked for is never read here.
    advice_scope: str = ""


#: What a ``ScoreEffect`` writes: an equity point, the pair blocks with their ledger event, a
#: refusal counted in the day's violations avoided, a decision for the history, or a forgotten
#: decision (the setup is gone, so the next one is new).
EffectKind = Literal["equity", "pair_blocked", "violation", "decision", "forget"]


@dataclass(frozen=True)
class ScoreEffect:
    """One write ``_score`` decided; ``_cycle`` applies them in order (``_apply_effects``).

    ``data`` is copied at score time: the steps after scoring change the rows' status and
    reason, and the history must keep the decision as it was scored.
    """

    kind: EffectKind
    data: Mapping[str, Any]

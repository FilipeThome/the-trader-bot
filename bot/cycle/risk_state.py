"""The account's risk level of one cycle (D5, D20): equity, drawdown, the daily loss and its pause.

``_risk_state`` measures and stores the level and hands back the halt reason; it never halts.
The ``Runtime`` runs the halt right after it, before the robots act, as the cycle always did.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from bot.collector import MarketSnapshot
from bot.config import BotConfig, RiskLimits
from bot.cycle import exits, market
from bot.cycle.context import Ledger, Publish
from bot.executor import Executor
from bot.risk import RiskManager, RiskState, buy_block, utc_day
from bot.store import Store


def _equity(snapshot: MarketSnapshot, moment: datetime, *, store: Store, config: BotConfig) -> tuple[float, float, float]:
    """Today's net PnL, the equity (configured capital, every closed day, the open positions
    at the last price) and the peak equity: the configured capital plus the PnL peak, so a
    change of capital never reads as a drawdown."""
    days = store.pnl_days()
    day = utc_day(moment)
    day_row = next((row for row in days if row["day"] == day), None)
    day_pnl = float(day_row["net_pnl"]) if day_row else 0.0
    positions = store.open_positions()
    capital = config.capital_for_sizing()
    pnl = sum(float(row["net_pnl"]) for row in days) + exits._unrealized(positions, snapshot)
    return day_pnl, capital + pnl, capital + max(store.peak_pnl(), pnl)


def _risk_state(
    snapshot: MarketSnapshot,
    moment: datetime,
    *,
    store: Store,
    executor: Executor,
    config: BotConfig,
    limits: RiskLimits,
    ledger: Ledger,
    publish: Publish,
    running: Callable[[], bool],
) -> tuple[RiskState, str | None]:
    """The account's risk level of this cycle, stored for the executor, and the halt reason
    when the level is the halt step (None otherwise).

    The drawdown is measured from the PnL peak and the daily loss on equity (open
    positions at the last price) against the first cycle of the UTC day. The level is
    stored in ``bot_state.risk_state``, where the executor refuses every buy while it is
    ``reducing``; the halt step is stored as ``reducing`` too, so buys stay refused after
    a manual resume until the drawdown recovers. A change of level goes to the ledger and
    the panel. The caller halts on the halt step; the daily loss exits the day trades first
    when ``flatten_day_on_daily_loss`` is on and ``running()`` (the cycle runs, no halt),
    then pauses the day. ``limits`` is the cycle's.
    """
    capital = config.capital_for_sizing()
    day_pnl, equity, peak = _equity(snapshot, moment, store=store, config=config)
    day_change = _day_change(moment, equity, capital, day_pnl, store=store)
    previous = store.get_state("risk_state")
    previous = previous if isinstance(previous, dict) else None
    state = RiskManager(limits, capital, capital).portfolio_state(
        day_change=day_change, peak_equity=peak, equity=equity, previous=previous
    )
    paused = _paused(moment, store=store)
    if paused is not None and not state.pause_day:
        # A pause started earlier today holds until the next UTC day, even if the equity recovers.
        state = replace(state, pause_day=True, reason="; ".join(part for part in (state.reason, paused) if part))
    _save_risk_state(state, previous, moment, store=store, ledger=ledger, publish=publish)
    if state.level == "halt":
        return state, state.reason
    if state.pause_day and paused is None:
        if config.flatten_day_on_daily_loss and running():
            _flatten_day_trades(store=store, executor=executor, config=config, ledger=ledger)
        _pause_day(moment, store=store, ledger=ledger)
    return state, None


def _day_change(moment: datetime, equity: float, capital: float, day_pnl: float, *, store: Store) -> float:
    """Equity now minus the equity at the start of the UTC day (``bot_state.day_start_equity``).

    The start is written by the first cycle of the day, less what closed earlier that day
    (a bot started mid-day still counts the day's realised loss). A change of the configured
    capital moves the equity, not the result, so it is netted out.
    """
    day = utc_day(moment)
    start = store.get_state("day_start_equity")
    if not isinstance(start, dict) or start.get("day") != day or not market._finite(start.get("equity")):
        if not (market._finite(equity) and market._finite(day_pnl)):
            return math.nan
        start = {"day": day, "equity": equity - day_pnl, "capital": capital}
        store.set_state("day_start_equity", start)
    base = float(start["capital"]) if market._finite(start.get("capital")) else capital
    return (equity - capital) - (float(start["equity"]) - base)


def _save_risk_state(
    state: RiskState, previous: dict[str, Any] | None, moment: datetime, *, store: Store, ledger: Ledger, publish: Publish
) -> None:
    level = "reducing" if state.level == "halt" else state.level
    before = str((previous or {}).get("level") or "normal")
    since = (previous or {}).get("since") if before == level else None
    record = {
        "level": level,
        "since": since or moment.isoformat(),
        "drawdown": round(state.drawdown, 6),
        "reason": state.reason,
    }
    if record != previous:
        store.set_state("risk_state", record)
    if level != before:
        ledger("risk_state", {"from": before, "to": level, "drawdown": state.drawdown, "reason": state.reason})
        publish({"type": "risk_state", **record})


def _flatten_day_trades(*, store: Store, executor: Executor, config: BotConfig, ledger: Ledger) -> None:
    """Exit every open day trade before the daily pause (``flatten_day_on_daily_loss``)."""
    positions = [row for row in store.open_positions() if exits._policy(row, config) == "day"]
    if not positions:
        return
    results = []
    for position in positions:
        result = executor.exit_position(int(position["id"]), "perda diária: day trade zerado antes da pausa")
        results.append({"position_id": position["id"], "symbol": position["symbol"], "status": result.get("status")})
    ledger("daily_loss_flatten", {"positions": len(positions), "results": results})


def _pause_day(moment: datetime, *, store: Store, ledger: Ledger) -> None:
    """Refuse every buy until the next UTC day: the daily-loss limit (``paused_until``).

    The cycle keeps running, so the sync (stuck stops), trailing, first targets, time
    exits and robot sales go on; the executor refuses each buy while the pause lasts.
    """
    tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    store.set_state("paused_until", tomorrow.isoformat())
    ledger("daily_pause", {"until": tomorrow.isoformat()})


def _paused(moment: datetime, *, store: Store) -> str | None:
    """The daily-loss pause still running at ``moment``, as its reason, or None."""
    return buy_block(None, store.get_state("paused_until"), moment)

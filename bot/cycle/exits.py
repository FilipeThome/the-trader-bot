"""Exits of one cycle: the MAE and MFE marks, the trailing stop, the strategy exits, the day
trade's first target and time limit, and the readers of a position's recorded leg.

Orders go through the ``executor`` given and events through the ``ledger`` given; prices are
read through ``market._last_price``.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from bot.collector import MarketSnapshot
from bot.config import BotConfig
from bot.cycle import market
from bot.cycle.context import Ledger
from bot.executor import Executor
from bot.indicators import ema
from bot.store import Store
from bot.strategies import Momentum, TrendBreakout
from bot.strategies.fixed import is_fixed
from bot.strategies.trailing import TRAILING_POLICIES

#: Exit policies of DCA and grid inventory: their instance decides every exit.
INSTANCE_POLICIES = frozenset({"dca", "grid"})
#: Exit frames of positions opened before the entry timeframe was recorded.
LEGACY_EXIT_TIMEFRAMES = {"trend": "1h", "mean_reversion": "15m", "momentum": "4h"}
_TREND = TrendBreakout()
_MOMENTUM = Momentum()


def _policy(position: dict[str, Any], config: BotConfig) -> str:
    """The exit rule a position opened under; rows from before it was recorded follow the config."""
    return str(position.get("exit_policy") or config.exit_policy)


def _mark(snapshot: MarketSnapshot, *, store: Store, executor: Executor) -> None:
    """Record each open position's lowest and highest last price (MAE and MFE). No order."""
    for position in store.open_positions():
        price = market._last_price(snapshot, position["symbol"], "15m")
        if price is not None and price > 0:
            executor.mark(int(position["id"]), Decimal(str(price)))


def _trail(snapshot: MarketSnapshot, *, store: Store, executor: Executor, config: BotConfig, ledger: Ledger) -> None:
    positions = store.open_positions()
    # One order per open position stays reserved for its own re-stop or exit.
    reserve = 1 + len(positions)
    for position in positions:
        if _policy(position, config) not in TRAILING_POLICIES:
            continue
        price = market._last_price(snapshot, position["symbol"], "15m")
        if price is None:
            continue
        outcome = executor.trail(position["id"], Decimal(str(price)), reserve=reserve)
        if outcome == "movido":
            ledger("trail", {"symbol": position["symbol"], "position_id": position["id"]})


def _exits(
    snapshot: MarketSnapshot, moment: datetime, *, store: Store, executor: Executor, config: BotConfig, ledger: Ledger
) -> None:
    positions = store.open_positions()
    reserve = 1 + len(positions)
    for position in positions:
        if _policy(position, config) in INSTANCE_POLICIES:
            continue
        if _policy(position, config) == "day":
            _day_exit(position, snapshot, moment, reserve, executor=executor, ledger=ledger)
            continue
        if is_fixed(_policy(position, config)):
            # The operator's rule: only the target recorded at the fill, or the broker stop.
            reason = _target_reason(position, snapshot)
            if reason is not None:
                result = executor.exit_position(position["id"], reason)
                ledger("exit", {"symbol": position["symbol"], "reason": reason, "status": result.get("status")})
            continue
        reason = _exit_reason(position, snapshot, moment, use_target=_policy(position, config) == "target")
        if reason is None:
            continue
        result = executor.exit_position(position["id"], reason)
        ledger("exit", {"symbol": position["symbol"], "reason": reason, "status": result.get("status")})


def _day_exit(
    position: dict[str, Any], snapshot: MarketSnapshot, moment: datetime, reserve: int, *, executor: Executor, ledger: Ledger
) -> None:
    """A day trade leaves at its time limit; before that, its first target sells part of it.

    The first target triggers on the close of the last closed 15m bar, as the
    backtest replays it; the stop at the broker covers everything else.
    """
    deadline = position.get("exit_deadline")
    if deadline and moment >= datetime.fromisoformat(str(deadline)):
        result = executor.exit_position(position["id"], "tempo máximo do day trade")
        ledger("exit", {"symbol": position["symbol"], "reason": "day_deadline", "status": result.get("status")})
        return
    target = position.get("target_price")
    if position.get("tp1_done") or not target:
        return
    frame = market._closed(snapshot, position["symbol"], "15m", moment)
    if frame is None or float(frame["close"].iloc[-1]) < float(target):
        return
    outcome = executor.take_profit(position["id"], reserve=reserve)
    if outcome in ("vendido", "concluído"):
        # A waiting first target is retried every cycle; only what happened goes to the ledger.
        ledger("take_profit", {"symbol": position["symbol"], "position_id": position["id"], "outcome": outcome})


def _inflight(position: dict[str, Any]) -> str | None:
    """Kind of the order a position recorded in flight (``pending_leg``), or None."""
    raw = position.get("pending_leg")
    try:
        leg = json.loads(str(raw)) if raw else None
    except ValueError:
        return None
    return str(leg.get("kind")) if isinstance(leg, dict) else None


def _rest_level(position: dict[str, Any]) -> int | None:
    """Level index a robot's resting buy waits at (``pending_leg.level``), or None."""
    raw = position.get("pending_leg")
    try:
        leg = json.loads(str(raw)) if raw else None
    except ValueError:
        return None
    level = leg.get("level") if isinstance(leg, dict) else None
    return level if isinstance(level, int) and not isinstance(level, bool) else None


def _target_reason(position: dict[str, Any], snapshot: MarketSnapshot) -> str | None:
    """"alvo atingido" when the last 15m price reached the position's recorded target."""
    price = market._last_price(snapshot, position["symbol"], "15m")
    target = position.get("target_price")
    if price is not None and target and price >= float(target):
        return "alvo atingido"
    return None


def _exit_reason(
    position: dict[str, Any], snapshot: MarketSnapshot, moment: datetime, *, use_target: bool = True
) -> str | None:
    """Target (unless the stop trails instead) and the strategy's own exit.

    The exit reads the closed bars of the timeframe the position entered on, as the
    replay does, with the exit hysteresis it entered with. A row from before both were
    recorded keeps the old frames (``LEGACY_EXIT_TIMEFRAMES``) and exits without hysteresis.
    """
    symbol = position["symbol"]
    if use_target and (reached := _target_reason(position, snapshot)):
        return reached
    strategy = str(position["strategy"])
    timeframe = str(position.get("timeframe") or LEGACY_EXIT_TIMEFRAMES.get(strategy, "1h"))
    hysteresis = bool(position.get("exit_hysteresis"))
    if strategy in ("trend", "momentum"):
        frame = market._closed(snapshot, symbol, timeframe, moment)
        rule = _TREND if strategy == "trend" else _MOMENTUM
        reason = rule.exit_reason(frame, "buy", hysteresis=hysteresis) if frame is not None else None
        if reason:
            return reason
    if strategy == "mean_reversion":
        frame = market._closed(snapshot, symbol, timeframe, moment)
        if frame is not None and len(frame) >= 20 and float(frame["close"].iloc[-1]) >= float(ema(frame["close"], 20).iloc[-1]):
            return "preço voltou à média"
    return None


def _unrealized(positions: list[dict[str, Any]], snapshot: MarketSnapshot) -> float:
    total = 0.0
    for row in positions:
        price = market._last_price(snapshot, row["symbol"], "15m")
        if price is not None:
            total += (price - float(row["avg_price"])) * float(row["qty"])
    return total

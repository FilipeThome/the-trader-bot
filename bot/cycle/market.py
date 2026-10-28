"""Market reads of one cycle: the pair choice, the collect, the account fees, the free USDT, and
the candle helpers every other part reads prices through.

``_last_price`` is owned here: the other cycle modules and the ``Runtime`` call it as
``market._last_price``, never through an imported name, so one patch of it reaches them all.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import pandas as pd

from bot.collector import Collector, MarketSnapshot
from bot.config import BotConfig
from bot.cycle.context import Ledger
from bot.exchange import service
from bot.exchange.weight import weight_pressure
from bot.markets import merge_symbols, rank_promising, venue_symbols
from bot.risk import CLUSTER_ANCHOR
from bot.store import Store

logger = logging.getLogger(__name__)

_PRICE_INTERVALS = ("1m", "15m", "1h", "4h")
#: Seconds between two automatic choices of the analysed pairs.
AUTO_SELECT_SECONDS = 3600
#: ``bot_state`` key of the liquid pool the last ranking read: the validation universe (Q-A6).
UNIVERSE_STATE = "auto_select_universe"
#: Read every cycle, analysed or not: the market filter and the correlation cluster need it.
MARKET_SYMBOL = CLUSTER_ANCHOR
#: Most seconds one cycle waits for the account-fee reads; the rest finish in the background.
FEE_WARMUP_BUDGET_SECONDS = 2.0

#: Bar widths of the intervals the strategies read.
_BAR = {
    "1m": pd.Timedelta(minutes=1),
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
}


@dataclass(frozen=True)
class _Market:
    """What one cycle read from Binance before taking the lock."""

    snapshot: MarketSnapshot
    cash: float | None
    #: Live positions (id, symbol, status, qty) when ``cash`` was read.
    positions: frozenset[tuple[int, str, str, float]] = frozenset()
    #: Pairs this collect read: the analysed ones plus every pair with a live position.
    read: frozenset[str] = frozenset()
    #: The account's taker fee per pair, when known (``service.trading_fees``).
    takers: Mapping[str, float] = field(default_factory=dict)


class FeeWarmup(Protocol):
    """The fee warm-up state the ``Runtime`` owns for its lifetime (``_account_takers``).

    ``retired`` is read again by the warm-up thread before each pair: a runtime replaced by a
    mode switch stops reading fees.
    """

    retired: bool
    _fee_lock: threading.Lock
    _fee_worker: threading.Thread | None
    _fee_takers: dict[str, float]


def _collect(
    store: Store,
    collector: Collector,
    config: BotConfig,
    *,
    account_takers: Callable[[list[str]], dict[str, float]],
    free_cash: Callable[[], float | None],
) -> _Market:
    """Network reads of one cycle after the pair choice, outside the lock: clock, candles,
    account fees and free USDT. ``config`` is the runtime's once the pair choice applied."""
    # Pairs with a position are always read, even when the analysed list dropped them
    # (a hand edit, the automatic choice): their stops and exits need fresh candles.
    # No cap here: every analysed pair is scored, so every one must be read too.
    # A DCA or grid instance needs its pair's price even while it holds nothing.
    # BTC/USDT is always read: the market filter and the correlation cluster need it.
    instanced = [str(row["symbol"]) for row in store.instances(live_only=True)]
    symbols = list(dict.fromkeys([*sorted(_held(_positions(store))), *instanced, *config.symbols, MARKET_SYMBOL]))
    collector.sync_clock()
    snapshot = collector.refresh(symbols)
    read = frozenset(symbols)
    takers = account_takers(symbols)
    positions = _positions(store)
    _refuse_blind(snapshot, _held(positions), read)
    return _Market(snapshot=snapshot, cash=free_cash(), positions=positions, read=read, takers=takers)


def _account_takers(owner: FeeWarmup, symbols: list[str], profile: str) -> dict[str, float]:
    """The account's taker fee of each pair (``service.trading_fees``), outside the lock.

    A pair is read from Binance at most once a day (once an hour after a failure);
    the cache it fills is what ``service.market_rules`` books fees with. Under API
    weight pressure nothing is read; a failed read leaves the pair out, so scoring
    keeps ``SPOT_TAKER``. The reads run on one background thread and the cycle waits
    for them at most ``FEE_WARMUP_BUDGET_SECONDS``: a pair not read by then counts with
    its last known fee, or none, and its read goes on for the next cycle. While a read
    hangs, no second thread starts.
    """
    if owner.retired or weight_pressure():
        return {}
    with owner._fee_lock:
        worker = owner._fee_worker
        if worker is None or not worker.is_alive():
            worker = threading.Thread(
                target=_read_fees, args=(owner, list(symbols), profile), name="bot-fee-warmup", daemon=True
            )
            owner._fee_worker = worker
            worker.start()
    worker.join(FEE_WARMUP_BUDGET_SECONDS)
    with owner._fee_lock:
        return {symbol: owner._fee_takers[symbol] for symbol in symbols if symbol in owner._fee_takers}


def _read_fees(owner: FeeWarmup, symbols: list[str], profile: str) -> None:
    """The fee warm-up thread: one ``service.trading_fees`` read per pair (most are cached)."""
    for symbol in symbols:
        if owner.retired or weight_pressure():
            return
        try:
            fees = service.trading_fees(symbol, profile)
        except Exception:  # noqa: BLE001 - a fee read never fails the cycle
            logger.exception("account fee read failed for %s", symbol)
            continue
        if fees is not None and _finite(fees[1]):
            with owner._fee_lock:
                owner._fee_takers[symbol] = float(fees[1])


def _choose_pairs(
    moment: datetime, *, retired: bool, config: BotConfig, store: Store, collector: Collector, ledger: Ledger
) -> list[str] | None:
    """Rank the most promising pairs, at most once an hour. Network only: runs outside the lock.

    Returns the pairs to analyse, or None to keep the current ones. A failure
    keeps the current pairs and is logged; it never counts as a failed cycle.
    The liquid pool the ranking read is saved as ``UNIVERSE_STATE``: validations
    replay that pool, and a change of it (not of the chosen pairs) makes them stale.
    """
    if retired or not config.auto_select or weight_pressure():
        return None
    try:
        last = store.get_state("auto_select_at")
        if last and (moment - datetime.fromisoformat(str(last))).total_seconds() < AUTO_SELECT_SECONDS:
            return None
        store.set_state("auto_select_at", moment.isoformat())
        listed = venue_symbols(collector, config.mode)
        chosen, universe = rank_promising(collector, config.auto_select_count, listed=listed)
        if universe:
            store.set_state(UNIVERSE_STATE, sorted(universe))
        if not chosen:
            # No pair is rising: keep the current pairs the venue can still trade.
            chosen = [symbol for symbol in config.symbols if listed is None or symbol in listed]
            if not chosen or chosen == config.symbols:
                ledger("auto_select", {"symbols": config.symbols, "kept": True})
                return None
        return chosen
    except Exception as exc:  # noqa: BLE001 - the cycle goes on with the pairs it has
        logger.exception("automatic pair selection failed")
        ledger("auto_select_error", {"error": type(exc).__name__})
        return None


def _apply_pairs(chosen: list[str], *, retired: bool, config: BotConfig, store: Store, ledger: Ledger) -> BotConfig | None:
    """Save the chosen pairs, with the pairs of live positions first. Caller holds the lock.

    Returns the runtime's config with the new pairs for the caller to keep, or None when
    nothing changes in memory.
    """
    if retired or not config.auto_select:
        # The panel rebound the runtime (pairs, auto-select or mode changed) while the
        # ranking ran: its choice wins, and this runtime writes nothing.
        return None
    try:
        live = [str(row["symbol"]) for row in store.live_positions()]
        # Start from the file, so a hand edit of bot.json (the gates live only there) is kept.
        on_disk = BotConfig.load() if config.config_path().is_file() else config
        data = on_disk.model_dump()
        if data["mode"] != config.mode:
            raise RuntimeError("bot.json changed mode outside the panel")
        data["symbols"] = merge_symbols(live, chosen)
        saved = BotConfig.model_validate(data)
        saved.save()
        # Only the pairs change in memory: a hand edit of entry mode, limits or exit rule
        # takes effect through the panel or a restart, which run their guards.
        updated = config.model_copy(update={"symbols": saved.symbols})
        ledger("auto_select", {"symbols": saved.symbols})
        return updated
    except Exception as exc:  # noqa: BLE001 - the cycle goes on with the pairs it has
        logger.exception("automatic pair selection failed")
        ledger("auto_select_error", {"error": type(exc).__name__})
        return None


def _free_cash(profile: str) -> float | None:
    """Free USDT of the trading profile; ``None`` when the account cannot be read."""
    try:
        value = service.free_balance("USDT", profile)
    except Exception:  # noqa: BLE001 - an unreadable balance blocks entries, never exits
        logger.exception("free USDT read failed")
        return None
    return None if value is None else float(value)


def _positions(store: Store) -> frozenset[tuple[int, str, str, float]]:
    return frozenset(
        (int(row["id"]), str(row["symbol"]), str(row["status"]), float(row["qty"] or 0)) for row in store.live_positions()
    )


def _held(positions: frozenset[tuple[int, str, str, float]]) -> frozenset[str]:
    return frozenset(symbol for _id, symbol, _status, _qty in positions)


def _refuse_blind(snapshot: MarketSnapshot, held: frozenset[str], read: frozenset[str]) -> None:
    """A position whose pair this collect did not read, or failed to read, cannot be
    trailed or exited on the snapshot: its candles would be an older cycle's."""
    blind = sorted(held - (read - snapshot.failed))
    if blind:
        raise RuntimeError(f"preços indisponíveis para pares com posição: {', '.join(blind)}")


def _price_interval(timeframe: str) -> str:
    return timeframe if timeframe in _PRICE_INTERVALS else "1h"


def _closed(snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime) -> pd.DataFrame | None:
    """The candles of ``timeframe`` without the one still forming at ``moment``.

    Binance returns the open candle last. The backtest decides once per closed
    bar, so the live strategies and their exits read closed bars too; prices
    and stops still use the live close (``_last_price``).
    """
    frame = _frame(snapshot, symbol, timeframe)
    width = _BAR.get(timeframe)
    if frame is None or width is None:
        return frame
    if frame.index[-1] + width > pd.Timestamp(moment):
        frame = frame.iloc[:-1]
    return None if frame.empty else frame


def _frame(snapshot: MarketSnapshot, symbol: str, timeframe: str) -> pd.DataFrame | None:
    series = snapshot.candles.get((symbol, timeframe))
    if series is None or series.frame.empty:
        return None
    return series.frame


def _last_price(snapshot: MarketSnapshot, symbol: str, timeframe: str) -> float | None:
    frame = _frame(snapshot, symbol, _price_interval(timeframe))
    if frame is None:
        return None
    return float(frame["close"].iloc[-1])


def _age(snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime) -> float | None:
    series = snapshot.candles.get((symbol, _price_interval(timeframe)))
    if series is None:
        return None
    return (moment - series.fetched_at).total_seconds()


def _held_usd(positions: list[dict[str, Any]], snapshot: MarketSnapshot) -> dict[str, float]:
    """What each held pair is worth at the last price (the average price when unread)."""
    held: dict[str, float] = {}
    for row in positions:
        price = _last_price(snapshot, row["symbol"], "15m") or float(row["avg_price"] or 0)
        held[str(row["symbol"])] = held.get(str(row["symbol"]), 0.0) + float(row["qty"] or 0) * price
    return held


def _returns(snapshot: MarketSnapshot, moment: datetime | None = None) -> dict[str, list[float]]:
    """Hourly returns of every pair read, on closed bars when ``moment`` is given."""
    out: dict[str, list[float]] = {}
    for (symbol, interval), series in snapshot.candles.items():
        if interval != "1h" or series.frame.empty:
            continue
        frame = _closed(snapshot, symbol, "1h", moment) if moment is not None else series.frame
        if frame is None:
            continue
        out[symbol] = frame["close"].pct_change().dropna().tolist()
    return out


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

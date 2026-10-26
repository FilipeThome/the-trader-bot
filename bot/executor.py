"""Position lifecycle on Binance spot. Every order goes through ``bot.exchange.service``.

A position moves ``pending -> open -> closed`` or ``pending -> void``:

* entry: a ``pending`` row reserves the slot, then a LIMIT IOC buy priced
  ``ENTRY_LIMIT_GAP`` above the ask and sized in base. A partial fill opens with
  the quantity filled, no fill voids the row, and the stop and target are set on
  the average fill from the setup's distances;
* protection: right after the fill a STOP_LOSS_LIMIT sell rests on Binance, so
  the position is protected even when this process is down;
* exit: the stop is taken off the book and the quantity is sold with LIMIT IOC
  orders below the bid (the Spot Testnet refuses market sells sized in base);
  a partial fill re-places the stop on what is left.

Every order key comes from the position id, the leg and an attempt counter
that moves on after each final no-fill outcome, so a rejected order never
blocks the next one and a retried unknown outcome replays instead of resending.

DCA and grid instances (``bot.instances``) hold their inventory in an ordinary
position: ``open_instance`` opens it, ``scale_in`` adds a market buy and moves
the stop over the whole, and partial IOC sales reuse ``exit_position``. The
order in flight is recorded in ``pending_leg`` before it is sent, so the sync
reads it back by its key.

An instance with ``resting_buys`` rests at most one LIMIT GTC buy under the
price instead (``rest_buy``, leg ``rest_buy``). Every sync reconciles it before
the stop: a fill is merged and the stop re-placed over the whole, a partial fill
is cancelled and merged, a dead order is forgotten. An exit, the halt and every
buy block cancel it first and book what it bought, and after a cancel the order
is always read again: it may have filled between the read and the cancel. No
sale and no stop close ever runs over a resting buy whose result is unknown. A
stop that sold while a resting buy filled leaves the row open with the coins
the buy added, under a new stop (``_carry_after_stop``). Until it is settled, the
mandate gate counts a resting buy as held at its full quantity on every buy
(``_held_for_buy``): it fills later without passing the gate again. Sells (stops
and exits) are checked against the open positions only (``_held_for_sell``), so
a resting buy never makes the gate refuse a stop or an exit near the cap.

A stop that triggered but rests unfilled because the price gapped below its
limit is found by price in the sync (last trade at or under the stop, bid under
its limit): it is cancelled and the position sold with an IOC below the bid,
then one more 2% below it (``EMERGENCY_LIMIT_GAP``).

When a position cannot be protected or closed, ``UnprotectedPosition`` is
raised and the runtime halts.

Every position records its execution quality: the quote its entry was priced on,
the slippage of each leg, the stop and the risk in USD at the fill, the lowest and
highest price while open (``mark``) and the result in R. Each time a stop is taken
off and put back (trailing, first target, partial sale, re-stop, remainder of an
exit), the time the position stood without it is stored (``stop_windows``).

While the portfolio is in REDUCING (``bot_state.risk_state``) or the daily-loss
pause runs (``bot_state.paused_until``), every buy is refused here: entries,
instance inventory, scale-ins and resting buys. Stops, exits and the sync go on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bot.evaluator import SPOT_TAKER, Opportunity
from bot.exchange import service
from bot.exchange.execution.rules import MarketRules, decimal_text, floor_to_step
from bot.exchange.weight import weight_pressure
from bot.instances import ORDERS_PER_RESTING_LEG
from bot.reports import append_ledger
from bot.store import Store
from bot.strategies import exit_policy_for
from bot.strategies.day import DAY
from bot.strategies.fixed import parse_fixed
from bot.strategies.trailing import trailing_level

logger = logging.getLogger(__name__)

#: A stop-limit sells at most this far below its trigger.
STOP_LIMIT_GAP = Decimal("0.005")
#: An exit IOC is priced this far below the bid so it crosses the book.
EXIT_LIMIT_GAP = Decimal("0.005")
#: An entry IOC is priced this far above the ask: it crosses the book, and the slippage
#: of a strategy entry is bounded by it.
ENTRY_LIMIT_GAP = Decimal("0.002")
#: Most room a trailing stop must leave below ``min(bid, last)`` (D16). It is at most half
#: the trail distance and never less than the stop-limit gap, so a small callback still moves.
TRAIL_CLEARANCE = Decimal("0.01")
#: The second and last IOC for a stop the price gapped through. No market sell follows:
#: the Spot Testnet refuses market sells sized in base, so that path could never be tested.
EMERGENCY_LIMIT_GAP = Decimal("0.02")
#: Reason recorded when a stuck stop is replaced by an IOC sale.
_STOP_GAPPED_REASON = "stop acionado sem execução (gap)"
#: ``_restop`` when the old stop did not come off: the part the buy added waits for the next sync.
RESTOP_OLD_STOP_PENDING = "o stop antigo não saiu da corretora; a parte nova espera o próximo ciclo"
#: End of ``_restop``'s answer under a write blocker: the old stop stays until the gate allows writes.
RESTOP_BLOCKED_SUFFIX = "; o stop antigo fica até a liberação"
#: Syncs in a row a stuck stop may fail to come off before the runtime halts.
STUCK_STOP_MAX_RETRIES = 3
_STUCK_RETRIES_KEY = "stop_gapped_retries"
#: ``cancel_rest`` when the resting buy's cancel or final state could not be confirmed.
REST_CANCEL_PENDING = "cancelamento da compra limitada pendente"
#: ``cancel_rest`` when the buy filled before the cancel but its re-stop did not finish: the coins it
#: added have no stop yet (a write blocker, or the old stop still on the book).
REST_RESTOP_PENDING = "compra limitada executada; o stop da parte nova ainda não foi refeito"
#: ``rest_buy`` outcomes for a buy that went to the book, or whose send has an unknown result.
REST_PLACED = "compra limitada no livro"
REST_PENDING = "compra limitada pendente"
#: Entry, stop and exit. An entry is refused unless the day still allows all three.
ORDERS_PER_ROUND_TRIP = 3
#: A day trade also sells its first target and re-places the stop at break-even.
ORDERS_PER_DAY_ROUND_TRIP = 5
#: The only profile a test-mode row may trade on.
TEST_PROFILE = "binance-paper-trade"
#: Stop distance used when a filled entry carries no planned stop.
DEFAULT_STOP_PCT = Decimal("0.02")
#: A trailing stop moves only when the new level is at least this much higher.
TRAIL_STEP = Decimal("0.005")
#: An order is sized this far under its notional cap.
_CAP_HEADROOM = Decimal("0.95")
#: A pending entry the exchange does not know is void after this many seconds.
_ACCEPTANCE_WINDOW_SECONDS = 120
_FILLED = frozenset({"closed", "filled"})
_DEAD = frozenset({"canceled", "cancelled", "expired", "rejected"})


class UnprotectedPosition(RuntimeError):
    """A position is open with no stop and could not be closed. The runtime halts."""


@dataclass(frozen=True)
class Fill:
    qty: Decimal
    cost: Decimal
    fee_base: Decimal
    fee_quote: Decimal

    @property
    def average(self) -> Decimal:
        return self.cost / self.qty if self.qty > 0 else Decimal(0)


#: Writes one ledger record: ``(kind, payload)``.
Ledger = Callable[[str, dict[str, Any]], None]


def _append_ledger(kind: str, payload: dict[str, Any]) -> None:
    """Default ledger. A ledger failure must not send, block or interrupt an order."""
    try:
        append_ledger({"kind": kind, **payload})
    except Exception:  # noqa: BLE001 - visible in the log; the exit goes on
        logger.exception("ledger append failed (%s)", kind)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day() -> str:
    return _now().strftime("%Y-%m-%d")


def _status(result: dict[str, Any]) -> str:
    return str(result.get("status") or "").lower()


def _reason(result: dict[str, Any]) -> str:
    for key in ("error", "reason"):
        if result.get(key):
            return str(result[key])
    reasons = result.get("reasons")
    if reasons:
        return ", ".join(str(item) for item in reasons)
    return "ordem recusada"


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _leg(position: dict[str, Any]) -> dict[str, Any] | None:
    """The order in flight a position recorded (``pending_leg``), or None."""
    raw = position.get("pending_leg")
    if not raw:
        return None
    try:
        leg = json.loads(str(raw))
    except ValueError:
        return None
    return leg if isinstance(leg, dict) else None


def _gate_margin(rules: MarketRules | None) -> Decimal:
    """Fee margin an order keeps: the larger of the account's taker fee and the market's default.

    The order gate checks free balances against the market's default fee (``qty x (1 + fee)``),
    so a stop sized on a lower account fee would be refused. The account's own fee stays the one
    booked (``_fill``) and scored.
    """
    if rules is None:
        return Decimal(str(SPOT_TAKER))
    gate = getattr(rules, "gate_taker_fee", None)
    return max(rules.taker_fee, gate) if isinstance(gate, Decimal) else rules.taker_fee


def _order_not_found(result: dict[str, Any]) -> bool:
    """A cancel Binance definitely refused because it knows no such order (ccxt ``OrderNotFound``,
    Binance's "Unknown order sent.")."""
    if _status(result) != "error":
        return False
    text = _reason(result).lower()
    return "ordernotfound" in text or "unknown order" in text


def _placed_at(position: dict[str, Any], leg: dict[str, Any]) -> datetime | None:
    """When the order of ``leg`` was sent (its ``placed_at``), or None when unreadable. A leg written
    before ``placed_at`` was recorded falls back to the row's ``updated_at``."""
    try:
        placed = datetime.fromisoformat(str(leg.get("placed_at") or position["updated_at"]))
    except ValueError:
        return None
    return placed if placed.tzinfo is not None else placed.replace(tzinfo=timezone.utc)


def _carried(leg: dict[str, Any] | None) -> bool:
    """A re-stop leg of a resting buy's fill: it records the coins the old stop does not cover."""
    return leg is not None and leg.get("kind") == "restop" and leg.get("added") is not None


def _positive(value: Any) -> Decimal | None:
    number = _dec(value)
    return number if number is not None and number > 0 else None


def _exit_slippage(price: Decimal, ref: Decimal | None) -> Decimal | None:
    """Share an exit sold below ``ref`` (positive: worse than the reference)."""
    return 1 - price / ref if ref is not None and ref > 0 and price > 0 else None


def trail_clearance(peak: Decimal, candidate: Decimal) -> Decimal:
    """Room a new trailing stop must leave below the price: half the trail distance,
    between ``STOP_LIMIT_GAP`` and ``TRAIL_CLEARANCE``."""
    half = (peak - candidate) / peak / 2 if peak > 0 else Decimal(0)
    return max(STOP_LIMIT_GAP, min(TRAIL_CLEARANCE, half))


def floor_price(value: Decimal, rules: MarketRules) -> Decimal:
    """Floor ``value`` onto every price tick Binance checks (0 when impossible)."""
    price = value
    for _ in range(len(rules.limit_ticks) + 1):
        for tick in rules.limit_ticks:
            price = floor_to_step(price, tick)
        if all(price % tick == 0 for tick in rules.limit_ticks):
            return price
    return Decimal(0)


class Executor:
    def __init__(
        self,
        store: Store,
        profile_id: str,
        *,
        exit_policy: str = "trailing",
        ledger: Ledger | None = None,
        exit_hysteresis: bool = True,
    ) -> None:
        self.store = store
        self.profile_id = profile_id
        #: Recorded on every position, so the hit rate only mixes trades of one exit rule.
        self.exit_policy = exit_policy
        self.ledger: Ledger = ledger if ledger is not None else _append_ledger
        #: Recorded on every strategy position: its exit rule keeps the hysteresis it entered with.
        self.exit_hysteresis = exit_hysteresis

    # -- entries ---------------------------------------------------------

    def act(
        self,
        opportunities: list[Opportunity],
        *,
        clock_blocked: bool,
        manual: bool = False,
        allow_test: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            self.enter(item, clock_blocked=clock_blocked, manual=manual, allow_test=allow_test) for item in opportunities
        ]

    def enter(
        self,
        opportunity: Opportunity,
        *,
        clock_blocked: bool = False,
        manual: bool = False,
        allow_test: bool = False,
    ) -> dict[str, Any]:
        """Open one position with a LIMIT IOC buy ``ENTRY_LIMIT_GAP`` above the ask.

        The quantity is ``size_usd / limit`` floored to the lot, so the order never
        spends more than its size. A test row needs the Spot Testnet profile and a
        manual order, or ``allow_test`` (automatic trading with the testnet test mode on).
        """
        position_id: int | None = None

        def decide(decision: str, reason: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
            return self._decide(
                opportunity.symbol,
                opportunity.strategy,
                decision,
                reason,
                manual=manual,
                result=result,
                position_id=position_id,
            )

        if opportunity.status != "pronta":
            return decide("ignorada", f"oportunidade em estado {opportunity.status}")
        if opportunity.direction <= 0:
            return decide("bloqueado", "venda a descoberto não existe no spot")
        if opportunity.test_only and (not (manual or allow_test) or self.profile_id != TEST_PROFILE):
            return decide("bloqueado", "linha de teste: só no Spot Testnet, manual ou com o modo teste no automático")
        reducing = self._buy_block()
        if reducing is not None:
            return decide("bloqueado", reducing)
        if clock_blocked or weight_pressure():
            return decide("nao_operar", "peso da API ou relógio fora de 1s — só cancelamentos e saídas")
        if opportunity.size_usd <= 0:
            return decide("bloqueado", "tamanho de ordem zerado")
        policy = exit_policy_for(opportunity.strategy, self.exit_policy)
        round_trip = ORDERS_PER_DAY_ROUND_TRIP if policy == "day" else ORDERS_PER_ROUND_TRIP
        # Every open position keeps one order in reserve for its own re-stop or exit.
        if service.orders_remaining(self.profile_id) < round_trip + len(self.store.open_positions()):
            return decide("bloqueado", "o orçamento diário de ordens não cobre entrada, stop e saída")
        symbol = opportunity.symbol
        rules = service.market_rules(symbol, self.profile_id)
        if rules is None:
            return decide("bloqueado", "regras do mercado indisponíveis")
        ask = self._ask(symbol)
        if ask is None or ask <= 0:
            # No market fallback: a market buy's slippage has no bound.
            return decide("bloqueado", "cotação sem ask")
        limit = floor_price(ask * (1 + ENTRY_LIMIT_GAP), rules)
        if limit < ask:
            return decide("bloqueado", "preço limite da entrada abaixo do ask")
        # The gate values a buy limit at qty x max(quote, limit) <= size: the per-order headroom holds.
        qty = floor_to_step(Decimal(f"{opportunity.size_usd:.2f}") / limit, rules.lot_step)
        if (
            qty <= 0
            or (rules.min_qty is not None and qty < rules.min_qty)
            or (rules.min_notional is not None and qty * limit < rules.min_notional)
        ):
            return decide("bloqueado", "entrada abaixo do mínimo da corretora")
        plan_stop = _positive(opportunity.risk_pct)
        plan_target = _positive(opportunity.plan_target_pct)
        position_id = self.store.open_pending(
            symbol,
            opportunity.strategy,
            stop_price=opportunity.stop_price,
            target_price=opportunity.target_price,
            exit_policy=policy,
            test_only=opportunity.test_only,
            entry_win_rate=opportunity.win_rate,
            # Written with the reservation: an entry the sync resolves later keeps its time limit.
            exit_deadline=(_now() + DAY.max_hold).isoformat() if policy == "day" else None,
            timeframe=opportunity.timeframe or None,
            plan_stop_pct=float(plan_stop) if plan_stop is not None and plan_stop < 1 else None,
            plan_target_pct=None if plan_target is None else float(plan_target),
            exit_hysteresis=self.exit_hysteresis,
        )
        if position_id is None:
            return decide("bloqueado", "já existe posição viva neste par e estratégia")
        key = self._key(position_id, "entry", 0)
        # Persisted before the write: a crash after the send still leaves a row the sync can resolve,
        # and the fill's slippage is measured against the quote the order was priced on.
        self.store.update_position(
            position_id,
            entry_client_order_id=service.client_order_id(self.profile_id, key),
            entry_ref_price=float(ask),
        )
        result = service.submit_order(
            symbol,
            self.profile_id,
            side="buy",
            order_type="limit",
            quantity=decimal_text(qty),
            limit_price=decimal_text(limit),
            time_in_force="ioc",
            idempotency_key=key,
            session_id="bot",
            held=self._held_for_buy(),
        )
        status = _status(result)
        if status == "ok":
            fill = self._open_from_fill(position_id, result)
            if fill is None:
                return decide("pendente", "execução da entrada ainda não confirmada", result)
            if fill.qty <= 0:
                return decide("bloqueado", "entrada IOC sem execução: o preço passou do limite", result)
            reason = opportunity.reason
            if fill.qty < qty:
                reason = f"{reason} (entrada IOC executada em parte: {decimal_text(fill.qty)} de {decimal_text(qty)})"
            return decide("operar", reason, result)
        if status == "unknown":
            return decide("pendente", f"resultado da entrada desconhecido: {_reason(result)}", result)
        self.store.void_position(position_id, _reason(result))
        return decide("bloqueado", _reason(result), result)

    def _open_from_fill(self, position_id: int, result: dict[str, Any]) -> Fill | None:
        """Turn a filled entry into an open position and put its stop on the book.

        Returns the fill; an empty one voids the row (an IOC that found no ask under its
        limit). ``None`` when the fill cannot be read yet, or the row is no longer
        pending: it stays as it is for the sync. A partial fill opens with what filled.
        """
        position = self._position(position_id)
        if position["status"] != "pending":
            # Already opened or voided by another path: never book the same entry twice.
            logger.warning("entry fill for position %s ignored: row is %s", position_id, position["status"])
            return None
        symbol = position["symbol"]
        base = symbol.split("/", 1)[0]
        rules = service.market_rules(symbol, self.profile_id)
        fill = self._fill(symbol, result, rules)
        if fill is None:
            return None
        if fill.qty <= 0:
            self.store.void_position(position_id, "ordem sem execução")
            return fill
        taker = _gate_margin(rules)
        net = fill.qty - fill.fee_base
        free = service.free_balance(base, self.profile_id)
        held = min(net, free) if free is not None else net
        # A sell needs free base >= qty x (1 + taker); keep that margin as dust.
        qty = held / (1 + taker)
        if rules is not None:
            qty = floor_to_step(qty, rules.lot_step)
        qty = max(qty, Decimal(0))
        avg = (fill.cost + fill.fee_quote) / net if net > 0 else fill.average
        stop = _dec(position.get("stop_price")) or avg * (1 - DEFAULT_STOP_PCT)
        target = _dec(position.get("target_price"))
        fixed = parse_fixed(position.get("exit_policy"))
        plan_stop = _positive(position.get("plan_stop_pct"))
        if fixed is not None and avg > 0:
            # The operator's percentages count from the real average fill, not the scoring price.
            stop = avg * (1 - Decimal(str(fixed.stop_loss_pct)))
            target = avg * (1 + Decimal(str(fixed.take_profit_pct)))
        elif plan_stop is not None and plan_stop < 1 and avg > 0:
            # So do the setup's own distances, for every other policy. Instance rows carry no
            # plan: their stop is the level the robot planned.
            stop = avg * (1 - plan_stop)
            plan_target = _positive(position.get("plan_target_pct"))
            if plan_target is not None:
                target = avg * (1 + plan_target)
        entry_fee = fill.fee_quote + fill.fee_base * fill.average
        trail_pct = (avg - stop) / avg if avg > 0 and stop < avg else DEFAULT_STOP_PCT
        risk_usd = qty * (avg - stop) if stop < avg else Decimal(0)
        ref = _positive(position.get("entry_ref_price"))
        self.store.update_position(
            position_id,
            status="open",
            qty=float(qty),
            avg_price=float(avg),
            stop_price=float(stop),
            target_price=None if target is None else float(target),
            fees=float(entry_fee),
            peak_price=float(avg),
            trail_pct=float(trail_pct),
            initial_stop=float(stop),
            initial_risk_usd=float(risk_usd) if risk_usd > 0 else None,
            mae_price=float(avg),
            mfe_price=float(avg),
            # Positive: paid above the quote the order was priced on (fees excluded).
            entry_slippage=float(fill.average / ref - 1) if ref is not None and fill.average > 0 else None,
        )
        self.store.record_day(_day(), trades=1, fees=float(entry_fee))
        self.protect(position_id)
        return fill

    # -- DCA and grid inventory ----------------------------------------------

    def open_instance(
        self, instance_id: int, kind: str, symbol: str, notional: Decimal, stop: Decimal, *, clock_blocked: bool = False
    ) -> dict[str, Any]:
        """Open the inventory of a DCA or grid instance: a market buy, then its resting stop.

        The row is an ordinary position whose strategy and exit policy are the kind, so
        the sync, the halt, the risk checks and the panel treat it like any other.
        """
        reducing = self._buy_block()
        if reducing is not None:
            return {"status": "blocked", "error": reducing}
        if clock_blocked:
            return {"status": "blocked", "error": "relógio fora de 1s — só cancelamentos e saídas"}
        blocker = service.write_blocker()
        if blocker is not None:
            return {"status": "blocked", "error": blocker}
        if weight_pressure():
            return {"status": "blocked", "error": "peso da API alto"}
        if service.orders_remaining(self.profile_id) < ORDERS_PER_ROUND_TRIP + len(self.store.open_positions()):
            return {"status": "blocked", "error": "o orçamento diário de ordens não cobre entrada, stop e saída"}
        position_id = self.store.open_pending(
            symbol, kind, stop_price=float(stop), exit_policy=kind, instance_id=instance_id
        )
        if position_id is None:
            return {"status": "blocked", "error": "já existe posição viva deste robô no par"}
        key = self._key(position_id, "entry", 0)
        self.store.update_position(position_id, entry_client_order_id=service.client_order_id(self.profile_id, key))
        result = service.submit_order(
            symbol,
            self.profile_id,
            side="buy",
            notional=f"{notional:.2f}",
            order_type="market",
            idempotency_key=key,
            session_id="bot",
            held=self._held_for_buy(),
        )
        status = _status(result)
        if status == "ok":
            opened = self._open_from_fill(position_id, result)
            if opened is None:
                return {"status": "pending", "position_id": position_id}
            if opened.qty <= 0:
                return {"status": "error", "error": "ordem sem execução", "position_id": position_id}
            self._decide(symbol, kind, "operar", f"robô {kind} comprou o estoque inicial", result=result, position_id=position_id)
            return {"status": "ok", "position_id": position_id}
        if status == "unknown":
            return {"status": "pending", "position_id": position_id, "error": _reason(result)}
        self.store.void_position(position_id, _reason(result))
        return {"status": "error", "error": _reason(result)}

    def scale_in(
        self, position_id: int, notional: Decimal, leg: str, next_stop: Decimal, *, reserve: int, clock_blocked: bool = False
    ) -> str:
        """Add a market buy to an open DCA or grid position, then move its stop over the whole.

        The buy goes first, while the old stop still rests: a spot buy spends USDT, not the
        base the stop locks. Before anything is sent, the whole position after the buy must
        fit one stop order under the per-order cap; otherwise nothing moves. The leg is
        recorded before the send, so an unknown result is read back by its key in the sync.
        ``leg`` must be new for each attempt.
        """
        position = self._position(position_id)
        if position["status"] != "open" or not position.get("stop_order_id"):
            return "sem stop confirmado"
        if position.get("pending_leg") or position.get("keep_qty") is not None:
            return "outra ordem da posição em andamento"
        reducing = self._buy_block()
        if reducing is not None:
            return reducing
        if clock_blocked:
            return "relógio fora de 1s — só cancelamentos e saídas"
        blocker = service.write_blocker()
        if blocker is not None:
            return blocker
        if weight_pressure():
            return "peso da API alto"
        if service.orders_remaining(self.profile_id) < 2 + reserve:
            return "orçamento diário de ordens"
        symbol = position["symbol"]
        rules = service.market_rules(symbol, self.profile_id)
        bid = self._bid(symbol)
        mark = self._mark(symbol)
        if rules is None or bid is None or bid <= 0 or mark is None:
            return "cotação ou regras indisponíveis"
        if rules.min_notional is not None and notional < rules.min_notional:
            return "compra abaixo do mínimo da corretora"
        if next_stop >= bid * (1 - STOP_LIMIT_GAP):
            return "novo stop perto demais do preço"
        cap = service.order_notional_cap(symbol, self.profile_id)
        if cap is None:
            return "teto por ordem indisponível"
        after = (Decimal(str(position["qty"])) + notional / bid) * max(bid, mark)
        if notional > cap * _CAP_HEADROOM or after > cap * _CAP_HEADROOM:
            return "posição depois da compra acima do teto por ordem: o stop atual fica"
        free = service.free_balance(symbol.split("/", 1)[1], self.profile_id)
        if free is None or free < notional * (1 + _gate_margin(rules)):
            return "saldo em USDT insuficiente"
        key = self._key(position_id, leg, 0)
        client_id = service.client_order_id(self.profile_id, key)
        self.store.update_position(
            position_id,
            pending_leg=json.dumps(
                {
                    "kind": "buy",
                    "leg": leg,
                    "client_order_id": client_id,
                    "stop": str(next_stop),
                    "placed_at": _now().isoformat(),
                }
            ),
        )
        result = service.submit_order(
            symbol,
            self.profile_id,
            side="buy",
            notional=f"{notional:.2f}",
            order_type="market",
            idempotency_key=key,
            session_id="bot",
            held=self._held_for_buy(),
        )
        status = _status(result)
        if status == "unknown":
            return "compra pendente"
        if status != "ok":
            self.store.update_position(position_id, pending_leg=None)
            self._decide(symbol, position["strategy"], "bloqueado", f"compra adicional recusada: {_reason(result)}", result=result)
            return _reason(result)
        fill = self._fill(symbol, result, rules)
        if fill is None:
            return "compra pendente"
        return self._merge_buy(position_id, fill, next_stop, rules)

    def _merge_buy(
        self,
        position_id: int,
        fill: Fill,
        next_stop: Decimal,
        rules: MarketRules | None,
        *,
        restop: bool = True,
        carry: bool = False,
    ) -> str:
        """Add a filled buy to the position, then re-place the stop at ``next_stop`` for the whole.

        With ``carry`` (a resting buy) the re-stop leg also records what the old stop does not
        cover: should that stop sell before it comes off, the row keeps the coins the buy added
        (``_carry_after_stop``). With ``restop`` False the caller re-places the stop (an exit, or
        a stop that already sold); the leg stays, so a sync re-stops the whole if the caller
        stops short.
        """
        position = self._position(position_id)
        if fill.qty <= 0:
            self.store.update_position(position_id, pending_leg=None)
            return "compra sem execução"
        taker = _gate_margin(rules)
        # The same sell margin as an entry: a sell needs free base >= qty x (1 + taker).
        added = (fill.qty - fill.fee_base) / (1 + taker)
        if rules is not None:
            added = floor_to_step(added, rules.lot_step)
        held = Decimal(str(position["qty"]))
        qty = held + added
        avg_before = Decimal(str(position["avg_price"]))
        avg = avg_before
        if qty > 0:
            avg = (avg * held + fill.cost + fill.fee_quote) / qty
        fee = fill.fee_quote + fill.fee_base * fill.average
        leg: dict[str, Any] = {"kind": "restop", "stop": str(next_stop)}
        if carry:
            leg.update(added=str(added), qty_before=str(held), avg_before=str(avg_before))
        self.store.update_position(
            position_id,
            qty=float(qty),
            avg_price=float(avg),
            fees=float(Decimal(str(position["fees"])) + fee),
            pending_leg=json.dumps(leg),
        )
        self.store.record_day(_day(), fees=float(fee))
        self._decide(position["symbol"], position["strategy"], "aumento", f"comprou {added} a {fill.average:.8g}", position_id=position_id)
        if not restop:
            return "comprado"
        return self._restop(position_id)

    def _restop(self, position_id: int) -> str:
        """Put the stop of a position that grew on its whole quantity, at the level its leg recorded.

        While the gate refuses every order the old stop stays: taking it off could not be
        followed by a new one. A stop that sold all it covered while a resting buy added coins
        leaves the row open with them (``_carry_after_stop``); they get the new stop here.
        """
        blocker = service.write_blocker()
        if blocker is not None:
            return f"{blocker}{RESTOP_BLOCKED_SUFFIX}"
        position = self._position(position_id)
        leg = _leg(position) or {}
        started: float | None = None
        if position.get("stop_order_id") or position.get("stop_client_order_id"):
            started = time.monotonic()
            settled = self._take_stop_off(position)
            if settled == "filled":
                raise UnprotectedPosition(f"{position['symbol']}: o stop executou durante o aumento; a compra nova ficou sem stop")
            if settled == "pending":
                return RESTOP_OLD_STOP_PENDING
            position = self._position(position_id)
            if position["status"] != "open":
                raise UnprotectedPosition(f"{position['symbol']}: posição fechada durante o aumento; a compra nova ficou sem stop")
        fields: dict[str, Any] = {"pending_leg": None}
        if leg.get("stop"):
            fields["stop_price"] = float(Decimal(str(leg["stop"])))
        self.store.update_position(position_id, **fields)
        self.protect(position_id)
        self._stop_window("restop", position_id, started)
        return "comprado"

    def _settle_buy(self, position: dict[str, Any], leg: dict[str, Any]) -> None:
        """Read a scale-in buy whose result was unknown and merge what it bought."""
        symbol = position["symbol"]
        client_id = str(leg.get("client_order_id") or "")
        lookup = service.order_status(symbol, self.profile_id, client_id)
        if lookup.get("status") == "not_found":
            # Timed from the send, not from ``updated_at``: ``mark`` moves that on every new low or
            # high, so a falling price would keep the leg (and every exit) waiting forever.
            placed = _placed_at(position, leg)
            if placed is not None and (_now() - placed).total_seconds() > _ACCEPTANCE_WINDOW_SECONDS:
                self.store.update_position(position["id"], pending_leg=None)
            return
        order = lookup.get("order") if lookup.get("status") == "ok" else None
        if not isinstance(order, dict):
            return
        state = str(order.get("status") or "").lower()
        if state not in _FILLED and state not in _DEAD:
            return
        filled = _dec(order.get("filled")) or Decimal(0)
        if filled <= 0:
            self.store.update_position(position["id"], pending_leg=None)
            return
        rules = service.market_rules(symbol, self.profile_id)
        fill = self._fill(symbol, {"status": "ok", **order, "client_order_id": client_id}, rules)
        if fill is not None:
            self._merge_buy(int(position["id"]), fill, Decimal(str(leg.get("stop") or position["stop_price"])), rules)

    # -- resting buys (``resting_buys``, D18) -----------------------------------

    def rest_buy(
        self,
        position_id: int,
        price: Decimal,
        qty: Decimal,
        leg: str,
        next_stop: Decimal,
        *,
        level: int,
        reserve: int,
        clock_blocked: bool = False,
    ) -> str:
        """Rest one LIMIT GTC buy of ``qty`` at ``price`` under an open DCA or grid position.

        Every check of ``scale_in`` runs before the send, for the fill: the position after it
        must fit one stop under the per-order cap, the stop ``next_stop`` must sit under the
        level, and the day must still hold ``ORDERS_PER_RESTING_LEG`` orders plus ``reserve``
        (the buy, its cancel and the re-placed stop). The price must be under the bid, so the
        order rests instead of crossing the book. The leg is recorded before the send, with a
        key from ``leg`` (new for each level and attempt), so an unknown result is read back
        by its key. ``level`` is the instance's level index, to tell when it is no longer next.
        """
        position = self._position(position_id)
        if position["status"] != "open" or not position.get("stop_order_id"):
            return "sem stop confirmado"
        if position.get("pending_leg") or position.get("keep_qty") is not None:
            return "outra ordem da posição em andamento"
        reducing = self._buy_block()
        if reducing is not None:
            return reducing
        if clock_blocked:
            return "relógio fora de 1s — só cancelamentos e saídas"
        blocker = service.write_blocker()
        if blocker is not None:
            return blocker
        if weight_pressure():
            return "peso da API alto"
        if service.orders_remaining(self.profile_id) < ORDERS_PER_RESTING_LEG + reserve:
            return "orçamento diário de ordens"
        symbol = position["symbol"]
        rules = service.market_rules(symbol, self.profile_id)
        bid = self._bid(symbol)
        mark = self._mark(symbol)
        if rules is None or bid is None or bid <= 0 or mark is None:
            return "cotação ou regras indisponíveis"
        limit = floor_price(price, rules)
        amount = floor_to_step(qty, rules.lot_step)
        if limit <= 0 or amount <= 0:
            return "compra limitada sem preço ou tamanho válido"
        notional = limit * amount
        if rules.min_notional is not None and notional < rules.min_notional:
            return "compra abaixo do mínimo da corretora"
        if limit >= bid:
            return "o preço já está no nível: a compra não ficaria no livro"
        if next_stop >= limit * (1 - STOP_LIMIT_GAP):
            return "novo stop perto demais do nível"
        cap = service.order_notional_cap(symbol, self.profile_id)
        if cap is None:
            return "teto por ordem indisponível"
        after = (Decimal(str(position["qty"])) + amount) * max(bid, mark)
        if notional > cap * _CAP_HEADROOM or after > cap * _CAP_HEADROOM:
            return "posição depois da compra acima do teto por ordem: a compra limitada não vai ao livro"
        free = service.free_balance(symbol.split("/", 1)[1], self.profile_id)
        if free is None or free < notional * (1 + _gate_margin(rules)):
            return "saldo em USDT insuficiente"
        key = self._key(position_id, leg, 0)
        record: dict[str, Any] = {
            "kind": "rest_buy",
            "leg": leg,
            "client_order_id": service.client_order_id(self.profile_id, key),
            "price": decimal_text(limit),
            "qty": decimal_text(amount),
            "stop": str(next_stop),
            "level": level,
            "placed_at": _now().isoformat(),
        }
        # Read before the leg is written: ``_held_for_buy`` counts a recorded resting buy, and the
        # gate already counts this order itself.
        held = self._held_for_buy()
        self.store.update_position(position_id, pending_leg=json.dumps(record))
        result = service.submit_order(
            symbol,
            self.profile_id,
            side="buy",
            order_type="limit",
            quantity=decimal_text(amount),
            limit_price=decimal_text(limit),
            time_in_force="gtc",
            idempotency_key=key,
            session_id="bot",
            held=held,
        )
        status = _status(result)
        if status == "unknown":
            self._rest_event("unknown", position, record, error=_reason(result))
            return REST_PENDING
        if status != "ok":
            self.store.update_position(position_id, pending_leg=None)
            self._decide(symbol, position["strategy"], "bloqueado", f"compra limitada recusada: {_reason(result)}", result=result)
            return _reason(result)
        if result.get("order_id"):
            record["order_id"] = str(result["order_id"])
            self.store.update_position(position_id, pending_leg=json.dumps(record))
        self._rest_event("placed", position, record)
        self._decide(
            symbol, position["strategy"], "ordem_limitada", f"compra limitada de {amount} a {limit} no livro",
            result=result, position_id=position_id,
        )
        if (_dec(result.get("filled")) or Decimal(0)) > 0:
            # Filled on arrival (the book moved since the quote): merged now, not a cycle later.
            settled = self._settle_rest(self._position(position_id), record, cancel=False)
            return "comprado" if settled == "settled" else REST_PLACED
        return REST_PLACED

    def cancel_rest(self, position_id: int) -> str:
        """Take the resting buy of a position off the book and merge what it bought (re-stopped)."""
        position = self._position(position_id)
        leg = _leg(position)
        if position["status"] != "open" or leg is None or leg.get("kind") != "rest_buy":
            return "sem compra limitada"
        held = Decimal(str(position["qty"]))
        if self._settle_rest(position, leg, cancel=True) != "settled":
            return REST_CANCEL_PENDING
        after = self._position(position_id)
        if after["status"] == "open" and _leg(after) is not None:
            # What it bought is merged, but its re-stop stopped short: the sync must finish it.
            return REST_RESTOP_PENDING
        if Decimal(str(after["qty"])) > held:
            return "compra limitada executada antes do cancelamento"
        return "compra limitada cancelada"

    def cancel_resting_buys(self) -> list[dict[str, Any]]:
        """Cancel every resting buy and merge what each bought, re-placing its stop (the halt, a stop
        of the cycle). One failure never stops the rest. A row is ``ok`` only when the buy is off the
        book and what it bought rests under a stop; ``unprotected`` marks a position that may be
        open without a stop."""
        results: list[dict[str, Any]] = []
        for position in self.store.live_positions():
            leg = _leg(position)
            if position["status"] != "open" or leg is None or leg.get("kind") != "rest_buy":
                continue
            row: dict[str, Any] = {"position_id": position["id"], "symbol": position["symbol"]}
            try:
                outcome = self.cancel_rest(int(position["id"]))
            except UnprotectedPosition as exc:
                logger.error("resting buy of position %s: %s", position["id"], exc)
                results.append({**row, "status": "error", "error": str(exc), "unprotected": True})
                continue
            except Exception as exc:  # noqa: BLE001 - one position must not stop the rest
                logger.exception("cancel of the resting buy of position %s failed", position["id"])
                results.append({**row, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
                continue
            failed = outcome in (REST_CANCEL_PENDING, REST_RESTOP_PENDING)
            results.append({**row, "status": "error" if failed else "ok", "outcome": outcome})
        return results

    def _settle_rest(self, position: dict[str, Any], leg: dict[str, Any], *, cancel: bool, restop: bool = True) -> str:
        """Reconcile a resting buy. Returns ``resting`` (on the book with nothing bought, left there),
        ``settled`` (off the book; what it bought is merged into the row) or ``unknown`` (the leg stays).

        A partial fill on an open order is cancelled and merged: one buy per level, never a rest
        waiting under a stop sized for less. With ``cancel`` the order always comes off. After a
        cancel the order is read again, whatever the cancel answered: it may have filled between
        the read and the cancel, or the cancel may not have landed. A fill is merged and re-stopped
        (``restop``), or left to the caller with the re-stop leg recorded.
        """
        position_id = int(position["id"])
        symbol = position["symbol"]
        client_id = str(leg.get("client_order_id") or "")
        found, order = self._rest_order(symbol, client_id)
        if found == "not_found":
            return self._rest_not_found(position, leg)
        if order is None:
            return "unknown"
        state = str(order.get("status") or "").lower()
        if state not in _FILLED and state not in _DEAD:
            if not cancel and (_dec(order.get("filled")) or Decimal(0)) <= 0:
                return "resting"
            order_id = str(leg.get("order_id") or order.get("order_id") or "")
            if not order_id:
                return "unknown"
            cancelled = service.cancel_order(order_id, self.profile_id, symbol=symbol)
            _found, read = self._rest_order(symbol, client_id)
            state = str((read or {}).get("status") or "").lower()
            if read is None or (state not in _FILLED and state not in _DEAD):
                self._rest_event("cancel_unconfirmed", position, leg, cancel=_status(cancelled), error=_reason(cancelled))
                return "unknown"
            order = read
        filled = _dec(order.get("filled")) or Decimal(0)
        if filled <= 0:
            self.store.update_position(position_id, pending_leg=None)
            self._rest_event(state, position, leg)
            return "settled"
        rules = service.market_rules(symbol, self.profile_id)
        fill = self._fill(symbol, {"status": "ok", **order, "client_order_id": client_id}, rules)
        if fill is None or fill.qty <= 0:
            return "unknown"
        self._rest_event("filled" if state in _FILLED else "partial", position, leg, filled=decimal_text(fill.qty))
        stop = Decimal(str(leg.get("stop") or position["stop_price"]))
        self._merge_buy(position_id, fill, stop, rules, restop=restop, carry=True)
        return "settled"

    def _rest_order(self, symbol: str, client_id: str) -> tuple[str, dict[str, Any] | None]:
        """``(lookup status, order)`` of a resting buy, read by its client id."""
        if not client_id:
            return "error", None
        lookup = service.order_status(symbol, self.profile_id, client_id)
        status = str(lookup.get("status") or "error")
        order = lookup.get("order") if status == "ok" else None
        return status, order if isinstance(order, dict) else None

    def _rest_not_found(self, position: dict[str, Any], leg: dict[str, Any]) -> str:
        """A resting buy Binance does not know: unknown inside the acceptance window (it may still be
        on its way), forgotten after it when it was never acknowledged (it never reached the book).

        One acknowledged by Binance (the leg has its ``order_id``) is never forgotten on a read alone:
        it may still rest, and a new buy next to it could fill twice. It is cancelled by its id, and
        forgotten only when the cancel answers that Binance knows no such order either; any other
        answer leaves it unknown, which blocks sales and new resting buys until a read settles it.
        """
        placed = _placed_at(position, leg)
        if placed is None or (_now() - placed).total_seconds() <= _ACCEPTANCE_WINDOW_SECONDS:
            return "unknown"
        order_id = str(leg.get("order_id") or "")
        if order_id:
            cancelled = service.cancel_order(order_id, self.profile_id, symbol=position["symbol"])
            if not _order_not_found(cancelled):
                self._rest_event("not_found_unconfirmed", position, leg, cancel=_status(cancelled), error=_reason(cancelled))
                return "unknown"
        self.store.update_position(int(position["id"]), pending_leg=None)
        self._rest_event("not_found", position, leg)
        return "settled"

    def _settle_rest_first(self, position: dict[str, Any]) -> dict[str, Any]:
        """Settle a resting buy before a stop's sale is booked, so the row never closes with coins
        outside it; returns the row as it is now. Raises ``UnprotectedPosition`` when the buy's
        result cannot be read: it may have bought coins no row and no stop hold."""
        leg = _leg(position)
        if leg is not None and leg.get("kind") == "rest_buy":
            if self._settle_rest(position, leg, cancel=True, restop=False) != "settled":
                raise UnprotectedPosition(
                    f"{position['symbol']}: o stop executou e a compra limitada não pôde ser lida; "
                    "ela pode ter comprado fora da posição"
                )
        return self._position(int(position["id"]))

    def _carry_after_stop(
        self, position: dict[str, Any], leg: dict[str, Any], sold: Decimal, proceeds: Decimal, fee: Decimal
    ) -> None:
        """The stop sold what it covered after a resting buy added coins it did not cover: book the
        stop's sale at the average before the buy, keep the rest open at what it cost, with no
        stop. The caller re-protects it at the leg's level (a price under it exits at once)."""
        position_id = int(position["id"])
        held = Decimal(str(position["qty"]))
        avg = Decimal(str(position["avg_price"]))
        basis = _dec(leg.get("avg_before")) or avg
        remaining = held - sold
        cost_left = avg * held - basis * sold
        avg_left = cost_left / remaining if remaining > 0 and cost_left > 0 else avg
        self._book_sale(position_id, sold, proceeds, fee, basis=basis)
        current = self._position(position_id)
        if current.get("stop_order_id") or current.get("stop_client_order_id"):
            self._clear_stop(position_id)
        fields: dict[str, Any] = {"qty": float(remaining), "avg_price": float(avg_left), "pending_leg": None}
        if leg.get("stop"):
            fields["stop_price"] = float(Decimal(str(leg["stop"])))
        self.store.update_position(position_id, **fields)
        self._rest_event("stop_filled_carry", position, leg, sold=decimal_text(sold), kept=decimal_text(remaining))
        self._decide(
            position["symbol"], position["strategy"], "saida_parcial",
            f"stop executado na corretora; a compra limitada de {remaining} segue na posição", position_id=position_id,
        )

    def _rest_event(self, event: str, position: dict[str, Any], leg: dict[str, Any], **extra: Any) -> None:
        self._record(
            "resting_buy",
            {
                "event": event,
                "position_id": position["id"],
                "symbol": position["symbol"],
                "leg": leg.get("leg"),
                "client_order_id": leg.get("client_order_id"),
                "price": leg.get("price"),
                "qty": leg.get("qty"),
                **extra,
            },
        )

    # -- protection --------------------------------------------------------

    def protect(self, position_id: int) -> bool:
        """Rest a STOP_LOSS_LIMIT sell for the whole position. Exit at once when refused."""
        position = self._position(position_id)
        if position["status"] != "open" or position.get("stop_order_id") or position.get("stop_client_order_id"):
            return True
        symbol = position["symbol"]
        rules = service.market_rules(symbol, self.profile_id)
        if rules is None:
            return self._emergency_exit(position_id, "regras do mercado indisponíveis para o stop")
        held = Decimal(str(position["qty"]))
        free = service.free_balance(symbol.split("/", 1)[0], self.profile_id)
        if free is not None and free < held:
            # The store counts a sale whose fill was never read (or coins left the account): the
            # stop never asks for more than is held, and the store follows the balance, so a later
            # exit or first target is sized on what is really there.
            # Sized like a fill: the gate wants ``qty x (1 + fee)`` free, so the whole free balance
            # would be refused and leave the position with no stop.
            held = free / (1 + _gate_margin(rules))
            self.store.update_position(position_id, qty=float(held))
            self._decide(symbol, position["strategy"], "saida_parcial", f"quantidade ajustada ao saldo livre: {free}")
        qty = floor_to_step(held, rules.lot_step)
        stop = floor_price(Decimal(str(position["stop_price"])), rules)
        limit = floor_price(stop * (1 - STOP_LIMIT_GAP), rules)
        bid = self._bid(symbol)
        if qty <= 0 or stop <= 0 or limit <= 0:
            return self._emergency_exit(position_id, "stop sem tamanho ou preço válido")
        if bid is not None and stop >= bid:
            return self._emergency_exit(position_id, "preço já abaixo do stop")
        attempt = int(position["stop_attempts"])
        result = service.submit_order(
            symbol,
            self.profile_id,
            side="sell",
            order_type="stop_loss_limit",
            quantity=decimal_text(qty),
            stop_price=decimal_text(stop),
            limit_price=decimal_text(limit),
            time_in_force="gtc",
            idempotency_key=self._key(position_id, "stop", attempt),
            session_id="bot",
            held=self._held_for_sell(),
        )
        status = _status(result)
        if status == "ok":
            # The next stop for this position (after an exit or a cancel) needs a new key.
            self.store.update_position(
                position_id,
                stop_order_id=str(result.get("order_id") or "") or None,
                stop_client_order_id=str(result.get("client_order_id") or "") or None,
                stop_attempts=attempt + 1,
            )
            return True
        if status == "unknown":
            # The next sync reads the order by its client id.
            self.store.update_position(position_id, stop_client_order_id=str(result.get("client_order_id") or "") or None)
            return True
        self.store.update_position(position_id, stop_attempts=attempt + 1)
        self._decide(symbol, position["strategy"], "bloqueado", f"stop recusado: {_reason(result)}", result=result)
        return self._emergency_exit(position_id, f"stop recusado: {_reason(result)}")

    def mark(self, position_id: int, last: Decimal) -> None:
        """Track the lowest and highest price an open position saw (MAE and MFE). No order."""
        position = self.store.position(position_id)
        if position is None or position["status"] != "open" or last <= 0:
            return
        avg = _dec(position.get("avg_price")) or last
        low = _dec(position.get("mae_price")) or avg
        high = _dec(position.get("mfe_price")) or avg
        fields: dict[str, Any] = {}
        if last < low:
            fields["mae_price"] = float(last)
        if last > high:
            fields["mfe_price"] = float(last)
        if fields:
            self.store.update_position(position_id, **fields)

    def trail(self, position_id: int, last_price: Decimal, *, reserve: int) -> str:
        """Raise the resting stop to follow the highest price. Returns what happened.

        The stop moves only when every check passes before it leaves the book:
        no halt or mandate problem, no API weight pressure, a confirmed resting
        stop, a new level at least ``TRAIL_STEP`` above the old one and below
        ``min(bid, last_price)`` by ``trail_clearance`` (D16), and ``reserve``
        orders left today (one per open position). Otherwise the current stop
        simply stays. A refused
        replacement goes through ``protect``: exit at once, or a halt. A
        ``trailing_tp`` position keeps its entry stop until the activation gain.
        """
        position = self._position(position_id)
        if position["status"] != "open" or not position.get("stop_order_id"):
            return "sem stop confirmado"
        avg = Decimal(str(position["avg_price"]))
        stop = Decimal(str(position["stop_price"]))
        trail_pct = _dec(position.get("trail_pct"))
        if trail_pct is None or trail_pct <= 0:
            trail_pct = (avg - stop) / avg if avg > 0 and stop < avg else DEFAULT_STOP_PCT
        peak = max(_dec(position.get("peak_price")) or avg, last_price)
        if peak != _dec(position.get("peak_price")):
            self.store.update_position(position_id, peak_price=float(peak), trail_pct=float(trail_pct))
        blocker = service.write_blocker()
        if blocker is not None:
            return blocker
        if weight_pressure():
            return "peso da API alto"
        rules = service.market_rules(position["symbol"], self.profile_id)
        if rules is None:
            return "regras do mercado indisponíveis"
        level = trailing_level(str(position.get("exit_policy") or "trailing"), avg, peak, trail_pct)
        if level is None:
            return "aguardando ativação do stop móvel"
        candidate = floor_price(level, rules)
        if candidate < stop * (1 + TRAIL_STEP):
            return "stop já no nível"
        bid = self._bid(position["symbol"])
        if bid is None or bid <= 0:
            return "cotação indisponível"
        clearance = trail_clearance(peak, candidate)
        if candidate >= min(bid, last_price) * (1 - clearance):
            return f"novo stop sem folga de {float(clearance):.1%} do preço"
        if service.orders_remaining(self.profile_id) < reserve:
            return "orçamento diário de ordens reservado"
        # The mandate gate values a sell by quantity at the quote's last price, the
        # testnet cap at a fresh bid. A replacement over the per-order cap would be
        # refused after the old stop is gone, so the old stop stays; the headroom
        # covers a tick between this check and the gate's.
        cap = service.order_notional_cap(position["symbol"], self.profile_id)
        if cap is None:
            return "teto por ordem indisponível"
        mark = self._mark(position["symbol"])
        if mark is None:
            return "cotação indisponível"
        if Decimal(str(position["qty"])) * max(bid, mark) > cap * _CAP_HEADROOM:
            return "posição acima do teto por ordem: o stop atual fica"
        started = time.monotonic()
        settled = self._take_stop_off(position)
        if settled == "filled":
            return "stop executado"
        if settled == "pending":
            return "o stop não saiu da corretora"
        if self._position(position_id)["status"] != "open":
            return "posição fechada"
        self.store.update_position(position_id, stop_price=float(candidate))
        self.protect(position_id)
        self._stop_window("trail", position_id, started)
        self._decide(position["symbol"], position["strategy"], "stop_movel", f"stop subiu para {candidate}")
        return "movido"

    def _emergency_exit(self, position_id: int, why: str) -> bool:
        result = self.exit_position(position_id, why, protect_remainder=False)
        position = self._position(position_id)
        if position["status"] == "open":
            raise UnprotectedPosition(f"{position['symbol']}: {why}; saída falhou: {_reason(result)}")
        return False

    # -- exits ---------------------------------------------------------------

    def take_profit(self, position_id: int, *, reserve: int) -> str:
        """Sell the first target of a day trade and move the stop of the rest to break-even.

        Every check runs while the stop still rests: write gate, API weight,
        orders for the sale and the new stop plus ``reserve``, a break-even below
        the bid by the stop-limit gap, the new stop under the per-order cap, and
        a sale above the exchange minimum. When one fails, nothing moves and the
        next cycle tries again. When the rest would fall below the exchange
        minimum, the whole position is sold. The quantity to keep is stored
        before the first order, so a retry after a partial fill, an unknown
        result or a crash sells only down to it; when what is left to sell of a
        started first target falls below the minimum, the first target is
        finished without another sale.
        """
        position = self._position(position_id)
        if position["status"] != "open" or position.get("tp1_done"):
            return "nada a fazer"
        if not position.get("stop_order_id"):
            return "sem stop confirmado"
        blocker = service.write_blocker()
        if blocker is not None:
            return blocker
        if weight_pressure():
            return "peso da API alto"
        if service.orders_remaining(self.profile_id) < 2 + reserve:
            return "orçamento diário de ordens"
        symbol = position["symbol"]
        rules = service.market_rules(symbol, self.profile_id)
        bid = self._bid(symbol)
        mark = self._mark(symbol)
        if rules is None or bid is None or bid <= 0 or mark is None:
            return "cotação ou regras indisponíveis"
        qty = Decimal(str(position["qty"]))
        stored = _dec(position.get("keep_qty"))
        keep = stored if stored is not None else floor_to_step(qty * Decimal(str(1 - DAY.tp1_fraction)), rules.lot_step)
        limit = floor_price(bid * (1 - EXIT_LIMIT_GAP), rules)
        minimum = rules.min_notional or Decimal(0)
        breakeven = floor_price(Decimal(str(position["avg_price"])) * Decimal(str(1 + DAY.breakeven_buffer)), rules)
        if breakeven >= bid * (1 - STOP_LIMIT_GAP):
            return "break-even acima do bid"
        cap = service.order_notional_cap(symbol, self.profile_id)
        if cap is None:
            return "teto por ordem indisponível"
        if (qty - keep) * limit < minimum:
            if stored is None:
                return "parte do alvo abaixo do mínimo da corretora"
            # A started first target with nothing sellable left: its sale completed before a crash, or
            # only a bit under the exchange minimum remains. Finish it; never sell another fraction.
            if qty * max(bid, mark) > cap * _CAP_HEADROOM:
                return "stop do resto acima do teto por ordem: o stop atual fica"
            return self._finish_take_profit(position, breakeven)
        if keep * limit < minimum:
            # The rest could not be sold later: the first target takes all of it.
            result = self.exit_position(position_id, "primeiro alvo; a sobra ficaria abaixo do mínimo")
            return "vendido" if result.get("status") == "ok" else str(result.get("error") or result.get("status"))
        # The break-even stop would be refused over the per-order cap after the old one is gone.
        if keep * max(bid, mark) > cap * _CAP_HEADROOM:
            return "stop do resto acima do teto por ordem: o stop atual fica"
        self.store.update_position(position_id, keep_qty=float(keep))
        result = self.exit_position(
            position_id, "primeiro alvo (TP1)", keep_qty=keep, leg="tp1", next_stop=breakeven
        )
        return "vendido" if result.get("status") == "ok" else str(result.get("error") or result.get("status"))

    def _finish_take_profit(self, position: dict[str, Any], breakeven: Decimal) -> str:
        """Mark the first target done and move the stop of what is held to break-even, without a sale."""
        position_id = int(position["id"])
        started = time.monotonic()
        settled = self._take_stop_off(position)
        if settled == "filled":
            return "stop executado"
        if settled == "pending":
            return "o stop não saiu da corretora"
        if self._position(position_id)["status"] != "open":
            return "posição fechada"
        self.store.update_position(position_id, stop_price=float(breakeven), tp1_done=1, keep_qty=None)
        self._decide(position["symbol"], position["strategy"], "saida_parcial", "primeiro alvo concluído; a sobra ficou abaixo do mínimo")
        self.protect(position_id)
        self._stop_window("tp1", position_id, started)
        return "concluído"

    def exit_position(
        self,
        position_id: int,
        reason: str,
        *,
        protect_remainder: bool = True,
        keep_qty: Decimal | None = None,
        leg: str = "exit",
        next_stop: Decimal | None = None,
        gap: Decimal = EXIT_LIMIT_GAP,
        exit_ref: Decimal | None = None,
    ) -> dict[str, Any]:
        """Take the stop off the book and sell the position with LIMIT IOC orders.

        With ``keep_qty`` it sells only down to that quantity (a day trade's first
        target): the row never closes, the stop goes back on the rest at
        ``next_stop`` once the sale is complete, or at the old stop when it is not.
        ``leg`` keeps the order keys of a partial sale apart from a later full exit.
        Each IOC is priced ``gap`` below the bid. A closing sale records its slippage
        against ``exit_ref`` (the price that decided the exit), or the bid at the first chunk.
        """
        position = self.store.position(position_id)
        if position is None or position["status"] != "open":
            return {"status": "error", "error": "posição não está aberta"}
        inflight = _leg(position)
        if inflight is not None and inflight.get("kind") == "buy":
            # A scale-in buy with an unknown result: closing the row now would leave what it
            # bought with no stop and no row. Settle it first; while unknown, the stop stays.
            self._settle_buy(position, inflight)
            position = self._position(position_id)
            if position["status"] != "open":
                return {"status": "ok", "closed_by": "stop"}
            if (_leg(position) or {}).get("kind") == "buy":
                return {"status": "blocked", "error": "compra adicional com resultado pendente; o stop segue na corretora"}
        if inflight is not None and inflight.get("kind") == "rest_buy":
            # A resting buy comes off the book first, and what it bought joins the sale: never a
            # sale over a buy that may still fill. While its result is unknown, the stop stays.
            if self._settle_rest(position, inflight, cancel=True, restop=False) != "settled":
                return {"status": "blocked", "error": "compra limitada com resultado pendente; o stop segue na corretora"}
            position = self._position(position_id)
        symbol = position["symbol"]
        blocker = service.write_blocker()
        if blocker is not None:
            # The gate would refuse the sell; the stop stays on the book.
            return {"status": "blocked", "error": f"{blocker}; o stop segue na corretora"}
        if service.orders_remaining(self.profile_id) < 1:
            return {"status": "blocked", "error": "orçamento diário de ordens esgotado; o stop segue na corretora"}
        # Time without a resting stop, from the cancel to the re-protect that follows (D17 metric).
        started: float | None = None
        if position.get("stop_order_id") or position.get("stop_client_order_id"):
            started = time.monotonic()
            settled = self._take_stop_off(position)
            if settled == "filled":
                return {"status": "ok", "closed_by": "stop"}
            if settled == "pending":
                return {"status": "error", "error": "o stop não saiu da corretora; ele continua protegendo"}
            position = self._position(position_id)
            if position["status"] != "open":
                return {"status": "ok", "closed_by": "stop"}
        restop = _leg(position)
        if restop is not None and restop.get("kind") == "restop":
            # A buy merged just before this exit: with the stop off, the re-stop it waited for is
            # the remainder's stop, at the level the buy recorded.
            fields: dict[str, Any] = {"pending_leg": None}
            if restop.get("stop"):
                fields["stop_price"] = float(Decimal(str(restop["stop"])))
            self.store.update_position(position_id, **fields)
            position = self._position(position_id)
        rules = service.market_rules(symbol, self.profile_id)
        if rules is None:
            return self._after_exit(position_id, reason, "regras do mercado indisponíveis", protect_remainder, started)
        partial = keep_qty is not None
        if keep_qty is not None:
            # The kept quantity and the leg are read back in the sync when the result is unknown
            # (``_settle_partial``), and a retry sells only down to the same quantity. A retry of
            # the same leg keeps the quantity the sale started from.
            sale_leg = _leg(position) or {}
            if sale_leg.get("kind") != "sell" or sale_leg.get("leg") != leg or sale_leg.get("qty_before") is None:
                sale_leg = {"qty_before": float(position["qty"])}
            self.store.update_position(
                position_id,
                keep_qty=float(keep_qty),
                pending_leg=json.dumps({"kind": "sell", "leg": leg, "qty_before": sale_leg["qty_before"]}),
            )
        remaining = Decimal(str(position["qty"])) - (keep_qty or Decimal(0))
        cap = service.order_notional_cap(symbol, self.profile_id)
        sold = proceeds = Decimal(0)
        failure = ""
        unknown = False
        ref = exit_ref if exit_ref is not None and exit_ref > 0 else None
        while remaining > 0:
            bid = self._bid(symbol)
            if bid is None or bid <= 0:
                failure = "cotação indisponível"
                break
            if ref is None:
                ref = bid
            limit = floor_price(bid * (1 - gap), rules)
            chunk = floor_to_step(remaining, rules.lot_step)
            if cap is not None:
                chunk = min(chunk, floor_to_step(cap * _CAP_HEADROOM / bid, rules.lot_step))
            if chunk <= 0 or limit <= 0 or (rules.min_notional is not None and chunk * limit < rules.min_notional):
                failure = "parte abaixo do mínimo da corretora" if partial else "sobra abaixo do mínimo da corretora"
                break
            attempt = int(self._position(position_id)["exit_attempts"])
            result = service.submit_order(
                symbol,
                self.profile_id,
                side="sell",
                order_type="limit",
                quantity=decimal_text(chunk),
                limit_price=decimal_text(limit),
                time_in_force="ioc",
                idempotency_key=self._key(position_id, leg, attempt),
                session_id="bot",
                held=self._held_for_sell(),
            )
            status = _status(result)
            if status == "unknown":
                unknown = True
                failure = f"resultado da saída desconhecido: {_reason(result)}"
                break
            if status != "ok":
                self.store.update_position(position_id, exit_attempts=attempt + 1)
                failure = _reason(result)
                break
            fill = self._fill(symbol, result, rules)
            if fill is None:
                # Same attempt next time: the key replays this order instead of selling again.
                unknown = True
                failure = "execução da saída ainda não confirmada"
                break
            self.store.update_position(position_id, exit_attempts=attempt + 1)
            if fill.qty <= 0:
                failure = "IOC sem execução"
                break
            fee = fill.fee_quote + fill.fee_base * fill.average
            self._book_sale(position_id, fill.qty, fill.cost, fee)
            sold += fill.qty
            proceeds += fill.cost
            remaining -= fill.qty
            held = Decimal(str(self._position(position_id)["qty"])) - fill.qty
            self.store.update_position(position_id, qty=float(max(held, Decimal(0))))
            if fill.qty < chunk:
                failure = "IOC executada em parte"
                break
        if partial:
            return self._after_partial(
                position_id, reason, remaining, sold, failure, unknown, next_stop,
                started=started, kind="tp1" if leg == "tp1" else "partial",
            )
        if remaining <= 0 or failure == "sobra abaixo do mínimo da corretora":
            if sold > 0:
                exit_avg = proceeds / sold
                self._close(position_id, exit_avg, exit_slippage=_exit_slippage(exit_avg, ref))
            else:
                self._close(position_id, Decimal(str(position["avg_price"])))
            self._decide(symbol, position["strategy"], "saida", reason)
            return {"status": "ok", "sold": float(sold)}
        self.store.update_position(position_id, qty=float(remaining))
        if unknown:
            # Reconciliation settles the order first; the next sync re-protects what is left.
            return {"status": "unknown", "error": failure, "sold": float(sold)}
        return self._after_exit(position_id, reason, failure, protect_remainder, started)

    def _after_partial(
        self,
        position_id: int,
        reason: str,
        remaining: Decimal,
        sold: Decimal,
        failure: str,
        unknown: bool,
        next_stop: Decimal | None,
        *,
        started: float | None = None,
        kind: str = "partial",
    ) -> dict[str, Any]:
        """Re-protect the rest of a partial sale. The row stays open: it still holds ``keep_qty``."""
        if unknown:
            # The next sync books this sale by its key (``_settle_partial``), then re-protects.
            return {"status": "unknown", "error": failure, "sold": float(sold)}
        if sold <= 0:
            position = self._position(position_id)
            begun = (_leg(position) or {}).get("qty_before")
            if begun is None or Decimal(str(position["qty"])) >= Decimal(str(begun)):
                # Nothing of this sale was ever sold: it is dropped, so no later cycle sends it
                # again at another price; the caller decides anew.
                self.store.update_position(position_id, keep_qty=None, pending_leg=None)
            # Otherwise an earlier attempt sold part of it: the kept quantity stays, so a retry
            # (a day trade's first target) sells only down to it, never more than planned.
            self.protect(position_id)
            self._stop_window(kind, position_id, started)
            return {"status": "error", "error": failure or "nada vendido na venda parcial"}
        if remaining <= 0 or (sold > 0 and failure == "parte abaixo do mínimo da corretora"):
            # Sold down to the kept size, or to less than one sellable order above it: that dust stays held.
            fields: dict[str, Any] = {"keep_qty": None, "pending_leg": None}
            if self._position(position_id).get("exit_policy") == "day":
                fields["tp1_done"] = 1
            if next_stop is not None:
                fields["stop_price"] = float(next_stop)
            self.store.update_position(position_id, **fields)
            position = self._position(position_id)
            self._decide(position["symbol"], position["strategy"], "saida_parcial", reason)
            self.protect(position_id)
            self._stop_window(kind, position_id, started)
            return {"status": "ok", "sold": float(sold), "partial": True}
        return self._after_exit(position_id, reason, failure or "venda parcial incompleta", True, started, kind)

    def _after_exit(
        self,
        position_id: int,
        reason: str,
        failure: str,
        protect_remainder: bool,
        started: float | None = None,
        kind: str = "exit_remainder",
    ) -> dict[str, Any]:
        position = self._position(position_id)
        self._decide(position["symbol"], position["strategy"], "saida_parcial", f"{reason}: {failure}")
        if protect_remainder:
            self.protect(position_id)
            self._stop_window(kind, position_id, started)
        return {"status": "error", "error": failure}

    def _take_stop_off(self, position: dict[str, Any]) -> str:
        """Cancel the resting stop. Returns ``cancelled``, ``filled`` or ``pending`` (still on the book).

        Raises ``UnprotectedPosition`` when the stop was cancelled but its fill
        cannot be read: the position has no stop and an unknown size, so neither
        a new stop nor an exit can be sized. The caller halts.
        """
        settled = self._take_stop_off_once(position)
        if settled == "cancelled_unknown":
            raise UnprotectedPosition(
                f"{position['symbol']}: stop cancelado, execução ilegível; posição sem stop até a sincronização"
            )
        return settled

    def _take_stop_off_once(self, position: dict[str, Any]) -> str:
        symbol = position["symbol"]
        order_id = position.get("stop_order_id")
        if order_id:
            cancelled = service.cancel_order(str(order_id), self.profile_id, symbol=symbol)
            if _status(cancelled) == "ok":
                return self._settle_cancelled_stop(position)
        client_id = position.get("stop_client_order_id")
        if not client_id:
            return "pending"
        lookup = service.order_status(symbol, self.profile_id, str(client_id))
        if lookup.get("status") == "not_found":
            self._clear_stop(position["id"])
            return "cancelled"
        order = lookup.get("order") if lookup.get("status") == "ok" else None
        if not isinstance(order, dict):
            return "pending"
        state = str(order.get("status") or "").lower()
        if state in _FILLED:
            self._close_from_stop(position, order)
            # A resting buy's coins carried past the stop keep the row open, with no stop.
            return "filled" if self._position(position["id"])["status"] != "open" else "cancelled"
        if state in _DEAD:
            return self._settle_cancelled_stop(position, order)
        if not order_id and order.get("order_id"):
            cancelled = service.cancel_order(str(order["order_id"]), self.profile_id, symbol=symbol)
            if _status(cancelled) == "ok":
                return self._settle_cancelled_stop(position)
        return "pending"

    def _settle_cancelled_stop(self, position: dict[str, Any], order: dict[str, Any] | None = None) -> str:
        """Book any part the stop sold before it was cancelled, then clear it."""
        if order is None and position.get("stop_client_order_id"):
            lookup = service.order_status(position["symbol"], self.profile_id, str(position["stop_client_order_id"]))
            if lookup.get("status") == "ok":
                order = lookup.get("order") if isinstance(lookup.get("order"), dict) else None
            elif lookup.get("status") != "not_found":
                # The stop is off the book but its fill is unknown: keep the ids, so the sync after
                # a resume reads the cancelled order, books it and re-protects the rest.
                return "cancelled_unknown"
        filled = _dec((order or {}).get("filled")) or Decimal(0)
        if filled > 0 and order is not None and filled >= Decimal(str(position["qty"])):
            # The stop sold all it covered: a resting buy is settled before the row may close.
            position = self._settle_rest_first(position)
        self._clear_stop(position["id"])
        if filled > 0 and order is not None:
            proceeds = _dec(order.get("cost")) or filled * (_dec(order.get("average")) or Decimal(0))
            fee = proceeds * Decimal(str(SPOT_TAKER))
            leg = _leg(position)
            if _carried(leg) and leg is not None and Decimal(str(position["qty"])) - filled > 0:
                self._carry_after_stop(position, leg, filled, proceeds, fee)
                return "cancelled"
            self._book_sale(position["id"], filled, proceeds, fee)
            remaining = Decimal(str(position["qty"])) - filled
            if remaining <= 0:
                exit_avg = proceeds / filled
                self._close(position["id"], exit_avg, exit_slippage=_exit_slippage(exit_avg, _dec(position.get("stop_price"))))
                return "filled"
            self.store.update_position(position["id"], qty=float(remaining))
        return "cancelled"

    def _clear_stop(self, position_id: int) -> None:
        """Forget the stop. A new stop for this position gets a new key, never a replay."""
        attempts = int(self._position(position_id)["stop_attempts"])
        self.store.update_position(position_id, stop_order_id=None, stop_client_order_id=None, stop_attempts=attempts + 1)

    def _close_from_stop(self, position: dict[str, Any], order: dict[str, Any]) -> None:
        """Book the stop's sale and close the row. A resting buy is settled first: when it bought,
        the row stays open with those coins and no stop (``_carry_after_stop``)."""
        position = self._settle_rest_first(position)
        leg = _leg(position)
        carry = _carried(leg)
        held = Decimal(str(position["qty"]))
        covered = (_dec(leg.get("qty_before")) if carry and leg is not None else None) or held
        filled = _dec(order.get("filled")) or covered
        proceeds = _dec(order.get("cost")) or filled * (_dec(order.get("average")) or Decimal(str(position["stop_price"])))
        fee = proceeds * Decimal(str(SPOT_TAKER))
        if carry and leg is not None and held - filled > 0:
            self._carry_after_stop(position, leg, filled, proceeds, fee)
            return
        self._book_sale(position["id"], filled, proceeds, fee)
        stop = _dec(position.get("stop_price"))
        exit_avg = proceeds / filled if filled > 0 else (stop or Decimal(0))
        self._close(position["id"], exit_avg, exit_slippage=_exit_slippage(exit_avg, stop))
        self._decide(position["symbol"], position["strategy"], "saida", "stop executado na corretora")

    def _book_sale(
        self, position_id: int, sold: Decimal, proceeds: Decimal, fees: Decimal, *, basis: Decimal | None = None
    ) -> None:
        """Book a sale against the row's average, or against ``basis`` when given."""
        position = self._position(position_id)
        avg = basis if basis is not None else Decimal(str(position["avg_price"]))
        gross = proceeds - avg * sold
        net = gross - fees
        self.store.update_position(
            position_id,
            realized_pnl=float(Decimal(str(position["realized_pnl"])) + net),
            fees=float(Decimal(str(position["fees"])) + fees),
        )
        self.store.record_day(_day(), net_pnl=float(net), gross_pnl=float(gross), fees=float(fees))

    def _close(self, position_id: int, exit_price: Decimal, *, exit_slippage: Decimal | None = None) -> None:
        """Close the row, with the closing sale's slippage and the result in R (net PnL over the
        risk at the fill; left empty for a row without one)."""
        position = self._position(position_id)
        fields: dict[str, Any] = {}
        if exit_slippage is not None:
            fields["exit_slippage"] = float(exit_slippage)
        risk = _positive(position.get("initial_risk_usd"))
        if risk is not None:
            fields["r_multiple"] = float(Decimal(str(position["realized_pnl"])) / risk)
        if fields:
            self.store.update_position(position_id, **fields)
        self.store.close_position(
            position_id,
            exit_price=float(exit_price),
            realized_pnl=float(position["realized_pnl"]),
            fees=float(position["fees"]),
        )

    def flatten(self) -> list[dict[str, Any]]:
        """Exit every open position. A remainder that cannot be sold keeps a stop.

        A row with ``unprotected: True`` is a position that may be open with no
        stop: the caller must halt.
        """
        results: list[dict[str, Any]] = []
        for position in self.store.open_positions():
            try:
                results.append(self.exit_position(position["id"], "zerar posições no halt"))
            except Exception as exc:  # noqa: BLE001 - one position must not stop the rest, or the halt
                logger.exception("flatten of position %s failed", position["id"])
                results.append(
                    {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "position_id": position["id"],
                        "symbol": position["symbol"],
                        # Only the raise itself is known; the stop may already be off the book.
                        "unprotected": True,
                    }
                )
        return results

    # -- reconciliation --------------------------------------------------------

    def sync_positions(self) -> None:
        """Settle unknown writes, resolve pending entries and check every stop.

        Reconciliation reads up to a hundred orders from Binance, so it runs
        only while the journal holds a write that blocks new orders.
        """
        if service.journal_status()["blocked"]:
            reconciled = service.reconcile(self.profile_id)
            if reconciled.get("status") == "error":
                logger.warning("journal reconciliation failed: %s", reconciled.get("error"))
        failures: list[str] = []
        for position in self.store.live_positions():
            try:
                if position["status"] == "pending":
                    self._resolve_pending(position)
                else:
                    self._check_stop(position)
            except UnprotectedPosition:
                raise
            except Exception as exc:  # noqa: BLE001 - collected and raised below
                logger.exception("sync of position %s failed", position["id"])
                failures.append(f"{position['symbol']}: {type(exc).__name__}")
        if failures:
            raise RuntimeError("sincronização de posições falhou: " + "; ".join(failures))

    def _resolve_pending(self, position: dict[str, Any]) -> None:
        client_id = position.get("entry_client_order_id")
        if not client_id:
            self.store.void_position(position["id"], "a entrada não chegou a ser enviada")
            return
        lookup = service.order_status(position["symbol"], self.profile_id, str(client_id))
        if lookup.get("status") == "not_found":
            opened = datetime.fromisoformat(str(position["opened_at"]))
            if (_now() - opened).total_seconds() > _ACCEPTANCE_WINDOW_SECONDS:
                self.store.void_position(position["id"], "a corretora não registrou a entrada")
            return
        order = lookup.get("order") if lookup.get("status") == "ok" else None
        if not isinstance(order, dict):
            return
        state = str(order.get("status") or "").lower()
        filled = _dec(order.get("filled")) or Decimal(0)
        if filled > 0 and (state in _FILLED or state in _DEAD):
            self._open_from_fill(position["id"], {"status": "ok", **order, "client_order_id": client_id})
        elif state in _DEAD:
            self.store.void_position(position["id"], f"entrada {state} sem execução")

    def _check_stop(self, position: dict[str, Any]) -> None:
        leg = _leg(position)
        if leg is not None and leg.get("kind") == "rest_buy":
            # A resting buy is reconciled before the stop: a fill is merged and the stop re-placed
            # over the whole. While it rests with nothing bought, the stop is checked as usual.
            held = Decimal(str(position["qty"]))
            self._settle_rest(position, leg, cancel=False)
            position = self._position(position["id"])
            if position["status"] != "open" or Decimal(str(position["qty"])) != held:
                return
            leg = _leg(position)
        if leg is not None and leg.get("kind") == "buy":
            # A scale-in buy with an unknown result: merge what it bought before the stop is checked.
            self._settle_buy(position, leg)
            position = self._position(position["id"])
            if position["status"] != "open":
                return
            leg = _leg(position)
        if leg is not None and leg.get("kind") == "restop":
            outcome = self._restop(int(position["id"]))
            if outcome == RESTOP_OLD_STOP_PENDING:
                # The old stop is still on the book: count it, then check it like any stop below, so a
                # stop the price gapped through is still found and sold.
                self._restop_left(position, outcome)
            elif not outcome.endswith(RESTOP_BLOCKED_SUFFIX):
                if outcome == "comprado":
                    self._clear_stuck_retries(int(position["id"]), kind="restop")
                return
            position = self._position(position["id"])
            if position["status"] != "open":
                return
        if (
            position.get("keep_qty") is not None
            and not position.get("tp1_done")
            and not position.get("stop_order_id")
            and not position.get("stop_client_order_id")
        ):
            # A partial sale (a first target, a grid level) took the stop off and its result was
            # unknown: book what it sold, by its own leg key, before a stop is sized on the quantity held.
            self._settle_partial(position, str((leg or {}).get("leg") or "tp1"))
            position = self._position(position["id"])
            if position["status"] != "open":
                return
        client_id = position.get("stop_client_order_id")
        if not client_id:
            self.protect(position["id"])
            return
        lookup = service.order_status(position["symbol"], self.profile_id, str(client_id))
        if lookup.get("status") == "not_found":
            self._clear_stop(position["id"])
            self.protect(position["id"])
            return
        order = lookup.get("order") if lookup.get("status") == "ok" else None
        if not isinstance(order, dict):
            return
        state = str(order.get("status") or "").lower()
        if not position.get("stop_order_id") and order.get("order_id"):
            self.store.update_position(position["id"], stop_order_id=str(order["order_id"]))
        if state in _FILLED:
            self._close_from_stop(position, order)
            if self._position(position["id"])["status"] == "open":
                # Coins a resting buy added after the stop was placed: a new stop, or an exit at once.
                self.protect(position["id"])
        elif state in _DEAD:
            self._settle_cancelled_stop(position, order)
            if self._position(position["id"])["status"] == "open":
                self.protect(position["id"])
        else:
            # Resting (new, open or partly filled): a stop the price gapped through never fills. A
            # STOP_LOSS_LIMIT triggers on the last trade price, so it is stuck only once the last trade
            # reached the stop and the bid sits below its limit, both read from one quote.
            stop = _dec(position.get("stop_price"))
            quote = self._quote(position["symbol"]) or {}
            bid = _dec(quote.get("bid"))
            last = _dec(quote.get("last"))
            if stop is None or bid is None or last is None:
                # Unreadable: not stuck, and the count of failed cancels stays for the next sync.
                return
            if 0 < bid < stop * (1 - STOP_LIMIT_GAP) and 0 < last <= stop:
                self._stop_gapped(self._position(position["id"]), order, bid)
            else:
                # Not stuck any more: a later stuck stop counts its failed cancels from zero.
                self._clear_stuck_retries(int(position["id"]))

    def _stop_gapped(self, position: dict[str, Any], order: dict[str, Any], bid: Decimal) -> None:
        """Replace a stop the price gapped through: cancel it, then sell with IOC orders.

        The first IOC goes ``EXIT_LIMIT_GAP`` below the bid, the second
        ``EMERGENCY_LIMIT_GAP`` below it. Each order key comes from the exit attempt
        counter, which moves on after every final outcome, so the second IOC never
        replays the first. The cancel books any part the stop sold; a stop that
        filled before the cancel closes the row. While a write blocker or a pending
        scale-in buy keeps the stop on the book, it stays there and the next sync
        tries again. A position still open after the IOCs raises
        ``UnprotectedPosition``, and so does an IOC of unknown outcome (another
        sale could sell twice): the runtime halts.
        """
        position_id = int(position["id"])
        symbol = position["symbol"]
        stop = Decimal(str(position["stop_price"]))
        limit = _dec(order.get("price")) or stop * (1 - STOP_LIMIT_GAP)
        self._record(
            "stop_gapped",
            {
                "position_id": position_id,
                "symbol": symbol,
                "stop": decimal_text(stop),
                "limit": decimal_text(limit),
                "bid": decimal_text(bid),
                "order_state": str(order.get("status") or "").lower(),
            },
        )
        avg = Decimal(str(position["avg_price"]))
        qty_before = Decimal(str(position["qty"]))
        booked_before = Decimal(str(position["realized_pnl"])) + Decimal(str(position["fees"]))
        attempts = 0
        result: dict[str, Any] = {}
        for gap in (EXIT_LIMIT_GAP, EMERGENCY_LIMIT_GAP):
            attempts += 1
            result = self.exit_position(position_id, _STOP_GAPPED_REASON, protect_remainder=False, gap=gap, exit_ref=stop)
            current = self._position(position_id)
            if current["status"] != "open":
                break
            resting = bool(current.get("stop_order_id") or current.get("stop_client_order_id"))
            if resting:
                # The stop still rests (a write blocker, or a cancel that did not land): no sale went
                # over it and the coins stay locked under it. The next sync tries again.
                self._stuck_stop_left(position_id, symbol, result)
                return
            if result.get("status") == "unknown":
                break
        current = self._position(position_id)
        if current["status"] == "open":
            raise UnprotectedPosition(f"{symbol}: {_STOP_GAPPED_REASON}; saída falhou: {_reason(result)}")
        self._clear_stuck_retries(position_id)
        # Every sale is booked on the row (gross = proceeds - avg x sold), stop fills included. A row
        # the stop itself closed keeps its quantity: its exit price is the stop's average fill.
        sold = qty_before - Decimal(str(current["qty"]))
        gross = Decimal(str(current["realized_pnl"])) + Decimal(str(current["fees"])) - booked_before
        exit_avg = (gross + avg * sold) / sold if sold > 0 else Decimal(str(current["exit_price"]))
        slippage = _exit_slippage(exit_avg, stop)
        if slippage is not None:
            # Over every sale since the stop was found stuck (its own part fill, both IOCs), not the last one.
            self.store.update_position(position_id, exit_slippage=float(slippage))
        self._record(
            "stop_gapped_exit",
            {
                "position_id": position_id,
                "exit_avg": float(exit_avg),
                "slippage_vs_stop": float(exit_avg / stop - 1) if stop > 0 else None,
                "attempts": attempts,
            },
        )

    def _stuck_stop_left(self, position_id: int, symbol: str, result: dict[str, Any]) -> None:
        """A stuck stop that could not come off stays on the book until the next sync.

        Only a write blocker (the gate refuses every order) waits for as long as it lasts. Every other
        reason counts: a cancel that failed, a resting buy whose result is unknown, a spent order
        budget. After ``STUCK_STOP_MAX_RETRIES`` syncs in a row the position cannot be sold below
        its gapped stop, so ``UnprotectedPosition`` halts the runtime.
        """
        logger.warning("stuck stop of position %s left on the book: %s", position_id, result.get("error"))
        if service.write_blocker() is not None:
            return
        retries = self._count_stuck(str(position_id))
        self._record("stop_gapped_retry", {"position_id": position_id, "symbol": symbol, "error": _reason(result), "retries": retries})
        if retries >= STUCK_STOP_MAX_RETRIES:
            self._clear_stuck_retries(position_id)
            raise UnprotectedPosition(
                f"{symbol}: {_STOP_GAPPED_REASON}; o stop não saiu da corretora em {retries} ciclos seguidos: {_reason(result)}"
            )

    def _restop_left(self, position: dict[str, Any], outcome: str) -> None:
        """A re-stop whose old stop did not come off: the coins the buy added have no stop. It counts
        like a stuck stop; after ``STUCK_STOP_MAX_RETRIES`` syncs in a row ``UnprotectedPosition`` halts."""
        position_id = int(position["id"])
        retries = self._count_stuck(f"{position_id}:restop")
        self._record("restop_retry", {"position_id": position_id, "symbol": position["symbol"], "error": outcome, "retries": retries})
        if retries >= STUCK_STOP_MAX_RETRIES:
            self._clear_stuck_retries(position_id, kind="restop")
            raise UnprotectedPosition(
                f"{position['symbol']}: o stop antigo não saiu da corretora em {retries} ciclos seguidos; a compra nova está sem stop"
            )

    def _count_stuck(self, key: str) -> int:
        counts = self.store.get_state(_STUCK_RETRIES_KEY)
        counts = dict(counts) if isinstance(counts, dict) else {}
        retries = int(counts.get(key) or 0) + 1
        counts[key] = retries
        self.store.set_state(_STUCK_RETRIES_KEY, counts)
        return retries

    def _clear_stuck_retries(self, position_id: int, *, kind: str = "gap") -> None:
        key = str(position_id) if kind == "gap" else f"{position_id}:{kind}"
        counts = self.store.get_state(_STUCK_RETRIES_KEY)
        if isinstance(counts, dict) and key in counts:
            self.store.set_state(_STUCK_RETRIES_KEY, {name: value for name, value in counts.items() if name != key})

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.ledger(kind, payload)
        except Exception:  # noqa: BLE001 - a ledger failure must not stop an exit
            logger.exception("ledger append failed (%s)", kind)

    def _settle_partial(self, position: dict[str, Any], leg: str) -> None:
        """Read the partial sell of ``leg`` at the current attempt by its key and book its fill."""
        position_id = int(position["id"])
        attempt = int(position["exit_attempts"])
        client_id = service.client_order_id(self.profile_id, self._key(position_id, leg, attempt))
        lookup = service.order_status(position["symbol"], self.profile_id, client_id)
        order = lookup.get("order") if lookup.get("status") == "ok" else None
        if not isinstance(order, dict):
            # Never placed, or unreadable: ``protect`` sizes the stop on the balance actually held.
            return
        state = str(order.get("status") or "").lower()
        if state not in _FILLED and state not in _DEAD:
            return
        self.store.update_position(position_id, exit_attempts=attempt + 1)
        filled = _dec(order.get("filled")) or Decimal(0)
        if filled <= 0:
            return
        proceeds = _dec(order.get("cost")) or filled * (_dec(order.get("average")) or Decimal(0))
        self._book_sale(position_id, filled, proceeds, proceeds * Decimal(str(SPOT_TAKER)))
        held = Decimal(str(position["qty"])) - filled
        self.store.update_position(position_id, qty=float(max(held, Decimal(0))))
        keep = _dec(position.get("keep_qty")) or Decimal(0)
        if held > keep:
            return
        if position.get("exit_policy") == "day":
            breakeven = Decimal(str(position["avg_price"])) * Decimal(str(1 + DAY.breakeven_buffer))
            self.store.update_position(position_id, stop_price=float(breakeven), tp1_done=1, keep_qty=None, pending_leg=None)
            self._decide(position["symbol"], position["strategy"], "saida_parcial", "primeiro alvo (TP1) confirmado na sincronização")
        else:
            self.store.update_position(position_id, keep_qty=None, pending_leg=None)
            self._decide(position["symbol"], position["strategy"], "saida_parcial", "venda parcial confirmada na sincronização")

    # -- helpers ---------------------------------------------------------------

    def _fill(self, symbol: str, result: dict[str, Any], rules: MarketRules | None) -> Fill | None:
        """Filled quantity, quote cost and fees. Reads the order when the result lacks them.

        ``None`` when the result carries no fill and the order cannot be read:
        a failed read is not an empty fill.
        """
        order = result
        if _dec(result.get("filled")) is None:
            if not result.get("client_order_id"):
                return None
            lookup = service.order_status(symbol, self.profile_id, str(result["client_order_id"]))
            if lookup.get("status") != "ok" or not isinstance(lookup.get("order"), dict):
                return None
            order = lookup["order"]
        qty = _dec(order.get("filled")) or Decimal(0)
        average = _dec(order.get("average")) or _dec(order.get("price"))
        cost = _dec(order.get("cost")) or ((average or Decimal(0)) * qty)
        base, quote = symbol.split("/", 1)
        taker = rules.taker_fee if rules is not None else Decimal(str(SPOT_TAKER))
        fee_base = fee_quote = Decimal(0)
        raw_fees = order.get("fees")
        fees: list[Any] = raw_fees if isinstance(raw_fees, list) else []
        for item in fees:
            amount = _dec(item.get("cost")) or Decimal(0)
            currency = str(item.get("currency") or "").upper()
            if currency == base:
                fee_base += amount
            elif currency == quote:
                fee_quote += amount
            else:
                # Paid in another asset (BNB): book the taker estimate in quote.
                fee_quote += cost * taker
        if not fees:
            fee_quote = cost * taker
        return Fill(qty=qty, cost=cost, fee_base=fee_base, fee_quote=fee_quote)

    def _bid(self, symbol: str) -> Decimal | None:
        return self._quote_field(symbol, "bid")

    def _ask(self, symbol: str) -> Decimal | None:
        return self._quote_field(symbol, "ask")

    def _buy_block(self) -> str | None:
        """Why every buy is refused now: REDUCING, or the daily-loss pause (``paused_until``), else None.
        Stops, exits and the sync go on under both."""
        return self._reducing() or self._paused()

    def _paused(self) -> str | None:
        """The daily-loss pause: buys wait for the next UTC day (``bot_state.paused_until``)."""
        until = self.store.get_state("paused_until")
        if isinstance(until, str) and until > _now().isoformat():
            return "pausado até o próximo dia UTC"
        return None

    def _reducing(self) -> str | None:
        """Why a buy is refused: the portfolio is in REDUCING (``bot_state.risk_state``), else None."""
        state = self.store.get_state("risk_state")
        if not isinstance(state, dict) or state.get("level") != "reducing":
            return None
        drawdown = state.get("drawdown")
        if isinstance(drawdown, (int, float)) and not isinstance(drawdown, bool):
            return f"modo só reduzir: drawdown de {drawdown:.1%}"
        return "modo só reduzir"

    def _stop_window(self, kind: str, position_id: int, started: float | None) -> None:
        """Store how long the position stood without a stop, once the new one is on the book.

        Nothing is stored when no stop was taken off (``started`` None) or none rests now
        (the row closed, or the next sync re-protects it). A failed write never stops an order.
        """
        if started is None:
            return
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            position = self.store.position(position_id)
            if position is None or position["status"] != "open":
                return
            if not (position.get("stop_order_id") or position.get("stop_client_order_id")):
                return
            self.store.record_stop_window(kind, position["symbol"], position_id, elapsed_ms)
        except Exception:  # noqa: BLE001 - a metric must not interrupt the order flow
            logger.exception("stop window of position %s not recorded", position_id)

    def _mark(self, symbol: str) -> Decimal | None:
        """Last trade price, the one the mandate gate values a sell by quantity at."""
        return self._quote_field(symbol, "last")

    def _quote_field(self, symbol: str, field: str) -> Decimal | None:
        quote = self._quote(symbol)
        return _dec(quote.get(field)) if quote is not None else None

    def _quote(self, symbol: str) -> dict[str, Any] | None:
        """The current quote of ``symbol`` (``bid``, ``ask``, ``last``), or None when unreadable."""
        try:
            payload = service.get_quote(symbol, self.profile_id)
        except Exception as exc:  # noqa: BLE001 - callers treat a missing price as "cannot price"
            logger.warning("quote for %s failed: %s", symbol, type(exc).__name__)
            return None
        quote = payload.get("quote") if isinstance(payload, dict) else None
        return quote if isinstance(quote, dict) else None

    def _held_for_sell(self) -> list[dict[str, Any]]:
        """The bot's exposure the mandate gate checks a sell against: the open positions only.

        The gate caps post-trade exposure on a sell too, so a resting buy counted here could refuse
        a protective stop or an exit on another symbol near the cap, and halt the runtime.
        """
        return [{"symbol": row["symbol"], "quantity": row["qty"]} for row in self.store.open_positions()]

    def _held_for_buy(self) -> list[dict[str, Any]]:
        """The bot's exposure the mandate gate checks a buy against: the open positions, and each
        resting buy not yet settled at its full quantity on its symbol (it fills without passing the
        gate again)."""
        held: list[dict[str, Any]] = []
        for row in self.store.open_positions():
            held.append({"symbol": row["symbol"], "quantity": row["qty"]})
            leg = _leg(row)
            resting = _positive(leg.get("qty")) if leg is not None and leg.get("kind") == "rest_buy" else None
            if resting is not None:
                held.append({"symbol": row["symbol"], "quantity": float(resting)})
        return held

    def _position(self, position_id: int) -> dict[str, Any]:
        position = self.store.position(position_id)
        if position is None:
            raise KeyError(position_id)
        return position

    def _key(self, position_id: int, leg: str, attempt: int) -> str:
        """Order key unique to this store, position, leg and attempt."""
        salt = self.store.get_state("order_key_salt")
        if not salt:
            salt = uuid.uuid4().hex
            self.store.set_state("order_key_salt", salt)
        raw = f"{salt}|{position_id}|{leg}|{attempt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _decide(
        self,
        symbol: str,
        strategy: str,
        decision: str,
        reason: str,
        *,
        manual: bool = False,
        result: dict[str, Any] | None = None,
        position_id: int | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "symbol": symbol,
            "strategy": strategy,
            "decision": decision,
            "reason": reason,
            "manual": manual,
        }
        if position_id is not None:
            row["position_id"] = position_id
        if result is not None:
            row["exchange"] = {key: result.get(key) for key in ("status", "order_id", "client_order_id", "reasons")}
        self.store.add_decision(row)
        return row

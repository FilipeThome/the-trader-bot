"""Position lifecycle on Binance spot. Every order goes through ``bot.exchange.service``.

A position moves ``pending -> open -> closed`` or ``pending -> void``:

* entry: a ``pending`` row reserves the slot, then a market buy sized in quote;
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

When a position cannot be protected or closed, ``UnprotectedPosition`` is
raised and the runtime halts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bot.evaluator import SPOT_TAKER, Opportunity
from bot.exchange import service
from bot.exchange.execution.rules import MarketRules, decimal_text, floor_to_step
from bot.exchange.weight import weight_pressure
from bot.store import Store
from bot.strategies import exit_policy_for
from bot.strategies.day import DAY
from bot.strategies.trailing import trailing_level

logger = logging.getLogger(__name__)

#: A stop-limit sells at most this far below its trigger.
STOP_LIMIT_GAP = Decimal("0.005")
#: An exit IOC is priced this far below the bid so it crosses the book.
EXIT_LIMIT_GAP = Decimal("0.005")
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
    def __init__(self, store: Store, profile_id: str, *, exit_policy: str = "trailing") -> None:
        self.store = store
        self.profile_id = profile_id
        #: Recorded on every position, so the hit rate only mixes trades of one exit rule.
        self.exit_policy = exit_policy

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
        """Open one position. A test row needs the Spot Testnet profile and a manual
        order, or ``allow_test`` (automatic trading with the testnet test mode on)."""
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
        if clock_blocked or weight_pressure():
            return decide("nao_operar", "peso da API ou relógio fora de 1s — só cancelamentos e saídas")
        if opportunity.size_usd <= 0:
            return decide("bloqueado", "tamanho de ordem zerado")
        policy = exit_policy_for(opportunity.strategy, self.exit_policy)
        round_trip = ORDERS_PER_DAY_ROUND_TRIP if policy == "day" else ORDERS_PER_ROUND_TRIP
        # Every open position keeps one order in reserve for its own re-stop or exit.
        if service.orders_remaining(self.profile_id) < round_trip + len(self.store.open_positions()):
            return decide("bloqueado", "o orçamento diário de ordens não cobre entrada, stop e saída")
        position_id = self.store.open_pending(
            opportunity.symbol,
            opportunity.strategy,
            stop_price=opportunity.stop_price,
            target_price=opportunity.target_price,
            exit_policy=policy,
            test_only=opportunity.test_only,
            entry_win_rate=opportunity.win_rate,
            # Written with the reservation: an entry the sync resolves later keeps its time limit.
            exit_deadline=(_now() + DAY.max_hold).isoformat() if policy == "day" else None,
        )
        if position_id is None:
            return decide("bloqueado", "já existe posição viva neste par e estratégia")
        key = self._key(position_id, "entry", 0)
        # Persisted before the write: a crash after the send still leaves a row the sync can resolve.
        self.store.update_position(position_id, entry_client_order_id=service.client_order_id(self.profile_id, key))
        result = service.submit_order(
            opportunity.symbol,
            self.profile_id,
            side="buy",
            notional=f"{opportunity.size_usd:.2f}",
            order_type="market",
            idempotency_key=key,
            session_id="bot",
            held=self._held(),
        )
        status = _status(result)
        if status == "ok":
            opened = self._open_from_fill(position_id, result)
            if opened is None:
                return decide("pendente", "execução da entrada ainda não confirmada", result)
            return decide("operar" if opened else "bloqueado", opportunity.reason if opened else "ordem sem execução", result)
        if status == "unknown":
            return decide("pendente", f"resultado da entrada desconhecido: {_reason(result)}", result)
        self.store.void_position(position_id, _reason(result))
        return decide("bloqueado", _reason(result), result)

    def _open_from_fill(self, position_id: int, result: dict[str, Any]) -> bool | None:
        """Turn a filled entry into an open position and put its stop on the book.

        ``None`` when the fill cannot be read yet: the row stays ``pending`` for the sync.
        """
        position = self._position(position_id)
        symbol = position["symbol"]
        base = symbol.split("/", 1)[0]
        rules = service.market_rules(symbol, self.profile_id)
        fill = self._fill(symbol, result, rules)
        if fill is None:
            return None
        if fill.qty <= 0:
            self.store.void_position(position_id, "ordem sem execução")
            return False
        taker = rules.taker_fee if rules is not None else Decimal(str(SPOT_TAKER))
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
        entry_fee = fill.fee_quote + fill.fee_base * fill.average
        trail_pct = (avg - stop) / avg if avg > 0 and stop < avg else DEFAULT_STOP_PCT
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
        )
        self.store.record_day(_day(), trades=1, fees=float(entry_fee))
        self.protect(position_id)
        return True

    # -- DCA and grid inventory ----------------------------------------------

    def open_instance(
        self, instance_id: int, kind: str, symbol: str, notional: Decimal, stop: Decimal, *, clock_blocked: bool = False
    ) -> dict[str, Any]:
        """Open the inventory of a DCA or grid instance: a market buy, then its resting stop.

        The row is an ordinary position whose strategy and exit policy are the kind, so
        the sync, the halt, the risk checks and the panel treat it like any other.
        """
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
            held=self._held(),
        )
        status = _status(result)
        if status == "ok":
            opened = self._open_from_fill(position_id, result)
            if opened is None:
                return {"status": "pending", "position_id": position_id}
            if not opened:
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
        if free is None or free < notional * (1 + rules.taker_fee):
            return "saldo em USDT insuficiente"
        key = self._key(position_id, leg, 0)
        client_id = service.client_order_id(self.profile_id, key)
        self.store.update_position(
            position_id,
            pending_leg=json.dumps({"kind": "buy", "leg": leg, "client_order_id": client_id, "stop": str(next_stop)}),
        )
        result = service.submit_order(
            symbol,
            self.profile_id,
            side="buy",
            notional=f"{notional:.2f}",
            order_type="market",
            idempotency_key=key,
            session_id="bot",
            held=self._held(),
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

    def _merge_buy(self, position_id: int, fill: Fill, next_stop: Decimal, rules: MarketRules | None) -> str:
        """Add a filled buy to the position, then re-place the stop at ``next_stop`` for the whole."""
        position = self._position(position_id)
        if fill.qty <= 0:
            self.store.update_position(position_id, pending_leg=None)
            return "compra sem execução"
        taker = rules.taker_fee if rules is not None else Decimal(str(SPOT_TAKER))
        # The same sell margin as an entry: a sell needs free base >= qty x (1 + taker).
        added = (fill.qty - fill.fee_base) / (1 + taker)
        if rules is not None:
            added = floor_to_step(added, rules.lot_step)
        held = Decimal(str(position["qty"]))
        qty = held + added
        avg = Decimal(str(position["avg_price"]))
        if qty > 0:
            avg = (avg * held + fill.cost + fill.fee_quote) / qty
        fee = fill.fee_quote + fill.fee_base * fill.average
        self.store.update_position(
            position_id,
            qty=float(qty),
            avg_price=float(avg),
            fees=float(Decimal(str(position["fees"])) + fee),
            pending_leg=json.dumps({"kind": "restop", "stop": str(next_stop)}),
        )
        self.store.record_day(_day(), fees=float(fee))
        self._decide(position["symbol"], position["strategy"], "aumento", f"comprou {added} a {fill.average:.8g}", position_id=position_id)
        return self._restop(position_id)

    def _restop(self, position_id: int) -> str:
        """Put the stop of a position that grew on its whole quantity, at the level its leg recorded."""
        position = self._position(position_id)
        leg = _leg(position) or {}
        if position.get("stop_order_id") or position.get("stop_client_order_id"):
            settled = self._take_stop_off(position)
            if settled == "filled":
                raise UnprotectedPosition(f"{position['symbol']}: o stop executou durante o aumento; a compra nova ficou sem stop")
            if settled == "pending":
                return "o stop antigo não saiu da corretora; a parte nova espera o próximo ciclo"
            position = self._position(position_id)
            if position["status"] != "open":
                raise UnprotectedPosition(f"{position['symbol']}: posição fechada durante o aumento; a compra nova ficou sem stop")
        fields: dict[str, Any] = {"pending_leg": None}
        if leg.get("stop"):
            fields["stop_price"] = float(Decimal(str(leg["stop"])))
        self.store.update_position(position_id, **fields)
        self.protect(position_id)
        return "comprado"

    def _settle_buy(self, position: dict[str, Any], leg: dict[str, Any]) -> None:
        """Read a scale-in buy whose result was unknown and merge what it bought."""
        symbol = position["symbol"]
        client_id = str(leg.get("client_order_id") or "")
        lookup = service.order_status(symbol, self.profile_id, client_id)
        if lookup.get("status") == "not_found":
            updated = datetime.fromisoformat(str(position["updated_at"]))
            if (_now() - updated).total_seconds() > _ACCEPTANCE_WINDOW_SECONDS:
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
            held = free
            self.store.update_position(position_id, qty=float(free))
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
            held=self._held(),
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

    def trail(self, position_id: int, last_price: Decimal, *, reserve: int) -> str:
        """Raise the resting stop to follow the highest price. Returns what happened.

        The stop moves only when every check passes before it leaves the book:
        no halt or mandate problem, no API weight pressure, a confirmed resting
        stop, a new level at least ``TRAIL_STEP`` above the old one and below
        the bid by the stop-limit gap, and ``reserve`` orders left today (one
        per open position). Otherwise the current stop simply stays. A refused
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
        if bid is None or candidate >= bid * (1 - STOP_LIMIT_GAP):
            return "novo stop perto demais do preço"
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
        settled = self._take_stop_off(position)
        if settled == "filled":
            return "stop executado"
        if settled == "pending":
            return "o stop não saiu da corretora"
        if self._position(position_id)["status"] != "open":
            return "posição fechada"
        self.store.update_position(position_id, stop_price=float(candidate))
        self.protect(position_id)
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
    ) -> dict[str, Any]:
        """Take the stop off the book and sell the position with LIMIT IOC orders.

        With ``keep_qty`` it sells only down to that quantity (a day trade's first
        target): the row never closes, the stop goes back on the rest at
        ``next_stop`` once the sale is complete, or at the old stop when it is not.
        ``leg`` keeps the order keys of a partial sale apart from a later full exit.
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
        symbol = position["symbol"]
        blocker = service.write_blocker()
        if blocker is not None:
            # The gate would refuse the sell; the stop stays on the book.
            return {"status": "blocked", "error": f"{blocker}; o stop segue na corretora"}
        if service.orders_remaining(self.profile_id) < 1:
            return {"status": "blocked", "error": "orçamento diário de ordens esgotado; o stop segue na corretora"}
        if position.get("stop_order_id") or position.get("stop_client_order_id"):
            settled = self._take_stop_off(position)
            if settled == "filled":
                return {"status": "ok", "closed_by": "stop"}
            if settled == "pending":
                return {"status": "error", "error": "o stop não saiu da corretora; ele continua protegendo"}
            position = self._position(position_id)
            if position["status"] != "open":
                return {"status": "ok", "closed_by": "stop"}
        rules = service.market_rules(symbol, self.profile_id)
        if rules is None:
            return self._after_exit(position_id, reason, "regras do mercado indisponíveis", protect_remainder)
        partial = keep_qty is not None
        if partial:
            # The kept quantity and the leg are read back in the sync when the result is unknown
            # (``_settle_partial``), and a retry sells only down to the same quantity. A retry of
            # the same leg keeps the quantity the sale started from.
            started = _leg(position) or {}
            if started.get("kind") != "sell" or started.get("leg") != leg or started.get("qty_before") is None:
                started = {"qty_before": float(position["qty"])}
            self.store.update_position(
                position_id,
                keep_qty=float(keep_qty),
                pending_leg=json.dumps({"kind": "sell", "leg": leg, "qty_before": started["qty_before"]}),
            )
        remaining = Decimal(str(position["qty"])) - (keep_qty or Decimal(0))
        cap = service.order_notional_cap(symbol, self.profile_id)
        sold = proceeds = Decimal(0)
        failure = ""
        unknown = False
        while remaining > 0:
            bid = self._bid(symbol)
            if bid is None or bid <= 0:
                failure = "cotação indisponível"
                break
            limit = floor_price(bid * (1 - EXIT_LIMIT_GAP), rules)
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
                held=self._held(),
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
            return self._after_partial(position_id, reason, remaining, sold, failure, unknown, next_stop)
        if remaining <= 0 or failure == "sobra abaixo do mínimo da corretora":
            self._close(position_id, proceeds / sold if sold > 0 else Decimal(str(position["avg_price"])))
            self._decide(symbol, position["strategy"], "saida", reason)
            return {"status": "ok", "sold": float(sold)}
        self.store.update_position(position_id, qty=float(remaining))
        if unknown:
            # Reconciliation settles the order first; the next sync re-protects what is left.
            return {"status": "unknown", "error": failure, "sold": float(sold)}
        return self._after_exit(position_id, reason, failure, protect_remainder)

    def _after_partial(
        self,
        position_id: int,
        reason: str,
        remaining: Decimal,
        sold: Decimal,
        failure: str,
        unknown: bool,
        next_stop: Decimal | None,
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
            return {"status": "ok", "sold": float(sold), "partial": True}
        return self._after_exit(position_id, reason, failure or "venda parcial incompleta", True)

    def _after_exit(self, position_id: int, reason: str, failure: str, protect_remainder: bool) -> dict[str, Any]:
        position = self._position(position_id)
        self._decide(position["symbol"], position["strategy"], "saida_parcial", f"{reason}: {failure}")
        if protect_remainder:
            self.protect(position_id)
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
            return "filled"
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
        self._clear_stop(position["id"])
        if filled > 0 and order is not None:
            proceeds = _dec(order.get("cost")) or filled * (_dec(order.get("average")) or Decimal(0))
            fee = proceeds * Decimal(str(SPOT_TAKER))
            self._book_sale(position["id"], filled, proceeds, fee)
            remaining = Decimal(str(position["qty"])) - filled
            if remaining <= 0:
                self._close(position["id"], proceeds / filled)
                return "filled"
            self.store.update_position(position["id"], qty=float(remaining))
        return "cancelled"

    def _clear_stop(self, position_id: int) -> None:
        """Forget the stop. A new stop for this position gets a new key, never a replay."""
        attempts = int(self._position(position_id)["stop_attempts"])
        self.store.update_position(position_id, stop_order_id=None, stop_client_order_id=None, stop_attempts=attempts + 1)

    def _close_from_stop(self, position: dict[str, Any], order: dict[str, Any]) -> None:
        filled = _dec(order.get("filled")) or Decimal(str(position["qty"]))
        proceeds = _dec(order.get("cost")) or filled * (_dec(order.get("average")) or Decimal(str(position["stop_price"])))
        fee = proceeds * Decimal(str(SPOT_TAKER))
        self._book_sale(position["id"], filled, proceeds, fee)
        self._close(position["id"], proceeds / filled if filled > 0 else Decimal(str(position["stop_price"])))
        self._decide(position["symbol"], position["strategy"], "saida", "stop executado na corretora")

    def _book_sale(self, position_id: int, sold: Decimal, proceeds: Decimal, fees: Decimal) -> None:
        position = self._position(position_id)
        avg = Decimal(str(position["avg_price"]))
        gross = proceeds - avg * sold
        net = gross - fees
        self.store.update_position(
            position_id,
            realized_pnl=float(Decimal(str(position["realized_pnl"])) + net),
            fees=float(Decimal(str(position["fees"])) + fees),
        )
        self.store.record_day(_day(), net_pnl=float(net), gross_pnl=float(gross), fees=float(fees))

    def _close(self, position_id: int, exit_price: Decimal) -> None:
        position = self._position(position_id)
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
        if leg is not None and leg.get("kind") == "buy":
            # A scale-in buy with an unknown result: merge what it bought before the stop is checked.
            self._settle_buy(position, leg)
            position = self._position(position["id"])
            if position["status"] != "open":
                return
            leg = _leg(position)
        if leg is not None and leg.get("kind") == "restop":
            self._restop(int(position["id"]))
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
        elif state in _DEAD:
            self._settle_cancelled_stop(position, order)
            if self._position(position["id"])["status"] == "open":
                self.protect(position["id"])

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

    def _mark(self, symbol: str) -> Decimal | None:
        """Last trade price, the one the mandate gate values a sell by quantity at."""
        return self._quote_field(symbol, "last")

    def _quote_field(self, symbol: str, field: str) -> Decimal | None:
        try:
            payload = service.get_quote(symbol, self.profile_id)
        except Exception as exc:  # noqa: BLE001 - callers treat a missing price as "cannot price"
            logger.warning("quote for %s failed: %s", symbol, type(exc).__name__)
            return None
        quote = payload.get("quote") if isinstance(payload, dict) else None
        return _dec(quote.get(field)) if isinstance(quote, dict) else None

    def _held(self) -> list[dict[str, Any]]:
        return [{"symbol": row["symbol"], "quantity": row["qty"]} for row in self.store.open_positions()]

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

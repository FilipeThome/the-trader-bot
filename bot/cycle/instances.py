"""The DCA and grid robots of one cycle: their plan, creation, step, wind-down and the book of
what they reserved.

``Robots`` holds the collaborators the ``Runtime`` looks up for each call (``Runtime._robots``):
a test or a mode switch may replace the executor, the config or a bound method at any time,
so nothing is kept between calls. Events go through the ``ledger`` given, and a position left
without protection goes to the ``halt`` given.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bot.collector import MarketSnapshot
from bot.config import BotConfig, RiskLimits
from bot.cycle import market
from bot.cycle.context import Ledger, profile_for
from bot.cycle.exits import _inflight, _rest_level
from bot.evaluator import SPOT_TAKER
from bot.exchange import service
from bot.executor import REST_PENDING, REST_PLACED, Executor, UnprotectedPosition
from bot.instances import (
    ORDERS_PER_LEG,
    ORDERS_PER_RESTING_LEG,
    DcaParams,
    GridParams,
    cooldown_ends,
    creation_problems,
    dca_ladder,
    dca_next,
    dca_plan,
    dca_rest_target,
    dca_stop,
    grid_k_from_qty,
    grid_levels,
    grid_next,
    grid_plan,
    grid_qty,
    grid_rest_target,
    instance_budget,
    worst_case_loss,
)
from bot.live.mandate.store import load_mandate
from bot.logging_setup import redact
from bot.risk import RiskState, buy_block, open_risk, utc_day
from bot.store import Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Robots:
    """The DCA and grid robots over one call's collaborators.

    ``read_limits`` reads the mandate-tightened limits now (``Runtime._limits``); it is called
    only where no cycle limits were passed in. ``halted`` reads the HALT flag now.
    """

    store: Store
    executor: Executor
    config: BotConfig
    ledger: Ledger
    halt: Callable[..., dict[str, Any]]
    halted: Callable[[], bool]
    read_limits: Callable[[], RiskLimits]
    min_notional: Callable[[str], float | None]

    def create_instance(
        self, kind: str, symbol: str, params: DcaParams | GridParams, *, clock_blocked: bool
    ) -> dict[str, Any]:
        """Start a DCA or grid instance once its whole plan passes; its first buy goes at once.
        The caller holds the lock and checked the halt; it starts the cycle when asked."""
        now = datetime.now(timezone.utc)
        # Outside the cycle: the level the last cycle stored, never one read on the panel's unpriced view.
        block = self._robot_buy_block(now, clock_blocked=clock_blocked, state=None)
        if block is not None:
            return {"status": "error", "error": block}
        plan = self._instance_plan(kind, symbol, params)
        if plan["status"] != "ok":
            return plan
        if plan["problems"]:
            return {"status": "error", "error": "; ".join(plan["problems"]), "plan": plan}
        price = float(plan["price"])
        state: dict[str, Any] = {"legs": 0, "position_id": None}
        state.update({"base_price": price, "filled": 0, "round": 0} if kind == "dca" else {"start_price": price})
        instance_id = self.store.create_instance(kind, symbol, params.model_dump(), state)
        if instance_id is None:
            return {"status": "error", "error": f"já existe um robô {kind} neste par"}
        self.ledger("instance_created", {"id": instance_id, "kind": kind, "symbol": symbol, "params": params.model_dump()})
        instance = self.store.instance(instance_id)
        assert instance is not None
        try:
            step = self._step_instance(instance, price, now)
        except UnprotectedPosition as exc:
            self.halt(f"posição sem proteção: {exc}", flatten=False)
            return {"status": "error", "error": redact(exc), "id": instance_id}
        return {"status": "ok", "id": instance_id, "step": step}

    def _stop_instance(self, instance_id: int, reason: str) -> dict[str, Any]:
        """Under the lock: mark the instance ``stopping`` and sell what it holds."""
        self.store.update_instance(instance_id, status="stopping", reason=reason)
        try:
            outcome = self._wind_down(instance_id, reason)
        except UnprotectedPosition as exc:
            self.halt(f"posição sem proteção: {exc}", flatten=False)
            return {"status": "error", "error": redact(exc)}
        current = self.store.instance(instance_id) or {}
        return {"status": "ok", "instance_status": current.get("status"), "outcome": outcome}

    def _instance_plan(self, kind: str, symbol: str, params: DcaParams | GridParams) -> dict[str, Any]:
        profile = profile_for(self.config)
        quote = self._quote(symbol)
        if quote is None:
            return {"status": "error", "error": "cotação do par indisponível"}
        bid, ask, price = quote
        spread = (ask - bid) / ((ask + bid) / 2) if ask > bid > 0 else 0.0
        rules = service.market_rules(symbol, profile)
        if rules is None:
            return {"status": "error", "error": "regras do mercado indisponíveis"}
        cap = service.order_notional_cap(symbol, profile)
        taker = float(rules.taker_fee)
        capital = self.config.capital_for_sizing()
        limits = self.read_limits()
        book = self._book()
        exposure = sum(float(row["qty"] or 0) * float(row["avg_price"] or 0) for row in book if row["status"] == "open")
        exposure += sum(float(row.get("reserved_usd") or 0) for row in book if row["status"] == "reserved")
        if isinstance(params, DcaParams):
            plan = dca_plan(params, price, taker=taker)
        else:
            plan = grid_plan(params, price, taker=taker, spread=spread)
        problems = creation_problems(
            "dca" if isinstance(params, DcaParams) else "grid",
            plan,
            min_notional=float(rules.min_notional or 0),
            order_cap=float(cap) if cap is not None else float("inf"),
            sizing_capital=capital,
            exposure_room=capital * limits.max_exposure_pct - exposure,
            risk_per_trade_pct=limits.risk_per_trade_pct,
            taker=taker,
            spread=spread,
            params=params,
            live_instances=len(self.store.instances(live_only=True)),
            max_instances=limits.max_instances,
        )
        if cap is None:
            problems.append(self._missing_cap_reason())
        risk = open_risk(book, pending_risk=capital * limits.risk_per_trade_pct)
        risk_cap = capital * limits.max_open_risk_pct
        if risk + float(plan["loss_at_stop_usd"]) > risk_cap:
            problems.append(f"risco da carteira passaria do limite de US$ {risk_cap:.2f} até os stops")
        if isinstance(params, GridParams) and not params.lower < price < params.upper:
            problems.append(f"o preço atual {price:.8g} está fora da faixa do grid")
        if any(str(row["symbol"]) == symbol for row in book):
            problems.append("o par já tem posição ou robô: um por par")
        reserved = sum(int(row["params"]["max_orders_per_day"]) for row in self.store.instances(live_only=True))
        if reserved + params.max_orders_per_day > limits.instance_orders_per_day:
            problems.append(
                f"ordens por dia dos robôs: {reserved} em uso + {params.max_orders_per_day} passa da reserva de "
                f"{limits.instance_orders_per_day} (limite instance_orders_per_day)"
            )
        if service.orders_remaining(profile) < 2 * ORDERS_PER_LEG + len(self.store.open_positions()):
            problems.append("o orçamento diário de ordens de hoje não cobre a primeira compra e o stop")
        return {
            "status": "ok",
            "kind": "dca" if isinstance(params, DcaParams) else "grid",
            "symbol": symbol,
            "price": price,
            "spread": spread,
            "taker": taker,
            "order_cap": None if cap is None else float(cap),
            "plan": plan,
            "problems": problems,
        }

    def _missing_cap_reason(self) -> str:
        """Why ``service.order_notional_cap`` gave no cap, pointing at the fix: the mandate, or on
        the Spot Testnet the per-order cap binance.json must set for the quote asset."""
        if load_mandate("binance") is not None and self.config.mode != "production":
            return "teto por ordem do testnet indisponível: defina testnet_max_order_notional em binance.json"
        return "teto por ordem indisponível: confirme o mandato em Controles"

    def _quote(self, symbol: str) -> tuple[float, float, float] | None:
        """Bid, ask and last price of ``symbol``; None when unreadable."""
        try:
            payload = service.get_quote(symbol, profile_for(self.config))
        except Exception:  # noqa: BLE001 - an unreadable quote refuses the plan
            return None
        quote = payload.get("quote") if isinstance(payload, dict) else None
        if not isinstance(quote, dict):
            return None
        try:
            bid = float(quote.get("bid") or 0)
            last = float(quote.get("last") or 0)
            ask = float(quote.get("ask") or bid)
        except (TypeError, ValueError):
            return None
        if last <= 0 or bid <= 0:
            return None
        return bid, ask, last

    def _book(self) -> list[dict[str, Any]]:
        """Live positions, plus what each DCA or grid instance reserved and does not hold yet."""
        live = self.store.live_positions()
        rows = list(live)
        for instance in self.store.instances(live_only=True):
            try:
                loss = worst_case_loss(instance, taker=SPOT_TAKER)
                budget = instance_budget(instance)
            except (KeyError, ValueError):
                logger.warning("instance %s has an unreadable plan", instance["id"])
                continue
            used_risk = used_usd = 0.0
            for row in live:
                if row.get("instance_id") == instance["id"] and row["status"] == "open":
                    qty, avg = float(row["qty"] or 0), float(row["avg_price"] or 0)
                    used_risk += max(0.0, qty * (avg - float(row["stop_price"] or 0)))
                    used_usd += qty * avg
            rows.append(
                {
                    "symbol": instance["symbol"],
                    "strategy": instance["kind"],
                    "status": "reserved",
                    "qty": 0.0,
                    "avg_price": 0.0,
                    "stop_price": None,
                    "reserved_risk": max(0.0, loss - used_risk),
                    "reserved_usd": max(0.0, budget - used_usd),
                }
            )
        return rows

    def _instances(
        self,
        snapshot: MarketSnapshot,
        moment: datetime,
        *,
        clock_blocked: bool = False,
        state: RiskState | None = None,
        limits: RiskLimits | None = None,
    ) -> None:
        """One step of every live instance. A blocked clock never lets them buy, only sell and stop.
        The daily-loss pause refuses their buys until the next UTC day while their sales and
        stops go on; so does REDUCING; the halt step stops them all. Every buy block also takes
        a resting buy off the book. ``state`` is the cycle's risk state (None: the stored level)
        and ``limits`` the cycle's (None: read from the mandate now)."""
        instances = self.store.instances(live_only=True)
        if not instances:
            return
        buy_block = self._robot_buy_block(moment, clock_blocked=clock_blocked, state=state)
        if self.halted():
            return
        for instance in instances:
            price = market._last_price(snapshot, str(instance["symbol"]), "15m")
            try:
                outcome = self._step_instance(instance, price, moment, buy_block=buy_block, limits=limits)
            except UnprotectedPosition:
                raise
            except Exception as exc:  # noqa: BLE001 - one instance must not stop the cycle
                logger.exception("instance %s failed", instance["id"])
                self.ledger("instance_error", {"id": instance["id"], "error": type(exc).__name__})
                continue
            if outcome:
                self.ledger("instance", {"id": instance["id"], "kind": instance["kind"], "symbol": instance["symbol"], "outcome": outcome})

    def _instance_position(self, instance_id: int) -> dict[str, Any] | None:
        return next((row for row in self.store.live_positions() if row.get("instance_id") == instance_id), None)

    def _robot_buy_block(self, moment: datetime, *, clock_blocked: bool, state: RiskState | None) -> str | None:
        """Why DCA and grid instances may not buy now, or None.

        The clock rule of entries, the daily pause, and the account's risk level strategy
        entries obey: a daily loss pauses the day, REDUCING allows sales only and the halt
        step halts here too, so a bot running only robots stays inside them. ``state`` is
        the cycle's risk state (``_risk_state``). Outside the cycle (None) the level and the
        pause the last cycle stored are read and nothing is written: only the cycle, on its
        priced snapshot, measures and stores the risk level and the day's start.
        """
        level = state if state is not None else self.store.get_state("risk_state")
        return buy_block(level, self.store.get_state("paused_until"), moment, clock_blocked=clock_blocked)

    def _trade_limit(self, moment: datetime, limits: RiskLimits | None = None) -> str | None:
        """New inventory (a DCA round, a grid restart) counts as a trade, like a strategy entry.
        ``limits`` is the cycle's; outside it (None) the mandate is read now."""
        if self.store.trades_today(utc_day(moment)) >= (limits or self.read_limits()).max_trades_per_day:
            return "limite de trades por dia"
        return None

    def _step_instance(
        self,
        instance: dict[str, Any],
        price: float | None,
        moment: datetime,
        *,
        buy_block: str | None = None,
        limits: RiskLimits | None = None,
    ) -> str | None:
        """One decision of one instance: at most one order and the stop it moves. ``limits`` is
        the cycle's (None outside it: ``_trade_limit`` reads the mandate)."""
        instance_id = int(instance["id"])
        if instance["status"] == "stopping":
            return self._wind_down(instance_id, str(instance.get("reason") or "robô parado"))
        position = self._instance_position(instance_id)
        if price is None or (position is not None and position["status"] == "pending"):
            return None
        if position is not None and _inflight(position) == "buy":
            # A buy with an unknown result: the sync settles it before anything else acts.
            return None
        state = dict(instance["state"])
        day = utc_day(moment)
        if state.get("orders_day") != day:
            state["orders_day"], state["orders_today"] = day, 0
        params: DcaParams | GridParams
        if instance["kind"] == "dca":
            params = DcaParams.model_validate(instance["params"])
        else:
            params = GridParams.model_validate(instance["params"])
        outcome: str | None
        if position is not None and buy_block is not None and _inflight(position) == "rest_buy":
            # Every buy block (clock, daily pause, REDUCING, halt step) takes the resting buy off the
            # book; its cancel was paid for when it was placed.
            outcome = self._cancel_rest(position, buy_block)
        elif int(state["orders_today"]) + ORDERS_PER_LEG > params.max_orders_per_day:
            outcome = "limite diário de ordens do robô; volta amanhã"
        elif isinstance(params, DcaParams):
            outcome = self._step_dca(
                instance_id, str(instance["symbol"]), params, state, position, price, moment, buy_block, limits
            )
        else:
            outcome = self._step_grid(
                instance_id, str(instance["symbol"]), params, state, position, price, moment, buy_block, limits
            )
        self.store.update_instance(instance_id, state=state, **({"reason": outcome} if outcome else {}))
        return outcome

    def _wind_down(self, instance_id: int, reason: str) -> str:
        """Sell what a stopping instance holds; once nothing is left, it is stopped."""
        position = self._instance_position(instance_id)
        if position is not None and position["status"] == "pending":
            return "entrada pendente; o robô para depois dela"
        if position is not None:
            result = self.executor.exit_position(int(position["id"]), reason)
            current = self.store.position(int(position["id"]))
            if current is not None and current["status"] == "open":
                return f"saída pendente: {result.get('error') or result.get('status')}"
        realized = sum(float(row["realized_pnl"] or 0) for row in self.store.instance_positions(instance_id))
        self.store.update_instance(
            instance_id, status="stopped", stopped_at=datetime.now(timezone.utc).isoformat(), reason=reason, realized_pnl=realized
        )
        self.ledger("instance_stopped", {"id": instance_id, "reason": reason, "realized_pnl": realized})
        return reason

    def _end(self, instance_id: int, reason: str) -> str:
        self.store.update_instance(instance_id, status="stopping", reason=reason)
        return self._wind_down(instance_id, reason)

    def _step_dca(
        self,
        instance_id: int,
        symbol: str,
        params: DcaParams,
        state: dict[str, Any],
        position: dict[str, Any] | None,
        price: float,
        moment: datetime,
        buy_block: str | None,
        limits: RiskLimits | None = None,
    ) -> str | None:
        if position is None:
            if state.get("position_id") is not None:
                # The round's position closed: by its take-profit, its stop, or the operator.
                if state.get("exit") != "take_profit" or not params.repeat:
                    closed = self.store.position(int(state["position_id"])) or {}
                    took = state.get("exit") == "take_profit" and closed.get("status") == "closed"
                    state["position_id"] = None
                    return self._end(instance_id, "alvo do DCA atingido" if took else "rodada do DCA encerrada pelo stop ou à mão")
                waiting = self._round_cooldown(int(state["position_id"]), params.cooldown_minutes, moment)
                if waiting is not None:
                    return waiting
            block = buy_block or self._trade_limit(moment, limits)
            if block is not None:
                return f"rodada do DCA em espera: {block}"
            stop = dca_stop(params, price)
            result = self.executor.open_instance(instance_id, "dca", symbol, Decimal(f"{params.base_usd:.2f}"), Decimal(str(stop)))
            if result["status"] not in ("ok", "pending"):
                return f"base do DCA recusada: {result.get('error')}"
            state.update(
                base_price=price, filled=0, position_id=result["position_id"], exit=None, pending_so=None,
                round=int(state.get("round") or 0) + 1,
            )
            state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_LEG
            return f"rodada {state['round']} do DCA aberta a {price:.8g}"
        state["exit"] = None
        pending = state.get("pending_so")
        if pending:
            if float(position["qty"]) > float(pending["qty_before"]) * 1.000001:
                state["filled"], state["pending_so"] = int(pending["index"]), None
            elif not position.get("pending_leg"):
                state["pending_so"] = None
            else:
                return None
        resting = state.get("rest_so")
        if resting:
            if float(position["qty"]) > float(resting["qty_before"]) * 1.000001:
                # The resting safety order bought (all of it, or the part before its cancel): it is filled.
                state["filled"], state["rest_so"] = max(int(state["filled"]), int(resting["index"])), None
            elif _inflight(position) != "rest_buy":
                state["rest_so"] = None
        inflight = _inflight(position)
        if position.get("pending_leg") and inflight != "rest_buy":
            return None
        base = float(state["base_price"])
        action = dca_next(params, base, int(state["filled"]), price, float(position["avg_price"]))
        if inflight == "rest_buy" and (action is None or action[0] != "take_profit"):
            # A take-profit sells over it: the exit cancels it first and sells what it bought too.
            if action is not None:
                return self._cancel_rest(position, f"o preço chegou à ordem de segurança {action[1]}")
            if _rest_level(position) != int(state["filled"]) + 1:
                return self._cancel_rest(position, "a ordem de segurança seguinte mudou")
            return None
        if action is None:
            if params.resting_buys and buy_block is None:
                return self._rest_dca(instance_id, params, state, position, base)
            return None
        if action[0] == "take_profit":
            state["exit"] = "take_profit"
            state["orders_today"] = int(state["orders_today"]) + 1
            result = self.executor.exit_position(int(position["id"]), "alvo do DCA")
            return f"alvo do DCA: {result.get('status')}"
        index = action[1]
        if buy_block is not None:
            return f"ordem de segurança {index} em espera: {buy_block}"
        usd = dca_ladder(params, base)[index]["usd"]
        state["legs"] = int(state.get("legs") or 0) + 1
        # Saved before the send: after a crash the next attempt gets a new key, never a replay booked twice.
        self.store.update_instance(instance_id, state=state)
        qty_before = float(position["qty"])
        outcome = self.executor.scale_in(
            int(position["id"]),
            Decimal(f"{usd:.2f}"),
            f"dca-{state['round']}-{index}-{state['legs']}",
            Decimal(str(dca_stop(params, base))),
            reserve=1 + len(self.store.open_positions()),
        )
        if outcome == "comprado":
            state["filled"] = index
        elif outcome == "compra pendente" or outcome.startswith("o stop antigo"):
            state["pending_so"] = {"index": index, "qty_before": qty_before}
        else:
            return f"ordem de segurança {index}: {outcome}"
        state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_LEG
        return f"ordem de segurança {index}: {outcome}"

    def _rest_dca(
        self, instance_id: int, params: DcaParams, state: dict[str, Any], position: dict[str, Any], base: float
    ) -> str | None:
        """Rest the next safety order on the book (``resting_buys``)."""
        target = dca_rest_target(params, base, int(state["filled"]))
        if target is None:
            return None
        index = int(target["index"])
        if int(state["orders_today"]) + ORDERS_PER_RESTING_LEG > params.max_orders_per_day:
            return f"ordem de segurança {index} fora do livro: limite diário de ordens do robô"
        state["legs"] = int(state.get("legs") or 0) + 1
        state["rest_so"] = {"index": index, "qty_before": float(position["qty"])}
        # Saved before the send: after a crash the next attempt gets a new key, never a replay booked twice.
        self.store.update_instance(instance_id, state=state)
        outcome = self.executor.rest_buy(
            int(position["id"]),
            Decimal(str(target["price"])),
            Decimal(str(target["usd"] / target["price"])),
            f"dca-rest-{state['round']}-{index}-{state['legs']}",
            Decimal(str(dca_stop(params, base))),
            level=index,
            reserve=1 + len(self.store.open_positions()),
        )
        if outcome not in (REST_PLACED, REST_PENDING, "comprado"):
            state["rest_so"] = None
            return f"ordem de segurança {index} fora do livro: {outcome}"
        state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_RESTING_LEG
        if outcome == "comprado":
            state["filled"], state["rest_so"] = index, None
        return f"ordem de segurança {index} a {target['price']:.8g}: {outcome}"

    def _rest_grid(
        self,
        instance_id: int,
        params: GridParams,
        state: dict[str, Any],
        position: dict[str, Any],
        k: int,
        stop: Decimal,
    ) -> str | None:
        """Rest the buy of the level under the lowest one held on the book (``resting_buys``)."""
        target = grid_rest_target(grid_levels(params), grid_qty(params), k)
        if target is None:
            return None
        level, level_price, qty = target
        if int(state["orders_today"]) + ORDERS_PER_RESTING_LEG > params.max_orders_per_day:
            return f"grid: compra do nível {level} fora do livro: limite diário de ordens do robô"
        state["legs"] = int(state.get("legs") or 0) + 1
        self.store.update_instance(instance_id, state=state)
        outcome = self.executor.rest_buy(
            int(position["id"]),
            Decimal(str(level_price)),
            Decimal(str(qty)),
            f"grid-rest-{level}-{state['legs']}",
            stop,
            level=level,
            reserve=1 + len(self.store.open_positions()),
        )
        if outcome not in (REST_PLACED, REST_PENDING, "comprado"):
            return f"grid: compra do nível {level} fora do livro: {outcome}"
        state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_RESTING_LEG
        return f"grid: compra do nível {level} a {level_price:.8g}: {outcome}"

    def _cancel_rest(self, position: dict[str, Any], why: str) -> str:
        """Cancel a robot's resting buy (what it bought is merged and re-stopped); the next cycle
        places the buy that is next by then."""
        return f"{self.executor.cancel_rest(int(position['id']))} ({why})"

    def _step_grid(
        self,
        instance_id: int,
        symbol: str,
        params: GridParams,
        state: dict[str, Any],
        position: dict[str, Any] | None,
        price: float,
        moment: datetime,
        buy_block: str | None,
        limits: RiskLimits | None = None,
    ) -> str | None:
        stop = Decimal(str(params.stop_price))
        levels = grid_levels(params)
        qtys = grid_qty(params)
        if price <= params.stop_price:
            return self._end(instance_id, "preço no stop do grid")
        if params.take_profit_price is not None and price >= params.take_profit_price:
            return self._end(instance_id, "take-profit do grid atingido")
        if position is None and state.get("position_id") is not None:
            closed = self.store.position(int(state["position_id"])) or {}
            # The grid only sells below its range through the stop.
            if closed.get("status") == "closed" and float(closed.get("exit_price") or 0) < params.lower:
                return self._end(instance_id, "o stop do grid executou")
            # Sold out above the range floor: the restart from an empty inventory waits the cooldown.
            waiting = self._round_cooldown(int(state["position_id"]), params.cooldown_minutes, moment)
            if waiting is not None:
                return waiting
        if position is not None:
            state["position_id"] = int(position["id"])
            raw = position.get("pending_leg")
            if _inflight(position) == "rest_buy":
                k = grid_k_from_qty(qtys, float(position["qty"]))
                step = grid_next(levels, k, price)
                # A sale goes ahead: the exit cancels the resting buy first and sells what it bought too.
                if step is None or step[0] == "buy":
                    if step is not None:
                        return self._cancel_rest(position, "o preço chegou ao nível")
                    target = grid_rest_target(levels, qtys, k)
                    if target is None or _rest_level(position) != target[0]:
                        return self._cancel_rest(position, "o nível seguinte mudou")
                    return None
            elif raw or position.get("keep_qty") is not None:
                leg = json.loads(str(raw)) if raw else {}
                resting = leg.get("kind") == "sell" and position.get("keep_qty") is not None and position.get("stop_order_id")
                if not resting:
                    return None
                still = grid_next(levels, grid_k_from_qty(qtys, float(position["qty"])), price)
                if still is not None and still[0] == "sell":
                    # A grid sale that stopped midway, while the price still pays it: finish it
                    # down to the same kept quantity.
                    state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_LEG
                    result = self.executor.exit_position(
                        int(position["id"]), "grid: retomando a venda",
                        keep_qty=Decimal(str(position["keep_qty"])), leg=str(leg["leg"]), next_stop=stop,
                    )
                    return f"grid retomou a venda: {result.get('status')}"
                # The price fell back under the level: the rest of that sale is dropped, never sold at a loss.
                self.store.update_position(int(position["id"]), keep_qty=None, pending_leg=None)
                position = self._instance_position(instance_id)
        held = float(position["qty"]) if position is not None else 0.0
        k = grid_k_from_qty(qtys, held)
        step = grid_next(levels, k, price)
        if step is None:
            if (
                position is not None
                and params.resting_buys
                and buy_block is None
                and not position.get("pending_leg")
                and position.get("keep_qty") is None
            ):
                return self._rest_grid(instance_id, params, state, position, k, stop)
            return None
        side, new_k = step
        if side == "buy":
            block = buy_block or (self._trade_limit(moment, limits) if position is None else None)
            if block is not None:
                return f"grid sem compra: {block}"
            notional = Decimal(f"{sum(qtys[new_k:k]) * price:.2f}")
            if position is None:
                result = self.executor.open_instance(instance_id, "grid", symbol, notional, stop)
                if result["status"] not in ("ok", "pending"):
                    return f"grid não comprou: {result.get('error')}"
                state["position_id"] = result["position_id"]
                state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_LEG
                return f"grid comprou {k - new_k} nível(is) a {price:.8g}"
            state["legs"] = int(state.get("legs") or 0) + 1
            self.store.update_instance(instance_id, state=state)
            outcome = self.executor.scale_in(
                int(position["id"]), notional, f"grid-buy-{state['legs']}", stop,
                reserve=1 + len(self.store.open_positions()),
            )
            if outcome == "comprado" or outcome == "compra pendente" or outcome.startswith("o stop antigo"):
                state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_LEG
            return f"grid comprou {k - new_k} nível(is) a {price:.8g}: {outcome}"
        if position is None:
            return None
        keep = held - sum(qtys[k:new_k])
        state["orders_today"] = int(state["orders_today"]) + ORDERS_PER_LEG
        if new_k >= len(qtys) or keep * price < (self.min_notional(symbol) or 0.0):
            result = self.executor.exit_position(int(position["id"]), "grid vendeu o estoque")
        else:
            state["legs"] = int(state.get("legs") or 0) + 1
            self.store.update_instance(instance_id, state=state)
            result = self.executor.exit_position(
                int(position["id"]), f"grid vendeu {new_k - k} nível(is)",
                keep_qty=Decimal(str(keep)), leg=f"grid-sell-{state['legs']}", next_stop=stop,
            )
        return f"grid vendeu {new_k - k} nível(is) a {price:.8g}: {result.get('status')}"

    def _round_cooldown(self, position_id: int, minutes: int, moment: datetime) -> str | None:
        """Why a new instance round waits: ``minutes`` after the previous round's position closed."""
        if minutes <= 0:
            return None
        closed_at = (self.store.position(position_id) or {}).get("closed_at")
        if not closed_at:
            return None
        ends = cooldown_ends(str(closed_at), minutes)
        if moment < ends:
            return f"pausa entre rodadas até {ends:%H:%M} UTC"
        return None

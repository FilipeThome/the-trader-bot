"""One cycle: pick pairs, sync the clock, collect, settle positions, exit, score, allocate, enter.

One process lock covers every action (the cycle's decisions and orders, manual
orders, the halt, a config or mode change), so the tick thread and the API never
act on the same position at once. The cycle's network reads (pair ranking, clock,
candles, balance) run before it takes the lock: a click in the panel waits for
the decisions of one cycle, never for Binance.

A halt is sticky. The cycle does not clear it. Three failed cycles in a row
trip it, and so does a position that can be neither protected nor closed.
With "flatten on halt", positions are sold before the HALT flag is written:
the flag then blocks every new order, as the ported gate requires.

Entries share the free USDT balance: ready rows are taken best score first,
one position per pair, within the position slots, the open-risk limit and the
cash (``bot.risk.allocate``). Orders are sized on the smaller of the configured
capital and what the account holds; equity, daily loss and drawdown stay on
the configured capital, so a withdrawal does not read as a loss.

Each cycle reads the account's risk level once, after the exits (``_risk_state``):
a drawdown past ``dd_reduce_pct`` halves the risk and allows one entry, past
``max_drawdown_pct`` only exits run (REDUCING, which the executor enforces from
``bot_state.risk_state``), and ``halt_drawdown_pct`` halts. The daily loss is
measured on equity, open positions included, and pauses the day's buys (the
cycle, its stops and its exits keep running until the next UTC day). A row trades
only when the strategy passed validation on the row's timeframe and pair set,
its Monte Carlo drawdown fits the limit, and the pair is neither demoted nor over
its slippage budget; the market filter, the macro calendar and the signal age
can hold entries back too.
"""

from __future__ import annotations

import json
import logging
import threading
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from bot.collector import Collector, MarketSnapshot
from bot.config import BotConfig, RiskLimits
from bot.decay import pair_demoted, pair_slippage_blocked, review_strategy
from bot.evaluator import SPOT_TAKER, Opportunity, classify, estimate_win_rate
from bot.exchange import service
from bot.exchange.weight import weight_pressure
from bot.executor import REST_PENDING, REST_PLACED, Executor, UnprotectedPosition
from bot.indicators import atr_pct_daily, btc_trend_ok, ema
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
from bot.live.halt import halt_flag_set, trip_halt
from bot.live.mandate.store import load_mandate
from bot.markets import merge_symbols, rank_promising, venue_symbols
from bot.paths import base_runtime_root
from bot.reports import append_ledger
from bot.risk import (
    CLUSTER_ANCHOR,
    LOSS_GUARD_WINDOW,
    RiskManager,
    RiskState,
    allocate,
    breakeven_win_rate,
    open_risk,
    planned_notional,
    protect,
    utc_day,
    vol_target_cap,
    vol_throttle,
)
from bot.store import Store
from bot.strategies import (
    DAY_STRATEGIES,
    STRATEGIES,
    FundingCarry,
    IntradayMomentum,
    Momentum,
    Strategy,
    TrendBreakout,
    VwapPullback,
    exit_policy_for,
    timeframe_passed,
    timeframe_result,
    validated_for,
)
from bot.strategies.day import DAY_WIN_RATE_MARGIN, in_quiet_hours
from bot.strategies.fixed import FIXED_WIN_RATE_MARGIN, is_fixed, parse_fixed
from bot.strategies.trailing import TRAILING_POLICIES
from bot.strategies.base import Setup

logger = logging.getLogger(__name__)

Publish = Callable[[dict[str, Any]], None]
#: Quality filters the Spot Testnet test mode may waive for a manual buy.
TESTABLE_BLOCKERS = frozenset({"score", "cost", "reward_risk", "regime"})
_PRICE_INTERVALS = ("1m", "15m", "1h", "4h")
#: Failed cycles in a row that trip the halt.
MAX_FAILURES = 3
#: Exit policies of DCA and grid inventory: their instance decides every exit.
INSTANCE_POLICIES = frozenset({"dca", "grid"})
#: How long an armed entry may wait for its trigger, in hours.
MIN_ARM_HOURS = 1
MAX_ARM_HOURS = 168
_TREND = TrendBreakout()
_MOMENTUM = Momentum()
#: Days of decisions, signals and equity points kept in the store.
RETENTION_DAYS = 30
#: Seconds between two automatic choices of the analysed pairs.
AUTO_SELECT_SECONDS = 3600
#: Read every cycle, analysed or not: the market filter and the correlation cluster need it.
MARKET_SYMBOL = CLUSTER_ANCHOR
#: Exit frames of positions opened before the entry timeframe was recorded.
LEGACY_EXIT_TIMEFRAMES = {"trend": "1h", "mean_reversion": "15m", "momentum": "4h"}
#: ``stop()`` refused: a resting buy is not resolved, so the cycle goes on until it is.
STOP_REFUSED_RESTING = "compra limitada ainda no livro: o ciclo continua para resolver"
#: Most seconds one cycle waits for the account-fee reads; the rest finish in the background.
FEE_WARMUP_BUDGET_SECONDS = 2.0
#: Refusal of a strategy with no current validation, or one demoted to signal only.
SIGNAL_ONLY = "estratégia em somente sinal: sem validação no histórico ou rebaixada"


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


@dataclass(frozen=True)
class _PairInputs:
    """What sizing reads of one pair in one cycle (``RiskManager.sizing``)."""

    venue_cap: Any
    atr_daily: float | None
    throttle: float


def profile_for(config: BotConfig) -> str:
    if config.mode == "production":
        return "binance-live-trade-bot"
    return "binance-paper-trade"


class Runtime:
    def __init__(
        self,
        config: BotConfig,
        store: Store,
        collector: Collector | None = None,
        *,
        publish: Publish | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.collector = collector or Collector()
        self.publish = publish or (lambda _event: None)
        self.lock = lock or threading.RLock()
        self.failures = 0
        #: Set under the lock when the API binds a new runtime (a mode switch). A tick of this
        #: one that was still reading Binance must not act on the other mode's store and pin.
        self.retired = False
        #: One tick at a time: the loop and ``POST /bot/tick`` must not collect and act out of order.
        self._ticking = threading.Lock()
        #: Last (status, reason) recorded per (symbol, strategy): a setup re-read every cycle from the
        #: same closed bar is recorded once, when its decision changes, not 240 times per 4h bar.
        self._decided: dict[tuple[str, str], tuple[str, str]] = {}
        #: Account-fee reads run on one background thread (``_account_takers``): a slow commission
        #: endpoint never holds a cycle longer than ``FEE_WARMUP_BUDGET_SECONDS``.
        self._fee_lock = threading.Lock()
        self._fee_worker: threading.Thread | None = None
        self._fee_takers: dict[str, float] = {}
        self.executor = Executor(
            store,
            profile_for(config),
            exit_policy=config.exit_policy,
            ledger=self._ledger,
            exit_hysteresis=config.exit_hysteresis,
        )

    @property
    def running(self) -> bool:
        return bool(self.store.get_state("running", False)) and not self.halted

    @property
    def halted(self) -> bool:
        try:
            return bool(halt_flag_set("binance"))
        except Exception:  # noqa: BLE001 - an unreadable flag is a halt
            return True

    def start(self) -> dict[str, Any]:
        with self.lock:
            if self.halted:
                return {"status": "error", "error": "halt ativo — retomada manual exigida"}
            # The daily-loss pause refuses buys only (``paused_until``): the cycle may run under it.
            self.store.set_state("running", True)
            # "Three failed cycles in a row" counts from this start, not from before a halt.
            self.failures = 0
            self._ledger("start", {"mode": self.config.mode})
            return {"status": "ok", "running": True}

    def stop(self) -> dict[str, Any]:
        """Stop the cycle. Nothing reconciles a resting buy while the cycle is stopped, so each comes
        off the book first and what one bought joins its position under a new stop. While one is not
        resolved (its cancel unconfirmed, or the stop over what it bought not re-placed), the cycle
        keeps running to resolve it and the stop is refused; a position left without a stop halts."""
        with self.lock:
            resting = self._cancel_resting_buys("stop")
            unprotected = [item for item in resting if item.get("unprotected")]
            if unprotected:
                self.store.set_state("running", False)
                self._ledger("stop", {"resting_failed": len(unprotected)})
                reason = f"posição sem proteção: {unprotected[0].get('error')}"
                self.halt(reason, flatten=False)
                return {"status": "error", "error": f"{reason}; halt acionado", "running": False, "resting": resting}
            unresolved = [item for item in resting if item.get("status") != "ok"]
            if unresolved:
                self.store.set_state("running", True)
                self._ledger("stop_refused", {"cause": "resting_buy", "results": unresolved})
                return {"status": "error", "error": STOP_REFUSED_RESTING, "running": True, "resting": unresolved}
            self.store.set_state("running", False)
            self._ledger("stop", {})
            return {"status": "ok", "running": False}

    def halt(self, reason: str, *, flatten: bool | None = None, by: str = "file") -> dict[str, Any]:
        """Stop the cycle, flatten when asked, then write the sticky HALT flag.

        Without ``flatten``, the committed mandate's ``flatten_on_halt`` decides when a
        mandate is on file, else the config's (D23).
        """
        with self.lock:
            self.store.set_state("running", False)
            # Resting buys come off the book before the flag, while the gate still lets the stop
            # over what they bought be re-placed. A failure is recorded; the halt goes on.
            resting = self._cancel_resting_buys("halt")
            flattened: list[dict[str, Any]] = []
            if flatten if flatten is not None else self._flatten_on_halt():
                try:
                    flattened = self.executor.flatten()
                except Exception as exc:  # noqa: BLE001 - the flag below must be written regardless
                    logger.exception("flatten before halt failed")
                    flattened = [{"status": "error", "error": type(exc).__name__}]
            trip_halt(by, reason, broker="binance")
            self.store.set_state("halt_reason", reason)
            # DCA and grid instances do not come back on resume: the operator starts them again.
            # Positions they leave keep their resting stop and can be closed from the panel.
            for instance in self.store.instances(live_only=True):
                self.store.update_instance(
                    int(instance["id"]), status="stopped", stopped_at=datetime.now(timezone.utc).isoformat(), reason=f"halt: {reason}"
                )
            failed = [item for item in flattened if item.get("status") != "ok"]
            self._ledger(
                "halt",
                {
                    "reason": reason,
                    "flattened": len(flattened),
                    "flatten_failed": len(failed),
                    "resting_buys": len(resting),
                    "resting_failed": sum(1 for item in resting if item.get("status") != "ok"),
                },
            )
            self.publish({"type": "halt", "reason": reason})
            return {"status": "ok", "halted": True, "reason": reason, "flatten": flattened}

    def _cancel_resting_buys(self, cause: str) -> list[dict[str, Any]]:
        """Cancel every resting buy of the robots (``Executor.cancel_resting_buys``); never raises."""
        try:
            results = self.executor.cancel_resting_buys()
        except Exception as exc:  # noqa: BLE001 - the stop or the halt must go on
            logger.exception("cancel of the resting buys failed")
            results = [{"status": "error", "error": f"{type(exc).__name__}: {exc}"}]
        if results:
            failed = [item for item in results if item.get("status") != "ok"]
            self._ledger("resting_buy", {"event": "cancel_all", "cause": cause, "results": results, "failed": len(failed)})
        return results

    def _flatten_on_halt(self) -> bool:
        mandate = load_mandate("binance")
        return bool(mandate.flatten_on_halt) if mandate is not None else self.config.flatten_on_halt

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        if not self._ticking.acquire(blocking=False):
            return {"status": "skipped", "reason": "outro ciclo em andamento"}
        try:
            return self._tick(now or datetime.now(timezone.utc))
        finally:
            self._ticking.release()

    def _tick(self, moment: datetime) -> dict[str, Any]:
        market: _Market | BaseException
        try:
            market = self._collect(moment)
        except Exception as exc:  # noqa: BLE001 - raised again under the lock, where failures are counted
            market = exc
        with self.lock:
            if self.retired:
                return {"status": "skipped", "reason": "runtime substituído durante a coleta"}
            try:
                if isinstance(market, BaseException):
                    raise market
                payload = self._cycle(moment, market)
            except UnprotectedPosition as exc:
                logger.error("unprotected position: %s", exc)
                self.halt(f"posição sem proteção: {exc}", flatten=False)
                return {"status": "error", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - counted, logged and halted on repeat
                logger.exception("cycle failed")
                self.failures += 1
                self._ledger("tick_error", {"error": type(exc).__name__, "failures": self.failures})
                if self.failures >= MAX_FAILURES:
                    self.halt(f"{MAX_FAILURES} falhas seguidas no ciclo: {type(exc).__name__}")
                return {"status": "error", "error": type(exc).__name__}
            self.failures = 0
            return {"status": "ok", **payload}

    def execute_manual(self, opportunity: Opportunity) -> dict[str, Any]:
        """Open one position from the panel.

        In manual mode the cycle opens nothing by itself, so a position opened
        here starts the cycle (``start``: the halt still refuses it; the daily
        pause refuses the buy itself) to keep its stop moving and its exits watched.
        """
        with self.lock:
            return self._enter_manual(opportunity)

    def _enter_manual(self, opportunity: Opportunity) -> dict[str, Any]:
        """``execute_manual`` under the lock; an armed entry fires through it too."""
        if self.halted:
            return {"results": [{"decision": "bloqueado", "reason": "halt ativo — retomada manual exigida"}], "cycle_started": False}
        if self.retired:
            # The mode switched while this request waited: the runtime root is the other mode's now.
            return {"results": [{"decision": "bloqueado", "reason": "ambiente trocado; tente de novo"}], "cycle_started": False}
        if opportunity.symbol not in self.config.symbols:
            # A row left from before the pair list changed: its candles are no longer read.
            reason = "par fora da lista analisada; aguarde o próximo ciclo"
            return {"results": [{"decision": "bloqueado", "reason": reason}], "cycle_started": False}
        if opportunity.status == "pronta" and opportunity.direction > 0:
            now = datetime.now(timezone.utc)
            self._protect([opportunity], now)
            returns = _returns(self.collector.view(), now)
            if opportunity.status != "pronta" or not self._allocate([opportunity], self._free_cash(), returns=returns):
                self.store.add_decision(
                    {
                        "symbol": opportunity.symbol,
                        "strategy": opportunity.strategy,
                        "decision": "bloqueado",
                        "reason": opportunity.reason,
                        "manual": True,
                    }
                )
                return {"results": [{"decision": "bloqueado", "reason": opportunity.reason}], "cycle_started": False}
        try:
            results = self.executor.act(
                [opportunity], clock_blocked=self.collector.snapshot.clock_blocked, manual=True
            )
        except UnprotectedPosition as exc:
            self.halt(f"posição sem proteção: {exc}", flatten=False)
            return {"results": [{"decision": "bloqueado", "reason": str(exc)}], "cycle_started": False}
        started = False
        entered = any(item.get("decision") in ("operar", "pendente") for item in results)
        if entered and self.config.entry_mode == "manual" and not self.running:
            started = self.start().get("status") == "ok"
        return {"results": results, "cycle_started": started}

    def arm_entry(self, symbol: str, strategy: str, trigger_price: float, hours: float) -> dict[str, Any]:
        """Arm an entry that buys when the last price reaches ``trigger_price`` (Binance's start condition).

        Below the last price it waits for a pullback, above it for a breakout. Nothing rests at the
        broker: each cycle compares the last 15m close with the trigger and, once reached, enters
        through the same checks as a manual entry, only while the row is still ``pronta``.
        """
        with self.lock:
            if self.halted:
                return {"status": "error", "error": "halt ativo — retomada manual exigida"}
            if self.retired:
                return {"status": "error", "error": "ambiente trocado; tente de novo"}
            if symbol not in self.config.symbols:
                return {"status": "error", "error": "par fora da lista analisada"}
            if not self.config.strategies.get(strategy):
                return {"status": "error", "error": "estratégia desligada"}
            if not trigger_price > 0 or not MIN_ARM_HOURS <= hours <= MAX_ARM_HOURS:
                return {"status": "error", "error": f"gatilho positivo e prazo entre {MIN_ARM_HOURS} e {MAX_ARM_HOURS} horas"}
            if any(row["symbol"] == symbol and row["strategy"] == strategy for row in self.store.live_positions()):
                return {"status": "error", "error": "já existe posição viva neste par e estratégia"}
            price = _last_price(self.collector.view(), symbol, "15m")
            if price is None:
                return {"status": "error", "error": "preço do par indisponível"}
            if trigger_price == price:
                return {"status": "error", "error": "gatilho igual ao preço atual; use a entrada manual"}
            condition = "below" if trigger_price < price else "above"
            expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            armed_id = self.store.arm_entry(symbol, strategy, trigger_price, condition, expires)
            if armed_id is None:
                return {"status": "error", "error": "já existe entrada armada neste par e estratégia"}
            self._ledger("arm", {"symbol": symbol, "strategy": strategy, "trigger": trigger_price, "condition": condition})
            started = False
            if self.config.entry_mode == "manual" and not self.running:
                started = self.start().get("status") == "ok"
            return {"status": "ok", "id": armed_id, "condition": condition, "expires_at": expires, "cycle_started": started}

    def disarm_entry(self, armed_id: int) -> dict[str, Any]:
        with self.lock:
            if not self.store.settle_armed(armed_id, "cancelled", "cancelada pelo operador"):
                return {"status": "error", "error": "entrada armada não encontrada"}
            return {"status": "ok"}

    # -- DCA and grid instances ---------------------------------------------

    def preview_instance(self, kind: str, symbol: str, params: DcaParams | GridParams) -> dict[str, Any]:
        """The plan of an instance at the current price and every reason it cannot run. Creates nothing.

        Read-only, so it runs without the runtime lock: its quote and rule reads never stall the
        cycle or the panel. ``create_instance`` recomputes the plan under the lock before it acts.
        """
        return self._instance_plan(kind, symbol, params)

    def create_instance(self, kind: str, symbol: str, params: DcaParams | GridParams) -> dict[str, Any]:
        """Start a DCA or grid instance once its whole plan passes; its first buy goes at once."""
        with self.lock:
            if self.halted:
                return {"status": "error", "error": "halt ativo — retomada manual exigida"}
            if self.retired:
                return {"status": "error", "error": "ambiente trocado; tente de novo"}
            now = datetime.now(timezone.utc)
            block = self._robot_buy_block(self.collector.view(), now, clock_blocked=self.collector.snapshot.clock_blocked)
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
            self._ledger("instance_created", {"id": instance_id, "kind": kind, "symbol": symbol, "params": params.model_dump()})
            instance = self.store.instance(instance_id)
            assert instance is not None
            try:
                step = self._step_instance(instance, price, now)
            except UnprotectedPosition as exc:
                self.halt(f"posição sem proteção: {exc}", flatten=False)
                return {"status": "error", "error": str(exc), "id": instance_id}
            started = False
            if self.config.entry_mode == "manual" and not self.running:
                started = self.start().get("status") == "ok"
            return {"status": "ok", "id": instance_id, "step": step, "cycle_started": started}

    def stop_instance(self, instance_id: int) -> dict[str, Any]:
        """Stop an instance and sell what it holds. While the sale cannot finish it stays ``stopping``."""
        with self.lock:
            instance = self.store.instance(instance_id)
            if instance is None or instance["status"] not in ("active", "stopping"):
                return {"status": "error", "error": "robô ativo não encontrado"}
            if self.halted:
                return {"status": "error", "error": "halt ativo — o stop segue na corretora"}
            return self._stop_instance(instance_id, "parado pelo operador")

    def _stop_instance(self, instance_id: int, reason: str) -> dict[str, Any]:
        """Under the lock: mark the instance ``stopping`` and sell what it holds."""
        self.store.update_instance(instance_id, status="stopping", reason=reason)
        try:
            outcome = self._wind_down(instance_id, reason)
        except UnprotectedPosition as exc:
            self.halt(f"posição sem proteção: {exc}", flatten=False)
            return {"status": "error", "error": str(exc)}
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
        limits = self._limits()
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
        self, snapshot: MarketSnapshot, moment: datetime, *, clock_blocked: bool = False, state: RiskState | None = None
    ) -> None:
        """One step of every live instance. A blocked clock never lets them buy, only sell and stop.
        The daily-loss pause refuses their buys until the next UTC day while their sales and
        stops go on; so does REDUCING; the halt step stops them all. Every buy block also takes
        a resting buy off the book. ``state`` is the cycle's risk state."""
        instances = self.store.instances(live_only=True)
        if not instances:
            return
        buy_block = self._robot_buy_block(snapshot, moment, clock_blocked=clock_blocked, state=state)
        if self.halted:
            return
        for instance in instances:
            price = _last_price(snapshot, str(instance["symbol"]), "15m")
            try:
                outcome = self._step_instance(instance, price, moment, buy_block=buy_block)
            except UnprotectedPosition:
                raise
            except Exception as exc:  # noqa: BLE001 - one instance must not stop the cycle
                logger.exception("instance %s failed", instance["id"])
                self._ledger("instance_error", {"id": instance["id"], "error": type(exc).__name__})
                continue
            if outcome:
                self._ledger("instance", {"id": instance["id"], "kind": instance["kind"], "symbol": instance["symbol"], "outcome": outcome})

    def _instance_position(self, instance_id: int) -> dict[str, Any] | None:
        return next((row for row in self.store.live_positions() if row.get("instance_id") == instance_id), None)

    def _robot_buy_block(
        self, snapshot: MarketSnapshot, moment: datetime, *, clock_blocked: bool, state: RiskState | None = None
    ) -> str | None:
        """Why DCA and grid instances may not buy now, or None.

        The clock rule of entries, the daily pause, the trades-per-day limit for new
        inventory, and the account's risk level strategy entries obey (``_risk_state``):
        a daily loss pauses the day, REDUCING allows sales only and the halt step halts
        here too, so a bot running only robots stays inside them. Without ``state`` the
        level is read now.
        """
        if clock_blocked:
            return "relógio fora de 1s — só cancelamentos e saídas"
        paused = self._paused(moment) is not None
        if state is None and not paused:
            state = self._risk_state(snapshot, moment)
        if state is not None and (state.level in ("halt", "reducing") or state.pause_day):
            return state.reason
        if paused:
            return "pausado até o próximo dia UTC"
        return None

    def _trade_limit(self, moment: datetime) -> str | None:
        """New inventory (a DCA round, a grid restart) counts as a trade, like a strategy entry."""
        if self.store.trades_today(utc_day(moment)) >= self._limits().max_trades_per_day:
            return "limite de trades por dia"
        return None

    def _step_instance(
        self, instance: dict[str, Any], price: float | None, moment: datetime, *, buy_block: str | None = None
    ) -> str | None:
        """One decision of one instance: at most one order and the stop it moves."""
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
            outcome = self._step_dca(instance_id, str(instance["symbol"]), params, state, position, price, moment, buy_block)
        else:
            outcome = self._step_grid(instance_id, str(instance["symbol"]), params, state, position, price, moment, buy_block)
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
        self._ledger("instance_stopped", {"id": instance_id, "reason": reason, "realized_pnl": realized})
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
            block = buy_block or self._trade_limit(moment)
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
            block = buy_block or (self._trade_limit(moment) if position is None else None)
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
        if new_k >= len(qtys) or keep * price < (self._min_notional(symbol) or 0.0):
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

    def close_position(self, position_id: int) -> dict[str, Any]:
        with self.lock:
            if self.halted:
                return {"status": "blocked", "error": "halt ativo — o stop segue na corretora"}
            if self.retired:
                return {"status": "blocked", "error": "ambiente trocado; tente de novo"}
            position = self.store.position(position_id) or {}
            owner = self.store.instance(int(position["instance_id"])) if position.get("instance_id") is not None else None
            if owner is not None and owner["status"] in ("active", "stopping"):
                # Closing a robot's inventory by hand stops the robot too: otherwise a grid
                # would read an empty inventory as levels to buy back on the next cycle.
                stopped = self._stop_instance(int(owner["id"]), "posição do robô fechada à mão")
                current = self.store.position(position_id) or {}
                if current.get("status") in ("open", "pending"):
                    return {"status": "error", "error": str(stopped.get("outcome") or stopped.get("error"))}
                return {"status": "ok", "instance_stopped": int(owner["id"])}
            try:
                return self.executor.exit_position(position_id, "fechamento manual")
            except UnprotectedPosition as exc:
                self.halt(f"posição sem proteção: {exc}", flatten=False)
                return {"status": "error", "error": str(exc)}

    # -- cycle ---------------------------------------------------------------

    def _collect(self, moment: datetime) -> "_Market":
        """Network reads of one cycle, outside the lock: pairs, clock, candles, account fees and free USDT."""
        chosen = self._choose_pairs(moment)
        if chosen is not None:
            with self.lock:
                self._apply_pairs(chosen)
        # Pairs with a position are always read, even when the analysed list dropped them
        # (a hand edit, the automatic choice): their stops and exits need fresh candles.
        # No cap here: every analysed pair is scored, so every one must be read too.
        # A DCA or grid instance needs its pair's price even while it holds nothing.
        # BTC/USDT is always read: the market filter and the correlation cluster need it.
        instanced = [str(row["symbol"]) for row in self.store.instances(live_only=True)]
        symbols = list(
            dict.fromkeys([*sorted(_held(self._positions())), *instanced, *self.config.symbols, MARKET_SYMBOL])
        )
        self.collector.sync_clock()
        snapshot = self.collector.refresh(symbols)
        read = frozenset(symbols)
        takers = self._account_takers(symbols)
        positions = self._positions()
        _refuse_blind(snapshot, _held(positions), read)
        return _Market(snapshot=snapshot, cash=self._free_cash(), positions=positions, read=read, takers=takers)

    def _account_takers(self, symbols: list[str]) -> dict[str, float]:
        """The account's taker fee of each pair (``service.trading_fees``), outside the lock.

        A pair is read from Binance at most once a day (once an hour after a failure);
        the cache it fills is what ``service.market_rules`` books fees with. Under API
        weight pressure nothing is read; a failed read leaves the pair out, so scoring
        keeps ``SPOT_TAKER``. The reads run on one background thread and the cycle waits
        for them at most ``FEE_WARMUP_BUDGET_SECONDS``: a pair not read by then counts with
        its last known fee, or none, and its read goes on for the next cycle. While a read
        hangs, no second thread starts.
        """
        if self.retired or weight_pressure():
            return {}
        profile = profile_for(self.config)
        with self._fee_lock:
            worker = self._fee_worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._read_fees, args=(list(symbols), profile), name="bot-fee-warmup", daemon=True
                )
                self._fee_worker = worker
                worker.start()
        worker.join(FEE_WARMUP_BUDGET_SECONDS)
        with self._fee_lock:
            return {symbol: self._fee_takers[symbol] for symbol in symbols if symbol in self._fee_takers}

    def _read_fees(self, symbols: list[str], profile: str) -> None:
        """The fee warm-up thread: one ``service.trading_fees`` read per pair (most are cached)."""
        for symbol in symbols:
            if self.retired or weight_pressure():
                return
            try:
                fees = service.trading_fees(symbol, profile)
            except Exception:  # noqa: BLE001 - a fee read never fails the cycle
                logger.exception("account fee read failed for %s", symbol)
                continue
            if fees is not None and _finite(fees[1]):
                with self._fee_lock:
                    self._fee_takers[symbol] = float(fees[1])

    def _positions(self) -> frozenset[tuple[int, str, str, float]]:
        return frozenset(
            (int(row["id"]), str(row["symbol"]), str(row["status"]), float(row["qty"] or 0)) for row in self.store.live_positions()
        )

    def _cycle(self, moment: datetime, market: "_Market") -> dict[str, Any]:
        snapshot = market.snapshot
        cash = market.cash
        positions = self._positions()
        if positions != market.positions:
            # A manual entry, fill or exit ran during the collect: its pair may not have been
            # read, and the free USDT read before it is wrong.
            _refuse_blind(snapshot, _held(positions), market.read)
            cash = self._free_cash()
        trading = self.running
        if snapshot.failed:
            self._ledger("collect_partial", {"failed": sorted(snapshot.failed)})
        if snapshot.clock_blocked:
            self.store.add_decision({"decision": "nao_operar", "reason": "relógio divergente", "symbol": "", "strategy": ""})
        if trading:
            self.executor.sync_positions()
            self._mark(snapshot)
            if not snapshot.clock_blocked:
                self._trail(snapshot)
            self._exits(snapshot, moment)
        # Once per cycle, after the exits: robots and entries obey the same level.
        state = self._risk_state(snapshot, moment)
        if trading:
            self._instances(snapshot, moment, clock_blocked=snapshot.clock_blocked, state=state)
        self._decay(moment)
        capital = self._sizing_capital(cash, snapshot)
        returns = _returns(snapshot, moment)
        opportunities = self._score(snapshot, moment, capital, state=state, takers=market.takers, returns=returns)
        self._protect([item for item in opportunities if item.status == "pronta"], moment)
        ready = [item for item in opportunities if item.status == "pronta"]
        entries: list[Opportunity] = []
        # Spot Testnet with the test mode on: automatic trading also takes test rows,
        # best score first, so the whole flow can be exercised without validated strategies.
        test_auto = self.config.test_mode_active
        if self.config.entry_mode == "auto":
            eligible = [item for item in ready if self._auto_eligible(item, test_auto)]
            entries = self._allocate(eligible, cash, returns=returns, max_entries=state.max_entries)
        else:
            # The operator picks one row at a time, so each row is checked on its own.
            for item in ready:
                self._allocate([item], cash, returns=returns, max_entries=state.max_entries)
        self.store.replace_opportunities([item.to_dict() for item in opportunities])
        acted: list[dict[str, Any]] = []
        if trading and self.running and not snapshot.clock_blocked and self.config.entry_mode == "auto":
            acted = self.executor.act(entries, clock_blocked=False, allow_test=test_auto)
        if trading and self.running and not snapshot.clock_blocked:
            self._fire_armed(opportunities, snapshot, moment)
        payload = {
            "opportunities": len(opportunities),
            "ready": sum(1 for item in opportunities if item.status == "pronta"),
            "acted": len(acted),
            "halted": self.halted,
            "running": self.running,
            "weight_pressure": weight_pressure(),
            "cash_usdt": cash,
        }
        self._ledger("tick", payload)
        self.publish({"type": "tick", **payload, "rows": [item.to_dict() for item in opportunities]})
        return payload

    def _auto_eligible(self, item: Opportunity, test_auto: bool) -> bool:
        """Whether automatic trading may take a ready row: its hit rate (or the test mode),
        and a signal no older than ``max_signal_age_seconds`` (D21). A late row stays ready
        for a manual or armed entry and says why the automatic one skipped it."""
        if not ((item.test_only and test_auto) or self._clears_floor(item)):
            return False
        limit = self.config.limits.max_signal_age_seconds
        age = item.signal_age_seconds
        if limit > 0 and age is not None and age > limit:
            item.reason = f"{item.reason}; sinal com {age:.0f} s de atraso: sem entrada automática"
            return False
        return True

    def _clears_floor(self, item: Opportunity) -> bool:
        """Whether a real (non-test) row's estimated hit rate allows an automatic entry.

        Swing rows need ``min_win_rate``. Day rows need the break-even of their
        first target plus ``DAY_WIN_RATE_MARGIN``: a day trade wins less often by
        design (half at 1.5R, the rest at break-even), so the swing floor would
        never let one in. Rows under the operator's fixed take-profit and stop-loss
        need the break-even of that payoff plus ``FIXED_WIN_RATE_MARGIN``: a 3%
        target over a 1.5% stop pays at a hit rate well under the swing floor.
        """
        if item.test_only or item.win_rate is None:
            return False
        policy = exit_policy_for(item.strategy, self.config.exit_policy)
        if policy == "day":
            margin = DAY_WIN_RATE_MARGIN
        elif is_fixed(policy):
            margin = FIXED_WIN_RATE_MARGIN
        else:
            return item.win_rate >= self.config.min_win_rate
        breakeven = breakeven_win_rate(item)
        return breakeven is not None and item.win_rate >= breakeven + margin

    def _choose_pairs(self, moment: datetime) -> list[str] | None:
        """Rank the most promising pairs, at most once an hour. Network only: runs outside the lock.

        Returns the pairs to analyse, or None to keep the current ones. A failure
        keeps the current pairs and is logged; it never counts as a failed cycle.
        """
        if self.retired or not self.config.auto_select or weight_pressure():
            return None
        try:
            last = self.store.get_state("auto_select_at")
            if last and (moment - datetime.fromisoformat(str(last))).total_seconds() < AUTO_SELECT_SECONDS:
                return None
            self.store.set_state("auto_select_at", moment.isoformat())
            listed = venue_symbols(self.collector, self.config.mode)
            chosen = rank_promising(self.collector, self.config.auto_select_count, listed=listed)
            if not chosen:
                # No pair is rising: keep the current pairs the venue can still trade.
                chosen = [symbol for symbol in self.config.symbols if listed is None or symbol in listed]
                if not chosen or chosen == self.config.symbols:
                    self._ledger("auto_select", {"symbols": self.config.symbols, "kept": True})
                    return None
            return chosen
        except Exception as exc:  # noqa: BLE001 - the cycle goes on with the pairs it has
            logger.exception("automatic pair selection failed")
            self._ledger("auto_select_error", {"error": type(exc).__name__})
            return None

    def _apply_pairs(self, chosen: list[str]) -> None:
        """Save the chosen pairs, with the pairs of live positions first. Caller holds the lock."""
        if self.retired or not self.config.auto_select:
            # The panel rebound the runtime (pairs, auto-select or mode changed) while the
            # ranking ran: its choice wins, and this runtime writes nothing.
            return
        try:
            live = [str(row["symbol"]) for row in self.store.live_positions()]
            # Start from the file, so a hand edit of bot.json (the gates live only there) is kept.
            on_disk = BotConfig.load() if self.config.config_path().is_file() else self.config
            data = on_disk.model_dump()
            if data["mode"] != self.config.mode:
                raise RuntimeError("bot.json changed mode outside the panel")
            data["symbols"] = merge_symbols(live, chosen)
            config = BotConfig.model_validate(data)
            config.save()
            # Only the pairs change in memory: a hand edit of entry mode, limits or exit rule
            # takes effect through the panel or a restart, which run their guards.
            self.config = self.config.model_copy(update={"symbols": config.symbols})
            self._ledger("auto_select", {"symbols": config.symbols})
        except Exception as exc:  # noqa: BLE001 - the cycle goes on with the pairs it has
            logger.exception("automatic pair selection failed")
            self._ledger("auto_select_error", {"error": type(exc).__name__})

    def _free_cash(self) -> float | None:
        """Free USDT of the trading profile; ``None`` when the account cannot be read."""
        try:
            value = service.free_balance("USDT", profile_for(self.config))
        except Exception:  # noqa: BLE001 - an unreadable balance blocks entries, never exits
            logger.exception("free USDT read failed")
            return None
        return None if value is None else float(value)

    def _sizing_capital(self, cash: float | None, snapshot: MarketSnapshot) -> float:
        """The configured capital, capped by the free USDT plus the bot's open positions."""
        capital = self.config.capital_for_sizing()
        if cash is None:
            return capital
        held = 0.0
        for row in self.store.open_positions():
            price = _last_price(snapshot, row["symbol"], "15m") or float(row["avg_price"])
            held += float(row["qty"]) * price
        return min(capital, cash + held)

    def _protect(self, rows: list[Opportunity], moment: datetime) -> None:
        """Cooldown and loss guard (``bot.risk.protect``) on rows about to be allocated."""
        if not rows:
            return
        limits = self.config.limits
        window = max(LOSS_GUARD_WINDOW, timedelta(minutes=limits.pair_cooldown_minutes))
        protect(rows, self.store.recent_losses((moment - window).isoformat()), limits, moment)

    def _limits(self) -> RiskLimits:
        mandate = load_mandate("binance")
        return self.config.tightened(mandate.hard_caps if mandate else None)

    def _allocate(
        self,
        rows: list[Opportunity],
        cash: float | None,
        *,
        returns: Mapping[str, list[float]] | None = None,
        max_entries: int | None = None,
    ) -> list[Opportunity]:
        """Open risk is measured on the configured capital, like equity; rows are already sized.

        ``returns`` (hourly, BTC/USDT included) turns on the correlation count across this
        cycle's rows and the BTC cluster's risk cap; ``max_entries`` is the risk level's.
        """
        if not rows:
            return []
        return allocate(
            rows,
            cash=cash,
            live=self._book(),
            capital=self.config.capital_for_sizing(),
            limits=self._limits(),
            min_notional=self._min_notional,
            min_profit_usd=self.config.min_profit_usd,
            returns_by_symbol=returns,
            max_entries=max_entries,
        )

    def _min_notional(self, symbol: str) -> float | None:
        rules = service.market_rules(symbol, profile_for(self.config))
        if rules is None:
            return None
        return float(rules.min_notional) if rules.min_notional is not None else 0.0

    def _fire_armed(self, rows: list[Opportunity], snapshot: MarketSnapshot, moment: datetime) -> None:
        """Enter the armed entries whose trigger the last 15m close reached; expire the stale ones."""
        for armed in self.store.armed_entries(live_only=True):
            armed_id = int(armed["id"])
            symbol, strategy = str(armed["symbol"]), str(armed["strategy"])
            if moment >= datetime.fromisoformat(str(armed["expires_at"])):
                self.store.settle_armed(armed_id, "expired", "prazo do gatilho acabou")
                continue
            if symbol not in self.config.symbols:
                self.store.settle_armed(armed_id, "expired", "o par saiu da lista analisada")
                continue
            price = _last_price(snapshot, symbol, "15m")
            trigger = float(armed["trigger_price"])
            if price is None or not (price <= trigger if armed["condition"] == "below" else price >= trigger):
                continue
            row = next((item for item in rows if item.symbol == symbol and item.strategy == strategy), None)
            if row is None or row.status != "pronta" or row.direction <= 0:
                reason = "gatilho atingido; o setup não está pronto agora"
                if armed.get("reason") != reason:
                    self.store.settle_armed(armed_id, "armed", reason)
                continue
            outcome = self._enter_manual(row)
            result = (outcome.get("results") or [{}])[0]
            if result.get("decision") in ("operar", "pendente"):
                self.store.settle_armed(armed_id, "fired", f"gatilho {trigger} atingido a {price}", position_id=result.get("position_id"))
                self._ledger("armed_fired", {"symbol": symbol, "strategy": strategy, "trigger": trigger, "price": price})
            else:
                self.store.settle_armed(armed_id, "armed", f"gatilho atingido; entrada bloqueada: {result.get('reason')}")

    def _policy(self, position: dict[str, Any]) -> str:
        """The exit rule a position opened under; rows from before it was recorded follow the config."""
        return str(position.get("exit_policy") or self.config.exit_policy)

    def _mark(self, snapshot: MarketSnapshot) -> None:
        """Record each open position's lowest and highest last price (MAE and MFE). No order."""
        for position in self.store.open_positions():
            price = _last_price(snapshot, position["symbol"], "15m")
            if price is not None and price > 0:
                self.executor.mark(int(position["id"]), Decimal(str(price)))

    def _trail(self, snapshot: MarketSnapshot) -> None:
        positions = self.store.open_positions()
        # One order per open position stays reserved for its own re-stop or exit.
        reserve = 1 + len(positions)
        for position in positions:
            if self._policy(position) not in TRAILING_POLICIES:
                continue
            price = _last_price(snapshot, position["symbol"], "15m")
            if price is None:
                continue
            outcome = self.executor.trail(position["id"], Decimal(str(price)), reserve=reserve)
            if outcome == "movido":
                self._ledger("trail", {"symbol": position["symbol"], "position_id": position["id"]})

    def _exits(self, snapshot: MarketSnapshot, moment: datetime) -> None:
        positions = self.store.open_positions()
        reserve = 1 + len(positions)
        for position in positions:
            if self._policy(position) in INSTANCE_POLICIES:
                continue
            if self._policy(position) == "day":
                self._day_exit(position, snapshot, moment, reserve)
                continue
            if is_fixed(self._policy(position)):
                # The operator's rule: only the target recorded at the fill, or the broker stop.
                reason = _target_reason(position, snapshot)
                if reason is not None:
                    result = self.executor.exit_position(position["id"], reason)
                    self._ledger("exit", {"symbol": position["symbol"], "reason": reason, "status": result.get("status")})
                continue
            reason = _exit_reason(position, snapshot, moment, use_target=self._policy(position) == "target")
            if reason is None:
                continue
            result = self.executor.exit_position(position["id"], reason)
            self._ledger("exit", {"symbol": position["symbol"], "reason": reason, "status": result.get("status")})

    def _day_exit(self, position: dict[str, Any], snapshot: MarketSnapshot, moment: datetime, reserve: int) -> None:
        """A day trade leaves at its time limit; before that, its first target sells part of it.

        The first target triggers on the close of the last closed 15m bar, as the
        backtest replays it; the stop at the broker covers everything else.
        """
        deadline = position.get("exit_deadline")
        if deadline and moment >= datetime.fromisoformat(str(deadline)):
            result = self.executor.exit_position(position["id"], "tempo máximo do day trade")
            self._ledger("exit", {"symbol": position["symbol"], "reason": "day_deadline", "status": result.get("status")})
            return
        target = position.get("target_price")
        if position.get("tp1_done") or not target:
            return
        frame = _closed(snapshot, position["symbol"], "15m", moment)
        if frame is None or float(frame["close"].iloc[-1]) < float(target):
            return
        outcome = self.executor.take_profit(position["id"], reserve=reserve)
        if outcome in ("vendido", "concluído"):
            # A waiting first target is retried every cycle; only what happened goes to the ledger.
            self._ledger("take_profit", {"symbol": position["symbol"], "position_id": position["id"], "outcome": outcome})

    def _decay(self, moment: datetime) -> None:
        """Once per UTC day: demote a strategy whose live evidence contradicts its backtest
        (``decay.review_strategy``), then prune old history."""
        day = utc_day(moment)
        if self.store.get_state("decay_day") == day:
            return
        capital = self.config.capital_for_sizing()
        for strategy_id in STRATEGIES:
            reason = review_strategy(self.store, strategy_id, capital=capital, now=moment)
            if reason:
                self.store.add_decision({"symbol": "", "strategy": strategy_id, "decision": "rebaixada", "reason": reason})
        self.store.set_state("decay_day", day)
        # Once a day, drop history older than the retention window; positions and PnL stay.
        # Housekeeping: a failure is logged and never fails the cycle.
        try:
            pruned = self.store.prune((moment - timedelta(days=RETENTION_DAYS)).isoformat())
        except Exception as exc:  # noqa: BLE001
            logger.exception("prune failed")
            self._ledger("prune_error", {"error": type(exc).__name__})
            return
        if any(pruned.values()):
            self._ledger("prune", pruned)

    def _score(
        self,
        snapshot: MarketSnapshot,
        moment: datetime,
        sizing_capital: float,
        *,
        state: RiskState | None = None,
        takers: Mapping[str, float] | None = None,
        returns: dict[str, list[float]] | None = None,
    ) -> list[Opportunity]:
        """Every enabled strategy's setup on every analysed pair: classified, checked and sized.

        ``state`` is the cycle's risk level (read now when absent), ``takers`` the account's
        taker fee per pair, and ``returns`` the closed hourly returns, BTC/USDT included.
        """
        capital = self.config.capital_for_sizing()
        limits = self._limits()
        if state is None:
            state = self._risk_state(snapshot, moment)
        risk = RiskManager(limits, capital, sizing_capital, risk_scale=state.risk_scale)
        if returns is None:
            returns = _returns(snapshot, moment)
        takers = takers or {}
        profile = profile_for(self.config)
        ignored = self.store.ignored_keys(moment)
        positions = self.store.open_positions()
        live = {(row["symbol"], row["strategy"]) for row in self.store.live_positions()}
        # Exposure at the last price, as the gate values it.
        held_usd = _held_usd(positions, snapshot)
        exposure = sum(abs(value) for value in held_usd.values())
        exposure += sum(float(row.get("reserved_usd") or 0) for row in self._book() if row["status"] == "reserved")
        day = utc_day(moment)
        day_pnl, equity, peak = self._equity(snapshot, moment)
        self.store.record_equity(equity, moment.isoformat(), pnl=equity - capital)
        trades_today = self.store.trades_today(day)
        market_block = self._market_block(snapshot, moment)
        macro_block = self._macro_block(moment)
        saved = self.store.get_state("pair_blocked")
        blocks: dict[str, str] = dict(saved) if isinstance(saved, dict) else {}
        enabled = {key: value for key, value in STRATEGIES.items() if self.config.strategies.get(key, False)}
        stats_by_id = {strategy_id: self.store.strategy(strategy_id) for strategy_id in enabled}
        ranked: list[Opportunity] = []
        for symbol in self.config.symbols:
            if symbol in snapshot.failed:
                # Its candles are from an earlier cycle: no setup is read from them.
                continue
            inputs: _PairInputs | None = None
            slippage_block: str | None = None
            for strategy_id, strategy in enabled.items():
                stats = stats_by_id[strategy_id]
                params = stats["params"] if stats is not None else None
                # A validation run under other rules (exit, market filter, hysteresis) does not validate these.
                keyed = stats is not None and validated_for(
                    params,
                    strategy_id,
                    self.config.exit_policy,
                    market_filter=self.config.market_filter,
                    hysteresis=self.config.exit_hysteresis,
                )
                passed = tuple(tf for tf in strategy.timeframes if keyed and timeframe_passed(params, tf))
                setup = _setup(strategy, symbol, snapshot, self.config.funding_annualized_threshold, moment, passed)
                if setup is None:
                    # The setup is gone: the next one is a new decision.
                    self._decided.pop((symbol, strategy_id), None)
                    continue
                if inputs is None:
                    inputs = self._pair_inputs(symbol, snapshot, moment, limits, profile)
                    slippage_block = self._pair_block(
                        blocks, symbol, None, "slippage",
                        pair_slippage_blocked(self.store, symbol, limits.slippage_budget_pct, now=moment),
                    )
                policy = exit_policy_for(strategy_id, self.config.exit_policy)
                fixed = parse_fixed(policy)
                if fixed is not None:
                    setup = fixed.apply(setup)
                result = timeframe_result(params, setup.timeframe) if keyed else None
                signal_reason = self._signal_only_reason(
                    stats, keyed, result, setup.timeframe, symbol, strategy_id, limits, moment, blocks
                )
                win_rate, win_rate_n = estimate_win_rate(
                    {"exit_policy": params.get("exit_policy"), **result} if params and result is not None else None,
                    self.store.strategy_outcomes(strategy_id, policy),
                    exit_policy=policy,
                    min_sample=self.config.min_win_rate_sample,
                )
                stale = snapshot.stale(symbol, _price_interval(setup.timeframe), now=moment)
                draft = classify(
                    setup,
                    slippage=snapshot.slippage(symbol, planned_notional(limits, sizing_capital, setup.stop_pct)),
                    blocked_reason=_entry_block(strategy_id, moment, macro_block, slippage_block, market_block),
                    ignored=(symbol, strategy_id) in ignored,
                    in_position=(symbol, strategy_id) in live,
                    score_threshold=self.config.score_threshold,
                    cost_multiple=self.config.cost_multiple,
                    # The operator chose the payoff of a fixed rule; the hit-rate floor judges it.
                    min_reward_risk=0.0 if fixed is not None else self.config.min_reward_risk,
                    # Never cheaper than the backtest's fee, whatever discount the account has.
                    taker=max(SPOT_TAKER, takers.get(symbol, SPOT_TAKER)),
                )
                draft.data_age_seconds = _age(snapshot, symbol, setup.timeframe, moment)
                draft.signal_age_seconds = _signal_age(snapshot, symbol, setup.timeframe, moment)
                # The executor re-applies the target distance on the average fill.
                draft.plan_target_pct = setup.target_pct if setup.target_pct > 0 else None
                draft.win_rate = win_rate
                draft.win_rate_n = win_rate_n
                if self.config.test_mode_active and setup.market == "spot" and setup.direction > 0:
                    waivable = draft.status == "potencial" and draft.blocker in TESTABLE_BLOCKERS
                    if waivable or (draft.status == "pronta" and signal_reason is not None):
                        draft.status = "pronta"
                        draft.test_only = True
                        draft.reason = f"[teste] {draft.reason}"
                if draft.status == "em_posicao":
                    draft.action = "Manter; stop na corretora"
                refuse_signal = signal_reason is not None and draft.status == "pronta" and not draft.test_only
                vol_cap: float | None = None
                if draft.status == "pronta" and not refuse_signal:
                    planned = risk.sizing(
                        draft, venue_cap_usd=inputs.venue_cap, atr_daily_pct=inputs.atr_daily, throttle=inputs.throttle
                    )
                    vol_cap = vol_target_cap(limits, sizing_capital, held_usd, symbol, planned.size_usd, returns)
                verdict = risk.assess(
                    draft,
                    open_positions=positions,
                    exposure_usd=exposure,
                    day_pnl=day_pnl,
                    peak_equity=peak,
                    equity=equity,
                    trades_today=trades_today,
                    returns_by_symbol=returns,
                    stale=stale,
                    signal_only=refuse_signal,
                    state=state,
                    venue_cap_usd=inputs.venue_cap,
                    atr_daily_pct=inputs.atr_daily,
                    throttle=inputs.throttle,
                    vol_target_cap_usd=vol_cap,
                )
                if draft.status == "pronta" and not verdict.allowed:
                    draft.status = "bloqueada"
                    draft.reason = signal_reason if refuse_signal and not stale and signal_reason else verdict.reason
                    self.store.record_day(day, violations_avoided=1)
                if verdict.allowed:
                    draft.size_usd = verdict.size_usd
                draft.size_limit = verdict.size_limit or None
                draft.size_caps = verdict.caps or None
                price = _last_price(snapshot, symbol, setup.timeframe)
                if price and draft.direction > 0:
                    draft.stop_price = price * (1 - draft.risk_pct)
                    draft.target_price = price * (1 + max(setup.target_pct, 0))
                ranked.append(draft)
                if self._decided.get((symbol, strategy_id)) != (draft.status, draft.reason):
                    self.store.add_decision(
                        {
                            "ts": moment.isoformat(),
                            "symbol": symbol,
                            "strategy": strategy_id,
                            "decision": draft.status,
                            "reason": draft.reason,
                        }
                    )
                    # Marked after the write: a failed insert is retried next cycle.
                    self._decided[(symbol, strategy_id)] = (draft.status, draft.reason)
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def _signal_only_reason(
        self,
        stats: dict[str, Any] | None,
        keyed: bool,
        result: dict[str, Any] | None,
        timeframe: str,
        symbol: str,
        strategy_id: str,
        limits: RiskLimits,
        moment: datetime,
        blocks: dict[str, str],
    ) -> str | None:
        """Why a setup may only signal, or None when it may trade (6.5, 6.12).

        The validation must hold for the current rules, the setup's own timeframe must have
        passed, the pair must belong to the validated set, the Monte Carlo drawdown at the
        risk per trade (``mc_p95_dd_r × risk_per_trade_pct``) must be readable and stay under
        ``max_drawdown_pct``, and the strategy must not be demoted on this pair.
        """
        if stats is None or bool(stats.get("signal_only")) or not keyed:
            return SIGNAL_ONLY
        if not (result and result.get("passed")):
            return f"somente sinal: o timeframe {timeframe} não passou na validação"
        if symbol not in ((stats.get("params") or {}).get("symbols") or ()):
            return f"somente sinal: {symbol} fora do conjunto de pares validado"
        mc = result.get("mc_p95_dd_r")
        if isinstance(mc, bool) or not _finite(mc):
            # A record without a readable Monte Carlo drawdown proves nothing about the limit.
            return "somente sinal — Monte Carlo: drawdown p95 ausente ou ilegível na validação; valide de novo"
        drawdown = float(mc or 0) * limits.risk_per_trade_pct
        if drawdown >= limits.max_drawdown_pct:
            return (
                f"somente sinal — Monte Carlo: drawdown p95 de {_pct(drawdown)} passa do limite "
                f"de {_pct(limits.max_drawdown_pct)}"
            )
        return self._pair_block(
            blocks, symbol, strategy_id, "expectancy", pair_demoted(self.store, strategy_id, symbol, now=moment)
        )

    def _pair_block(
        self, blocks: dict[str, str], symbol: str, strategy: str | None, kind: str, reason: str | None
    ) -> str | None:
        """Return ``reason``, writing a ``pair_blocked`` ledger event when the pair's block changed.

        ``blocks`` is ``bot_state.pair_blocked`` of this cycle; it is saved on each change.
        """
        key = f"{symbol}|{strategy or ''}|{kind}"
        if blocks.get(key) != reason:
            if reason is None:
                blocks.pop(key, None)
            else:
                blocks[key] = reason
            self.store.set_state("pair_blocked", blocks)
            self._ledger(
                "pair_blocked",
                {"symbol": symbol, "strategy": strategy, "reason": kind, "blocked": reason is not None, "detail": reason},
            )
        return reason

    def _pair_inputs(
        self, symbol: str, snapshot: MarketSnapshot, moment: datetime, limits: RiskLimits, profile: str
    ) -> _PairInputs:
        """The venue cap, the daily ATR and the 7-day volatility throttle of one pair, from closed 4h bars.

        The cap is read from the mandate and binance.json (no network); an unreadable one allows nothing.
        """
        four_hour = _closed(snapshot, symbol, "4h", moment)
        atr = atr_pct_daily(four_hour) if four_hour is not None else None
        throttle = vol_throttle(four_hour["close"].to_numpy(), limits.vol_throttle_ratio) if four_hour is not None else 1.0
        try:
            cap: Any = service.order_notional_cap(symbol, profile)
        except Exception:  # noqa: BLE001 - an unreadable cap sizes the order to zero
            logger.exception("order cap read failed for %s", symbol)
            cap = Decimal(0)
        return _PairInputs(cap, atr, throttle)

    def _market_block(self, snapshot: MarketSnapshot, moment: datetime) -> str | None:
        """The market filter (D13): with ``btc_ema40d``, no entry while BTC's last closed 4h close
        is under its 240-bar EMA, nor while BTC could not be read."""
        if self.config.market_filter == "off":
            return None
        frame = None if MARKET_SYMBOL in snapshot.failed else _closed(snapshot, MARKET_SYMBOL, "4h", moment)
        if frame is None:
            return "filtro de mercado: BTC sem leitura neste ciclo"
        if not btc_trend_ok(frame["close"]):
            return "filtro de mercado: BTC abaixo da EMA de 40 dias"
        return None

    def _macro_block(self, moment: datetime) -> str | None:
        """The operator's macro calendar (D21): day entries pause within ``macro_pause_minutes`` of an event.
        A convention of the operator: the backtest does not replay it."""
        minutes = self.config.macro_pause_minutes
        if minutes <= 0:
            return None
        window = timedelta(minutes=minutes)
        for raw in self.config.macro_events_utc:
            event = datetime.fromisoformat(raw)
            event = event if event.tzinfo else event.replace(tzinfo=timezone.utc)
            if abs(moment - event) <= window:
                return f"pausa de evento macro até {event + window:%H:%M} UTC"
        return None

    def _equity(self, snapshot: MarketSnapshot, moment: datetime) -> tuple[float, float, float]:
        """Today's net PnL, the equity (configured capital, every closed day, the open positions
        at the last price) and the peak equity: the configured capital plus the PnL peak, so a
        change of capital never reads as a drawdown."""
        days = self.store.pnl_days()
        day = utc_day(moment)
        day_row = next((row for row in days if row["day"] == day), None)
        day_pnl = float(day_row["net_pnl"]) if day_row else 0.0
        positions = self.store.open_positions()
        capital = self.config.capital_for_sizing()
        pnl = sum(float(row["net_pnl"]) for row in days) + _unrealized(positions, snapshot)
        return day_pnl, capital + pnl, capital + max(self.store.peak_pnl(), pnl)

    def _risk_state(self, snapshot: MarketSnapshot, moment: datetime) -> RiskState:
        """The account's risk level of this cycle (D5, D20), acted on and stored for the executor.

        The drawdown is measured from the PnL peak and the daily loss on equity (open
        positions at the last price) against the first cycle of the UTC day. The level is
        stored in ``bot_state.risk_state``, where the executor refuses every buy while it is
        ``reducing``; the halt step is stored as ``reducing`` too, so buys stay refused after
        a manual resume until the drawdown recovers. A change of level goes to the ledger and
        the panel. The halt step halts; the daily loss exits the day trades first when
        ``flatten_day_on_daily_loss`` is on and the cycle runs, then pauses the day.
        """
        capital = self.config.capital_for_sizing()
        day_pnl, equity, peak = self._equity(snapshot, moment)
        day_change = self._day_change(moment, equity, capital, day_pnl)
        previous = self.store.get_state("risk_state")
        previous = previous if isinstance(previous, dict) else None
        state = RiskManager(self._limits(), capital, capital).portfolio_state(
            day_change=day_change, peak_equity=peak, equity=equity, previous=previous
        )
        paused = self._paused(moment)
        if paused is not None and not state.pause_day:
            # A pause started earlier today holds until the next UTC day, even if the equity recovers.
            state = replace(state, pause_day=True, reason="; ".join(part for part in (state.reason, paused) if part))
        self._save_risk_state(state, previous, moment)
        if state.level == "halt":
            if not self.halted:
                self.halt(state.reason)
            return state
        if state.pause_day and paused is None:
            if self.config.flatten_day_on_daily_loss and self.running and not self.halted:
                self._flatten_day_trades()
            self._pause_day(moment)
        return state

    def _day_change(self, moment: datetime, equity: float, capital: float, day_pnl: float) -> float:
        """Equity now minus the equity at the start of the UTC day (``bot_state.day_start_equity``).

        The start is written by the first cycle of the day, less what closed earlier that day
        (a bot started mid-day still counts the day's realised loss). A change of the configured
        capital moves the equity, not the result, so it is netted out.
        """
        day = utc_day(moment)
        start = self.store.get_state("day_start_equity")
        if not isinstance(start, dict) or start.get("day") != day or not _finite(start.get("equity")):
            if not (_finite(equity) and _finite(day_pnl)):
                return math.nan
            start = {"day": day, "equity": equity - day_pnl, "capital": capital}
            self.store.set_state("day_start_equity", start)
        base = float(start["capital"]) if _finite(start.get("capital")) else capital
        return (equity - capital) - (float(start["equity"]) - base)

    def _save_risk_state(self, state: RiskState, previous: dict[str, Any] | None, moment: datetime) -> None:
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
            self.store.set_state("risk_state", record)
        if level != before:
            self._ledger("risk_state", {"from": before, "to": level, "drawdown": state.drawdown, "reason": state.reason})
            self.publish({"type": "risk_state", **record})

    def _flatten_day_trades(self) -> None:
        """Exit every open day trade before the daily pause (``flatten_day_on_daily_loss``)."""
        positions = [row for row in self.store.open_positions() if self._policy(row) == "day"]
        if not positions:
            return
        results = []
        for position in positions:
            result = self.executor.exit_position(int(position["id"]), "perda diária: day trade zerado antes da pausa")
            results.append({"position_id": position["id"], "symbol": position["symbol"], "status": result.get("status")})
        self._ledger("daily_loss_flatten", {"positions": len(positions), "results": results})

    def _pause_day(self, moment: datetime) -> None:
        """Refuse every buy until the next UTC day: the daily-loss limit (``paused_until``).

        The cycle keeps running, so the sync (stuck stops), trailing, first targets, time
        exits and robot sales go on; the executor refuses each buy while the pause lasts.
        """
        tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.store.set_state("paused_until", tomorrow.isoformat())
        self._ledger("daily_pause", {"until": tomorrow.isoformat()})

    def _paused(self, moment: datetime) -> str | None:
        """The daily-loss pause still running at ``moment``, as its reason, or None."""
        until = self.store.get_state("paused_until")
        if isinstance(until, str) and until > moment.isoformat():
            return "pausado até o próximo dia UTC"
        return None

    def _ledger(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            append_ledger({"kind": kind, "mode": self.config.mode, **payload})
        except Exception:
            # A ledger failure must not send or block an order. It is visible in the store and the log.
            logger.exception("ledger append failed (%s)", kind)
            self.store.set_state("ledger_error", kind)


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
    price = _last_price(snapshot, position["symbol"], "15m")
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
        frame = _closed(snapshot, symbol, timeframe, moment)
        rule = _TREND if strategy == "trend" else _MOMENTUM
        reason = rule.exit_reason(frame, "buy", hysteresis=hysteresis) if frame is not None else None
        if reason:
            return reason
    if strategy == "mean_reversion":
        frame = _closed(snapshot, symbol, timeframe, moment)
        if frame is not None and len(frame) >= 20 and float(frame["close"].iloc[-1]) >= float(ema(frame["close"], 20).iloc[-1]):
            return "preço voltou à média"
    return None


def _unrealized(positions: list[dict[str, Any]], snapshot: MarketSnapshot) -> float:
    total = 0.0
    for row in positions:
        price = _last_price(snapshot, row["symbol"], "15m")
        if price is not None:
            total += (price - float(row["avg_price"])) * float(row["qty"])
    return total


def _setup(
    strategy: Strategy,
    symbol: str,
    snapshot: MarketSnapshot,
    funding_threshold: float,
    moment: datetime,
    passed: tuple[str, ...] = (),
) -> Setup | None:
    """The strategy's setup on the pair's closed bars, or None.

    A swing strategy tries its timeframes in order, the ones in ``passed`` (validated)
    first, so a setup on a validated timeframe is preferred to one that may only signal.
    """
    if isinstance(strategy, (IntradayMomentum, VwapPullback)):
        # 15m entries filtered by the closed 1h bars (bot.strategies.day).
        entry = _closed(snapshot, symbol, "15m", moment)
        if entry is None:
            return None
        day_setup: Setup | None = strategy.evaluate(symbol, "15m", entry, hourly=_closed(snapshot, symbol, "1h", moment))
        return day_setup
    if isinstance(strategy, FundingCarry):
        frame = _closed(snapshot, symbol, "1h", moment)
        if frame is None:
            return None
        found: Setup | None = strategy.evaluate(
            symbol,
            "8h",
            frame,
            funding_rates=snapshot.funding.get(symbol),
            threshold=funding_threshold,
        )
        return found
    ordered = [tf for tf in strategy.timeframes if tf in passed] + [tf for tf in strategy.timeframes if tf not in passed]
    for timeframe in ordered:
        frame = _closed(snapshot, symbol, timeframe, moment)
        if frame is None:
            continue
        setup: Setup | None = strategy.evaluate(symbol, timeframe, frame)
        if setup is not None:
            return setup
    return None


def _price_interval(timeframe: str) -> str:
    return timeframe if timeframe in _PRICE_INTERVALS else "1h"


#: Bar widths of the intervals the strategies read.
_BAR = {
    "1m": pd.Timedelta(minutes=1),
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
}


def _held(positions: frozenset[tuple[int, str, str, float]]) -> frozenset[str]:
    return frozenset(symbol for _id, symbol, _status, _qty in positions)


def _refuse_blind(snapshot: MarketSnapshot, held: frozenset[str], read: frozenset[str]) -> None:
    """A position whose pair this collect did not read, or failed to read, cannot be
    trailed or exited on the snapshot: its candles would be an older cycle's."""
    blind = sorted(held - (read - snapshot.failed))
    if blind:
        raise RuntimeError(f"preços indisponíveis para pares com posição: {', '.join(blind)}")


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


def _signal_age(snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime) -> float | None:
    """Seconds since the bar that produced the signal closed; None without a closed bar of that width."""
    width = _BAR.get(timeframe)
    frame = _closed(snapshot, symbol, timeframe, moment) if width is not None else None
    if frame is None or width is None:
        return None
    closed_at = pd.Timestamp(frame.index[-1]) + width
    return max(0.0, float((pd.Timestamp(moment) - closed_at).total_seconds()))


def _entry_block(
    strategy_id: str, moment: datetime, macro: str | None, slippage: str | None, market: str | None
) -> str | None:
    """Why no entry of this strategy may open now, whatever its score: day trades skip the
    thin hours and the macro windows, a pair over its slippage budget takes no entry, and
    the market filter holds every entry."""
    if strategy_id in DAY_STRATEGIES:
        if in_quiet_hours(moment):
            return "hora sem liquidez: sem entrada de day trade entre 03:00 e 07:00 UTC"
        if macro:
            return macro
    return slippage or market


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


def _pct(value: float) -> str:
    """A share as a percentage with at most one decimal: 0.05 → 5%, 0.075 → 7.5%."""
    return f"{value:.1%}".replace(".0%", "%")


def mode_root(config: BotConfig) -> Path:
    root = base_runtime_root() / config.mode
    root.mkdir(parents=True, exist_ok=True)
    return root

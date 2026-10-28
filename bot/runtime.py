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

With the DeepSeek advisor on (``bot.advisor``), the cycle copies its verdict cache into the
context before scoring and queues the new ready rows without waiting after the score effects
are written; in ``filter`` a confident veto keeps a row out of automatic trading and a
``reduce`` verdict shrinks its size. The calls run on the advisor's own thread, never under
this lock.

The steps themselves live in ``bot.cycle`` (market reads, exits, the risk level, scoring,
the DCA and grid robots). The runtime keeps the lock, the halt, the ledger, the config and
the order of the steps, and hands its collaborators to each step on every call.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bot.advisor import prompt
from bot.advisor.service import AdviceKey, AdviceRequest, AdviceState, Advisor
from bot.collector import Collector, MarketSnapshot
from bot.config import BotConfig, RiskLimits
from bot.cycle import exits, instances, market, risk_state, scoring
from bot.cycle.context import CycleContext, Publish, ScoreEffect
# Re-exported: the API, ops and the tests import it from here.
from bot.cycle.context import profile_for as profile_for
from bot.decay import review_strategy
from bot.evaluator import Opportunity
from bot.exchange import service
from bot.exchange.weight import weight_pressure
from bot.executor import Executor, UnprotectedPosition
from bot.features import build_features
from bot.instances import DcaParams, GridParams
from bot.live.halt import clear_halt, halt_flag_set, trip_halt
from bot.live.mandate.store import load_mandate
from bot.logging_setup import redact
from bot.paths import base_runtime_root
from bot.reports import append_ledger
from bot.risk import LOSS_GUARD_WINDOW, RiskState, allocate, protect, utc_day
from bot.store import Store
from bot.strategies import STRATEGIES

logger = logging.getLogger(__name__)

#: Failed cycles in a row that trip the halt.
MAX_FAILURES = 3
#: How long an armed entry may wait for its trigger, in hours.
MIN_ARM_HOURS = 1
MAX_ARM_HOURS = 168
#: Days of decisions, signals and equity points kept in the store.
RETENTION_DAYS = 30
#: ``stop()`` refused: a resting buy is not resolved, so the cycle goes on until it is.
STOP_REFUSED_RESTING = "compra limitada ainda no livro: o ciclo continua para resolver"
#: ``start()``, or ``stop()`` of a stopped cycle, refused: a resting buy is not resolved and no cycle watches it.
RESTING_UNRESOLVED = "compra limitada sem resolução (cancelamento ou novo stop não confirmados); tente de novo"
#: ``resume()`` refused: the halt is tripped again while a resting buy is not resolved.
RESUME_REFUSED_RESTING = "compra limitada sem resolução (cancelamento ou novo stop não confirmados); o halt continua"


class Runtime:
    def __init__(
        self,
        config: BotConfig,
        store: Store,
        collector: Collector | None = None,
        *,
        publish: Publish | None = None,
        lock: threading.RLock | None = None,
        advisor: Advisor | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.collector = collector or Collector()
        #: The process's DeepSeek advisor (``BotContext.advisor``); None turns it off whatever the config says.
        self.advisor = advisor
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
        #: endpoint never holds a cycle longer than ``market.FEE_WARMUP_BUDGET_SECONDS``.
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
        """Start the cycle. A stopped cycle reconciles no resting buy, so one still on the book (a halt
        whose cancel did not land) comes off first, and the start is refused while it is unresolved."""
        with self.lock:
            if self.halted:
                return {"status": "error", "error": "halt ativo — retomada manual exigida"}
            if not self.running:
                refused = self._settle_resting("start", RESTING_UNRESOLVED)
                if refused is not None:
                    return refused
            # The daily-loss pause refuses buys only (``paused_until``): the cycle may run under it.
            self.store.set_state("running", True)
            # "Three failed cycles in a row" counts from this start, not from before a halt.
            self.failures = 0
            self._ledger("start", {"mode": self.config.mode})
            return {"status": "ok", "running": True}

    def stop(self) -> dict[str, Any]:
        """Stop the cycle. Nothing reconciles a resting buy while the cycle is stopped, so each comes
        off the book first and what one bought joins its position under a new stop. While one is not
        resolved (its cancel unconfirmed, or the stop over what it bought not re-placed), the stop is
        refused: a running cycle keeps running to resolve it, a stopped one stays stopped (never
        started here). A position left without a stop halts."""
        with self.lock:
            refused = self._settle_resting("stop", STOP_REFUSED_RESTING if self.running else RESTING_UNRESOLVED)
            if refused is not None:
                return refused
            self.store.set_state("running", False)
            self._ledger("stop", {})
            return {"status": "ok", "running": False}

    def resume(self) -> dict[str, Any]:
        """The operator's restart after a halt: the flag clears and the cycle stays stopped.

        A resting buy the halt could not resolve is cancelled now, after the flag: the stop over
        what it bought needs the gate open. While one stays unresolved the halt is tripped again
        and the resume refused; a position left without a stop halts too.
        """
        with self.lock:
            clear_halt("binance")
            clear_halt(None)
            self.store.set_state("running", False)
            self.store.set_state("halt_reason", None)
            self.failures = 0
            self._ledger("resume", {"by": "operator"})
            refused = self._settle_resting("resume", RESUME_REFUSED_RESTING)
            if refused is not None:
                if not self.halted:
                    self.halt(RESUME_REFUSED_RESTING, flatten=False)
                return {**refused, "halted": self.halted}
            return {"status": "ok", "halted": self.halted}

    def _settle_resting(self, cause: str, refusal: str) -> dict[str, Any] | None:
        """Cancel every resting buy before ``cause`` goes on (``_cancel_resting_buys``). Returns the
        refusal of ``cause``, or None when each one is resolved. A position that may be left without
        a stop halts; any other unresolved buy refuses ``cause`` with ``refusal``. Never changes
        ``running`` otherwise."""
        resting = self._cancel_resting_buys(cause)
        unprotected = [item for item in resting if item.get("unprotected")]
        if unprotected:
            self._ledger(f"{cause}_refused", {"cause": "unprotected", "results": unprotected})
            reason = f"posição sem proteção: {unprotected[0].get('error')}"
            self.halt(reason, flatten=False)
            return {"status": "error", "error": f"{reason}; halt acionado", "running": False, "resting": resting}
        unresolved = [item for item in resting if item.get("status") != "ok"]
        if unresolved:
            self._ledger(f"{cause}_refused", {"cause": "resting_buy", "results": unresolved})
            return {"status": "error", "error": refusal, "running": self.running, "resting": unresolved}
        return None

    def halt(self, reason: str, *, flatten: bool | None = None, by: str = "file") -> dict[str, Any]:
        """Stop the cycle, flatten when asked, then write the sticky HALT flag.

        Without ``flatten``, the committed mandate's ``flatten_on_halt`` decides when a
        mandate is on file, else the config's (D23). The reason is redacted first: it may carry
        an exception's text (a signed URL), and it reaches the flag, the store, the ledger and the panel.
        """
        reason = redact(reason)
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
            # A resting buy left unresolved is shown: resume and start cancel it before they go on.
            self.publish({"type": "halt", "reason": reason, "resting": resting})
            return {"status": "ok", "halted": True, "reason": reason, "flatten": flattened, "resting": resting}

    def _cancel_resting_buys(self, cause: str) -> list[dict[str, Any]]:
        """Cancel every resting buy of the robots (``Executor.cancel_resting_buys``); never raises."""
        try:
            results = self.executor.cancel_resting_buys()
        except Exception as exc:  # noqa: BLE001 - the stop or the halt must go on
            logger.exception("cancel of the resting buys failed")
            results = [{"status": "error", "error": redact(f"{type(exc).__name__}: {exc}")}]
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
        collected: market._Market | BaseException
        try:
            collected = self._collect(moment)
        except Exception as exc:  # noqa: BLE001 - raised again under the lock, where failures are counted
            collected = exc
        if not isinstance(collected, BaseException):
            self._resolve_advice(collected.snapshot, moment)
        with self.lock:
            if self.retired:
                return {"status": "skipped", "reason": "runtime substituído durante a coleta"}
            try:
                if isinstance(collected, BaseException):
                    raise collected
                payload = self._cycle(moment, collected)
            except UnprotectedPosition as exc:
                logger.error("unprotected position: %s", exc)
                self.halt(f"posição sem proteção: {exc}", flatten=False)
                return {"status": "error", "error": redact(exc)}
            except Exception as exc:  # noqa: BLE001 - counted, logged and halted on repeat
                logger.exception("cycle failed")
                self.failures += 1
                self._ledger(
                    "tick_error",
                    {"error": type(exc).__name__, "detail": redact(exc)[:300], "failures": self.failures},
                )
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
            # Outside the cycle: the entry reads its own context and the mandate, with no risk level.
            context = self._context(
                datetime.now(timezone.utc), self.collector.view(), self._free_cash(), state=None, takers={}, limits=self._limits()
            )
            self._protect([opportunity], context)
            if opportunity.status != "pronta" or not self._allocate([opportunity], context):
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
            return {"results": [{"decision": "bloqueado", "reason": redact(exc)}], "cycle_started": False}
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
            price = market._last_price(self.collector.view(), symbol, "15m")
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

    # -- DCA and grid instances (``bot.cycle.instances``) --------------------

    def _robots(self) -> instances.Robots:
        """The robots over this runtime's collaborators, looked up now: a test or a mode switch may
        replace the executor, the config or a bound method between two calls."""
        return instances.Robots(
            store=self.store,
            executor=self.executor,
            config=self.config,
            ledger=self._ledger,
            halt=self.halt,
            halted=lambda: self.halted,
            read_limits=self._limits,
            min_notional=self._min_notional,
        )

    def preview_instance(self, kind: str, symbol: str, params: DcaParams | GridParams) -> dict[str, Any]:
        """The plan of an instance at the current price and every reason it cannot run. Creates nothing.

        Read-only, so it runs without the runtime lock: its quote and rule reads never stall the
        cycle or the panel. ``create_instance`` recomputes the plan under the lock before it acts.
        """
        return self._robots()._instance_plan(kind, symbol, params)

    def create_instance(self, kind: str, symbol: str, params: DcaParams | GridParams) -> dict[str, Any]:
        """Start a DCA or grid instance once its whole plan passes; its first buy goes at once."""
        with self.lock:
            if self.halted:
                return {"status": "error", "error": "halt ativo — retomada manual exigida"}
            if self.retired:
                return {"status": "error", "error": "ambiente trocado; tente de novo"}
            result = self._robots().create_instance(kind, symbol, params, clock_blocked=self.collector.snapshot.clock_blocked)
            if result["status"] != "ok":
                return result
            started = False
            if self.config.entry_mode == "manual" and not self.running:
                started = self.start().get("status") == "ok"
            return {**result, "cycle_started": started}

    def stop_instance(self, instance_id: int) -> dict[str, Any]:
        """Stop an instance and sell what it holds. While the sale cannot finish it stays ``stopping``."""
        with self.lock:
            instance = self.store.instance(instance_id)
            if instance is None or instance["status"] not in ("active", "stopping"):
                return {"status": "error", "error": "robô ativo não encontrado"}
            if self.halted:
                return {"status": "error", "error": "halt ativo — o stop segue na corretora"}
            return self._robots()._stop_instance(instance_id, "parado pelo operador")

    def _book(self) -> list[dict[str, Any]]:
        """Live positions, plus what each DCA or grid instance reserved and does not hold yet."""
        return self._robots()._book()

    def _instances(
        self,
        snapshot: MarketSnapshot,
        moment: datetime,
        *,
        clock_blocked: bool = False,
        state: RiskState | None = None,
        limits: RiskLimits | None = None,
    ) -> None:
        """One step of every live instance (``Robots._instances``). ``state`` is the cycle's risk
        state (None: the stored level) and ``limits`` the cycle's (None: read from the mandate now)."""
        self._robots()._instances(snapshot, moment, clock_blocked=clock_blocked, state=state, limits=limits)

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
                stopped = self._robots()._stop_instance(int(owner["id"]), "posição do robô fechada à mão")
                current = self.store.position(position_id) or {}
                if current.get("status") in ("open", "pending"):
                    return {"status": "error", "error": str(stopped.get("outcome") or stopped.get("error"))}
                return {"status": "ok", "instance_stopped": int(owner["id"])}
            try:
                return self.executor.exit_position(position_id, "fechamento manual")
            except UnprotectedPosition as exc:
                self.halt(f"posição sem proteção: {exc}", flatten=False)
                return {"status": "error", "error": redact(exc)}

    # -- cycle ---------------------------------------------------------------

    def _collect(self, moment: datetime) -> market._Market:
        """Network reads of one cycle, outside the lock: pairs, clock, candles, account fees and free USDT.

        The pair choice is saved under the lock and becomes this runtime's config before the
        candles are read, so the new pairs are read in the same cycle.
        """
        chosen = market._choose_pairs(
            moment, retired=self.retired, config=self.config, store=self.store, collector=self.collector, ledger=self._ledger
        )
        if chosen is not None:
            with self.lock:
                updated = market._apply_pairs(chosen, retired=self.retired, config=self.config, store=self.store, ledger=self._ledger)
                if updated is not None:
                    self.config = updated
        return market._collect(
            self.store, self.collector, self.config, account_takers=self._account_takers, free_cash=self._free_cash
        )

    def _account_takers(self, symbols: list[str]) -> dict[str, float]:
        """The account's taker fee of each pair, waited for at most ``FEE_WARMUP_BUDGET_SECONDS``
        (``market._account_takers``); the warm-up thread's state is this runtime's."""
        return market._account_takers(self, symbols, profile_for(self.config))

    def _free_cash(self) -> float | None:
        """Free USDT of the trading profile; ``None`` when the account cannot be read."""
        return market._free_cash(profile_for(self.config))

    def _cycle(self, moment: datetime, collected: market._Market) -> dict[str, Any]:
        # The mandate is read once: the risk level, the robots, scoring and allocation share it.
        limits = self._limits()
        snapshot = collected.snapshot
        cash = collected.cash
        positions = market._positions(self.store)
        if positions != collected.positions:
            # A manual entry, fill or exit ran during the collect: its pair may not have been
            # read, and the free USDT read before it is wrong.
            market._refuse_blind(snapshot, market._held(positions), collected.read)
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
        state = self._risk_state(snapshot, moment, limits=limits)
        if trading:
            self._instances(snapshot, moment, clock_blocked=snapshot.clock_blocked, state=state, limits=limits)
        self._decay(moment)
        # Read after the robots acted, so scoring sees the book they left.
        context = self._context(
            moment, snapshot, cash, state=state, takers=collected.takers, limits=limits, advice=self._advice_snapshot()
        )
        opportunities, effects = self._score(context)
        # Written before protect, the hit-rate floor and allocation change the rows: the
        # history keeps each decision as it was scored.
        self._apply_effects(effects)
        # Queued without waiting; the verdicts reach a later cycle through the cache.
        queued = self._consult(opportunities, context)
        self._protect([item for item in opportunities if item.status == "pronta"], context)
        ready = [item for item in opportunities if item.status == "pronta"]
        entries: list[Opportunity] = []
        # Spot Testnet with the test mode on: automatic trading also takes test rows,
        # best score first, so the whole flow can be exercised without validated strategies.
        test_auto = self.config.test_mode_active
        if self.config.entry_mode == "auto":
            eligible = [
                item
                for item in ready
                if scoring._auto_eligible(item, test_auto, self.config) and self._advice_allows(item, context, queued)
            ]
            entries = self._allocate(eligible, context)
        else:
            # The operator picks one row at a time, so each row is checked on its own.
            for item in ready:
                self._allocate([item], context)
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

    def _sizing_capital(self, cash: float | None, snapshot: MarketSnapshot) -> float:
        """The configured capital, capped by the free USDT plus the bot's open positions."""
        capital = self.config.capital_for_sizing()
        if cash is None:
            return capital
        held = 0.0
        for row in self.store.open_positions():
            price = market._last_price(snapshot, row["symbol"], "15m") or float(row["avg_price"])
            held += float(row["qty"]) * price
        return min(capital, cash + held)

    def _context(
        self,
        moment: datetime,
        snapshot: MarketSnapshot,
        cash: float | None,
        *,
        state: RiskState | None,
        takers: Mapping[str, float],
        limits: RiskLimits,
        advice: Mapping[AdviceKey, AdviceState] | None = None,
    ) -> CycleContext:
        """Read the book, equity and capital once (``CycleContext``). Reads only; writes nothing.
        ``advice`` is the advisor's cache copy for the cycle (none outside it)."""
        positions = self.store.open_positions()
        return CycleContext(
            moment=moment,
            snapshot=snapshot,
            positions=positions,
            live=frozenset((str(row["symbol"]), str(row["strategy"])) for row in self.store.live_positions()),
            book=self._book(),
            held_usd=market._held_usd(positions, snapshot),
            equity=risk_state._equity(snapshot, moment, store=self.store, config=self.config)[1],
            capital=self.config.capital_for_sizing(),
            sizing_capital=self._sizing_capital(cash, snapshot),
            cash=cash,
            returns=market._returns(snapshot, moment),
            state=state,
            takers=takers,
            limits=limits,
            advice=advice or {},
        )

    # -- DeepSeek advisor (``bot.advisor``) ---------------------------------

    def _advising(self) -> Advisor | None:
        """The advisor when the config turns it on, else None."""
        return self.advisor if self.config.advisor.mode != "off" else None

    def _advice_snapshot(self) -> dict[AdviceKey, AdviceState]:
        """The advisor's cache, copied under the cycle's lock: a dict copy, no wait and no network."""
        advisor = self._advising()
        return advisor.snapshot() if advisor is not None else {}

    def _consult(self, opportunities: list[Opportunity], context: CycleContext) -> dict[AdviceKey, AdviceState]:
        """Queue a request for each ready strategy row whose signal bar was not judged yet.

        Test rows, robots and armed entries are never asked about. The request is built here
        from numbers and enums (``prompt.user_data``) and queued with ``put_nowait``: a full
        queue is no verdict. Returns the states of the keys queued now, which the cycle's cache
        copy does not have yet.
        """
        advisor = self._advising()
        rows = [item for item in opportunities if item.status == "pronta" and not item.test_only and item.direction > 0]
        if advisor is None or not rows or not advisor.configured():
            return {}
        settings = self.config.advisor
        snapshot, moment = context.snapshot, context.moment
        btc = market._closed(snapshot, market.MARKET_SYMBOL, "4h", moment)
        btc_trend = prompt.btc_trend(btc["close"].to_numpy() if btc is not None else None)
        state = context.state
        ledger = self._advice_ledger()
        queued: dict[AdviceKey, AdviceState] = {}
        for item in rows:
            found = scoring.advice_request_key(item, snapshot, moment, settings.model)
            if found is None or found[0] in context.advice:
                continue
            key, signal_close = found
            try:
                data = prompt.user_data(
                    item,
                    features=_features(snapshot, item.symbol),
                    btc_trend=btc_trend,
                    risk_level=state.level if state is not None else "normal",
                    drawdown=state.drawdown if state is not None else 0.0,
                    open_positions=len(context.positions),
                )
            except ValueError as exc:
                logger.warning("advisor: %s skipped: %s", item.symbol, exc)
                continue
            request = AdviceRequest(
                key=key,
                store=self.store,
                ledger=ledger,
                mode=settings.mode,
                model=settings.model,
                daily_budget_usd=settings.daily_budget_usd,
                data=data,
                entry_price=signal_close,
                stop_pct=item.risk_pct,
                target_pct=item.plan_target_pct or 0.0,
                requested_at=moment,
            )
            known = advisor.request(request)
            if known is not None:
                queued[key] = known
                item.advice = known.public()
        return queued

    def _advice_allows(self, item: Opportunity, context: CycleContext, queued: Mapping[AdviceKey, AdviceState]) -> bool:
        """Whether the advisor lets automatic trading take ``item`` (``scoring._advice_allows``).
        Test rows were never asked, so the advisor holds none of them back."""
        settings = self.config.advisor
        if item.test_only or settings.mode != "filter" or self.advisor is None:
            return True
        found = scoring.advice_request_key(item, context.snapshot, context.moment, settings.model)
        entry = None if found is None else context.advice.get(found[0]) or queued.get(found[0])
        return scoring._advice_allows(item, entry, context.moment, settings)

    def _advice_ledger(self) -> Callable[[dict[str, Any]], None]:
        """The ``advice`` event writer of this runtime's mode: its ledger sits beside its store,
        so a verdict that arrives after a mode switch is still recorded where it was asked."""
        mode = self.config.mode
        path = self.store.path.parent / "bot-ledger.jsonl"

        def write(payload: dict[str, Any]) -> None:
            append_ledger({"kind": "advice", "mode": mode, **payload}, path=path)

        return write

    def _resolve_advice(self, snapshot: MarketSnapshot, moment: datetime) -> None:
        """Label logged verdicts with their counterfactual R (``Advisor.maybe_resolve``), before the
        lock: housekeeping that never fails the cycle."""
        if self.advisor is None or self.retired:
            return
        try:
            self.advisor.maybe_resolve(self.store, snapshot, moment)
        except Exception:  # noqa: BLE001
            logger.exception("advice resolution failed")

    def _protect(self, rows: list[Opportunity], context: CycleContext) -> None:
        """Cooldown and loss guard (``bot.risk.protect``) on rows about to be allocated."""
        if not rows:
            return
        limits = context.limits
        window = max(LOSS_GUARD_WINDOW, timedelta(minutes=limits.pair_cooldown_minutes))
        protect(rows, self.store.recent_losses((context.moment - window).isoformat()), limits, context.moment)

    def _limits(self) -> RiskLimits:
        mandate = load_mandate("binance")
        return self.config.tightened(mandate.hard_caps if mandate else None)

    def _allocate(self, rows: list[Opportunity], context: CycleContext) -> list[Opportunity]:
        """Open risk is measured on the configured capital, like equity; rows are already sized.

        The context's hourly returns (BTC/USDT included) turn on the correlation count across
        this cycle's rows and the BTC cluster's risk cap; its risk level lowers the entries per
        cycle (none outside the cycle).
        """
        if not rows:
            return []
        return allocate(
            rows,
            cash=context.cash,
            live=context.book,
            capital=context.capital,
            limits=context.limits,
            min_notional=self._min_notional,
            min_profit_usd=self.config.min_profit_usd,
            returns_by_symbol=context.returns,
            max_entries=context.state.max_entries if context.state is not None else None,
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
            price = market._last_price(snapshot, symbol, "15m")
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

    def _mark(self, snapshot: MarketSnapshot) -> None:
        """Record each open position's lowest and highest last price (MAE and MFE). No order."""
        exits._mark(snapshot, store=self.store, executor=self.executor)

    def _trail(self, snapshot: MarketSnapshot) -> None:
        exits._trail(snapshot, store=self.store, executor=self.executor, config=self.config, ledger=self._ledger)

    def _exits(self, snapshot: MarketSnapshot, moment: datetime) -> None:
        exits._exits(snapshot, moment, store=self.store, executor=self.executor, config=self.config, ledger=self._ledger)

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

    def _score(self, context: CycleContext) -> tuple[list[Opportunity], list[ScoreEffect]]:
        """Every enabled strategy's setup on every analysed pair (``scoring._score``); writes nothing."""
        return scoring._score(context, store=self.store, config=self.config, decided=self._decided)

    def _apply_effects(self, effects: list[ScoreEffect]) -> None:
        """Write what ``_score`` decided, in its order.

        A decision is marked in ``_decided`` only after its insert: a failed one is retried
        next cycle. The pair blocks are saved whole on each change, with their ledger event.
        """
        for effect in effects:
            data = effect.data
            if effect.kind == "equity":
                self.store.record_equity(data["equity"], data["ts"], pnl=data["pnl"])
            elif effect.kind == "pair_blocked":
                self.store.set_state("pair_blocked", data["blocks"])
                self._ledger("pair_blocked", dict(data["event"]))
            elif effect.kind == "violation":
                self.store.record_day(data["day"], violations_avoided=1)
            elif effect.kind == "decision":
                record = data["record"]
                self.store.add_decision(dict(record))
                self._decided[data["key"]] = (record["decision"], record["reason"])
            else:
                self._decided.pop(data["key"], None)

    def _risk_state(self, snapshot: MarketSnapshot, moment: datetime, *, limits: RiskLimits | None = None) -> RiskState:
        """The account's risk level of this cycle (``risk_state._risk_state``), acted on and stored
        for the executor. The halt step halts here, right after the level is measured.
        ``limits`` is the cycle's (None: read from the mandate now).
        """
        state, halt_reason = risk_state._risk_state(
            snapshot,
            moment,
            store=self.store,
            executor=self.executor,
            config=self.config,
            limits=limits or self._limits(),
            ledger=self._ledger,
            publish=self.publish,
            running=lambda: self.running and not self.halted,
        )
        if halt_reason is not None and not self.halted:
            self.halt(halt_reason)
        return state

    def _paused(self, moment: datetime) -> str | None:
        """The daily-loss pause still running at ``moment``, as its reason, or None."""
        return risk_state._paused(moment, store=self.store)

    def _ledger(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            append_ledger({"kind": kind, "mode": self.config.mode, **payload})
        except Exception:
            # A ledger failure must not send or block an order. It is visible in the store and the log.
            logger.exception("ledger append failed (%s)", kind)
            self.store.set_state("ledger_error", kind)


def _features(snapshot: MarketSnapshot, symbol: str) -> dict[str, Any] | None:
    """The panel's feature series of a pair (``build_features``), or None without its 1h and 15m bars."""
    try:
        return build_features(snapshot, symbol)
    except (KeyError, IndexError, ValueError):
        return None


def mode_root(config: BotConfig) -> Path:
    root = base_runtime_root() / config.mode
    root.mkdir(parents=True, exist_ok=True)
    return root

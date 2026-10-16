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
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from bot.collector import Collector, MarketSnapshot
from bot.config import BotConfig, RiskLimits
from bot.decay import apply_decay
from bot.evaluator import Opportunity, classify, estimate_win_rate
from bot.exchange import service
from bot.exchange.weight import weight_pressure
from bot.executor import Executor, UnprotectedPosition
from bot.indicators import ema
from bot.live.halt import halt_flag_set, trip_halt
from bot.live.mandate.store import load_mandate
from bot.markets import merge_symbols, rank_promising, venue_symbols
from bot.paths import base_runtime_root
from bot.reports import append_ledger
from bot.risk import LOSS_GUARD_WINDOW, RiskManager, allocate, protect, utc_day
from bot.store import Store
from bot.strategies import (
    STRATEGIES,
    FundingCarry,
    IntradayMomentum,
    Momentum,
    Strategy,
    TrendBreakout,
    VwapPullback,
    exit_policy_for,
    validated_for,
)
from bot.strategies.day import in_quiet_hours
from bot.strategies.base import Setup

logger = logging.getLogger(__name__)

Publish = Callable[[dict[str, Any]], None]
#: Quality filters the Spot Testnet test mode may waive for a manual buy.
TESTABLE_BLOCKERS = frozenset({"score", "cost", "reward_risk", "regime"})
_PRICE_INTERVALS = ("1m", "15m", "1h", "4h")
#: Failed cycles in a row that trip the halt.
MAX_FAILURES = 3
_TREND = TrendBreakout()
_MOMENTUM = Momentum()
#: Days of decisions, signals and equity points kept in the store.
RETENTION_DAYS = 30
#: Seconds between two automatic choices of the analysed pairs.
AUTO_SELECT_SECONDS = 3600


@dataclass(frozen=True)
class _Market:
    """What one cycle read from Binance before taking the lock."""

    snapshot: MarketSnapshot
    cash: float | None
    #: Live positions (id, symbol, status, qty) when ``cash`` was read.
    positions: frozenset[tuple[int, str, str, float]] = frozenset()
    #: Pairs this collect read: the analysed ones plus every pair with a live position.
    read: frozenset[str] = frozenset()


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
        self.executor = Executor(store, profile_for(config), exit_policy=config.exit_policy)

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
            paused = self.store.get_state("paused_until")
            if paused and paused > datetime.now(timezone.utc).isoformat():
                return {"status": "error", "error": "pausado até o próximo dia UTC"}
            self.store.set_state("running", True)
            # "Three failed cycles in a row" counts from this start, not from before a halt.
            self.failures = 0
            self._ledger("start", {"mode": self.config.mode})
            return {"status": "ok", "running": True}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self.store.set_state("running", False)
            self._ledger("stop", {})
            return {"status": "ok", "running": False}

    def halt(self, reason: str, *, flatten: bool | None = None, by: str = "file") -> dict[str, Any]:
        """Stop the cycle, flatten when asked, then write the sticky HALT flag."""
        with self.lock:
            self.store.set_state("running", False)
            flattened: list[dict[str, Any]] = []
            if flatten if flatten is not None else self.config.flatten_on_halt:
                try:
                    flattened = self.executor.flatten()
                except Exception as exc:  # noqa: BLE001 - the flag below must be written regardless
                    logger.exception("flatten before halt failed")
                    flattened = [{"status": "error", "error": type(exc).__name__}]
            trip_halt(by, reason, broker="binance")
            self.store.set_state("halt_reason", reason)
            failed = [item for item in flattened if item.get("status") != "ok"]
            self._ledger("halt", {"reason": reason, "flattened": len(flattened), "flatten_failed": len(failed)})
            self.publish({"type": "halt", "reason": reason})
            return {"status": "ok", "halted": True, "reason": reason, "flatten": flattened}

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
        here starts the cycle (``start``: halt and daily pause still refuse it)
        to keep its stop moving and its exits watched.
        """
        with self.lock:
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
                self._protect([opportunity], datetime.now(timezone.utc))
                if opportunity.status != "pronta" or not self._allocate([opportunity], self._free_cash()):
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

    def close_position(self, position_id: int) -> dict[str, Any]:
        with self.lock:
            if self.halted:
                return {"status": "blocked", "error": "halt ativo — o stop segue na corretora"}
            if self.retired:
                return {"status": "blocked", "error": "ambiente trocado; tente de novo"}
            try:
                return self.executor.exit_position(position_id, "fechamento manual")
            except UnprotectedPosition as exc:
                self.halt(f"posição sem proteção: {exc}", flatten=False)
                return {"status": "error", "error": str(exc)}

    # -- cycle ---------------------------------------------------------------

    def _collect(self, moment: datetime) -> "_Market":
        """Network reads of one cycle, outside the lock: pairs, clock, candles and free USDT."""
        chosen = self._choose_pairs(moment)
        if chosen is not None:
            with self.lock:
                self._apply_pairs(chosen)
        # Pairs with a position are always read, even when the analysed list dropped them
        # (a hand edit, the automatic choice): their stops and exits need fresh candles.
        # No cap here: every analysed pair is scored, so every one must be read too.
        symbols = list(dict.fromkeys([*sorted(_held(self._positions())), *self.config.symbols]))
        self.collector.sync_clock()
        snapshot = self.collector.refresh(symbols)
        read = frozenset(symbols)
        positions = self._positions()
        _refuse_blind(snapshot, _held(positions), read)
        return _Market(snapshot=snapshot, cash=self._free_cash(), positions=positions, read=read)

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
            if not snapshot.clock_blocked:
                self._trail(snapshot)
            self._exits(snapshot, moment)
        self._decay(moment)
        capital = self._sizing_capital(cash, snapshot)
        opportunities = self._score(snapshot, moment, capital)
        self._protect([item for item in opportunities if item.status == "pronta"], moment)
        ready = [item for item in opportunities if item.status == "pronta"]
        entries: list[Opportunity] = []
        # Spot Testnet with the test mode on: automatic trading also takes test rows,
        # best score first, so the whole flow can be exercised without validated strategies.
        test_auto = self.config.test_mode_active
        if self.config.entry_mode == "auto":
            eligible = [
                item
                for item in ready
                if (item.test_only and test_auto)
                or (not item.test_only and item.win_rate is not None and item.win_rate >= self.config.min_win_rate)
            ]
            entries = self._allocate(eligible, cash)
        else:
            # The operator picks one row at a time, so each row is checked on its own.
            for item in ready:
                self._allocate([item], cash)
        self.store.replace_opportunities([item.to_dict() for item in opportunities])
        acted: list[dict[str, Any]] = []
        if trading and self.running and not snapshot.clock_blocked and self.config.entry_mode == "auto":
            acted = self.executor.act(entries, clock_blocked=False, allow_test=test_auto)
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

    def _allocate(self, rows: list[Opportunity], cash: float | None) -> list[Opportunity]:
        """Open risk is measured on the configured capital, like equity; rows are already sized."""
        if not rows:
            return []
        return allocate(
            rows,
            cash=cash,
            live=self.store.live_positions(),
            capital=self.config.capital_for_sizing(),
            limits=self._limits(),
            min_notional=self._min_notional,
            min_profit_usd=self.config.min_profit_usd,
        )

    def _min_notional(self, symbol: str) -> float | None:
        rules = service.market_rules(symbol, profile_for(self.config))
        if rules is None:
            return None
        return float(rules.min_notional) if rules.min_notional is not None else 0.0

    def _policy(self, position: dict[str, Any]) -> str:
        """The exit rule a position opened under; rows from before it was recorded follow the config."""
        return str(position.get("exit_policy") or self.config.exit_policy)

    def _trail(self, snapshot: MarketSnapshot) -> None:
        positions = self.store.open_positions()
        # One order per open position stays reserved for its own re-stop or exit.
        reserve = 1 + len(positions)
        for position in positions:
            if self._policy(position) != "trailing":
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
            if self._policy(position) == "day":
                self._day_exit(position, snapshot, moment, reserve)
                continue
            reason = _exit_reason(position, snapshot, moment, use_target=self._policy(position) != "trailing")
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
        day = utc_day(moment)
        if self.store.get_state("decay_day") == day:
            return
        capital = self.config.capital_for_sizing()
        for strategy_id in STRATEGIES:
            if apply_decay(self.store, strategy_id, self.store.strategy_daily_returns(strategy_id, capital)):
                self.store.add_decision(
                    {
                        "symbol": "",
                        "strategy": strategy_id,
                        "decision": "rebaixada",
                        "reason": "Sharpe dos últimos 30 dias abaixo de metade do backtest",
                    }
                )
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

    def _score(self, snapshot: MarketSnapshot, moment: datetime, sizing_capital: float) -> list[Opportunity]:
        capital = self.config.capital_for_sizing()
        risk = RiskManager(self._limits(), capital, sizing_capital)
        ignored = self.store.ignored_keys(moment)
        positions = self.store.open_positions()
        live = {(row["symbol"], row["strategy"]) for row in self.store.live_positions()}
        exposure = sum(abs(float(row["qty"]) * float(row["avg_price"])) for row in positions)
        day = utc_day(moment)
        days = self.store.pnl_days()
        day_row = next((row for row in days if row["day"] == day), None)
        day_pnl = float(day_row["net_pnl"]) if day_row else 0.0
        equity = capital + sum(float(row["net_pnl"]) for row in days) + _unrealized(positions, snapshot)
        self.store.record_equity(equity, moment.isoformat())
        peak = max(self.store.peak_equity(), equity)
        returns = _returns(snapshot)
        trades_today = self.store.trades_today(day)
        ranked: list[Opportunity] = []
        for symbol in self.config.symbols:
            if symbol in snapshot.failed:
                # Its candles are from an earlier cycle: no setup is read from them.
                continue
            for strategy_id, strategy in STRATEGIES.items():
                if not self.config.strategies.get(strategy_id, False):
                    continue
                setup = _setup(strategy, symbol, snapshot, self.config.funding_annualized_threshold, moment)
                if setup is None:
                    # The setup is gone: the next one is a new decision.
                    self._decided.pop((symbol, strategy_id), None)
                    continue
                stats = self.store.strategy(strategy_id)
                policy = exit_policy_for(strategy_id, self.config.exit_policy)
                # A validation run under other exit rules does not validate these.
                matches = stats is not None and validated_for(stats["params"], strategy_id, self.config.exit_policy)
                signal_only = stats is None or bool(stats["signal_only"]) or not matches
                win_rate, win_rate_n = estimate_win_rate(
                    stats["params"] if stats and matches else None,
                    self.store.strategy_outcomes(strategy_id, policy),
                    exit_policy=policy,
                    min_sample=self.config.min_win_rate_sample,
                )
                stale = snapshot.stale(symbol, _price_interval(setup.timeframe), now=moment)
                quiet = policy == "day" and in_quiet_hours(moment)
                draft = classify(
                    setup,
                    slippage=snapshot.slippage(symbol, 1000),
                    blocked_reason="hora sem liquidez: sem entrada de day trade entre 03:00 e 07:00 UTC" if quiet else None,
                    ignored=(symbol, strategy_id) in ignored,
                    in_position=(symbol, strategy_id) in live,
                    score_threshold=self.config.score_threshold,
                    cost_multiple=self.config.cost_multiple,
                    min_reward_risk=self.config.min_reward_risk,
                )
                draft.data_age_seconds = _age(snapshot, symbol, setup.timeframe, moment)
                draft.win_rate = win_rate
                draft.win_rate_n = win_rate_n
                if self.config.test_mode_active and setup.market == "spot" and setup.direction > 0:
                    waivable = draft.status == "potencial" and draft.blocker in TESTABLE_BLOCKERS
                    if waivable or (draft.status == "pronta" and signal_only):
                        draft.status = "pronta"
                        draft.test_only = True
                        draft.reason = f"[teste] {draft.reason}"
                if draft.status == "em_posicao":
                    draft.action = "Manter; stop na corretora"
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
                    signal_only=signal_only and draft.status == "pronta" and not draft.test_only,
                )
                if draft.status == "pronta" and not verdict.allowed:
                    draft.status = "bloqueada"
                    draft.reason = verdict.reason
                    self.store.record_day(day, violations_avoided=1)
                if verdict.allowed:
                    draft.size_usd = verdict.size_usd
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
                if verdict.halt:
                    self.halt(verdict.reason)
                    return sorted(ranked, key=lambda item: item.score, reverse=True)
                if verdict.pause_day:
                    tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    self.store.set_state("paused_until", tomorrow.isoformat())
                    self.store.set_state("running", False)
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def _ledger(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            append_ledger({"kind": kind, "mode": self.config.mode, **payload})
        except Exception:
            # A ledger failure must not send or block an order. It is visible in the store and the log.
            logger.exception("ledger append failed (%s)", kind)
            self.store.set_state("ledger_error", kind)


def _exit_reason(
    position: dict[str, Any], snapshot: MarketSnapshot, moment: datetime, *, use_target: bool = True
) -> str | None:
    """Target (unless the stop trails instead) and the strategy's own exit."""
    symbol = position["symbol"]
    price = _last_price(snapshot, symbol, "15m")
    target = position.get("target_price")
    if use_target and price is not None and target and price >= float(target):
        return "alvo atingido"
    strategy = position["strategy"]
    hourly = _closed(snapshot, symbol, "1h", moment)
    if strategy == "trend" and hourly is not None:
        reason = _TREND.exit_reason(hourly, "buy")
        if reason:
            return reason
    if strategy == "momentum":
        four_hour = _closed(snapshot, symbol, "4h", moment)
        reason = _MOMENTUM.exit_reason(four_hour, "buy") if four_hour is not None else None
        if reason:
            return reason
    if strategy == "mean_reversion":
        frame = _closed(snapshot, symbol, "15m", moment)
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
    strategy: Strategy, symbol: str, snapshot: MarketSnapshot, funding_threshold: float, moment: datetime
) -> Setup | None:
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
    for timeframe in strategy.timeframes:
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


def _returns(snapshot: MarketSnapshot) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for (symbol, interval), series in snapshot.candles.items():
        if interval != "1h" or series.frame.empty:
            continue
        out[symbol] = series.frame["close"].pct_change().dropna().tolist()
    return out


def mode_root(config: BotConfig) -> Path:
    root = base_runtime_root() / config.mode
    root.mkdir(parents=True, exist_ok=True)
    return root

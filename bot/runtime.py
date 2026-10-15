"""One tick: collect, score, size, and send only what the gate accepts.

A halt is sticky. The loop does not clear it. Three exchange errors in a row
trip it. After a halt the bot cancels open orders and, when asked, flattens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from bot.collector import Collector, MarketSnapshot
from bot.config import BotConfig
from bot.evaluator import Opportunity, classify
from bot.exchange.weight import weight_pressure
from bot.governance.ledger import append_record
from bot.live.halt import halt_flag_set, trip_halt
from bot.live.mandate.store import load_mandate
from bot.reports import ledger_path
from bot.risk import RiskManager, utc_day
from bot.store import Store
from bot.strategies import STRATEGIES
from bot.strategies.base import Setup

Publish = Callable[[dict[str, Any]], None]


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
    ) -> None:
        self.config = config
        self.store = store
        self.collector = collector or Collector()
        self.publish = publish or (lambda _event: None)
        self.api_errors = 0
        from bot.executor import Executor

        self.executor = Executor(store, profile_for(config))

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
        if self.halted:
            return {"status": "error", "error": "halt ativo — retomada manual exigida"}
        paused = self.store.get_state("paused_until")
        if paused and paused > datetime.now(timezone.utc).isoformat():
            return {"status": "error", "error": "pausado até o próximo dia UTC"}
        self.store.set_state("running", True)
        self._ledger("start", {"mode": self.config.mode})
        return {"status": "ok", "running": True}

    def stop(self) -> dict[str, Any]:
        self.store.set_state("running", False)
        self._ledger("stop", {})
        return {"status": "ok", "running": False}

    def halt(self, reason: str, *, flatten: bool | None = None) -> dict[str, Any]:
        trip_halt("binance", reason=reason)
        self.store.set_state("running", False)
        self.store.set_state("halt_reason", reason)
        cancelled = self.executor.cancel_open_orders()
        flattened: list[dict[str, Any]] = []
        if flatten if flatten is not None else self.config.flatten_on_halt:
            flattened = self.executor.flatten()
        self._ledger("halt", {"reason": reason, "cancelled": len(cancelled), "flattened": len(flattened)})
        self.publish({"type": "halt", "reason": reason})
        return {"status": "ok", "halted": True, "reason": reason}

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        try:
            snapshot = self.collector.refresh(self.config.symbols)
            self.api_errors = 0
        except Exception as exc:  # noqa: BLE001 - repeated feed failures halt
            self.api_errors += 1
            if self.api_errors >= 3:
                self.halt(f"três erros de API: {type(exc).__name__}")
            self._ledger("tick_error", {"error": type(exc).__name__})
            return {"status": "error", "error": type(exc).__name__}
        if self.collector.snapshot.clock_blocked:
            self.store.add_decision({"decision": "nao_operar", "reason": "relógio divergente", "symbol": "", "strategy": ""})
        opportunities = self._score(snapshot, moment)
        self.store.replace_opportunities([item.to_dict() for item in opportunities])
        acted: list[dict[str, Any]] = []
        if self.running and not self.halted and not self.collector.snapshot.clock_blocked:
            actionable = [item for item in opportunities if item.status == "pronta"]
            acted = self.executor.act(actionable, clock_blocked=False)
        self._mark_equity(moment)
        payload = {
            "opportunities": len(opportunities),
            "ready": sum(1 for item in opportunities if item.status == "pronta"),
            "acted": len(acted),
            "halted": self.halted,
            "running": self.running,
            "weight_pressure": weight_pressure(),
        }
        self._ledger("tick", payload)
        self.publish({"type": "tick", **payload, "rows": [item.to_dict() for item in opportunities]})
        return {"status": "ok", **payload}

    def _score(self, snapshot: MarketSnapshot, moment: datetime) -> list[Opportunity]:
        mandate = load_mandate("binance")
        limits = self.config.tightened(mandate.hard_caps if mandate else None)
        risk = RiskManager(limits, self.config.capital_for_sizing())
        ignored = self.store.ignored_keys(moment)
        positions = self.store.open_positions()
        exposure = sum(abs(float(row["qty"]) * float(row["avg_price"])) for row in positions)
        day = utc_day(moment)
        day_row = next((row for row in self.store.pnl_days() if row["day"] == day), None)
        day_pnl = float(day_row["net_pnl"]) if day_row else 0.0
        equity = self.config.capital_for_sizing() + sum(float(row["net_pnl"]) for row in self.store.pnl_days())
        self.store.record_equity(equity, moment.isoformat())
        peak = max(self.store.peak_equity(), equity)
        returns = _returns(snapshot)
        ranked: list[Opportunity] = []
        for symbol in self.config.symbols:
            for strategy_id, strategy in STRATEGIES.items():
                if not self.config.strategies.get(strategy_id, False):
                    continue
                setup = _setup(strategy, symbol, snapshot, self.config.funding_annualized_threshold)
                if setup is None:
                    continue
                self.store.add_signal(
                    {
                        "symbol": symbol,
                        "strategy": strategy_id,
                        "timeframe": setup.timeframe,
                        "direction": setup.direction,
                        "confidence": setup.confidence,
                    }
                )
                stats = self.store.strategy(strategy_id)
                signal_only = bool(stats["signal_only"]) if stats else True
                stale = snapshot.stale(symbol, setup.timeframe if setup.timeframe in {"1m", "15m", "1h", "4h"} else "1h", now=moment)
                draft = classify(
                    setup,
                    slippage=snapshot.slippage(symbol, 1000),
                    ignored=(symbol, strategy_id) in ignored,
                    in_position=any(row["symbol"] == symbol and row["strategy"] == strategy_id for row in positions),
                    score_threshold=self.config.score_threshold,
                    cost_multiple=self.config.cost_multiple,
                    min_reward_risk=self.config.min_reward_risk,
                )
                draft.data_age_seconds = _age(snapshot, symbol, setup.timeframe, moment)
                if draft.status == "em_posicao":
                    draft.action = "Manter, subir stop"
                verdict = risk.assess(
                    draft,
                    open_positions=positions,
                    exposure_usd=exposure,
                    day_pnl=day_pnl,
                    peak_equity=peak,
                    equity=equity,
                    trades_today=self.store.trades_today(day),
                    returns_by_symbol=returns,
                    stale=stale,
                    signal_only=signal_only and draft.status == "pronta",
                )
                if verdict.halt:
                    self.halt(verdict.reason)
                if verdict.pause_day:
                    tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    self.store.set_state("paused_until", tomorrow.isoformat())
                    self.store.set_state("running", False)
                if draft.status == "pronta" and not verdict.allowed:
                    draft.status = "bloqueada"
                    draft.reason = verdict.reason
                    self.store.record_day(day, violations_avoided=1)
                if verdict.allowed:
                    draft.size_usd = verdict.size_usd
                price = _last_price(snapshot, symbol, setup.timeframe)
                if price and draft.direction > 0:
                    draft.stop_price = price * (1 - draft.risk_pct)
                    draft.target_price = price * (1 + max(draft.net_return, 0))
                ranked.append(draft)
                self.store.add_decision(
                    {
                        "ts": moment.isoformat(),
                        "symbol": symbol,
                        "strategy": strategy_id,
                        "decision": draft.status,
                        "reason": draft.reason,
                    }
                )
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def _mark_equity(self, moment: datetime) -> None:
        equity = self.config.capital_for_sizing() + sum(float(row["net_pnl"]) for row in self.store.pnl_days())
        self.store.record_equity(equity, moment.isoformat())

    def _ledger(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            append_record(ledger_path(), {"kind": kind, "mode": self.config.mode, **payload})
        except Exception:
            # A ledger failure must not send an order. The tick already decided.
            self.store.set_state("ledger_error", kind)


def _setup(strategy: Any, symbol: str, snapshot: MarketSnapshot, funding_threshold: float) -> Setup | None:
    if strategy.id == "funding_carry":
        frame = _frame(snapshot, symbol, "1h")
        if frame is None:
            return None
        return strategy.evaluate(
            symbol,
            "8h",
            frame,
            funding_rates=snapshot.funding.get(symbol),
            threshold=funding_threshold,
        )
    for timeframe in strategy.timeframes:
        frame = _frame(snapshot, symbol, timeframe)
        if frame is None:
            continue
        setup = strategy.evaluate(symbol, timeframe, frame)
        if setup is not None:
            return setup
    return None


def _frame(snapshot: MarketSnapshot, symbol: str, timeframe: str) -> pd.DataFrame | None:
    series = snapshot.candles.get((symbol, timeframe))
    if series is None or series.frame.empty:
        return None
    return series.frame


def _last_price(snapshot: MarketSnapshot, symbol: str, timeframe: str) -> float | None:
    frame = _frame(snapshot, symbol, timeframe if timeframe in {"1m", "15m", "1h", "4h"} else "1h")
    if frame is None:
        return None
    return float(frame["close"].iloc[-1])


def _age(snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime) -> float | None:
    key = timeframe if timeframe in {"1m", "15m", "1h", "4h"} else "1h"
    series = snapshot.candles.get((symbol, key))
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


def mode_root(config: BotConfig):
    from bot.paths import base_runtime_root

    root = base_runtime_root() / config.mode
    root.mkdir(parents=True, exist_ok=True)
    return root

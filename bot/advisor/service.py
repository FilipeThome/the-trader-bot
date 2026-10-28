"""The advisor of the process: one worker thread, a bounded queue and the verdict cache.

One ``Advisor`` lives in ``BotContext`` next to the collector and is handed to each ``Runtime``
(R-A7). Under the cycle's lock the runtime only copies the cache (``snapshot``) and queues
requests with ``put_nowait`` (``request``): a full queue is no verdict, never a wait. The worker
never takes the runtime's or the API's lock (S-A4); it holds only the advisor's own locks.

The cache key is (store, pair, strategy, timeframe, signal bar close, ``PROMPT_VERSION``, model):
one call per signal and mode, not per cycle. The store (``Store.identity``) is in the key because
one advisor serves both modes: a testnet verdict never gates or links a production row. Each
request carries the ``Store`` and the ledger writer of the runtime that asked, so a verdict lands
in the database of the mode that asked even after a mode switch. A signal that store already
logged (asked before a restart) is read back from ``advice_log`` instead of asked again.

The daily budget is per DeepSeek key, so it sums ``advisor_usage_daily`` over both mode stores
(``combined_usage``) before each call, under a lock of its own: it survives a restart and a mode
switch, and two calls never pass it together. A failed call the API billed counts too.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast, get_args

import httpx

from bot.advisor import calibration, credentials, prompt
from bot.advisor.client import AdvisorError, Completion, DeepSeekClient, estimate_cost
from bot.advisor.prompt import PROMPT_VERSION, Advice, Verdict
from bot.collector import MarketSnapshot
from bot.config import AdvisorConfig, Mode
from bot.exchange.weight import weight_pressure
from bot.logging_setup import redact
from bot.paths import base_runtime_root
from bot.store import Store, advisor_usage_at

logger = logging.getLogger(__name__)

#: (store identity, symbol, strategy, timeframe, signal bar close as ISO time, prompt version, model).
AdviceKey = tuple[str, str, str, str, str, str, str]
#: Requests waiting for the worker; one more is refused, not waited for.
QUEUE_SIZE = 32
#: Verdicts older than this leave the cache: their signal bar is long gone.
CACHE_TTL = timedelta(days=2)
#: The counterfactual labels are refreshed at most this often.
RESOLVE_EVERY = timedelta(hours=1)


def advice_key(scope: str, symbol: str, strategy: str, timeframe: str, signal_close_at: str, model: str) -> AdviceKey:
    """``scope`` is the asking store's ``Store.identity``."""
    return (scope, symbol, strategy, timeframe, signal_close_at, PROMPT_VERSION, model)


def combined_usage(store: Store, day: str) -> dict[str, Any]:
    """The advisor counters of ``day`` in ``store`` plus those of every other mode's store whose
    file exists. One DeepSeek key pays for both modes, so the daily budget and the spend the panel
    shows are one. Another store that cannot be read counts nothing (logged): the budget is a
    cost guard, not a gate on trading."""
    total = store.advisor_usage(day, day)
    own = store.path.resolve()
    for mode in get_args(Mode):
        path = base_runtime_root() / mode / "bot.sqlite"
        if path.resolve() == own:
            continue
        try:
            other = advisor_usage_at(path, day, day)
        except sqlite3.Error as exc:
            logger.warning("advisor usage of the %s store unreadable: %s", mode, type(exc).__name__)
            continue
        if other is not None:
            total = {name: total[name] + other[name] for name in total}
    return total


@dataclass(frozen=True)
class AdviceState:
    """One key's verdict: ``pending`` since ``requested_at``, ``done`` with ``advice``, or
    ``failed`` with a redacted ``error`` (no key, budget spent, an API failure)."""

    status: Literal["pending", "done", "failed"]
    requested_at: datetime
    advice: Advice | None = None
    error: str | None = None
    #: The ``advice_log`` row of a ``done`` verdict.
    log_id: int | None = None
    #: The ``Store.identity`` whose ``advice_log`` holds ``log_id``.
    scope: str = ""

    def public(self) -> dict[str, Any]:
        """What a row shows and a position records (``Opportunity.advice``). ``store`` names the
        database of ``id``: the executor links only an id of its own store."""
        out: dict[str, Any] = {"status": self.status, "requested_at": self.requested_at.isoformat(), "store": self.scope}
        if self.advice is not None:
            out.update(
                verdict=self.advice.verdict,
                confidence=self.advice.confidence,
                size_factor=self.advice.size_factor,
                regime=self.advice.regime,
                risk_flags=list(self.advice.risk_flags),
                rationale=self.advice.rationale,
                id=self.log_id,
            )
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass(frozen=True)
class AdviceRequest:
    """One row to judge, with everything the worker needs and nothing it must look up later."""

    key: AdviceKey
    store: Store
    #: Writes the ``advice`` event to the ledger of the mode that asked.
    ledger: Callable[[dict[str, Any]], None]
    mode: str
    model: str
    daily_budget_usd: float
    #: ``prompt.user_data``: numbers and enums only.
    data: dict[str, Any]
    #: The signal bar's close, and the row's stop and target distances (0: no target).
    entry_price: float
    stop_pct: float
    target_pct: float
    requested_at: datetime


class Advisor:
    """``transport`` replaces the network in tests; production leaves it None."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        #: Guards the cache, the counters and the last outcome. Never held during a call.
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        #: Held from the budget read to the usage write, so two calls never pass the budget together.
        self._budget_lock = threading.Lock()
        self._queue: queue.Queue[AdviceRequest] = queue.Queue(maxsize=QUEUE_SIZE)
        self._cache: dict[AdviceKey, AdviceState] = {}
        self._outstanding = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        self._resolved_at: datetime | None = None
        self.last_ok: str | None = None
        self.last_error: str | None = None

    # -- the cycle's side: no wait, no network --------------------------------

    def configured(self) -> bool:
        return credentials.configured()

    def snapshot(self) -> dict[AdviceKey, AdviceState]:
        """A copy of the cache, for the cycle's context."""
        with self._lock:
            return dict(self._cache)

    def request(self, request: AdviceRequest) -> AdviceState | None:
        """Queue ``request`` without waiting. Returns its key's state (pending, or what is already
        known), or None when the queue is full or the advisor closed: that row has no verdict.

        A signal the request's store already logged (asked before a restart) seeds the cache from
        that row: no call and no budget. The read is one indexed row, outside the advisor's lock.
        """
        with self._lock:
            if self._closed:
                return None
            known = self._cache.get(request.key)
            if known is not None:
                return known
        logged = self._logged(request)
        with self._lock:
            if self._closed:
                return None
            known = self._cache.get(request.key)
            if known is not None:
                return known
            if logged is not None:
                self._cache[request.key] = logged
                return logged
            try:
                self._queue.put_nowait(request)
            except queue.Full:
                return None
            state = AdviceState("pending", request.requested_at, scope=request.key[0])
            self._cache[request.key] = state
            self._outstanding += 1
            self._prune(request.requested_at)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="advisor", daemon=True)
                self._thread.start()
            return state

    def maybe_resolve(self, store: Store, snapshot: MarketSnapshot, moment: datetime) -> int:
        """Label the unresolved ``advice_log`` rows (``calibration.resolve_pending``) at most once
        per ``RESOLVE_EVERY``, and not under the API weight pressure. Runs outside the cycle's lock."""
        with self._lock:
            if self._resolved_at is not None and moment - self._resolved_at < RESOLVE_EVERY:
                return 0
            if weight_pressure():
                return 0
            self._resolved_at = moment
        return calibration.resolve_pending(store, snapshot, moment)

    def status(self, settings: AdvisorConfig, store: Store, now: datetime) -> dict[str, Any]:
        """The panel's block: settings, whether a key exists, the last outcome and today's usage
        (``today`` over both modes, what the budget checks; ``today_mode`` of ``store``). Never the key."""
        today = now.date().isoformat()
        with self._lock:
            last_ok, last_error = self.last_ok, self.last_error
        return {
            **settings.model_dump(),
            "configured": self.configured(),
            "last_ok": last_ok,
            "last_error": last_error,
            "today": combined_usage(store, today),
            "today_mode": store.advisor_usage(today, today),
            "prompt_version": PROMPT_VERSION,
        }

    def wait_idle(self, timeout: float) -> bool:
        """Wait until every queued request was processed. For tests and shutdown, never the cycle."""
        with self._idle:
            return self._idle.wait_for(lambda: self._outstanding == 0, timeout)

    def close(self) -> None:
        """Stop taking requests; the worker ends after the one it is on."""
        with self._lock:
            self._closed = True

    # -- the worker's side ------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                request = self._queue.get(timeout=1.0)
            except queue.Empty:
                with self._lock:
                    if self._closed:
                        return
                continue
            try:
                self.process(request)
            except Exception as exc:  # noqa: BLE001 - one bad request never stops the worker
                logger.exception("advisor request failed")
                self._fail(request, f"falha interna ({type(exc).__name__})")
            finally:
                with self._idle:
                    self._outstanding -= 1
                    self._idle.notify_all()

    def process(self, request: AdviceRequest) -> AdviceState:
        """Ask DeepSeek about one row, within the budget, and record the verdict. The worker's
        step; tests call it directly."""
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        logged = self._logged(request)
        if logged is not None:
            with self._lock:
                self._cache[request.key] = logged
            return logged
        try:
            found = credentials.load()
        except credentials.CredentialsError as exc:
            return self._fail(request, str(exc))
        if found is None:
            return self._fail(request, "sem chave da DeepSeek (deepseek.json)")
        user = prompt.render(request.data)
        with self._budget_lock:
            spent = combined_usage(request.store, day)["cost_usd"]
            if spent >= request.daily_budget_usd:
                return self._fail(request, f"orçamento diário da IA esgotado (US$ {request.daily_budget_usd:.2f})")
            try:
                with DeepSeekClient(found.api_key, found.base_url, transport=self._transport) as client:
                    completion = client.complete(request.model, prompt.SYSTEM_PROMPT, user)
            except AdvisorError as exc:
                # An attempt the API answered is billed even when the call failed (an empty answer).
                request.store.record_advisor_usage(day, errors=1, **_billed(request.model, exc.usage, now))
                return self._fail(request, str(exc))
            advice = prompt.parse_advice(completion.content)
            billed = _billed(request.model, completion, now)
            cost = billed["cost_usd"]
            request.store.record_advisor_usage(day, calls=1, **billed, **{advice.verdict: 1})
        scope, symbol, strategy, timeframe, signal_close_at, version, model = request.key
        digest = prompt.prompt_sha256(user)
        log_id = request.store.add_advice(
            {
                "ts": now.isoformat(),
                "mode": request.mode,
                "model": model,
                "prompt_version": version,
                "prompt_sha256": digest,
                "symbol": symbol,
                "strategy": strategy,
                "timeframe": timeframe,
                "signal_close_at": signal_close_at,
                "verdict": advice.verdict,
                "confidence": advice.confidence,
                "size_factor": advice.size_factor,
                "regime": advice.regime,
                "risk_flags": list(advice.risk_flags),
                "rationale": advice.rationale,
                "prompt_tokens": completion.prompt_tokens,
                "cache_hit_tokens": completion.cache_hit_tokens,
                "completion_tokens": completion.completion_tokens,
                "cost_usd": cost,
                "latency_ms": completion.latency_ms,
                "entry_price": request.entry_price,
                "stop_pct": request.stop_pct,
                "target_pct": request.target_pct,
            }
        )
        try:
            request.ledger(
                {
                    "advice_id": log_id,
                    "advisor_mode": request.mode,
                    "symbol": symbol,
                    "strategy": strategy,
                    "timeframe": timeframe,
                    "signal_close_at": signal_close_at,
                    "model": model,
                    "prompt_version": version,
                    "prompt_sha256": digest,
                    "verdict": advice.verdict,
                    "confidence": advice.confidence,
                    "size_factor": advice.size_factor,
                    "cost_usd": cost,
                }
            )
        except Exception:  # noqa: BLE001 - the verdict is in the store; the log says the ledger failed
            logger.exception("advice ledger append failed")
        state = AdviceState("done", request.requested_at, advice, log_id=log_id, scope=scope)
        with self._lock:
            self._cache[request.key] = state
            self.last_ok = now.isoformat()
        return state

    def _logged(self, request: AdviceRequest) -> AdviceState | None:
        """The verdict the request's store already logged for its signal, as a ``done`` state; None
        when there is none or the read failed (the request then goes on as new)."""
        scope, *signal = request.key
        try:
            row = request.store.advice_for(*signal)
        except sqlite3.Error:
            logger.exception("advice_log read failed")
            return None
        if row is None:
            return None
        advice = Advice(
            cast(Verdict, str(row["verdict"])),
            float(row["confidence"]),
            float(row["size_factor"]),
            row["regime"],
            tuple(str(flag) for flag in row["risk_flags"]),
            str(row["rationale"] or ""),
        )
        return AdviceState("done", _utc(str(row["ts"])), advice, log_id=int(row["id"]), scope=scope)

    def _fail(self, request: AdviceRequest, message: str) -> AdviceState:
        """Record a failure for this key: the row goes on without a verdict (or waits, with
        ``require_verdict``). The text is redacted: an error may echo a URL or a header."""
        clean = redact(message)
        state = AdviceState("failed", request.requested_at, error=clean, scope=request.key[0])
        with self._lock:
            self._cache[request.key] = state
            self.last_error = f"{datetime.now(timezone.utc).isoformat()} {clean}"
        logger.warning("advisor: %s", clean)
        return state

    def _prune(self, now: datetime) -> None:
        """Drop verdicts whose request is older than ``CACHE_TTL``. Caller holds ``_lock``."""
        cutoff = now - CACHE_TTL
        for key in [key for key, state in self._cache.items() if state.requested_at < cutoff]:
            del self._cache[key]


def _billed(model: str, usage: Completion | None, moment: datetime) -> dict[str, float]:
    """The usage counters of the billed attempts: tokens and the estimated cost (nothing without
    a reported usage, as a timeout before any answer)."""
    if usage is None:
        return {}
    return {
        "prompt_tokens": usage.prompt_tokens,
        "cache_hit_tokens": usage.cache_hit_tokens,
        "completion_tokens": usage.completion_tokens,
        "cost_usd": estimate_cost(model, usage, moment),
    }


def _utc(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

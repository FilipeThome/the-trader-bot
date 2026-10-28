"""Background validation jobs: the backtest gate, its trials and the re-validation of stale results.

The routes in ``bot.api.app`` start these jobs; this module never imports the app. Threads
are started with ``_run_validation`` looked up on this module, so one monkeypatch covers
the validate route and the queue alike.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.api.state import ctx
from bot.backtest.loaders.binance_loader import WeightPressure
from bot.backtest.runner import (
    VALIDATABLE,
    BacktestResult,
    strategy_record,
    timeframe_record,
    validate_strategy,
    validation_days,
)
from bot.config import BotConfig
from bot.logging_setup import redact
from bot.reports import append_ledger
from bot.runtime import Runtime, mode_root
from bot.store import Store
from bot.strategies import (
    DAY_STRATEGIES,
    GATE_VERSION,
    STRATEGIES,
    STRATEGY_VERSIONS,
    exit_policy_for,
    pairset_hash,
    timeframe_result,
    uses_hysteresis,
    validated_for,
)

logger = logging.getLogger(__name__)
#: The market filter reads BTC's 4h bars; its EMA of 240 bars needs about 40 days before the span.
MARKET_SYMBOL = "BTC/USDT"
MARKET_WARMUP_DAYS = 40
#: Extra days of 1h bars that warm a day strategy's EMA 50 trend filter.
HOURLY_WARMUP_DAYS = 5
#: A pair-set change re-validates a strategy at most once per this period (D12).
PAIRSET_REVALIDATE_EVERY = timedelta(hours=24)
#: An automatic re-validation that failed is tried again after this, not on every loop step.
REVALIDATE_RETRY = timedelta(hours=1)


def _utc(value: Any) -> datetime:
    """A stored ISO stamp as an aware datetime; one written without an offset is UTC."""
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _claim_validation(config: BotConfig, strategy_id: str, stage: str) -> dict[str, Any] | None:
    """Register a validation job, or None when one of this strategy is already running."""
    key = (config.mode, strategy_id)
    with ctx.validation_lock:
        running = ctx.validations.get(key)
        if running is not None and running["status"] == "running":
            return None
        job: dict[str, Any] = {
            "status": "running",
            "stage": stage,
            "done": 0,
            # One download per pair and operated timeframe.
            "total": len(config.symbols) * len(STRATEGIES[strategy_id].timeframes),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        ctx.validations[key] = job
        return dict(job)


def _stale_cause(params: dict[str, Any] | None, strategy_id: str, config: BotConfig) -> str | None:
    """Why the stored validation does not hold for ``config``; None while it does.

    "new": never validated. "gate": an older gate or strategy version. "rules": another
    exit rule, market filter or exit hysteresis, or an operated timeframe it did not
    replay. "pairs": another pair set (D6).
    """
    if not params:
        return "new"
    if params.get("gate") != GATE_VERSION or params.get("version") != STRATEGY_VERSIONS.get(strategy_id):
        return "gate"
    if not validated_for(params, strategy_id, config.exit_policy, market_filter=config.market_filter, hysteresis=config.exit_hysteresis):
        return "rules"
    if any(timeframe_result(params, timeframe) is None for timeframe in STRATEGIES[strategy_id].timeframes):
        return "rules"
    if params.get("pairset") != pairset_hash(config.symbols):
        return "pairs"
    return None


def _auto_validate(config: BotConfig, store: Store) -> list[str]:
    """Validate, one after another, each enabled strategy without a result for the current rules.

    Changing the take-profit and stop-loss, the market filter or the hysteresis, or
    turning a strategy on, then needs no click: the gate runs by itself and nothing
    trades until it passes. A strategy that already has a result for these rules,
    passed or not, is not re-run (a changed pair set waits for ``revalidate_stale``);
    one running is left alone.
    """
    stored = {row["strategy_id"]: row["params"] for row in store.strategies()}
    wanted = [
        strategy_id
        for strategy_id in VALIDATABLE
        if config.strategies.get(strategy_id) and _stale_cause(stored.get(strategy_id), strategy_id, config) in ("new", "gate", "rules")
    ]
    return _queue_validations(config, store, wanted)


def revalidate_stale(runtime: Runtime, *, startup: bool = False, now: datetime | None = None) -> list[str]:
    """Queue a validation of each enabled strategy whose stored result went stale (D12); returns them.

    A new gate or strategy version, or another exit rule, market filter, hysteresis or
    operated timeframe, re-validates at once; a changed pair set at most once per
    ``PAIRSET_REVALIDATE_EVERY`` since the strategy's last start (``revalidate_at:<id>``).
    A strategy never validated is left to the operator and to config changes
    (``_auto_validate``); a failed run waits ``REVALIDATE_RETRY``. Called at startup
    and by the process loop after each tick, so it only reads the store when nothing is stale.
    """
    config, store = runtime.config, runtime.store
    moment = now or datetime.now(timezone.utc)
    stored = {row["strategy_id"]: row["params"] for row in store.strategies()}
    causes: dict[str, str] = {}
    for strategy_id in VALIDATABLE:
        if not config.strategies.get(strategy_id):
            continue
        cause = _stale_cause(stored.get(strategy_id), strategy_id, config)
        if cause in (None, "new") or _failed_recently(config.mode, strategy_id, moment):
            continue
        if cause == "pairs" and _started_within(store, strategy_id, moment, PAIRSET_REVALIDATE_EVERY):
            continue
        causes[strategy_id] = str(cause)
    queued = _queue_validations(config, store, list(causes), now=moment)
    for stale in sorted({causes[strategy_id] for strategy_id in queued}):
        strategies = [strategy_id for strategy_id in queued if causes[strategy_id] == stale]
        runtime._ledger("revalidate", {"strategies": strategies, "cause": "startup" if startup else stale, "stale": stale})
    return queued


def _failed_recently(mode: str, strategy_id: str, moment: datetime) -> bool:
    with ctx.validation_lock:
        job = ctx.validations.get((mode, strategy_id))
        finished = job.get("finished_at") if job is not None and job.get("status") == "error" else None
    return finished is not None and _utc(finished) > moment - REVALIDATE_RETRY


def _started_within(store: Store, strategy_id: str, moment: datetime, period: timedelta) -> bool:
    stamp = store.get_state(f"revalidate_at:{strategy_id}")
    try:
        return stamp is not None and _utc(stamp) > moment - period
    except (TypeError, ValueError):
        return False


def _mark_started(store: Store, strategy_ids: list[str], moment: datetime) -> None:
    """Record when each validation started: the pair-set rate limit counts from it."""
    for strategy_id in strategy_ids:
        store.set_state(f"revalidate_at:{strategy_id}", moment.isoformat())


def _queue_validations(config: BotConfig, store: Store, strategy_ids: list[str], *, now: datetime | None = None) -> list[str]:
    """Claim a job for each strategy not already running and validate them one after another.

    A single worker keeps the history downloads off each other.
    """
    queued = [strategy_id for strategy_id in strategy_ids if _claim_validation(config, strategy_id, "na fila") is not None]
    if not queued:
        return []
    _mark_started(store, queued, now or datetime.now(timezone.utc))
    snapshot = config.model_copy(deep=True)
    root = mode_root(snapshot)

    def work() -> None:
        for strategy_id in queued:
            key = (snapshot.mode, strategy_id)
            _update_job(key, stage="baixando")
            _run_validation(key, strategy_id, snapshot, root)

    threading.Thread(target=work, name="validate-auto", daemon=True).start()
    return queued


def _validation_jobs(mode: str) -> dict[str, dict[str, Any]]:
    with ctx.validation_lock:
        return {strategy: dict(job) for (job_mode, strategy), job in ctx.validations.items() if job_mode == mode}


def _update_job(key: tuple[str, str], **fields: Any) -> None:
    with ctx.validation_lock:
        ctx.validations[key].update(fields)


class _HistoryMissing(RuntimeError):
    """History the validation cannot run without; the message is the operator's."""


def _run_validation(key: tuple[str, str], strategy_id: str, config: BotConfig, root: Any) -> None:
    """Validate ``strategy_id`` on every timeframe it trades (D7) over the config's pair set (D6).

    Each timeframe replays ``validation_days`` of history (D11). A day strategy also
    reads 1h bars for its trend filter, and the market filter BTC's 4h bars with the
    warm-up of its EMA. Each timeframe is one trial (D9), logged with ``add_trial``.
    The record keeps one result per timeframe (``strategy_record``).
    """
    from bot.backtest.loaders.registry import resolve_loader

    began = time.monotonic()
    symbols = list(config.symbols)
    spans = {timeframe: validation_days(strategy_id, timeframe) for timeframe in STRATEGIES[strategy_id].timeframes}
    longest = max(spans.values())
    end = datetime.now(timezone.utc)
    try:
        loader = resolve_loader("crypto")
        frames: dict[str, dict[str, Any]] = {}
        for index, (timeframe, days) in enumerate(spans.items()):
            frames[timeframe] = loader.fetch(
                symbols,
                (end - timedelta(days=days)).isoformat(),
                end.isoformat(),
                interval=timeframe,
                on_symbol=_progress(key, index * len(symbols)),
            )
        hourly = None
        if strategy_id in DAY_STRATEGIES:
            # The 1h trend filter replays on real 1h bars, with a few extra days to warm EMA 50.
            start = end - timedelta(days=longest + HOURLY_WARMUP_DAYS)
            hourly = loader.fetch(symbols, start.isoformat(), end.isoformat(), interval="1h")
        market = None
        if config.market_filter != "off":
            start = end - timedelta(days=longest + MARKET_WARMUP_DAYS)
            market = loader.fetch([MARKET_SYMBOL], start.isoformat(), end.isoformat(), interval="4h").get(MARKET_SYMBOL)
            if market is None or market.empty:
                # Without BTC the replay would run unfiltered under the filter's key.
                raise _HistoryMissing("histórico do BTC indisponível para o filtro de mercado")
        _update_job(key, stage="calculando")
        store = Store(root / "bot.sqlite")
        try:
            results = _replay_timeframes(store, strategy_id, frames, config, hourly=hourly, market=market)
            elapsed = round(time.monotonic() - began, 1)
            record = strategy_record(
                results,
                symbols=symbols,
                exit_policy=config.exit_policy,
                market_filter=config.market_filter,
                hysteresis=config.exit_hysteresis,
                extra={"days": spans, "elapsed_s": elapsed},
            )
            store.save_strategy(record)
        finally:
            store.close()
        for result in results.values():
            try:
                append_ledger({"kind": "validate", "mode": config.mode, **result.to_dict()}, path=root / "bot-ledger.jsonl")
            except Exception:
                logger.exception("ledger append for validation failed")
        _update_job(
            key,
            status="done",
            stage="pronto",
            result=_job_result(record, results),
            elapsed_s=elapsed,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except (WeightPressure, _HistoryMissing) as exc:
        _update_job(key, status="error", error=redact(exc), finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # noqa: BLE001 - a failed job is reported in /bot/status and the log
        logger.exception("validation of %s failed", strategy_id)
        _update_job(
            key,
            status="error",
            error=f"histórico da Binance indisponível ({type(exc).__name__})",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def _progress(key: tuple[str, str], offset: int) -> Callable[[str, int], None]:
    """The loader's per-pair callback: downloads of earlier timeframes count first."""

    def report(_symbol: str, done: int) -> None:
        _update_job(key, done=offset + done)

    return report


def _replay_timeframes(
    store: Store,
    strategy_id: str,
    frames: dict[str, dict[str, Any]],
    config: BotConfig,
    *,
    hourly: dict[str, Any] | None,
    market: Any,
) -> dict[str, BacktestResult]:
    """Run the gate on each timeframe and log each run as a trial.

    The deflated Sharpe's N is ``_trial_count`` of the trials since the strategy's last
    pass, the earlier timeframes of this run included, plus the current combination; the
    variance uses those trials' per-trade Sharpe.
    """
    seen = store.trials_since_pass(strategy_id)
    results: dict[str, BacktestResult] = {}
    for timeframe, by_symbol in frames.items():
        combination = _config_combination(strategy_id, timeframe, config)
        _timeframe, policy, pairset, _market_filter, hysteresis = combination
        result = validate_strategy(
            strategy_id,
            by_symbol,
            timeframe,
            exit_policy=config.exit_policy,
            hourly_frames=hourly,
            hysteresis=config.exit_hysteresis,
            market_frame=market,
            trials=_trial_count(seen, [combination]),
            trial_sharpes=[float(trial["sharpe_trade"]) for trial in seen],
        )
        sharpe = float(result.sharpe_trade)
        trial = {
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "exit_policy": policy,
            "pairset": pairset,
            "market_filter": config.market_filter,
            "hysteresis": hysteresis,
            "sharpe_trade": sharpe if math.isfinite(sharpe) else 0.0,
            "trades": result.trades,
            "passed": result.passed,
            "payload": timeframe_record(result),
        }
        store.add_trial(trial)
        seen.append(trial)
        results[timeframe] = result
    return results


_Combination = tuple[str, str, str, str, bool]


def _combination(trial: dict[str, Any]) -> _Combination:
    """A logged trial's (timeframe, exit rule, pair set, market filter, hysteresis)."""
    return (
        str(trial["timeframe"]),
        str(trial["exit_policy"]),
        str(trial["pairset"]),
        str(trial["market_filter"]),
        bool(trial["hysteresis"]),
    )


def _config_combination(strategy_id: str, timeframe: str, config: BotConfig) -> _Combination:
    """The combination a validation of ``strategy_id`` on ``timeframe`` under ``config`` logs."""
    policy = exit_policy_for(strategy_id, config.exit_policy)
    hysteresis = bool(config.exit_hysteresis) and uses_hysteresis(strategy_id, policy)
    return (timeframe, policy, pairset_hash(config.symbols), config.market_filter, hysteresis)


def _config_combinations(strategy_id: str, config: BotConfig) -> list[_Combination]:
    """One combination per timeframe the strategy operates, as its next validation runs them."""
    return [_config_combination(strategy_id, timeframe, config) for timeframe in STRATEGIES[strategy_id].timeframes]


def _trial_count(seen: list[dict[str, Any]], current: list[_Combination]) -> int:
    """The deflated Sharpe's N (D9): the distinct combinations among ``seen`` (the trials since
    the last pass) and ``current``.

    The validation passes the timeframe it is about to replay; ``/bot/status`` passes every
    operated timeframe of the config, the N the next validation reaches on its last one.
    """
    return len({_combination(trial) for trial in seen} | set(current))


def _job_result(record: dict[str, Any], results: dict[str, BacktestResult]) -> dict[str, Any]:
    """The job's summary: the verdict and pooled counts, the headline timeframe's metrics
    (the record's choice: the best passed daily Sharpe) and every timeframe's result."""
    passed = [result for result in results.values() if result.passed]
    best = max(passed or list(results.values()), key=lambda result: result.daily_sharpe)
    params = record["params"]
    return {
        "passed": not record["signal_only"],
        "exit_policy": best.exit_policy,
        "timeframe": best.timeframe,
        "sharpe": best.sharpe,
        "daily_sharpe": best.daily_sharpe,
        "dsr": best.dsr,
        "trials": best.trials,
        "max_drawdown": best.max_drawdown,
        "trades": params["trades"],
        "wins": params["wins"],
        "reason": params["reason"],
        "timeframes": params["timeframes"],
    }

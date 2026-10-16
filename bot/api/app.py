"""FastAPI application. Every ``/bot`` route passes ``bot.api.auth`` first."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.api.auth import load_or_create_token, refusal
from bot.api.hub import EventHub
from bot.api.state import BotContext
from bot.backtest.loaders.binance_loader import WeightPressure
from bot.backtest.runner import VALIDATABLE, promotion_record, shutdown_pool, validate_strategy
from bot.collector import Collector
from bot.config import DEFAULT_SYMBOLS, MAX_SYMBOLS, BotConfig
from bot.evaluator import Opportunity
from bot.exchange import service
from bot.exchange.weight import weight_pressure
from bot.features import build_features, ensure_chart_data
from bot.live.halt import clear_halt
from bot.mandate_io import commit_mandate_for, mandate_is_restricted
from bot.markets import CHART_LEAD, chart_interval, merge_symbols, top_liquid_usdt, tradable_symbols, venue_klines, venue_symbols
from bot.paths import pin_mode_root
from bot.promotion import switch_to_production
from bot.recommend import recommendations
from bot.reports import append_ledger, build_daily_report, verify_ledger
from bot.runtime import Runtime, mode_root, profile_for
from bot.store import Store
from bot.strategies import STRATEGIES

logger = logging.getLogger(__name__)
hub = EventHub()
ctx = BotContext()
#: History each validation timeframe replays: about 4,300 to 5,800 candles.
_HISTORY_DAYS = {"15m": 60, "1h": 180, "4h": 365}
#: Liquid pairs read for the "top" preset; the venue filter keeps the first ``count`` it can trade.
_TOP_POOL = 60
#: Single flight for the recommendations scan.
_RECOMMEND_LOCK = threading.Lock()
#: Seconds a trade chart's candles are served from memory; the panel polls every 5 s.
_CHART_TTL_SECONDS = 5.0
#: Minutes of chart kept after a trade closed.
_CHART_TAIL = timedelta(minutes=15)
_TRADE_FIELDS = (
    "id", "symbol", "strategy", "side", "status", "qty", "avg_price", "stop_price", "target_price",
    "peak_price", "exit_policy", "opened_at", "closed_at", "exit_price", "realized_pnl", "reason", "test_only",
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _startup()
    yield
    shutdown_pool()


app = FastAPI(title="The Trader Bot", version="1.0.0", lifespan=_lifespan)


@app.middleware("http")
async def _guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if request.url.path.startswith("/bot"):
        headers = {key.lower(): value for key, value in request.headers.items()}
        denied = refusal(request.method.upper(), headers, ctx.token)
        if denied is not None:
            return JSONResponse(status_code=denied[0], content={"detail": denied[1]})
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with where and why, never the rejected value: Infinity or NaN cannot be encoded, and a value may be a secret."""
    errors = [
        {"loc": list(error.get("loc", ())), "msg": str(error.get("msg", "")), "type": str(error.get("type", ""))}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: str | None = None
    confirmation: str | None = None
    allocated_capital_usd: float | None = None
    symbols: list[str] | None = None
    strategies: dict[str, bool] | None = None
    flatten_on_halt: bool | None = None
    limits: dict[str, float | int] | None = None
    entry_mode: str | None = None
    min_win_rate: float | None = None
    trailing_stop: bool | None = None
    testnet_test_mode: bool | None = None
    auto_select: bool | None = None
    auto_select_count: int | None = None
    min_profit_usd: float | None = None
    reconfirm_mandate: bool = False


class ManualOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    strategy: str


class SymbolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: Literal["top", "default"]
    count: int = Field(default=MAX_SYMBOLS, ge=5, le=MAX_SYMBOLS)


class PositionClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: int


def _bind(config: BotConfig) -> Runtime:
    """Point the process at ``config.mode`` and rebuild the runtime. Caller holds ``ctx.lock``."""
    pin_mode_root(mode_root(config))
    if ctx.collector is None:
        ctx.collector = Collector()
    if ctx.runtime is not None:
        # A tick of the old runtime still reading Binance stops before it acts.
        ctx.runtime.retired = True
    store = ctx.store_for(config)
    runtime = Runtime(config, store, ctx.collector, publish=hub.publish, lock=ctx.lock)
    ctx.runtime = runtime
    ctx.config = config
    return runtime


def _startup() -> None:
    ctx.token = load_or_create_token()
    with ctx.lock:
        _bind(BotConfig.load())
    # Appends verify only the active segment; the whole history is checked once per start.
    threading.Thread(target=_verify_ledger_history, name="verify-ledger", daemon=True).start()


def _verify_ledger_history() -> None:
    try:
        result = verify_ledger()
    except Exception:  # noqa: BLE001 - a failed check is logged, the bot keeps running
        logger.exception("ledger history check failed")
        return
    if not result["ok"]:
        logger.error("ledger history is broken: %s", result["break"])


def _runtime() -> Runtime:
    # Reads need no lock (the reference is replaced atomically); only a first bind takes it,
    # so a long cycle never stalls the panel's reads.
    runtime = ctx.runtime
    if runtime is not None:
        return runtime
    with ctx.lock:
        if ctx.runtime is None:
            _bind(BotConfig.load())
        if ctx.runtime is None:
            raise RuntimeError("runtime was not bound")
        return ctx.runtime


def _listed(collector: Collector, symbol: str) -> bool:
    try:
        return any(row["symbol"] == symbol for row in tradable_symbols(collector))
    except Exception:  # noqa: BLE001 - an unreadable catalog lists nothing
        return False


def _validation_detail(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]


@app.get("/bot/status")
def status() -> dict[str, Any]:
    runtime = _runtime()
    store = runtime.store
    days = store.pnl_days()
    today = days[-1] if days else None
    return {
        "running": runtime.running,
        "halted": runtime.halted,
        "halt_reason": store.get_state("halt_reason"),
        "journal": service.journal_status(),
        "mode": runtime.config.mode,
        "profile": profile_for(runtime.config),
        "capital_usd": runtime.config.capital_for_sizing(),
        "allocated_capital_usd": runtime.config.allocated_capital_usd,
        "symbols": runtime.config.symbols,
        "strategies": runtime.config.strategies,
        "validated": {
            row["strategy_id"]: not row["signal_only"] and row["params"].get("exit_policy") == runtime.config.exit_policy
            for row in store.strategies()
        },
        "validations": _validation_jobs(runtime.config.mode),
        "testnet_test_mode": runtime.config.testnet_test_mode,
        "limits": runtime.config.limits.model_dump(),
        "flatten_on_halt": runtime.config.flatten_on_halt,
        "entry_mode": runtime.config.entry_mode,
        "min_win_rate": runtime.config.min_win_rate,
        "min_win_rate_sample": runtime.config.min_win_rate_sample,
        "trailing_stop": runtime.config.trailing_stop,
        "auto_select": runtime.config.auto_select,
        "auto_select_count": runtime.config.auto_select_count,
        "auto_select_at": store.get_state("auto_select_at"),
        "min_profit_usd": runtime.config.min_profit_usd,
        "gates": runtime.config.gates,
        "day": today,
        "violations_avoided": int(today["violations_avoided"]) if today else 0,
        "net_pnl": float(today["net_pnl"]) if today else 0.0,
        "trades": int(today["trades"]) if today else 0,
    }


@app.get("/bot/opportunities")
def opportunities() -> dict[str, Any]:
    return {"rows": _runtime().store.opportunities()}


@app.get("/bot/positions")
def positions() -> dict[str, Any]:
    return {"rows": _runtime().store.open_positions()}


@app.get("/bot/performance")
def performance() -> dict[str, Any]:
    store = _runtime().store
    return {
        "days": store.pnl_days(),
        "strategies": store.strategies(),
        "equity_peak": store.peak_equity(),
        "calibration": store.calibration(),
    }


_BALANCE_TTL_SECONDS = 20
_BALANCE_ERRORS = {
    "credentials_missing": "chaves da Binance ausentes para este ambiente",
    "config_invalid": "binance.json ilegível",
    "balance_unavailable": "a Binance não respondeu ao pedido de saldo",
}


@app.get("/bot/balance")
def balance() -> dict[str, Any]:
    """Balances of the active environment's Binance account. Cached for a few seconds."""
    runtime = _runtime()
    profile_id = profile_for(runtime.config)
    now = datetime.now(timezone.utc)
    cached = ctx.balance_cache
    if cached is not None and cached[0] == profile_id and (now - cached[1]).total_seconds() < _BALANCE_TTL_SECONDS:
        return cached[2]
    result = service.account_balances(profile_id)
    if result.get("status") != "ok":
        reason = str(result.get("error") or "")
        raise HTTPException(status_code=503, detail=_BALANCE_ERRORS.get(reason, "saldo indisponível"))
    body = {**result, "mode": runtime.config.mode, "updated_at": now.isoformat()}
    ctx.balance_cache = (profile_id, now, body)
    return body


@app.get("/bot/markets")
def markets() -> dict[str, Any]:
    try:
        rows = tradable_symbols(_runtime().collector)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="lista da Binance indisponível") from exc
    return {"symbols": rows}


@app.get("/bot/recommendations")
def recommendation_rows() -> dict[str, Any]:
    runtime = _runtime()
    # One ranking at a time: parallel panel requests would each scan Binance and spend weight.
    # The loads it makes merge under the collector's own lock, never across the network.
    with _RECOMMEND_LOCK:
        try:
            rows = recommendations(runtime.collector, min_volume=runtime.config.min_quote_volume_usd)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="recomendações indisponíveis") from exc
    return {"rows": rows}


@app.get("/bot/features")
def features(symbol: str = "BTC/USDT") -> dict[str, Any]:
    runtime = _runtime()
    collector = runtime.collector
    if symbol not in runtime.config.symbols and not _listed(collector, symbol):
        raise HTTPException(status_code=404, detail="par fora da Binance")
    # The reads run outside every lock and merge under the collector's; the chart is built
    # from a copy, so it never waits on Binance for the cycle and never races its merge.
    try:
        ensure_chart_data(collector, symbol)
    except Exception as exc:
        logger.warning("chart data for %s failed: %s", symbol, type(exc).__name__)
    snapshot = collector.view()
    if (symbol, "1h") not in snapshot.candles or (symbol, "15m") not in snapshot.candles:
        raise HTTPException(status_code=503, detail="mercado indisponível")
    return build_features(snapshot, symbol, funding_threshold=runtime.config.funding_annualized_threshold)


@app.get("/bot/history")
def history() -> dict[str, Any]:
    return {"rows": _runtime().store.decisions()}


@app.post("/bot/start")
def start() -> dict[str, Any]:
    result = _runtime().start()
    if result.get("status") != "ok":
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/bot/stop")
def stop() -> dict[str, Any]:
    return _runtime().stop()


@app.post("/bot/halt")
def halt() -> dict[str, Any]:
    runtime = _runtime()
    return runtime.halt("Parar tudo", flatten=runtime.config.flatten_on_halt, by="frontend")


@app.post("/bot/resume")
def resume() -> dict[str, Any]:
    """Manual restart after a halt. The cycle never calls this."""
    with ctx.lock:
        clear_halt("binance")
        clear_halt(None)
        runtime = _runtime()
        runtime.store.set_state("running", False)
        runtime.store.set_state("halt_reason", None)
        runtime.failures = 0
        runtime._ledger("resume", {"by": "operator"})
        return {"status": "ok", "halted": runtime.halted}


@app.post("/bot/opportunities/execute")
def execute_manual(body: ManualOrder) -> dict[str, Any]:
    runtime = _runtime()
    match = next(
        (
            row
            for row in runtime.store.opportunities()
            if row["symbol"] == body.symbol and row["strategy"] == body.strategy
        ),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="oportunidade não encontrada")
    opportunity = Opportunity(**{key: match[key] for key in Opportunity.__dataclass_fields__ if key in match})
    return {"status": "ok", **runtime.execute_manual(opportunity)}


@app.post("/bot/opportunities/ignore")
def ignore(body: ManualOrder) -> dict[str, Any]:
    until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    _runtime().store.ignore(body.symbol, body.strategy, until)
    return {"status": "ok", "until": until}


@app.post("/bot/positions/close")
def close_position(body: PositionClose) -> dict[str, Any]:
    runtime = _runtime()
    position = runtime.store.position(body.position_id)
    if position is None or position["status"] != "open":
        raise HTTPException(status_code=404, detail="posição aberta não encontrada")
    return {"status": "ok", "result": runtime.close_position(body.position_id)}


@app.get("/bot/trades/{position_id}")
def trade(position_id: int) -> dict[str, Any]:
    """One position with the candles of its venue from 30 minutes before the entry, for the live chart."""
    runtime = _runtime()
    position = runtime.store.position(position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="trade não encontrado")
    now = datetime.now(timezone.utc)
    opened = _utc(position["opened_at"])
    closed = _utc(position["closed_at"]) if position.get("closed_at") else None
    start = opened - CHART_LEAD
    end = min(now, closed + _CHART_TAIL) if closed else now
    interval, candles = _chart_candles(runtime.config.mode, position["symbol"], start, end)
    last = candles[-1]["c"] if candles else None
    avg = float(position.get("avg_price") or 0)
    qty = float(position.get("qty") or 0)
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    if position["status"] == "open" and avg > 0 and last is not None:
        pnl_usd = (last - avg) * qty
        pnl_pct = last / avg - 1
    elif position["status"] == "closed" and avg > 0:
        pnl_usd = float(position.get("realized_pnl") or 0)
        exit_price = float(position.get("exit_price") or 0)
        pnl_pct = exit_price / avg - 1 if exit_price > 0 else None
    return {
        "position": {field: position.get(field) for field in _TRADE_FIELDS},
        "interval": interval,
        "candles": candles,
        "last_price": last,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "updated_at": now.isoformat(),
    }


def _utc(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _chart_candles(mode: str, symbol: str, start: datetime, end: datetime) -> tuple[str, list[dict[str, Any]]]:
    """Candles from a 5-second cache. Under weight pressure a stale copy is served, or 503.

    Stops and exits need the request weight more than a chart does.
    """
    interval, _count = chart_interval(start, end)
    key = (mode, symbol, interval, int(start.timestamp()))
    with ctx.chart_lock:
        cached = ctx.chart_cache.get(key)
    fresh = cached is not None and time.monotonic() - cached[0] < _CHART_TTL_SECONDS
    # Testnet reads have their own limit; only live reads compete with live stops and exits.
    pressure = mode != "testnet" and weight_pressure()
    if cached is not None and (fresh or pressure):
        return interval, cached[1]
    if pressure:
        raise HTTPException(status_code=503, detail="peso da API alto: o gráfico volta em instantes")
    collector = _runtime().collector
    try:
        interval, candles = venue_klines(collector, symbol, mode, start=start, end=end)
    except Exception as exc:
        if cached is not None:
            return interval, cached[1]
        raise HTTPException(status_code=503, detail="velas da Binance indisponíveis") from exc
    now = time.monotonic()
    with ctx.chart_lock:
        # Charts of trades nobody is watching any more leave the cache after a minute.
        for old in [item for item, (read_at, _rows) in ctx.chart_cache.items() if now - read_at > 60]:
            del ctx.chart_cache[old]
        ctx.chart_cache[key] = (now, candles)
    return interval, candles


@app.post("/bot/strategies/{strategy_id}/validate", status_code=202)
def validate(strategy_id: str) -> dict[str, Any]:
    """Start the backtest gate in the background; ``/bot/status`` reports its progress.

    The job captures the config and the mode's directory when it starts, so a
    mode switch while it runs neither moves its result nor loses its state.
    """
    if strategy_id not in VALIDATABLE:
        raise HTTPException(status_code=422, detail="só trend, mean_reversion e momentum têm teste histórico")
    config = _runtime().config.model_copy(deep=True)
    key = (config.mode, strategy_id)
    with ctx.validation_lock:
        running = ctx.validations.get(key)
        if running is not None and running["status"] == "running":
            raise HTTPException(status_code=409, detail="essa validação já está rodando")
        job: dict[str, Any] = {
            "status": "running",
            "stage": "baixando",
            "done": 0,
            "total": len(config.symbols),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        ctx.validations[key] = job
    threading.Thread(
        target=_run_validation, args=(key, strategy_id, config, mode_root(config)), name=f"validate-{strategy_id}", daemon=True
    ).start()
    return dict(job)


def _validation_jobs(mode: str) -> dict[str, dict[str, Any]]:
    with ctx.validation_lock:
        return {strategy: dict(job) for (job_mode, strategy), job in ctx.validations.items() if job_mode == mode}


def _update_job(key: tuple[str, str], **fields: Any) -> None:
    with ctx.validation_lock:
        ctx.validations[key].update(fields)


def _run_validation(key: tuple[str, str], strategy_id: str, config: BotConfig, root: Any) -> None:
    from bot.backtest.loaders.registry import resolve_loader

    timeframe = STRATEGIES[strategy_id].timeframes[0]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=_HISTORY_DAYS.get(timeframe, 180))
    try:
        frames = resolve_loader("crypto").fetch(
            list(config.symbols),
            start.isoformat(),
            end.isoformat(),
            interval=timeframe,
            on_symbol=lambda _symbol, done: _update_job(key, done=done),
        )
        _update_job(key, stage="calculando")
        result = validate_strategy(strategy_id, frames, timeframe, exit_policy=config.exit_policy)
        store = Store(root / "bot.sqlite")
        try:
            store.save_strategy(
                promotion_record(result, {"timeframe": timeframe, "symbols": list(config.symbols), "days": (end - start).days})
            )
        finally:
            store.close()
        try:
            append_ledger({"kind": "validate", "mode": config.mode, **result.to_dict()}, path=root / "bot-ledger.jsonl")
        except Exception:
            logger.exception("ledger append for validation failed")
        _update_job(key, status="done", stage="pronto", result=result.to_dict(), finished_at=datetime.now(timezone.utc).isoformat())
    except WeightPressure as exc:
        _update_job(key, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a failed job is reported in /bot/status and the log
        logger.exception("validation of %s failed", strategy_id)
        _update_job(key, status="error", error=f"histórico da Binance indisponível ({type(exc).__name__})")


@app.post("/bot/symbols")
def choose_symbols(body: SymbolChoice) -> dict[str, Any]:
    """Analyse the most liquid USDT pairs, or the default five.

    Pairs with a live position always stay, so their prices, stop and exits
    keep being followed. ``left_out`` names the pairs the preset asked for that
    the order venue cannot trade (on the testnet: too few trades for a fresh
    quote), so the panel can say why the list is shorter.
    """
    runtime = _runtime()
    mode = runtime.config.mode
    if runtime.config.auto_select:
        raise HTTPException(status_code=409, detail="a seleção automática escolhe os pares; desligue-a antes")
    # Binance reads first, without the lock: the cycle and other clicks are not held on them.
    try:
        listed = venue_symbols(runtime.collector, mode)
        wanted = top_liquid_usdt(runtime.collector, _TOP_POOL) if body.preset == "top" else list(DEFAULT_SYMBOLS)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="lista da Binance indisponível") from exc
    tradable = [symbol for symbol in wanted if listed is None or symbol in listed]
    chosen = tradable[: body.count] if body.preset == "top" else tradable
    asked = wanted[: body.count] if body.preset == "top" else wanted
    left_out = [symbol for symbol in asked if symbol not in chosen]
    with ctx.lock:
        runtime = _runtime()
        if runtime.config.mode != mode or runtime.config.auto_select:
            raise HTTPException(status_code=409, detail="a configuração mudou durante a escolha; tente de novo")
        live = [row["symbol"] for row in runtime.store.live_positions()]
        symbols = merge_symbols(live, chosen)
        data = runtime.config.model_dump()
        data["symbols"] = symbols
        config = BotConfig.model_validate(data)
        config.save()
        rebound = _bind(config)
        rebound._ledger("symbols", {"preset": body.preset, "symbols": symbols, "left_out": left_out})
        return {"status": "ok", "symbols": symbols, "left_out": left_out}


@app.put("/bot/config")
def update_config(body: ConfigUpdate) -> dict[str, Any]:
    if body.mode not in (None, "testnet", "production"):
        raise HTTPException(status_code=422, detail="mode deve ser testnet ou production")
    if body.strategies is not None and set(body.strategies) - set(STRATEGIES):
        raise HTTPException(status_code=422, detail="estratégia desconhecida")
    with ctx.lock:
        runtime = _runtime()
        current = runtime.config
        data = current.model_dump()
        if body.allocated_capital_usd is not None:
            data["allocated_capital_usd"] = body.allocated_capital_usd
        if body.symbols is not None:
            data["symbols"] = body.symbols
        if body.strategies is not None:
            data["strategies"] = {**data["strategies"], **body.strategies}
        if body.flatten_on_halt is not None:
            data["flatten_on_halt"] = body.flatten_on_halt
        if body.limits is not None:
            data["limits"] = {**data["limits"], **body.limits}
        for name in (
            "entry_mode",
            "min_win_rate",
            "trailing_stop",
            "testnet_test_mode",
            "auto_select",
            "auto_select_count",
            "min_profit_usd",
        ):
            value = getattr(body, name)
            if value is not None:
                data[name] = value
        try:
            config = BotConfig.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc
        if body.symbols is not None and config.auto_select:
            raise HTTPException(status_code=409, detail="a seleção automática escolhe os pares; desligue-a antes")
        if body.symbols is not None:
            _require_listed(runtime.collector, config.symbols, config.mode)
            dropped = sorted({row["symbol"] for row in runtime.store.live_positions()} - set(config.symbols))
            if dropped:
                raise HTTPException(status_code=409, detail=f"pares com posição aberta não podem sair da lista: {dropped}")
        reasons: list[str] = []
        if body.mode == "production" and current.mode != "production":
            keys = service.check_status("binance-live-trade-bot")
            config, reasons = switch_to_production(
                config,
                runtime.store,
                confirmation=body.confirmation or "",
                bot_stopped=not runtime.running,
                open_orders=len(runtime.store.live_positions()),
                mandate_ok=mandate_is_restricted(),
                keys_ok=bool(keys.get("ok")),
            )
            if reasons:
                # Refused as a whole: nothing in this request is saved, the runtime keeps its config.
                raise HTTPException(status_code=409, detail={"mode": current.mode, "reasons": reasons})
            config.save()
            rebound = _bind(config)
            # The word PRODUCAO is the consent; the production mandate uses production capital.
            commit_mandate_for(config)
        elif body.mode == "testnet" and current.mode == "production":
            flattened = runtime.executor.flatten() if config.flatten_on_halt else []
            failed = [item for item in flattened if item.get("status") != "ok"]
            unprotected = [item for item in failed if item.get("unprotected")]
            if unprotected:
                symbols = ", ".join(str(item.get("symbol")) for item in unprotected)
                runtime.halt(f"posição de produção possivelmente sem stop ao sair de produção: {symbols}", flatten=False)
            left = runtime.store.live_positions()
            if left:
                # Nothing would sync, exit or re-protect them once the process points at testnet.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "mode": "production",
                        "reasons": [f"{len(left)} posição(ões) de produção ainda abertas; feche-as antes de voltar"],
                        "flatten": failed,
                        "halted": bool(unprotected),
                    },
                )
            runtime.stop()
            config.mode = "testnet"
            config.save()
            rebound = _bind(config)
        else:
            config.save()
            rebound = _bind(config)
            if body.reconfirm_mandate:
                commit_mandate_for(config)
        if config.entry_mode == "auto" and current.entry_mode != "auto":
            # Automatic entries always need an explicit Start after the switch.
            rebound.stop()
        if config.auto_select and not current.auto_select:
            # The next cycle picks the pairs instead of waiting for the hour.
            rebound.store.set_state("auto_select_at", None)
        rebound._ledger("config", {"mode": config.mode, "operator": "panel", "reasons": reasons})
        return {"status": "ok", "mode": config.mode, "config": json.loads(config.model_dump_json())}


def _require_listed(collector: Collector, symbols: list[str], mode: str) -> None:
    try:
        listed = {row["symbol"] for row in tradable_symbols(collector)}
        venue = venue_symbols(collector, mode)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="lista da Binance indisponível") from exc
    missing = [symbol for symbol in symbols if symbol not in listed]
    if missing:
        raise HTTPException(status_code=422, detail=f"pares fora da Binance spot: {missing}")
    absent = [symbol for symbol in symbols if venue is not None and symbol not in venue]
    if absent:
        raise HTTPException(status_code=422, detail=f"pares fora do Spot Testnet ou sem negociação ativa lá: {absent}")


@app.get("/bot/events")
async def events() -> StreamingResponse:
    queue = hub.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            if hub.latest is not None:
                yield f"data: {json.dumps(hub.latest, default=str)}\n\n"
            while True:
                payload = await queue.get()
                yield f"data: {payload}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/bot/tick")
def tick() -> dict[str, Any]:
    """Run one analysis cycle. The process loop calls the same cycle every ``tick_seconds``."""
    return _runtime().tick()


@app.get("/bot/report")
def report(day: str | None = None) -> dict[str, Any]:
    if day is not None:
        try:
            day = date.fromisoformat(day).isoformat()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="day deve ser uma data AAAA-MM-DD") from exc
    return build_daily_report(_runtime().store, day=day)

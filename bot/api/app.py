"""FastAPI application."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from bot.api.hub import EventHub
from bot.api.state import BotContext
from bot.config import BotConfig, RiskLimits
from bot.exchange import service
from bot.features import build_features, ensure_chart_data
from bot.markets import tradable_symbols
from bot.recommend import recommendations
from bot.live.halt import clear_halt
from bot.paths import pin_mode_root
from bot.promotion import switch_to_production
from bot.reports import build_daily_report
from bot.runtime import Runtime, mode_root, profile_for

app = FastAPI(title="The Trader Bot", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
hub = EventHub()
ctx = BotContext()


class ConfigUpdate(BaseModel):
    mode: str | None = None
    confirmation: str | None = None
    allocated_capital_usd: float | None = None
    symbols: list[str] | None = None
    strategies: dict[str, bool] | None = None
    flatten_on_halt: bool | None = None
    limits: dict[str, float | int] | None = None
    gates: dict[str, bool] | None = None
    reconfirm_mandate: bool = False


class ManualOrder(BaseModel):
    symbol: str
    strategy: str


def _bind(config: BotConfig) -> Runtime:
    pin_mode_root(mode_root(config))
    store = ctx.store_for(config)
    runtime = Runtime(config, store, ctx.collector, publish=hub.publish)
    ctx.runtime = runtime
    ctx.config = config
    return runtime


@app.on_event("startup")
def _startup() -> None:
    config = BotConfig.load()
    _bind(config)


def _listed(collector: Any, symbol: str) -> bool:
    try:
        return any(row["symbol"] == symbol for row in tradable_symbols(collector))
    except Exception:
        return False


def _runtime() -> Runtime:
    if ctx.runtime is None or ctx.config is None:
        _startup()
    assert ctx.runtime is not None
    return ctx.runtime


@app.get("/bot/status")
def status() -> dict[str, Any]:
    runtime = _runtime()
    store = runtime.store
    days = store.pnl_days()
    today = days[-1] if days else None
    return {
        "running": runtime.running,
        "halted": runtime.halted,
        "mode": runtime.config.mode,
        "profile": profile_for(runtime.config),
        "capital_usd": runtime.config.capital_for_sizing(),
        "allocated_capital_usd": runtime.config.allocated_capital_usd,
        "symbols": runtime.config.symbols,
        "strategies": runtime.config.strategies,
        "limits": runtime.config.limits.model_dump(),
        "flatten_on_halt": runtime.config.flatten_on_halt,
        "gates": runtime.config.gates,
        "day": today,
        "violations_avoided": int(today["violations_avoided"]) if today else 0,
        "net_pnl": float(today["net_pnl"]) if today else 0.0,
        "trades": int(today["trades"]) if today else 0,
    }


@app.get("/bot/opportunities")
def opportunities() -> dict[str, Any]:
    runtime = _runtime()
    return {"rows": runtime.store.opportunities()}


@app.get("/bot/positions")
def positions() -> dict[str, Any]:
    return {"rows": _runtime().store.open_positions()}


@app.get("/bot/performance")
def performance() -> dict[str, Any]:
    store = _runtime().store
    days = store.pnl_days()
    return {"days": days, "strategies": store.strategies(), "equity_peak": store.peak_equity()}


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
    try:
        ensure_chart_data(collector, symbol)
    except Exception as exc:
        if (symbol, "1h") not in collector.snapshot.candles or (symbol, "15m") not in collector.snapshot.candles:
            raise HTTPException(status_code=503, detail="mercado indisponível") from exc
    if (symbol, "1h") not in collector.snapshot.candles or (symbol, "15m") not in collector.snapshot.candles:
        raise HTTPException(status_code=503, detail="mercado indisponível")
    return build_features(
        collector.snapshot,
        symbol,
        funding_threshold=runtime.config.funding_annualized_threshold,
    )


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
    return _runtime().halt("Parar tudo", flatten=_runtime().config.flatten_on_halt)


@app.post("/bot/resume")
def resume() -> dict[str, Any]:
    """Manual restart after a halt. The tick never calls this."""
    clear_halt("binance")
    clear_halt(None)
    runtime = _runtime()
    runtime.store.set_state("running", False)
    runtime.store.set_state("halt_reason", None)
    runtime._ledger("resume", {"by": "operator"})
    return {"status": "ok", "halted": runtime.halted}


@app.post("/bot/opportunities/execute")
def execute_manual(body: ManualOrder) -> dict[str, Any]:
    from bot.evaluator import Opportunity

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
    results = runtime.executor.act([opportunity], clock_blocked=runtime.collector.snapshot.clock_blocked, manual=True)
    return {"status": "ok", "results": results}


@app.post("/bot/opportunities/ignore")
def ignore(body: ManualOrder) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    _runtime().store.ignore(body.symbol, body.strategy, until)
    return {"status": "ok", "until": until}


@app.put("/bot/config")
def update_config(body: ConfigUpdate) -> dict[str, Any]:
    runtime = _runtime()
    config = runtime.config.model_copy(deep=True)
    if body.allocated_capital_usd is not None:
        config.allocated_capital_usd = body.allocated_capital_usd
    if body.symbols is not None:
        config.symbols = body.symbols
    if body.strategies is not None:
        config.strategies.update(body.strategies)
    if body.flatten_on_halt is not None:
        config.flatten_on_halt = body.flatten_on_halt
    if body.limits is not None:
        config.limits = RiskLimits.model_validate({**config.limits.model_dump(), **body.limits})
    if body.gates is not None:
        config.gates.update(body.gates)
    if body.reconfirm_mandate:
        from bot.mandate_io import commit_binance_mandate

        capital = config.capital_for_sizing()
        commit_binance_mandate(
            capital_usd=capital,
            max_order_notional_usd=capital * config.limits.max_order_notional_pct,
            max_total_exposure_usd=capital * config.limits.max_exposure_pct,
            max_leverage=config.limits.max_leverage,
            max_trades_per_day=config.limits.max_trades_per_day,
            flatten_on_halt=config.flatten_on_halt,
        )
    reasons: list[str] = []
    if body.mode == "production" and config.mode != "production":
        from bot.mandate_io import mandate_is_restricted

        keys = service.check_status("binance-live-trade-bot")
        config, reasons = switch_to_production(
            config,
            runtime.store,
            confirmation=body.confirmation or "",
            bot_stopped=not runtime.running,
            open_orders=0,
            mandate_ok=mandate_is_restricted(),
            keys_ok=bool(keys.get("ok")),
        )
        if reasons:
            config.save()
            raise HTTPException(status_code=409, detail={"mode": "testnet", "reasons": reasons})
    elif body.mode == "testnet" and config.mode == "production":
        runtime.executor.cancel_open_orders()
        if config.flatten_on_halt:
            runtime.executor.flatten()
        runtime.stop()
        config.mode = "testnet"
    config.save()
    rebound = _bind(config)
    rebound._ledger(
        "config",
        {"mode": config.mode, "operator": "panel", "reasons": reasons},
    )
    return {"status": "ok", "mode": config.mode, "config": json.loads(config.model_dump_json())}


@app.get("/bot/events")
async def events() -> StreamingResponse:
    queue = hub.subscribe()

    async def stream():
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
    """Run one analysis cycle. The scheduler calls this every ``tick_seconds``."""
    return _runtime().tick()


@app.get("/bot/report")
def report(day: str | None = None) -> dict[str, Any]:
    return build_daily_report(_runtime().store, day=day)


@app.post("/bot/positions/close")
def close_position(body: ManualOrder) -> dict[str, Any]:
    from bot.evaluator import Opportunity

    runtime = _runtime()
    rows = [row for row in runtime.store.open_positions() if row["symbol"] == body.symbol and row["strategy"] == body.strategy]
    if not rows:
        raise HTTPException(status_code=404, detail="posição não encontrada")
    position = rows[0]
    opportunity = Opportunity(
        symbol=position["symbol"],
        strategy=position["strategy"],
        action="Sair",
        score=1,
        net_return=0,
        risk_pct=0.01,
        status="em_posicao",
        reason="fechamento manual",
        timeframe="",
        direction=-1 if position["side"] == "buy" else 1,
        cost=0,
        gross_return=0,
        size_usd=abs(float(position["qty"]) * float(position["avg_price"])),
    )
    return {"status": "ok", "results": runtime.executor.act([opportunity], clock_blocked=False, manual=True)}

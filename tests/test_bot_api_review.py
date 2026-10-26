"""Review fixes in the API: the Stop contract, drawdown steps, slippage state, Monte Carlo rule, mandate target."""

from __future__ import annotations

from typing import Any

from bot import decay
from bot.api import app as module
from bot.api.app import ctx
from bot.live.halt import trip_halt
from bot.live.mandate.store import load_mandate
from bot.mandate_io import commit_mandate_for
from bot.runtime import RESUME_REFUSED_RESTING, Runtime
from bot.strategies import validation_params


def _runtime() -> Runtime:
    assert ctx.runtime is not None
    return ctx.runtime


def test_stop_answers_409_with_the_runtime_error_and_200_when_it_stopped(api_client, monkeypatch) -> None:
    refused = {"status": "error", "error": "compra em espera ainda sem resolução", "running": True, "resting": []}
    monkeypatch.setattr(_runtime(), "stop", lambda: refused)
    response = api_client.post("/bot/stop")
    assert response.status_code == 409 and response.json() == {"detail": refused["error"]}
    monkeypatch.setattr(_runtime(), "stop", lambda: {"status": "ok", "running": False})
    response = api_client.post("/bot/stop")
    assert response.status_code == 200 and response.json() == {"status": "ok", "running": False}


def test_resume_goes_through_the_runtime_and_clears_the_halt(api_client) -> None:
    trip_halt("test", "halt de teste", broker="binance")
    assert _runtime().halted is True
    response = api_client.post("/bot/resume")
    assert response.status_code == 200 and response.json() == {"status": "ok", "halted": False}
    assert _runtime().halted is False and _runtime().running is False


def test_resume_answers_409_and_keeps_the_halt_while_a_resting_buy_is_unresolved(api_client, monkeypatch) -> None:
    trip_halt("test", "halt de teste", broker="binance")
    unresolved = [{"status": "error", "error": "cancelamento sem confirmação"}]
    monkeypatch.setattr(_runtime().executor, "cancel_resting_buys", lambda: unresolved)
    response = api_client.post("/bot/resume")
    assert response.status_code == 409 and response.json() == {"detail": RESUME_REFUSED_RESTING}
    assert _runtime().halted is True and _runtime().running is False


def test_a_new_drawdown_limit_keeps_steps_that_fit_and_rederives_the_rest() -> None:
    saved = {"max_drawdown_pct": 0.10, "dd_reduce_pct": 0.04, "halt_drawdown_pct": 0.12, "max_exposure_pct": 0.6}
    # A custom halt at 12% still sits above 11%: it is not moved to the derived 16.5%.
    assert module._merge_limits(saved, {"max_drawdown_pct": 0.11}) == {**saved, "max_drawdown_pct": 0.11}
    # At 12% the halt at 12% is out of order and dropped; the reduce step at 4% still fits.
    merged = module._merge_limits(saved, {"max_drawdown_pct": 0.12})
    assert merged == {"max_drawdown_pct": 0.12, "dd_reduce_pct": 0.04, "max_exposure_pct": 0.6}
    # At 4% the reduce step is out of order and dropped; the halt still fits.
    assert "dd_reduce_pct" not in module._merge_limits(saved, {"max_drawdown_pct": 0.04})
    # A reduce step of 0 is off at any limit.
    off = {**saved, "dd_reduce_pct": 0}
    assert module._merge_limits(off, {"max_drawdown_pct": 0.02})["dd_reduce_pct"] == 0
    # A halt and a limit both at 100% are in order.
    full = {**saved, "max_drawdown_pct": 0.9, "halt_drawdown_pct": 1.0}
    assert module._merge_limits(full, {"max_drawdown_pct": 1.0})["halt_drawdown_pct"] == 1.0
    # Steps sent with the limit are the operator's, kept as sent.
    sent = {"max_drawdown_pct": 0.2, "halt_drawdown_pct": 0.1}
    assert module._merge_limits(saved, sent)["halt_drawdown_pct"] == 0.1


def test_a_failed_slippage_check_reports_the_pair_as_unknown(api_client, monkeypatch) -> None:
    def broken(_store: Any, symbol: str, _budget: float, *, now: Any) -> str | None:
        if symbol == "ETH/USDT":
            raise RuntimeError("store read failed")
        return None

    monkeypatch.setattr(decay, "pair_slippage_blocked", broken)
    pairs = {row["symbol"]: row for row in api_client.get("/bot/performance").json()["pairs"]}
    assert pairs["ETH/USDT"]["blocked"] is None
    assert pairs["ETH/USDT"]["blocked_reason"] == "verificação de slippage falhou"
    assert (pairs["BTC/USDT"]["blocked"], pairs["BTC/USDT"]["blocked_reason"]) == (False, None)


def _store_momentum(result: dict[str, Any]) -> None:
    config = _runtime().config
    params = validation_params(
        "momentum",
        config.exit_policy,
        timeframes={"4h": result},
        symbols=config.symbols,
        market_filter=config.market_filter,
        hysteresis=config.exit_hysteresis,
    )
    _runtime().store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": params})


def test_a_missing_monte_carlo_drawdown_is_not_ok_and_not_validated(api_client) -> None:
    passed = {"passed": True, "sharpe": 1.6, "daily_sharpe": 1.2, "dsr": 0.97, "trials": 1}
    _store_momentum(passed)
    body = api_client.get("/bot/status").json()
    # No p95: nothing proves the drawdown limit, as the cycle's signal-only rule reads it.
    assert body["validation_detail"]["momentum"]["4h"]["mc_ok"] is False
    assert body["validated"]["momentum"] is False
    _store_momentum({**passed, "mc_p95_dd_r": "NaN"})
    assert api_client.get("/bot/status").json()["validated"]["momentum"] is False
    # 5 R at 1% risk per trade is 5%, under the 10% limit.
    _store_momentum({**passed, "mc_p95_dd_r": 5.0})
    body = api_client.get("/bot/status").json()
    assert body["validation_detail"]["momentum"]["4h"]["mc_ok"] is True and body["validated"]["momentum"] is True


def test_mandate_target_is_what_a_reconfirm_would_write_without_writing_it(api_client) -> None:
    runtime = _runtime()
    swing = api_client.get("/bot/status").json()
    limits = swing["limits"]
    target = swing["mandate_target"]
    assert swing["mandate"] is None and load_mandate("binance") is None
    assert target["max_trades_per_day"] == 4 * limits["max_trades_per_day"] + limits["instance_orders_per_day"]
    # A day strategy adds the first-target sale and the break-even stop: 6 orders per entry.
    runtime.config = runtime.config.model_copy(update={"strategies": {**runtime.config.strategies, "intraday_momentum": True}})
    day = api_client.get("/bot/status").json()
    assert day["mandate_target"]["max_trades_per_day"] == 6 * limits["max_trades_per_day"] + limits["instance_orders_per_day"]
    assert day["mandate"] is None and load_mandate("binance") is None
    # The reconfirm writes exactly the target.
    commit_mandate_for(runtime.config)
    after = api_client.get("/bot/status").json()
    assert after["mandate"] == after["mandate_target"] == day["mandate_target"]

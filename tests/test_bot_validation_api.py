"""Validation orchestration through the API: timeframes, trials, stale results, status detail (6.5, 6.14)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from bot.api import app as module
from bot.api import validation
from bot.api.app import ctx
from bot.collector import Series
from bot.config import BotConfig
from bot.runtime import Runtime
from bot.strategies import GATE_VERSION, pairset_hash, validation_params


class _Loader:
    """History stub: flat candles for every pair; records (interval, symbols, days) per read."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, tuple[str, ...], int]] = []

    def fetch(self, symbols: list[str], start: str, end: str, interval: str = "1d", *, on_symbol: Any = None) -> dict[str, Any]:
        self.reads.append((interval, tuple(symbols), (pd.Timestamp(end) - pd.Timestamp(start)).days))
        freq = {"15m": "15min", "1h": "h", "4h": "4h"}.get(interval, "h")
        index = pd.date_range("2026-01-01", periods=120, freq=freq, tz="UTC")
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}, index=index)
        for done, symbol in enumerate(symbols, start=1):
            if on_symbol is not None:
                on_symbol(symbol, done)
        return {symbol: frame for symbol in symbols}


def _join() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("validate-"):
            thread.join(10)


def _use(config_update: dict[str, Any]) -> Runtime:
    """Change the bound runtime's config in memory, without a PUT (no validation thread starts)."""
    assert ctx.runtime is not None
    ctx.runtime.config = ctx.runtime.config.model_copy(update=config_update)
    return ctx.runtime


def _params(strategy_id: str, timeframes: dict[str, dict[str, Any]], *, symbols: list[str] | None = None, **key: Any) -> dict[str, Any]:
    """A stored validation for the bound config, or for the given pair set and key."""
    assert ctx.runtime is not None
    config = ctx.runtime.config
    return validation_params(
        strategy_id,
        key.get("exit_policy", config.exit_policy),
        timeframes=timeframes,
        symbols=symbols if symbols is not None else config.symbols,
        market_filter=key.get("market_filter", config.market_filter),
        hysteresis=key.get("hysteresis", config.exit_hysteresis),
    )


@pytest.fixture
def loader(monkeypatch) -> _Loader:
    stub = _Loader()
    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _market: stub)
    return stub


@pytest.fixture
def runs(monkeypatch) -> list[str]:
    """Replace the validation job: it records the strategy and finishes at once."""
    started: list[str] = []

    def run(key: tuple[str, str], strategy_id: str, config: BotConfig, root: Any) -> None:
        started.append(strategy_id)
        validation._update_job(key, status="done", stage="pronto", finished_at=datetime.now(timezone.utc).isoformat())

    monkeypatch.setattr(validation, "_run_validation", run)
    return started


def test_validation_runs_every_operated_timeframe(api_client, loader) -> None:
    _use({"market_filter": "btc_ema40d"})
    assert api_client.post("/bot/strategies/trend/validate").status_code == 202
    _join()
    symbols = tuple(ctx.runtime.config.symbols)  # type: ignore[union-attr]
    # 240 days of 1h and 480 of 4h (D11), then BTC's 4h bars with 40 days of EMA warm-up.
    assert loader.reads == [("1h", symbols, 240), ("4h", symbols, 480), ("4h", ("BTC/USDT",), 520)]
    job = api_client.get("/bot/status").json()["validations"]["trend"]
    assert job["status"] == "done" and job["done"] == job["total"] == 2 * len(symbols)
    stats = ctx.runtime.store.strategy("trend")  # type: ignore[union-attr]
    assert stats is not None
    params = stats["params"]
    assert set(params["timeframes"]) == {"1h", "4h"} and params["gate"] == GATE_VERSION
    assert params["symbols"] == sorted(symbols) and params["pairset"] == pairset_hash(symbols)
    assert params["market_filter"] == "btc_ema40d" and params["days"] == {"1h": 240, "4h": 480}
    assert params["timeframes"]["4h"]["days"] >= 0 and "elapsed_s" in params["timeframes"]["1h"]


def test_the_market_filter_refuses_to_validate_without_btc_history(api_client, monkeypatch) -> None:
    class NoBtc(_Loader):
        def fetch(self, symbols, start, end, interval="1d", *, on_symbol=None):
            frames = super().fetch(symbols, start, end, interval, on_symbol=on_symbol)
            return {} if symbols == ["BTC/USDT"] else frames

    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _market: NoBtc())
    _use({"market_filter": "btc_ema40d"})
    assert api_client.post("/bot/strategies/momentum/validate").status_code == 202
    _join()
    job = api_client.get("/bot/status").json()["validations"]["momentum"]
    assert job["status"] == "error" and job["error"] == "histórico do BTC indisponível para o filtro de mercado"
    assert ctx.runtime.store.strategy("momentum") is None  # type: ignore[union-attr]


def test_each_run_logs_a_trial_and_dsr_counts_trials_since_the_last_pass(api_client, loader, monkeypatch) -> None:
    calls: list[tuple[str, int, list[float]]] = []
    real = validation.validate_strategy

    def spy(strategy_id: str, frames: Any, timeframe: str, **kwargs: Any) -> Any:
        calls.append((timeframe, kwargs["trials"], list(kwargs["trial_sharpes"])))
        return real(strategy_id, frames, timeframe, **kwargs)

    monkeypatch.setattr(validation, "validate_strategy", spy)
    store = ctx.runtime.store  # type: ignore[union-attr]

    def validate() -> None:
        assert api_client.post("/bot/strategies/trend/validate").status_code == 202
        _join()

    validate()
    # Each timeframe is a trial; the second counts the first, as a distinct combination.
    assert calls == [("1h", 1, []), ("4h", 2, [0.0])]
    trials = store.trials_since_pass("trend")
    assert [(trial["timeframe"], trial["passed"]) for trial in trials] == [("1h", False), ("4h", False)]
    assert trials[0]["pairset"] == pairset_hash(ctx.runtime.config.symbols)  # type: ignore[union-attr]
    assert trials[1]["payload"]["trials"] == 2
    calls.clear()
    validate()
    # Re-running the same combinations does not grow N, but every run is in the variance.
    assert calls == [("1h", 2, [0.0, 0.0]), ("4h", 2, [0.0, 0.0, 0.0])]
    calls.clear()
    _use({"take_profit_pct": 0.03, "stop_loss_pct": 0.015})
    validate()
    # Another exit rule is a new combination for both timeframes.
    assert [(timeframe, trials) for timeframe, trials, _sharpes in calls] == [("1h", 3), ("4h", 4)]
    # A passing trial resets the count.
    store.add_trial(
        {
            "strategy_id": "trend",
            "timeframe": "1h",
            "exit_policy": "trailing",
            "pairset": "x",
            "market_filter": "off",
            "hysteresis": True,
            "sharpe_trade": 0.4,
            "trades": 40,
            "passed": True,
        }
    )
    calls.clear()
    validate()
    assert calls[0][1:] == (1, [])
    assert api_client.get("/bot/status").json()["trials"]["trend"] == 2


def test_startup_revalidates_stale_gate_results(api_client, runs, monkeypatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(Runtime, "_ledger", lambda _self, kind, payload: events.append((kind, payload)))
    config = ctx.runtime.config.model_copy(  # type: ignore[union-attr]
        update={"strategies": {"trend": True, "momentum": True, "mean_reversion": True, "intraday_momentum": False}}
    )
    config.save()
    store = ctx.runtime.store  # type: ignore[union-attr]
    stale = {**_params("trend", {"1h": {"passed": True}, "4h": {"passed": True}}), "gate": GATE_VERSION - 1}
    store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": stale})
    current = _params("momentum", {"4h": {"passed": True}})
    store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": current})
    # An older result of a strategy now off is left alone, and one never validated waits for the operator.
    store.save_strategy({"strategy_id": "intraday_momentum", "signal_only": True, "params": {"gate": 1}})
    module._startup()
    _join()
    assert runs == ["trend"]
    assert ("revalidate", {"strategies": ["trend"], "cause": "startup", "stale": "gate"}) in events
    assert ctx.runtime.store.get_state("revalidate_at:trend") is not None  # type: ignore[union-attr]


def test_a_rule_change_revalidates_at_once(api_client, runs) -> None:
    runtime = _use({"strategies": {**ctx.runtime.config.strategies, "momentum": True}})  # type: ignore[union-attr]
    runtime.store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": _params("momentum", {"4h": {"passed": True}})})
    assert module.revalidate_stale(runtime) == []
    _use({"market_filter": "btc_ema40d"})
    assert module.revalidate_stale(runtime) == ["momentum"]


def test_a_threshold_change_makes_a_passed_validation_stale(api_client, runs) -> None:
    runtime = _use({"strategies": {**ctx.runtime.config.strategies, "momentum": True}})  # type: ignore[union-attr]
    passed = {**_params("momentum", {"4h": {"passed": True}}), "thresholds": validation._thresholds(runtime.config)}
    runtime.store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": passed})
    universe = validation.replay_universe(runtime.config, runtime.store)
    assert validation._stale_cause(passed, "momentum", runtime.config, universe) is None
    assert module.revalidate_stale(runtime) == []
    _use({"score_threshold": 0.6})
    assert validation._stale_cause(passed, "momentum", runtime.config, universe) == "rules"
    assert module.revalidate_stale(runtime) == ["momentum"]
    # A record from before the top-level key: the timeframe results carry the thresholds it ran with.
    older = _params("momentum", {"4h": {"passed": True, "score_threshold": 0.3, "cost_multiple": 2.0, "min_reward_risk": 1.5}})
    assert validation._stale_cause(older, "momentum", runtime.config, universe) == "rules"


def test_pair_set_staleness_revalidates_at_most_daily(api_client, runs) -> None:
    runtime = _use({"strategies": {**ctx.runtime.config.strategies, "momentum": True}})  # type: ignore[union-attr]
    other_pairs = _params("momentum", {"4h": {"passed": True}}, symbols=["BTC/USDT"])
    runtime.store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": other_pairs})
    start = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    assert module.revalidate_stale(runtime, now=start) == ["momentum"]
    _join()
    # The stub left the result stale: the pair set waits a day from the last start.
    assert module.revalidate_stale(runtime, now=start + timedelta(hours=23)) == []
    assert module.revalidate_stale(runtime, now=start + timedelta(hours=25)) == ["momentum"]
    _join()
    # A gate change does not wait for the pair-set limit.
    stale = {**other_pairs, "gate": GATE_VERSION - 1}
    runtime.store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": stale})
    assert module.revalidate_stale(runtime, now=start + timedelta(hours=26)) == ["momentum"]
    assert runs == ["momentum", "momentum", "momentum"]


def test_a_failed_revalidation_waits_before_trying_again(api_client, monkeypatch) -> None:
    def fail(key: tuple[str, str], *_args: Any) -> None:
        validation._update_job(key, status="error", error="x", finished_at=datetime.now(timezone.utc).isoformat())

    monkeypatch.setattr(validation, "_run_validation", fail)
    runtime = _use({"strategies": {**ctx.runtime.config.strategies, "momentum": True}})  # type: ignore[union-attr]
    stale = {**_params("momentum", {"4h": {"passed": True}}), "gate": GATE_VERSION - 1}
    runtime.store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": stale})
    now = datetime.now(timezone.utc)
    assert module.revalidate_stale(runtime, now=now) == ["momentum"]
    _join()
    assert module.revalidate_stale(runtime, now=now + timedelta(minutes=30)) == []
    assert module.revalidate_stale(runtime, now=now + timedelta(minutes=61)) == ["momentum"]
    _join()


def test_status_reports_validation_detail_with_raw_and_deflated_sharpe(api_client) -> None:
    store = ctx.runtime.store  # type: ignore[union-attr]
    passed = {"passed": True, "sharpe": 1.6, "daily_sharpe": 1.2, "dsr": 0.97, "trials": 3, "mc_p95_dd_r": 5.0}
    store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": _params("momentum", {"4h": passed})})
    # The Monte Carlo drawdown of 20 R at 1% risk per trade reaches the 10% limit.
    risky = {**passed, "mc_p95_dd_r": 20.0}
    store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": _params("trend", {"1h": risky, "4h": {"passed": False}})})
    # Not signal-only but no timeframe passed: not executable.
    failed = _params("mean_reversion", {"15m": {"passed": False}, "1h": {"passed": False}})
    store.save_strategy({"strategy_id": "mean_reversion", "signal_only": False, "params": failed})
    store.add_trial(
        {
            "strategy_id": "momentum",
            "timeframe": "4h",
            "exit_policy": "trailing",
            "pairset": "x",
            "market_filter": "off",
            "hysteresis": True,
            "sharpe_trade": 0.2,
            "trades": 31,
            "passed": False,
        }
    )
    body = api_client.get("/bot/status").json()
    detail = body["validation_detail"]
    assert detail["momentum"]["4h"]["sharpe"] == 1.6 and detail["momentum"]["4h"]["dsr"] == 0.97
    assert detail["momentum"]["4h"]["trials"] == 3 and detail["momentum"]["4h"]["mc_ok"] is True
    assert detail["trend"]["1h"]["mc_ok"] is False
    # As the cycle reads it: trend's only passed timeframe fails the Monte Carlo rule, so it may only signal.
    assert body["validated"] == {"momentum": True, "trend": False, "mean_reversion": False}
    assert body["trials"]["momentum"] == 1 and body["trials"]["trend"] == 0
    # N: momentum's trial on another pair set plus its 4h; trend's 1h and 4h with nothing logged.
    assert body["trials_n"]["momentum"] == 2 and body["trials_n"]["trend"] == 2
    # The key follows the config: another market filter leaves every result unvalidated.
    _use({"market_filter": "btc_ema40d"})
    assert not any(api_client.get("/bot/status").json()["validated"].values())


def test_flatten_on_halt_change_requires_mandate_reconfirm(api_client, monkeypatch) -> None:
    from bot.live.mandate.store import load_mandate

    refused = api_client.put("/bot/config", json={"flatten_on_halt": True})
    assert refused.status_code == 409 and "mandato" in refused.json()["detail"]
    assert api_client.get("/bot/status").json()["flatten_on_halt"] is False
    # Sending the value it already has changes nothing and needs no reconfirm.
    assert api_client.put("/bot/config", json={"flatten_on_halt": False}).status_code == 200
    ok = api_client.put("/bot/config", json={"flatten_on_halt": True, "reconfirm_mandate": True})
    assert ok.status_code == 200
    mandate = load_mandate("binance")
    assert mandate is not None and mandate.flatten_on_halt is True
    status = api_client.get("/bot/status").json()
    assert status["flatten_on_halt"] is True and status["mandate"]["flatten_on_halt"] is True
    # The halt button leaves the flatten choice to the runtime (the mandate's, D23).
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ctx.runtime, "halt", lambda reason, **kwargs: calls.append(kwargs) or {"status": "ok"})
    assert api_client.post("/bot/halt").status_code == 200
    assert calls == [{"by": "frontend"}]


def test_positions_include_r_mae_mfe(api_client) -> None:
    runtime = ctx.runtime
    assert runtime is not None
    store = runtime.store
    planned = store.open_pending("BTC/USDT", "trend", stop_price=95.0)
    store.update_position(planned, status="open", qty=1.0, avg_price=100.0, initial_stop=95.0, mae_price=97.0, mfe_price=110.0)
    legacy = store.open_pending("ETH/USDT", "trend", stop_price=90.0)
    store.update_position(legacy, status="open", qty=1.0, avg_price=100.0)
    index = pd.date_range("2026-10-20", periods=3, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 100.0, "high": 106.0, "low": 99.0, "close": [101.0, 103.0, 105.0], "volume": 1.0}, index=index)
    with runtime.collector.lock:
        runtime.collector.snapshot.candles[("BTC/USDT", "15m")] = Series(frame, datetime.now(timezone.utc), "test")
    rows = {row["id"]: row for row in api_client.get("/bot/positions").json()["rows"]}
    assert (rows[planned]["r_now"], rows[planned]["mae_r"], rows[planned]["mfe_r"]) == (1.0, -0.6, 2.0)
    assert (rows[legacy]["r_now"], rows[legacy]["mae_r"], rows[legacy]["mfe_r"]) == (None, None, None)


def test_status_trials_n_is_the_n_the_next_validation_reaches(api_client, loader, monkeypatch) -> None:
    seen_n: list[int] = []
    real = validation.validate_strategy

    def spy(strategy_id: str, frames: Any, timeframe: str, **kwargs: Any) -> Any:
        seen_n.append(kwargs["trials"])
        return real(strategy_id, frames, timeframe, **kwargs)

    monkeypatch.setattr(validation, "validate_strategy", spy)

    def status() -> dict[str, Any]:
        return api_client.get("/bot/status").json()

    def validate() -> None:
        assert api_client.post("/bot/strategies/trend/validate").status_code == 202
        _join()

    # Nothing logged: trend's 1h and 4h under this config.
    assert status()["trials_n"]["trend"] == 2
    validate()
    assert seen_n[-1] == 2 and status()["trials_n"]["trend"] == 2
    assert status()["trials"]["trend"] == 2
    # Another exit rule adds its two combinations before the run, and the run reaches that N.
    _use({"take_profit_pct": 0.03, "stop_loss_pct": 0.015})
    assert status()["trials_n"]["trend"] == 4
    validate()
    assert seen_n[-1] == 4 and status()["trials_n"]["trend"] == 4
    # The rows keep counting every run; N counts combinations.
    assert status()["trials"]["trend"] == 4
    validate()
    assert status()["trials"]["trend"] == 6 and status()["trials_n"]["trend"] == 4


def test_auto_select_replays_the_liquid_universe_not_the_chosen_pairs(api_client, loader, monkeypatch) -> None:
    from bot.cycle.market import UNIVERSE_STATE

    universe = ["ADA/USDT", "BTC/USDT", "ETH/USDT", "SOL/USDT"]
    strategies = {**ctx.runtime.config.strategies, "trend": True}  # type: ignore[union-attr]
    runtime = _use({"auto_select": True, "symbols": ["SOL/USDT", "ETH/USDT"], "strategies": strategies})
    runtime.store.set_state(UNIVERSE_STATE, universe)
    assert api_client.post("/bot/strategies/trend/validate").status_code == 202
    _join()
    assert loader.reads[0] == ("1h", tuple(universe), 240)
    params = runtime.store.strategy("trend")["params"]  # type: ignore[index]
    assert params["symbols"] == universe and params["pairset"] == pairset_hash(universe) and params["universe"] == "liquid"
    assert all(trial["pairset"] == pairset_hash(universe) for trial in runtime.store.trials_since_pass("trend"))
    job = api_client.get("/bot/status").json()["validations"]["trend"]
    assert job["done"] == job["total"] == 2 * len(universe)
    detail = api_client.get("/bot/status").json()["validation_detail"]["trend"]["1h"]
    assert detail["universe"] == "liquid" and detail["universe_pairs"] == 4
    monkeypatch.setattr(validation, "_run_validation", lambda *_args: None)
    start = datetime.now(timezone.utc)
    # The hourly choice moves inside the universe: nothing is stale, and the N stays.
    _use({"symbols": ["ADA/USDT"]})
    assert module.revalidate_stale(runtime, now=start) == []
    assert api_client.get("/bot/status").json()["trials_n"]["trend"] == 2
    # The universe itself changes: stale, at most once a day from the last start (D12).
    runtime.store.set_state(UNIVERSE_STATE, [*universe, "XRP/USDT"])
    assert module.revalidate_stale(runtime, now=start + timedelta(hours=1)) == []
    assert module.revalidate_stale(runtime, now=start + timedelta(hours=25)) == ["trend"]


def test_the_validation_keeps_the_limits_it_replayed_and_a_limit_change_keeps_it(api_client, loader) -> None:
    from bot.config import RiskLimits

    runtime = _use({"limits": RiskLimits(max_positions=3), "strategies": {**ctx.runtime.config.strategies, "trend": True}})  # type: ignore[union-attr]
    assert api_client.post("/bot/strategies/trend/validate").status_code == 202
    _join()
    params = runtime.store.strategy("trend")["params"]  # type: ignore[index]
    replayed = {"max_positions": 3, "pair_cooldown_minutes": 120, "loss_guard_trades": 3, "loss_guard_r": 2.0}
    assert params["limits"] == replayed and params["universe"] == "pairs"
    detail = api_client.get("/bot/status").json()["validation_detail"]["trend"]
    assert detail["1h"]["limits"] == replayed and detail["1h"]["skipped_by_limits"] == 0
    assert detail["4h"]["score_threshold"] == 0.3 and detail["4h"]["filtered_trades"] == 0
    assert params["thresholds"] == {"score_threshold": 0.3, "cost_multiple": runtime.config.cost_multiple, "min_reward_risk": 1.5}
    # Limits are not part of the key: another limit re-validates nothing.
    _use({"limits": RiskLimits(max_positions=1, loss_guard_r=1.0)})
    assert module.revalidate_stale(runtime, now=datetime.now(timezone.utc) + timedelta(days=2)) == []


def test_a_strategy_without_a_replay_lists_every_validatable_one(api_client) -> None:
    from bot.backtest.runner import VALIDATABLE

    reply = api_client.post("/bot/strategies/funding_carry/validate")
    assert reply.status_code == 422
    assert reply.json()["detail"] == f"só {', '.join(VALIDATABLE)} têm teste histórico"
    assert all(strategy_id in reply.json()["detail"] for strategy_id in ("intraday_momentum", "vwap_pullback"))


# -- rigorous review: thresholds in the live key, the universe read by the validation ---------------


def test_a_threshold_change_leaves_a_pass_unvalidated_in_the_status(api_client) -> None:
    runtime = _use({"strategies": {**ctx.runtime.config.strategies, "momentum": True}})  # type: ignore[union-attr]
    passed = {**_params("momentum", {"4h": {"passed": True, "mc_p95_dd_r": 5.0}}), "thresholds": validation._thresholds(runtime.config)}
    runtime.store.save_strategy({"strategy_id": "momentum", "signal_only": False, "params": passed})
    assert api_client.get("/bot/status").json()["validated"]["momentum"] is True
    # As the cycle reads it: until the re-validation passes, the old pass is signal-only.
    _use({"cost_multiple": 3.0})
    assert api_client.get("/bot/status").json()["validated"]["momentum"] is False
    _use({"cost_multiple": runtime.config.cost_multiple, "score_threshold": 0.6})
    assert api_client.get("/bot/status").json()["validated"]["momentum"] is False


def _auto_trend() -> Runtime:
    strategies = {**ctx.runtime.config.strategies, "trend": True}  # type: ignore[union-attr]
    return _use({"auto_select": True, "symbols": ["SOL/USDT"], "strategies": strategies})


def test_auto_select_without_a_saved_universe_reads_the_liquid_pool_and_saves_it(api_client, loader, monkeypatch) -> None:
    from bot.cycle.market import UNIVERSE_STATE

    read: list[Any] = []
    monkeypatch.setattr(validation, "venue_symbols", lambda _collector, mode: None)
    pool = ["SOL/USDT", "ADA/USDT", "BTC/USDT"]
    monkeypatch.setattr(validation, "liquid_pool", lambda collector, listed=None: read.append(collector) or list(pool))
    runtime = _auto_trend()
    assert runtime.store.get_state(UNIVERSE_STATE) is None
    assert api_client.post("/bot/strategies/trend/validate").status_code == 202
    _join()
    universe = ["ADA/USDT", "BTC/USDT", "SOL/USDT"]
    assert read == [ctx.collector] and loader.reads[0] == ("1h", tuple(universe), 240)
    params = runtime.store.strategy("trend")["params"]  # type: ignore[index]
    assert params["symbols"] == universe and params["pairset"] == pairset_hash(universe) and params["universe"] == "liquid"
    assert runtime.store.get_state(UNIVERSE_STATE) == universe


def test_auto_select_with_no_liquid_pool_ends_the_validation_with_an_error(api_client, loader, monkeypatch) -> None:
    from bot.cycle.market import UNIVERSE_STATE

    monkeypatch.setattr(validation, "venue_symbols", lambda _collector, mode: None)
    monkeypatch.setattr(validation, "liquid_pool", lambda _collector, listed=None: [])
    runtime = _auto_trend()
    assert api_client.post("/bot/strategies/trend/validate").status_code == 202
    _join()
    job = api_client.get("/bot/status").json()["validations"]["trend"]
    assert job["status"] == "error" and job["error"] == "universo de pares líquidos indisponível para a seleção automática"
    assert loader.reads == [] and runtime.store.strategy("trend") is None
    assert runtime.store.get_state(UNIVERSE_STATE) is None

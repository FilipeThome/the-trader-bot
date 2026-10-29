from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any

import pandas as pd
import pytest

from bot.api.app import ctx
from bot.api.auth import ALLOWED_ORIGINS


def test_a_request_without_the_token_is_refused(api_client) -> None:
    response = api_client.get("/bot/status", headers={"X-Bot-Token": "wrong"})
    assert response.status_code == 401
    assert api_client.get("/bot/status").status_code == 200


def test_a_foreign_host_is_refused(api_client) -> None:
    response = api_client.get("/bot/status", headers={"Host": "evil.example:8000"})
    assert response.status_code == 403


def test_a_foreign_origin_is_refused_even_for_a_read(api_client) -> None:
    response = api_client.get("/bot/status", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    allowed = api_client.get("/bot/status", headers={"Origin": sorted(ALLOWED_ORIGINS)[0]})
    assert allowed.status_code == 200


@pytest.mark.parametrize("path", ["/bot/resume", "/bot/start", "/bot/tick", "/bot/halt"])
def test_a_write_without_the_panel_header_is_refused(api_client, path: str) -> None:
    response = api_client.post(path, headers={"X-Bot-Client": ""})
    assert response.status_code == 403


def test_capital_zero_is_refused_and_bot_json_stays_valid(api_client) -> None:
    response = api_client.put("/bot/config", json={"allocated_capital_usd": 0})
    assert response.status_code == 422
    assert api_client.get("/bot/status").json()["allocated_capital_usd"] == 1000.0


def test_gates_cannot_be_set_through_the_api(api_client) -> None:
    response = api_client.put("/bot/config", json={"gates": {"g1": True}})
    assert response.status_code == 422


def test_limits_have_server_side_ceilings(api_client) -> None:
    assert api_client.put("/bot/config", json={"limits": {"max_trades_per_day": 1000}}).status_code == 422
    assert api_client.put("/bot/config", json={"limits": {"unknown": 1}}).status_code == 422
    ok = api_client.put("/bot/config", json={"limits": {"max_leverage": 5}})
    assert ok.status_code == 200
    assert ok.json()["config"]["limits"]["max_leverage"] == 1.0


def test_symbols_must_be_listed_spot_usdt_pairs(api_client) -> None:
    assert ctx.runtime is not None
    ctx.runtime.collector.market_cache = (
        datetime.now(timezone.utc),
        [
            {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"},
            {"symbol": "ETH/USDT", "base": "ETH", "quote": "USDT"},
            {"symbol": "JUP/USDT", "base": "JUP", "quote": "USDT"},
        ],
    )
    ctx.runtime.collector.testnet_cache = (datetime.now(timezone.utc), frozenset({"BTC/USDT", "ETH/USDT"}))
    assert api_client.put("/bot/config", json={"symbols": ["DOGE/USDT"]}).status_code == 422
    absent = api_client.put("/bot/config", json={"symbols": ["JUP/USDT"]})
    assert absent.status_code == 422 and "Spot Testnet" in absent.json()["detail"]
    assert api_client.put("/bot/config", json={"symbols": ["ETH/BTC"]}).status_code == 422
    ok = api_client.put("/bot/config", json={"symbols": ["btc/usdt", "ETH/USDT"]})
    assert ok.status_code == 200
    assert ok.json()["config"]["symbols"] == ["BTC/USDT", "ETH/USDT"]


def test_mandate_allows_four_orders_per_entry(api_client) -> None:
    from bot.live.mandate.store import load_mandate

    response = api_client.put("/bot/config", json={"allocated_capital_usd": 500, "reconfirm_mandate": True})
    assert response.status_code == 200
    mandate = load_mandate("binance")
    assert mandate is not None
    assert mandate.hard_caps.max_trades_per_day == 4 * 20
    assert mandate.hard_caps.max_leverage == 1.0


def test_production_switch_without_gates_stays_on_testnet(api_client) -> None:
    response = api_client.put("/bot/config", json={"mode": "production", "confirmation": "PRODUCAO"})
    assert response.status_code == 409
    assert response.json()["detail"]["mode"] == "testnet"
    assert api_client.get("/bot/status").json()["mode"] == "testnet"


class _Loader:
    """History stub. ``gate`` holds the download until a test releases it."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.gate.set()
        self.starts: list[str] = []

    def fetch(self, symbols: list[str], start: str, end: str, interval: str = "1d", *, on_symbol: Any = None) -> dict[str, Any]:
        self.starts.append(start)
        self.gate.wait(5)
        index = pd.date_range("2026-01-01", periods=120, freq="h", tz="UTC")
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}, index=index)
        for done, symbol in enumerate(symbols, start=1):
            if on_symbol is not None:
                on_symbol(symbol, done)
        return {symbol: frame for symbol in symbols}


def _join_validations() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("validate-"):
            thread.join(10)


def test_validation_runs_in_the_background_and_records_the_verdict(api_client, monkeypatch) -> None:
    loader = _Loader()
    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _market: loader)
    response = api_client.post("/bot/strategies/trend/validate")
    assert response.status_code == 202 and response.json()["status"] == "running"
    _join_validations()
    job = api_client.get("/bot/status").json()["validations"]["trend"]
    # Five pairs on each of trend's two timeframes.
    assert job["status"] == "done" and job["done"] == job["total"] == 10
    assert job["result"]["passed"] is False and set(job["result"]["timeframes"]) == {"1h", "4h"}
    assert ctx.runtime is not None
    stats = ctx.runtime.store.strategy("trend")
    assert stats is not None and stats["signal_only"] is True and stats["params"]["days"] == {"1h": 240, "4h": 480}
    started = pd.Timestamp(loader.starts[0])
    assert 239 <= (pd.Timestamp.now(tz="UTC") - started).days <= 240
    assert api_client.post("/bot/strategies/funding_carry/validate").status_code == 422


def test_a_running_validation_survives_a_rebind_and_refuses_a_second_start(api_client, monkeypatch) -> None:
    loader = _Loader()
    loader.gate.clear()
    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _market: loader)
    assert api_client.post("/bot/strategies/mean_reversion/validate").status_code == 202
    assert api_client.put("/bot/config", json={"min_win_rate": 0.65}).status_code == 200
    assert api_client.post("/bot/strategies/mean_reversion/validate").status_code == 409
    loader.gate.set()
    _join_validations()
    assert api_client.get("/bot/status").json()["validations"]["mean_reversion"]["status"] == "done"
    assert ctx.runtime is not None and ctx.runtime.store.strategy("mean_reversion") is not None


def test_close_takes_a_position_id(api_client, monkeypatch) -> None:
    assert ctx.runtime is not None
    store = ctx.runtime.store
    position_id = store.open_pending("BTC/USDT", "trend")
    assert position_id is not None
    store.update_position(position_id, status="open", qty=0.1, avg_price=100.0)
    closed: list[int] = []
    monkeypatch.setattr(ctx.runtime, "close_position", lambda pid: closed.append(pid) or {"status": "ok"})
    assert api_client.post("/bot/positions/close", json={"symbol": "BTC/USDT", "strategy": "trend"}).status_code == 422
    assert api_client.post("/bot/positions/close", json={"position_id": position_id}).status_code == 200
    assert closed == [position_id]
    assert api_client.post("/bot/positions/close", json={"position_id": 999}).status_code == 404


def test_leaving_production_with_open_positions_is_refused(api_client) -> None:
    from bot.api.app import _bind
    from bot.config import BotConfig

    with ctx.lock:
        runtime = _bind(BotConfig(mode="production"))
    position_id = runtime.store.open_pending("BTC/USDT", "trend")
    assert position_id is not None
    runtime.store.update_position(position_id, status="open", qty=0.1, avg_price=100.0)
    refused = api_client.put("/bot/config", json={"mode": "testnet"})
    assert refused.status_code == 409
    assert api_client.get("/bot/status").json()["mode"] == "production"
    runtime.store.close_position(position_id, exit_price=100.0, realized_pnl=0.0, fees=0.0)
    assert api_client.put("/bot/config", json={"mode": "testnet"}).status_code == 200
    assert api_client.get("/bot/status").json()["mode"] == "testnet"


def test_an_unprotected_position_while_leaving_production_halts(api_client, monkeypatch) -> None:
    from bot.api.app import _bind
    from bot.config import BotConfig

    with ctx.lock:
        runtime = _bind(BotConfig(mode="production", flatten_on_halt=True))
    position_id = runtime.store.open_pending("BTC/USDT", "trend")
    assert position_id is not None
    runtime.store.update_position(position_id, status="open", qty=0.1, avg_price=100.0)
    row = {"status": "error", "error": "UnprotectedPosition: stop recusado", "symbol": "BTC/USDT", "unprotected": True}
    monkeypatch.setattr(runtime.executor, "flatten", lambda: [row])
    refused = api_client.put("/bot/config", json={"mode": "testnet"})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["halted"] is True and detail["flatten"] == [row]
    assert runtime.halted is True
    assert api_client.get("/bot/status").json()["mode"] == "production"


def test_status_shows_whether_the_journal_blocks_orders(api_client) -> None:
    body = api_client.get("/bot/status").json()
    assert body["journal"] == {"blocked": False, "error": None, "blocking": []}


def test_switching_to_auto_entries_stops_the_cycle(api_client) -> None:
    assert ctx.runtime is not None
    ctx.runtime.store.set_state("running", True)
    response = api_client.put("/bot/config", json={"entry_mode": "auto", "min_win_rate": 0.7})
    assert response.status_code == 200
    status = api_client.get("/bot/status").json()
    assert status["entry_mode"] == "auto" and status["min_win_rate"] == 0.7 and status["running"] is False
    assert api_client.put("/bot/config", json={"min_win_rate": 0.3}).status_code == 422
    assert api_client.put("/bot/config", json={"entry_mode": "sempre"}).status_code == 422



def _catalog() -> list[dict[str, str]]:
    return [{"symbol": f"{base}/USDT", "base": base, "quote": "USDT"} for base in ("BTC", "ETH", "SOL", "USDC", "RLUSD", "BTCUP", "JUP")]


def test_top_pairs_skip_stablecoins_and_leveraged_tokens_and_keep_live_positions(api_client) -> None:
    assert ctx.runtime is not None
    collector = ctx.runtime.collector
    collector.market_cache = (pd.Timestamp.now(tz="UTC").to_pydatetime(), _catalog())
    # The Spot Testnet does not list JUP, so testnet never picks it.
    listed = frozenset(row["symbol"] for row in _catalog()) - {"JUP/USDT"}
    collector.testnet_cache = (pd.Timestamp.now(tz="UTC").to_pydatetime(), listed)
    tickers = [
        {"symbol": "USDCUSDT", "quoteVolume": "9e9"},
        {"symbol": "RLUSDUSDT", "quoteVolume": "8.5e9"},
        {"symbol": "BTCUPUSDT", "quoteVolume": "8e9"},
        {"symbol": "ETHUSDT", "quoteVolume": "5e9"},
        {"symbol": "JUPUSDT", "quoteVolume": "4e9"},
        {"symbol": "BTCUSDT", "quoteVolume": "3e9"},
    ]
    collector._fetch = lambda url: (tickers, {})
    position_id = ctx.runtime.store.open_pending("XRP/USDT", "trend")
    assert position_id is not None
    ctx.runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=1.0)
    response = api_client.post("/bot/symbols", json={"preset": "top", "count": 5})
    assert response.status_code == 200
    assert response.json()["symbols"] == ["XRP/USDT", "ETH/USDT", "BTC/USDT"]
    # The panel says which asked-for pair the testnet cannot trade.
    assert response.json()["left_out"] == ["JUP/USDT"]
    reset = api_client.post("/bot/symbols", json={"preset": "default"})
    assert reset.json()["symbols"][0] == "XRP/USDT" and "SOL/USDT" in reset.json()["symbols"]
    # The default five keep only the pairs the Spot Testnet can trade (BNB is not active there).
    assert "BNB/USDT" not in reset.json()["symbols"]
    assert set(reset.json()["left_out"]) == {"BNB/USDT", "XRP/USDT"}


def test_a_pair_with_an_open_position_cannot_leave_the_list(api_client) -> None:
    assert ctx.runtime is not None
    ctx.runtime.collector.market_cache = (pd.Timestamp.now(tz="UTC").to_pydatetime(), _catalog())
    ctx.runtime.collector.testnet_cache = (
        pd.Timestamp.now(tz="UTC").to_pydatetime(),
        frozenset(row["symbol"] for row in _catalog()),
    )
    position_id = ctx.runtime.store.open_pending("SOL/USDT", "trend")
    assert position_id is not None
    ctx.runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=1.0)
    response = api_client.put("/bot/config", json={"symbols": ["BTC/USDT"]})
    assert response.status_code == 409 and "SOL/USDT" in response.json()["detail"]


def test_a_refused_production_switch_saves_nothing_from_the_request(api_client) -> None:
    response = api_client.put(
        "/bot/config", json={"mode": "production", "confirmation": "PRODUCAO", "allocated_capital_usd": 500}
    )
    assert response.status_code == 409
    status = api_client.get("/bot/status").json()
    assert status["mode"] == "testnet" and status["allocated_capital_usd"] == 1000.0


def _production_ready(monkeypatch) -> None:
    """Keys, mandate and gates as a switch needs them; the testnet history stays empty."""
    from bot.api.app import _bind
    from bot.config import BotConfig

    monkeypatch.setattr("bot.api.app.service.check_status", lambda _name: {"ok": True})
    monkeypatch.setattr("bot.api.app.mandate_is_restricted", lambda: True)
    config = BotConfig.load()
    config.gates = {"g1": True, "g2": True, "g3": True, "g4": True}
    config.save()
    with ctx.lock:
        _bind(config)


def test_the_pilot_fields_only_go_with_the_switch_to_production(api_client) -> None:
    assert api_client.put("/bot/config", json={"pilot_cap_usd": 25}).status_code == 422
    assert api_client.put("/bot/config", json={"pilot_skip_validation": True}).status_code == 422
    refused = api_client.put(
        "/bot/config", json={"mode": "production", "confirmation": "PRODUCAO", "pilot_skip_validation": True}
    )
    assert refused.status_code == 422 and "capital do piloto" in refused.json()["detail"]
    for cap in (0, 50.01):
        body = {"mode": "production", "confirmation": "PRODUCAO", "pilot_cap_usd": cap}
        assert api_client.put("/bot/config", json=body).status_code == 422, cap
    assert api_client.get("/bot/status").json()["mode"] == "testnet"


def test_a_pilot_from_the_panel_records_its_cap_and_waiver_in_the_production_store(api_client, monkeypatch) -> None:
    from bot.promotion import PILOT_SKIP_VALIDATION_STATE, PILOT_STATE

    _production_ready(monkeypatch)
    # The production capital (10% of the allocated) must fit the cap: 100 of 1000 does not.
    over = api_client.put("/bot/config", json={"mode": "production", "confirmation": "PRODUCAO", "pilot_cap_usd": 25})
    assert over.status_code == 409 and any("piloto" in reason for reason in over.json()["detail"]["reasons"])
    body = {
        "mode": "production",
        "confirmation": "PRODUCAO",
        "allocated_capital_usd": 200,
        "pilot_cap_usd": 25,
        "pilot_skip_validation": True,
    }
    response = api_client.put("/bot/config", json=body)
    assert response.status_code == 200, response.json()
    status = api_client.get("/bot/status").json()
    assert status["mode"] == "production" and status["pilot_cap_usd"] == 25
    assert status["pilot_skip_validation"] is True and status["production_capital_usd"] == 20.0
    assert ctx.runtime.store.get_state(PILOT_STATE) == 25 and ctx.runtime.store.get_state(PILOT_SKIP_VALIDATION_STATE) is True
    # The cap recorded at the switch still binds the capital.
    assert api_client.put("/bot/config", json={"allocated_capital_usd": 300}).status_code == 409

    # Back on the testnet nothing of the pilot shows; a new pilot without the waiver clears it.
    assert api_client.put("/bot/config", json={"mode": "testnet"}).status_code == 200
    status = api_client.get("/bot/status").json()
    assert status["pilot_cap_usd"] is None and status["pilot_skip_validation"] is False
    again = api_client.put("/bot/config", json={"mode": "production", "confirmation": "PRODUCAO", "pilot_cap_usd": 30})
    assert again.status_code == 200, again.json()
    status = api_client.get("/bot/status").json()
    assert status["pilot_cap_usd"] == 30 and status["pilot_skip_validation"] is False
    assert ctx.runtime.store.get_state(PILOT_SKIP_VALIDATION_STATE) is False


def test_a_full_promotion_ignores_the_pilot_fields_and_clears_the_waiver(api_client, monkeypatch) -> None:
    from datetime import date, datetime, timedelta, timezone

    from bot.promotion import PILOT_SKIP_VALIDATION_STATE, PILOT_STATE

    _production_ready(monkeypatch)
    store = ctx.runtime.store
    today = date.today()
    for back in range(1, 31):
        store.record_day((today - timedelta(days=back)).isoformat(), net_pnl=1.0, gross_pnl=1.0, fees=0.0)
    for _ in range(30):
        position_id = store.open_pending("BTC/USDT", "trend")
        assert position_id is not None
        store.update_position(position_id, status="open", qty=1.0, avg_price=100.0, r_multiple=0.5)
        store.close_position(position_id, exit_price=101.0, realized_pnl=1.0, fees=0.0)
    assert store.closed_trades(since=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    body = {"mode": "production", "confirmation": "PRODUCAO", "pilot_cap_usd": 25, "pilot_skip_validation": True}
    response = api_client.put("/bot/config", json=body)
    assert response.status_code == 200, response.json()
    status = api_client.get("/bot/status").json()
    # 100 of production capital, above the cap sent: the history made it a full promotion, no pilot.
    assert status["mode"] == "production" and status["pilot_cap_usd"] is None
    assert status["pilot_skip_validation"] is False
    assert ctx.runtime.store.get_state(PILOT_STATE) is None
    assert ctx.runtime.store.get_state(PILOT_SKIP_VALIDATION_STATE) is False


def test_infinite_or_nan_numbers_never_reach_the_config(api_client) -> None:
    from pydantic import ValidationError

    from bot.config import BotConfig, RiskLimits

    for raw in ('{"allocated_capital_usd": Infinity}', '{"min_profit_usd": NaN}', '{"limits": {"max_exposure_pct": Infinity}}'):
        response = api_client.put("/bot/config", content=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 422, raw
    assert api_client.get("/bot/status").json()["allocated_capital_usd"] == 1000.0
    with pytest.raises(ValidationError):
        BotConfig(allocated_capital_usd=float("inf"))
    with pytest.raises(ValidationError):
        RiskLimits(max_order_notional_pct=float("nan"))


def test_the_report_day_is_a_date_and_never_a_path(api_client, tmp_path) -> None:
    for day in ("../../escaped", "..%2F..%2Fbinance", "2026-13-01", "x"):
        assert api_client.get("/bot/report", params={"day": day}).status_code == 422, day
    assert not (tmp_path / "escaped.json").exists()
    ok = api_client.get("/bot/report", params={"day": "2026-10-15"})
    assert ok.status_code == 200
    assert ok.json()["export"] in (None, "2026-10-15.json")


def test_chart_data_is_read_outside_the_collector_lock(api_client, monkeypatch) -> None:
    from bot.api import app as module

    owned: list[bool] = []

    def fill(collector: Any, _symbol: str) -> None:
        # The network read must not hold the lock the cycle's merge needs.
        owned.append(collector.lock._is_owned())
        raise RuntimeError("offline")

    monkeypatch.setattr(module, "ensure_chart_data", fill)
    assert api_client.get("/bot/features", params={"symbol": "BTC/USDT"}).status_code == 503
    assert owned == [False]
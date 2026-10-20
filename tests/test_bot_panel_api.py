from __future__ import annotations

import time

from bot.api.app import ctx


def test_opportunities_are_ranked_and_fast(api_client) -> None:
    rows = []
    for index in range(50):
        rows.append(
            {
                "symbol": f"P{index}/USDT",
                "strategy": "trend",
                "action": "Comprar",
                "score": index / 100,
                "net_return": 0.01,
                "risk_pct": 0.01,
                "status": "potencial",
                "reason": "sinal",
                "timeframe": "1h",
                "direction": 1,
                "cost": 0.002,
                "gross_return": 0.012,
            }
        )
    assert ctx.runtime is not None
    ctx.runtime.store.replace_opportunities(rows)
    started = time.perf_counter()
    response = api_client.get("/bot/opportunities")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < 0.2
    body = response.json()["rows"]
    assert len(body) == 50
    assert body[0]["score"] >= body[-1]["score"]
    assert [row["score"] for row in body] == sorted((row["score"] for row in body), reverse=True)


def test_auto_select_owns_the_pair_list_and_portfolio_limits_are_checked(api_client) -> None:
    bad = api_client.put("/bot/config", json={"limits": {"risk_per_trade_pct": 0.02, "max_open_risk_pct": 0.01}})
    assert bad.status_code == 422
    assert api_client.put("/bot/config", json={"limits": {"max_open_risk_pct": 0.5}}).status_code == 422
    assert api_client.put("/bot/config", json={"auto_select_count": 30}).status_code == 422
    ok = api_client.put("/bot/config", json={"auto_select": True, "auto_select_count": 10, "strategies": {"momentum": True}})
    assert ok.status_code == 200
    status = api_client.get("/bot/status").json()
    assert status["auto_select"] is True and status["auto_select_count"] == 10 and status["strategies"]["momentum"] is True
    assert api_client.post("/bot/symbols", json={"preset": "default"}).status_code == 409
    assert api_client.put("/bot/config", json={"symbols": ["BTC/USDT"]}).status_code == 409


def test_the_trade_chart_serves_the_position_its_candles_and_pnl(api_client, monkeypatch) -> None:
    from datetime import datetime, timezone

    from bot.api import app as module

    assert ctx.runtime is not None
    store = ctx.runtime.store
    assert api_client.get("/bot/trades/999").status_code == 404
    pending = store.open_pending("BTC/USDT", "trend", stop_price=90.0)
    assert pending is not None
    calls: list[str] = []

    def klines(_collector, symbol, mode, *, start, end):
        calls.append(symbol)
        return "1m", [{"t": datetime.now(timezone.utc).isoformat(), "o": 100.0, "h": 111.0, "l": 99.0, "c": 110.0}]

    monkeypatch.setattr(module, "venue_klines", klines)
    module.ctx.chart_cache.clear()
    body = api_client.get(f"/bot/trades/{pending}").json()
    assert body["pnl_usd"] is None and body["pnl_pct"] is None and body["position"]["status"] == "pending"
    store.update_position(pending, status="open", qty=2.0, avg_price=100.0)
    body = api_client.get(f"/bot/trades/{pending}").json()
    assert body["last_price"] == 110.0 and body["pnl_usd"] == 20.0 and abs(body["pnl_pct"] - 0.1) < 1e-9
    # The second read came from the five-second cache.
    assert calls == ["BTC/USDT"]


def test_the_trade_chart_backs_off_under_weight_pressure(api_client, monkeypatch) -> None:
    from bot.api import app as module

    assert ctx.runtime is not None
    position = ctx.runtime.store.open_pending("ETH/USDT", "trend", stop_price=90.0)
    module.ctx.chart_cache.clear()
    monkeypatch.setattr(module, "weight_pressure", lambda: True)
    # Live weight pressure only holds back charts of production trades.
    ctx.runtime.config = ctx.runtime.config.model_copy(update={"mode": "production"})
    monkeypatch.setattr(module, "venue_klines", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no read")))
    assert api_client.get(f"/bot/trades/{position}").status_code == 503



def test_performance_carries_the_calibration_report(api_client) -> None:
    body = api_client.get("/bot/performance").json()
    assert body["calibration"] == []


def test_status_reports_stop_window_stats(api_client) -> None:
    from datetime import datetime, timedelta, timezone

    assert ctx.runtime is not None
    store = ctx.runtime.store
    store.record_stop_window("trail", "BTC/USDT", 1, 100.0)
    store.record_stop_window("tp1", "BTC/USDT", 1, 300.0)
    # Older than the 30 days the status covers.
    store.record_stop_window("trail", "BTC/USDT", 1, 9000.0, ts=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat())
    windows = api_client.get("/bot/status").json()["stop_windows"]
    assert windows["count"] == 2 and windows["max_ms"] == 300.0 and windows["mean_ms"] == 200.0
    assert set(windows["by_kind"]) == {"trail", "tp1"}


def test_status_reports_the_risk_state_the_mandate_and_the_new_settings(api_client, monkeypatch) -> None:
    from bot.api import app as module

    monkeypatch.setattr(module, "_run_validation", lambda *_args: None)
    assert ctx.runtime is not None
    body = api_client.get("/bot/status").json()
    assert body["risk_state"] is None and body["mandate"] is None
    assert (body["market_filter"], body["exit_hysteresis"], body["flatten_day_on_daily_loss"]) == ("off", True, False)
    assert (body["macro_events_utc"], body["macro_pause_minutes"]) == ([], 15)
    saved = api_client.put(
        "/bot/config",
        json={
            "market_filter": "btc_ema40d",
            "exit_hysteresis": False,
            "flatten_day_on_daily_loss": True,
            "macro_events_utc": ["2026-11-05T19:00:00+01:00"],
            "macro_pause_minutes": 30,
            "reconfirm_mandate": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert ctx.runtime is not None
    ctx.runtime.store.set_state("risk_state", {"level": "cautious", "since": "x", "drawdown": 0.06, "reason": "r"})
    body = api_client.get("/bot/status").json()
    assert body["risk_state"]["level"] == "cautious"
    assert (body["market_filter"], body["exit_hysteresis"], body["flatten_day_on_daily_loss"]) == ("btc_ema40d", False, True)
    assert body["macro_events_utc"] == ["2026-11-05T18:00:00+00:00"] and body["macro_pause_minutes"] == 30
    capital = body["capital_usd"]
    assert body["mandate"] == {
        "max_order_notional_usd": capital * body["limits"]["max_order_notional_pct"],
        "max_total_exposure_usd": capital * body["limits"]["max_exposure_pct"],
        "max_trades_per_day": 4 * body["limits"]["max_trades_per_day"] + body["limits"]["instance_orders_per_day"],
        "flatten_on_halt": False,
    }
    assert api_client.put("/bot/config", json={"market_filter": "sp500"}).status_code == 422
    assert api_client.put("/bot/config", json={"macro_events_utc": ["amanhã"]}).status_code == 422


def test_changing_only_the_drawdown_limit_derives_its_steps_again(api_client) -> None:
    def limits(body: dict) -> dict:
        response = api_client.put("/bot/config", json={"limits": body})
        assert response.status_code == 200, response.text
        return response.json()["config"]["limits"]

    raised = limits({"max_drawdown_pct": 0.2})
    assert (raised["dd_reduce_pct"], raised["halt_drawdown_pct"]) == (0.1, 0.3)
    lowered = limits({"max_drawdown_pct": 0.04})
    assert (lowered["dd_reduce_pct"], lowered["halt_drawdown_pct"]) == (0.02, 0.15)
    # Steps sent with the limit are kept as sent.
    sent = limits({"max_drawdown_pct": 0.12, "dd_reduce_pct": 0.03, "halt_drawdown_pct": 0.2})
    assert (sent["dd_reduce_pct"], sent["halt_drawdown_pct"]) == (0.03, 0.2)
    # Another limit leaves the steps alone.
    assert limits({"max_exposure_pct": 0.4})["halt_drawdown_pct"] == 0.2
    # A reduce step of 0 is "off" and stays off.
    limits({"dd_reduce_pct": 0})
    off = limits({"max_drawdown_pct": 0.3})
    assert (off["dd_reduce_pct"], off["halt_drawdown_pct"]) == (0, 0.45)
    # Steps sent out of order are still refused.
    assert api_client.put("/bot/config", json={"limits": {"max_drawdown_pct": 0.2, "halt_drawdown_pct": 0.1}}).status_code == 422


def test_performance_reports_slippage_per_pair(api_client, monkeypatch) -> None:
    from bot import decay

    assert ctx.runtime is not None
    store = ctx.runtime.store
    for entry, exit_ in ((0.001, 0.002), (0.003, 0.004), (None, 0.001)):
        position = store.open_pending("SOL/USDT", "trend", stop_price=90.0)
        store.update_position(position, status="open", qty=1.0, avg_price=100.0, entry_slippage=entry, exit_slippage=exit_)
        store.close_position(position, exit_price=101.0, realized_pnl=1.0, fees=0.0)
    blocked = {"SOL/USDT": "slippage realizado acima do orçamento"}
    monkeypatch.setattr(decay, "pair_slippage_blocked", lambda _store, symbol, _budget, *, now: blocked.get(symbol), raising=False)
    pairs = {row["symbol"]: row for row in api_client.get("/bot/performance").json()["pairs"]}
    assert set(ctx.runtime.config.symbols) <= set(pairs)
    sol = pairs["SOL/USDT"]
    # The trade without an entry slippage is counted but not averaged.
    assert sol["trades"] == 3 and abs(sol["slippage_rt"] - 0.005) < 1e-12
    assert sol["blocked"] is True and sol["blocked_reason"] == blocked["SOL/USDT"]
    assert pairs["BTC/USDT"] == {"symbol": "BTC/USDT", "trades": 0, "slippage_rt": None, "blocked": False, "blocked_reason": None}


def test_the_day_preset_preview_sizes_an_order_at_a_1pct_stop(api_client) -> None:
    sizing = api_client.get("/bot/presets/day").json()["sizing"]
    status = api_client.get("/bot/status").json()
    capital, limits = status["capital_usd"], status["limits"]
    # The preset sets 0.5% risk per trade: 1% stop -> 50% of capital, then the order cap less headroom.
    risk_cap = capital * 0.005 / 0.01
    safety = capital * limits["max_order_notional_pct"] * 0.90
    assert sizing["stop_pct"] == 0.01
    assert sizing["caps"]["risk"] == round(risk_cap, 2) and sizing["caps"]["safety"] == round(safety, 2)
    assert sizing["size_usd"] == round(min(risk_cap, safety), 2)
    assert (sizing["binding"], sizing["label"]) == ("safety", "teto por ordem")

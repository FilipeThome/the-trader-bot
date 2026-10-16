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

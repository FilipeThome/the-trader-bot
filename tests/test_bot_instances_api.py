from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.backtest.instances import replay_dca, replay_grid
from bot.instances import DcaParams, GridParams

GRID = {"lower": 90, "upper": 110, "grids": 4, "invest_usd": 40, "stop_price": 85}


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-10-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)


def test_the_grid_replay_books_each_level_sold_above_its_buy() -> None:
    params = GridParams.model_validate(GRID)
    result = replay_grid(params, _frame([100.0, 94.0, 100.0, 94.0, 100.0, 106.0]), taker=0.001)
    assert result["started"] and result["grid_trades"] == 3 and result["realized_usd"] > 0
    assert replay_grid(params, _frame([100.0, 84.0, 100.0]), taker=0.001)["ended"] == "stop"


def test_the_dca_replay_repeats_rounds_until_the_stop() -> None:
    params = DcaParams(base_usd=10, so_count=2, step_pct=0.02, take_profit_pct=0.03, stop_pct=0.05)
    # Round 1: base at 100, safety order at 97.9, take-profit at 104. Round 2 starts at 100 and hits the stop.
    result = replay_dca(params, _frame([100.0, 97.9, 101.5, 100.0, 104.0, 100.0, 80.0]), taker=0.001)
    assert result["rounds"] == 2 and result["wins"] == 1 and result["ended"] == "stop"
    calm = replay_dca(params, _frame(list(np.linspace(100, 103.5, 20))), taker=0.001)
    assert calm["wins"] == 1 and calm["pnl_usd"] > 0


@pytest.fixture
def listed(monkeypatch) -> None:
    monkeypatch.setattr("bot.api.app._require_listed", lambda *_a: None)

    class Loader:
        def fetch(self, symbols, start, end, interval="15m", **_kwargs):
            return {symbols[0]: _frame([100.0, 94.0, 100.0, 106.0])}

    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _kind: Loader())


def test_a_preview_shows_the_plan_its_problems_and_the_replay(api_client, venue, listed) -> None:
    body = api_client.post("/bot/instances/preview", json={"kind": "grid", "symbol": "BTC/USDT", "params": GRID})
    assert body.status_code == 200, body.text
    preview = body.json()
    assert preview["plan"]["levels"] == [90.0, 95.0, 100.0, 105.0, 110.0]
    assert any("instance_orders_per_day" in item for item in preview["problems"])
    # 94 buys level 1, 100 sells it back, 106 sells level 2.
    assert preview["replay"]["grid_trades"] == 2
    refused = api_client.post("/bot/instances", json={"kind": "grid", "symbol": "BTC/USDT", "params": GRID})
    assert refused.status_code == 409 and "instance_orders_per_day" in refused.json()["detail"]


def test_instances_are_created_listed_and_stopped(api_client, venue, listed) -> None:
    saved = api_client.put("/bot/config", json={"limits": {"instance_orders_per_day": 40}})
    assert saved.status_code == 200, saved.text
    created = api_client.post("/bot/instances", json={"kind": "grid", "symbol": "BTC/USDT", "params": GRID})
    assert created.status_code == 200, created.text
    rows = api_client.get("/bot/instances").json()["rows"]
    assert rows[0]["status"] == "active" and rows[0]["position"]["strategy"] == "grid"
    stopped = api_client.post(f"/bot/instances/{created.json()['id']}/stop")
    assert stopped.status_code == 200 and stopped.json()["instance_status"] == "stopped"


def test_bad_instance_params_are_refused(api_client, venue, listed) -> None:
    bad = api_client.post("/bot/instances/preview", json={"kind": "grid", "symbol": "BTC/USDT", "params": {**GRID, "stop_price": 95}})
    assert bad.status_code == 422
    assert api_client.post("/bot/instances/preview", json={"kind": "dca", "symbol": "BTCUSDT", "params": {}}).status_code == 422


def test_armed_entries_go_through_the_api(api_client) -> None:
    from bot.api.app import ctx

    api_client.get("/bot/status")
    assert api_client.post("/bot/armed", json={"symbol": "BTC/USDT", "strategy": "trend", "trigger_price": 0}).status_code == 422
    refused = api_client.post("/bot/armed", json={"symbol": "BTC/USDT", "strategy": "trend", "trigger_price": 95})
    assert refused.status_code == 409
    assert ctx.runtime is not None
    armed_id = ctx.runtime.store.arm_entry("BTC/USDT", "trend", 95.0, "below", "2099-01-01T00:00:00+00:00")
    assert api_client.get("/bot/armed").json()["rows"][0]["id"] == armed_id
    assert api_client.delete(f"/bot/armed/{armed_id}").status_code == 200
    assert api_client.delete(f"/bot/armed/{armed_id}").status_code == 404

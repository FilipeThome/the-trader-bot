from __future__ import annotations

import time

from fastapi.testclient import TestClient

from bot.api.app import app, ctx
from bot.config import BotConfig


def test_opportunities_are_ranked_and_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    BotConfig().save()
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
    with TestClient(app) as client:
        assert ctx.runtime is not None
        ctx.runtime.store.replace_opportunities(rows)
        started = time.perf_counter()
        response = client.get("/bot/opportunities")
        elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < 0.2
    body = response.json()["rows"]
    assert len(body) == 50
    assert body[0]["score"] >= body[-1]["score"]
    assert [row["score"] for row in body] == sorted((row["score"] for row in body), reverse=True)

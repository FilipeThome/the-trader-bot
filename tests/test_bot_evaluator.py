from __future__ import annotations

from bot.evaluator import classify
from bot.strategies.base import Setup


def _setup(**overrides) -> Setup:
    payload = dict(
        strategy_id="trend",
        symbol="BTC/USDT",
        timeframe="4h",
        action="Comprar",
        direction=1.0,
        expected_return=0.04,
        sigma=0.01,
        stop_pct=0.01,
        target_pct=0.04,
        reason="rompimento",
        regime="trend_up",
        confidence=1.0,
    )
    payload.update(overrides)
    return Setup(**payload)


def test_ready_potential_and_blocked_follow_the_threshold() -> None:
    ready = classify(_setup())
    assert ready.status == "pronta"
    weak = classify(_setup(expected_return=0.001, sigma=0.05, confidence=0.2))
    assert weak.status == "potencial"
    blocked = classify(_setup(), blocked_reason="exposição total já em 58% do limite de 60%")
    assert blocked.status == "bloqueada"
    futures = classify(_setup(strategy_id="trend", market="usdm", direction=-1, regime="trend_down"))
    assert futures.status == "potencial"
    assert "futuros" in futures.reason

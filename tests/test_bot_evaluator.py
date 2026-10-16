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


def test_a_spot_sell_without_a_position_is_never_ready() -> None:
    short = classify(_setup(strategy_id="mean_reversion", direction=-1.0, regime="range"))
    assert short.status == "potencial"
    assert "venda a descoberto" in short.reason
    held = classify(_setup(strategy_id="mean_reversion", direction=-1.0, regime="range"), in_position=True)
    assert held.status == "em_posicao"


def test_backtest_trades_of_another_exit_policy_are_left_out() -> None:
    from bot.evaluator import estimate_win_rate

    params = {"wins": 15, "trades": 20, "exit_policy": "trailing"}
    assert estimate_win_rate(params, (3, 5), exit_policy="trailing", min_sample=20) == (19 / 27, 25)
    assert estimate_win_rate(params, (3, 5), exit_policy="target", min_sample=20) == (None, 5)
    assert estimate_win_rate(None, (3, 5), exit_policy="target", min_sample=5) == (4 / 7, 5)



def test_classify_names_the_blocker() -> None:
    assert classify(_setup()).blocker is None
    assert classify(_setup(expected_return=0.001, sigma=0.05, confidence=0.2)).blocker == "score"
    assert classify(_setup(regime="range", strategy_id="trend", direction=-1.0, market="spot")).blocker == "short"
    assert classify(_setup(regime="trend_down")).blocker == "regime"
    assert classify(_setup(), in_position=True).blocker == "in_position"

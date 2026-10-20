from __future__ import annotations

import pytest

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


def test_classify_uses_the_given_taker() -> None:
    from bot.evaluator import SPOT_TAKER, round_trip_cost, score_setup

    setup = _setup()
    default = classify(setup)
    dearer = classify(setup, taker=0.002)
    assert dearer.cost == pytest.approx(default.cost + 2 * (0.002 - SPOT_TAKER))
    assert dearer.net_return < default.net_return and dearer.score < default.score
    assert round_trip_cost(setup, taker=0.002) == pytest.approx(dearer.cost)
    assert score_setup(setup, taker=0.002) == pytest.approx(dearer.score, abs=1e-4)
    assert classify(setup, taker=SPOT_TAKER) == default
    # A wide enough fee turns a ready row into a cost blocker.
    assert classify(setup, taker=0.007).blocker == "cost"
    # USD-M legs keep the futures fee; the spot taker does not apply to them.
    futures = _setup(market="usdm")
    assert round_trip_cost(futures, taker=0.002) == round_trip_cost(futures)


def test_new_opportunity_fields_default_and_come_last() -> None:
    row = classify(_setup(), slippage=0.0007).to_dict()
    added = ["size_limit", "size_caps", "signal_age_seconds", "cluster", "slippage_est", "plan_target_pct"]
    assert list(row)[-len(added):] == added
    assert row["slippage_est"] == 0.0007
    assert all(row[name] is None for name in added if name != "slippage_est")

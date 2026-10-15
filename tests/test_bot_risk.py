from __future__ import annotations

import pytest

from bot.config import RiskLimits
from bot.evaluator import Opportunity
from bot.risk import RiskManager


def _opportunity() -> Opportunity:
    return Opportunity(
        symbol="BTC/USDT",
        strategy="trend",
        action="Comprar",
        score=0.8,
        net_return=0.02,
        risk_pct=0.01,
        status="pronta",
        reason="rompimento",
        timeframe="4h",
        direction=1,
        cost=0.002,
        gross_return=0.022,
    )


def _manager() -> RiskManager:
    return RiskManager(RiskLimits(), capital_usd=10_000)


def _assess(manager: RiskManager, **overrides):
    payload = dict(
        open_positions=[],
        exposure_usd=0,
        day_pnl=0,
        peak_equity=10_000,
        equity=10_000,
        trades_today=0,
        returns_by_symbol={},
        stale=False,
        signal_only=False,
    )
    payload.update(overrides)
    return manager.assess(_opportunity(), **payload)


def test_each_risk_limit_blocks_its_case() -> None:
    manager = _manager()
    assert _assess(manager).allowed
    assert "atraso" in _assess(manager, stale=True).reason
    assert "somente sinal" in _assess(manager, signal_only=True).reason
    paused = _assess(manager, day_pnl=-400)
    assert paused.pause_day and not paused.allowed
    halted = _assess(manager, peak_equity=10_000, equity=8_900)
    assert halted.halt and not halted.allowed
    assert "trades" in _assess(manager, trades_today=20).reason
    positions = [{"symbol": f"S{i}/USDT"} for i in range(5)]
    assert "simultâneas" in _assess(manager, open_positions=positions).reason
    assert "exposição" in _assess(manager, exposure_usd=6_000).reason
    returns = {
        "BTC/USDT": [0.01, 0.02, -0.01, 0.03, 0.01, 0.02],
        "ETH/USDT": [0.01, 0.02, -0.01, 0.03, 0.01, 0.02],
        "SOL/USDT": [0.01, 0.02, -0.01, 0.03, 0.01, 0.02],
    }
    correlated = [{"symbol": "ETH/USDT"}, {"symbol": "SOL/USDT"}]
    verdict = _assess(manager, open_positions=correlated, returns_by_symbol=returns)
    assert "correlacionadas" in verdict.reason


def test_order_is_capped_at_two_percent() -> None:
    size = _manager().size_for(_opportunity())
    assert size <= 10_000 * 0.02
    assert size > 0


@pytest.mark.parametrize("equity", [10_000, 9_500])
def test_drawdown_halt_is_only_past_ten_percent(equity: float) -> None:
    verdict = _assess(_manager(), peak_equity=10_000, equity=equity)
    assert verdict.halt is (equity <= 9_000)

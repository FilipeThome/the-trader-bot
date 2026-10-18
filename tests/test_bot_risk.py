from __future__ import annotations

import pytest

from bot.config import RiskLimits
from bot.evaluator import Opportunity
from bot.risk import RiskManager, protect


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


def test_a_pair_waits_out_its_cooldown_after_a_loss() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    losses = [{"symbol": "BTC/USDT", "closed_at": (now - timedelta(minutes=30)).isoformat(), "realized_pnl": -1.0}]
    btc, eth = _opportunity(), _opportunity()
    eth.symbol = "ETH/USDT"
    protect([btc, eth], losses, RiskLimits(pair_cooldown_minutes=120), now)
    assert btc.status == "bloqueada" and "pausa" in btc.reason and eth.status == "pronta"
    later = _opportunity()
    protect([later], losses, RiskLimits(pair_cooldown_minutes=120), now + timedelta(minutes=91))
    assert later.status == "pronta"


def test_the_loss_guard_pauses_every_entry_and_zero_turns_it_off() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    losses = [
        {"symbol": symbol, "closed_at": (now - timedelta(hours=hours)).isoformat(), "realized_pnl": -1.0}
        for symbol, hours in (("SOL/USDT", 5), ("ETH/USDT", 3), ("XRP/USDT", 1))
    ]
    row = _opportunity()
    protect([row], losses, RiskLimits(loss_guard_trades=3, pair_cooldown_minutes=0), now)
    assert row.status == "bloqueada" and "3 perdas" in row.reason
    off = _opportunity()
    protect([off], losses, RiskLimits(loss_guard_trades=0, pair_cooldown_minutes=0), now)
    assert off.status == "pronta"
    # The oldest loss ages out of the six-hour window.
    aged = _opportunity()
    protect([aged], losses, RiskLimits(loss_guard_trades=3, pair_cooldown_minutes=0), now + timedelta(hours=1, minutes=1))
    assert aged.status == "pronta"


def test_the_breakeven_hit_rate_is_where_kelly_turns_positive() -> None:
    from bot.evaluator import Opportunity
    from bot.risk import RiskManager, breakeven_win_rate

    def row(win_rate: float | None, net: float = 0.0125) -> Opportunity:
        return Opportunity(
            symbol="BTC/USDT", strategy="intraday_momentum", action="Comprar", score=1.0, net_return=net,
            risk_pct=0.01, status="pronta", reason="r", timeframe="15m", direction=1, cost=0.0025,
            gross_return=0.015, win_rate=win_rate,
        )

    assert breakeven_win_rate(row(None)) == pytest.approx(1 / 2.25)
    assert breakeven_win_rate(row(None, net=0.0)) is None
    manager = RiskManager(RiskLimits(), 1000.0)
    below, above = 1 / 2.25 - 0.001, 1 / 2.25 + 0.001
    assert manager.kelly_risk(row(below)) < 0 < manager.kelly_risk(row(above))  # type: ignore[operator]

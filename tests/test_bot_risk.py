from __future__ import annotations

import inspect
import math
from decimal import Decimal
from typing import Any

import pytest

from bot.config import RiskLimits
from bot.evaluator import Opportunity
from bot.risk import (
    SIZE_LIMITS,
    RiskManager,
    RiskState,
    allocate,
    mc_drawdown,
    mc_drawdown_ok,
    planned_notional,
    portfolio_vol,
    protect,
    vol_target_cap,
    vol_throttle,
)


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


ACCOUNT_FIGURES = ("day_pnl", "peak_equity", "equity")


def _state_of(manager: RiskManager, overrides: dict[str, Any], capital: float) -> RiskState:
    """The risk state the cycle reads from the account figures, taken out of ``overrides``
    (``assess`` reads only the state, never the figures)."""
    figures: dict[str, float] = {"day_pnl": 0.0, "peak_equity": capital, "equity": capital}
    figures.update({key: overrides.pop(key) for key in ACCOUNT_FIGURES if key in overrides})
    return manager.portfolio_state(
        day_change=figures["day_pnl"], peak_equity=figures["peak_equity"], equity=figures["equity"]
    )


def _assess(manager: RiskManager, **overrides):
    state = _state_of(manager, overrides, 10_000)
    payload = dict(
        open_positions=[],
        exposure_usd=0,
        trades_today=0,
        returns_by_symbol={},
        stale=False,
        signal_only=False,
    )
    payload.update(overrides)
    payload.setdefault("state", state)
    return manager.assess(_opportunity(), **payload)


def test_each_risk_limit_blocks_its_case() -> None:
    manager = _manager()
    assert _assess(manager).allowed
    assert "atraso" in _assess(manager, stale=True).reason
    assert "somente sinal" in _assess(manager, signal_only=True).reason
    paused = _assess(manager, day_pnl=-400)
    assert paused.pause_day and not paused.allowed
    reducing = _assess(manager, peak_equity=10_000, equity=8_900)
    assert reducing.reducing and not reducing.halt and not reducing.allowed
    halted = _assess(manager, peak_equity=10_000, equity=8_500)
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
    # A saved bot.json keeps its 2% order cap (fresh installs default to 25%).
    size = RiskManager(RiskLimits(max_order_notional_pct=0.02), capital_usd=10_000).size_for(_opportunity())
    assert size <= 10_000 * 0.02
    assert size > 0


@pytest.mark.parametrize("equity", [10_000, 9_500, 9_000, 8_500])
def test_drawdown_halt_is_only_at_the_halt_step(equity: float) -> None:
    verdict = _assess(_manager(), peak_equity=10_000, equity=equity)
    assert verdict.halt is (equity <= 8_500)
    assert verdict.allowed is (equity > 9_000)


def test_assess_requires_the_risk_state() -> None:
    # No call may skip the stepped account limits: there is no stateless fallback.
    payload: dict[str, Any] = dict(
        open_positions=[], exposure_usd=0.0, trades_today=0, returns_by_symbol={}, stale=False, signal_only=False,
    )
    with pytest.raises(TypeError, match="state"):
        _manager().assess(_opportunity(), **payload)


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


# -- sizing (6.1) ------------------------------------------------------------

CAPITAL = 10_000.0


def _row(symbol: str = "BTC/USDT", *, stop: float = 0.02, win_rate: float | None = None, net: float = 0.04,
         score: float = 1.0, size: float = 0.0) -> Opportunity:
    return Opportunity(
        symbol=symbol, strategy="trend", action="Comprar", score=score, net_return=net, risk_pct=stop,
        status="pronta", reason="setup", timeframe="4h", direction=1, cost=0.002, gross_return=net + 0.002,
        size_usd=size, win_rate=win_rate,
    )


def _uncapped(**overrides: Any) -> RiskLimits:
    """Order ceiling at 100% and the volatility cap and target off: only the stop risk sizes."""
    return RiskLimits(**{"max_order_notional_pct": 1.0, "vol_cap_pct": 0.0, "target_vol_annual": 0.0, **overrides})


def _defaults(**overrides: Any) -> RiskLimits:
    """The defaults once the safety ceiling is 25% (the W3-A default)."""
    return RiskLimits(**{"max_order_notional_pct": 0.25, **overrides})


def _verdict(manager: RiskManager, row: Opportunity, **overrides: Any):
    state = _state_of(manager, overrides, CAPITAL)
    payload: dict[str, Any] = dict(
        open_positions=[], exposure_usd=0.0, trades_today=0, returns_by_symbol={}, stale=False, signal_only=False,
    )
    payload.update(overrides)
    payload.setdefault("state", state)
    return manager.assess(row, **payload)


def test_risk_sizing_buys_half_the_capital_at_a_2pct_stop_when_no_cap_binds() -> None:
    decision = RiskManager(_uncapped(), CAPITAL).sizing(_row(stop=0.02), atr_daily_pct=0.10)
    # 1% of the capital lost at a 2% stop: half the capital. The volatility cap is off.
    assert decision.size_usd == pytest.approx(5_000.0)
    assert decision.binding == "risk"
    assert decision.caps == {"risk": 5_000.0, "safety": 9_000.0}


def test_a_4pct_stop_halves_the_order() -> None:
    manager = RiskManager(_uncapped(), CAPITAL)
    assert manager.size_for(_row(stop=0.04)) == pytest.approx(manager.size_for(_row(stop=0.02)) / 2)
    assert manager.size_for(_row(stop=0.04)) == pytest.approx(2_500.0)


def test_the_default_safety_cap_binds_and_is_reported() -> None:
    manager = RiskManager(_defaults(), CAPITAL)
    decision = manager.sizing(_row(stop=0.02))
    # 25% of the capital less the 10% headroom for the stop.
    assert decision.size_usd == pytest.approx(2_250.0)
    assert decision.binding == "safety" and SIZE_LIMITS[decision.binding] == "teto por ordem"
    verdict = _verdict(manager, _row(stop=0.02))
    assert verdict.allowed and verdict.size_usd == 2_250.0 and verdict.size_limit == "safety"
    assert verdict.caps["risk"] == 5_000.0 and verdict.caps["exposure"] == 6_000.0


def test_a_wide_stop_is_sized_by_risk_under_the_cap() -> None:
    decision = RiskManager(_defaults(), CAPITAL).sizing(_row(stop=0.08))
    assert decision.size_usd == pytest.approx(1_250.0) and decision.binding == "risk"


def test_venue_cap_binds_below_the_safety_cap() -> None:
    manager = RiskManager(_defaults(), CAPITAL)
    # The mandate or testnet cap comes as a Decimal; an entry keeps the same 10% headroom under it.
    decision = manager.sizing(_row(stop=0.02), venue_cap_usd=Decimal("1000"))
    assert decision.size_usd == pytest.approx(900.0)
    assert decision.binding == "venue" and SIZE_LIMITS["venue"] == "teto da corretora"
    assert decision.caps["safety"] == 2_250.0 and decision.caps["venue"] == 900.0
    verdict = _verdict(manager, _row(stop=0.02), venue_cap_usd=1000.0)
    assert verdict.allowed and verdict.size_usd == 900.0 and verdict.size_limit == "venue"
    # A cap above the safety ceiling does not bind; no cap known is no venue cap.
    assert manager.sizing(_row(stop=0.02), venue_cap_usd=50_000).binding == "safety"
    assert "venue" not in manager.sizing(_row(stop=0.02)).caps
    # A zero or unreadable cap allows no order at all.
    for cap in (0, math.nan, -5):
        refused = _verdict(manager, _row(stop=0.02), venue_cap_usd=cap)
        assert not refused.allowed and "teto da corretora" in refused.reason and refused.size_limit == "venue"


def test_the_daily_atr_cap_shrinks_a_volatile_pair() -> None:
    manager = RiskManager(_defaults(), CAPITAL)
    # A 10% daily ATR: one ATR of a 1,000 USDT order costs 1% of the capital.
    volatile = manager.sizing(_row(stop=0.02), atr_daily_pct=0.10)
    assert volatile.size_usd == pytest.approx(1_000.0) and volatile.binding == "volatility"
    # BTC-like 3% ATR: 3,333 USDT, above the safety ceiling, so it does not bind.
    calm = manager.sizing(_row(stop=0.02), atr_daily_pct=0.03)
    assert calm.binding == "safety" and calm.caps["volatility"] == pytest.approx(3_333.33)
    for unknown in (None, 0.0, math.nan, -0.1):
        assert manager.sizing(_row(stop=0.02), atr_daily_pct=unknown).binding == "safety"
    off = RiskManager(_defaults(vol_cap_pct=0.0), CAPITAL).sizing(_row(stop=0.02), atr_daily_pct=0.10)
    assert off.binding == "safety" and "volatility" not in off.caps


def test_kelly_lowers_the_share_and_is_reported() -> None:
    manager = RiskManager(_uncapped(kelly_fraction=0.25), CAPITAL)
    # p=0.42, b=1.5: a quarter of full Kelly is 0.83%, below the 1% trade risk.
    thin = manager.sizing(_row(stop=0.04, net=0.06, win_rate=0.42))
    share = 0.25 * (0.42 - 0.58 / 1.5)
    assert thin.binding == "kelly" and SIZE_LIMITS["kelly"] == "Kelly"
    assert thin.size_usd == pytest.approx(CAPITAL * share / 0.04)
    # p=0.45: a quarter Kelly of 2.1% is above the trade risk, which binds.
    rich = manager.sizing(_row(stop=0.04, net=0.06, win_rate=0.45))
    assert rich.binding == "risk" and rich.size_usd == pytest.approx(2_500.0)
    # An unreadable hit rate estimates no edge.
    assert manager.sizing(_row(stop=0.04, net=0.06, win_rate=math.nan)).size_usd == 0.0


def test_the_risk_scale_shrinks_the_risk_size_only() -> None:
    half = RiskManager(_uncapped(), CAPITAL, risk_scale=0.5)
    assert half.sizing(_row(stop=0.02)).size_usd == pytest.approx(2_500.0)
    # Under the safety ceiling the scale changes nothing: the ceiling is not a risk share.
    assert RiskManager(_defaults(), CAPITAL, risk_scale=0.5).sizing(_row(stop=0.02)).binding == "safety"
    assert RiskManager(_uncapped(), CAPITAL, risk_scale=3.0).risk_scale == 1.0
    with pytest.raises(ValueError):
        RiskManager(_uncapped(), CAPITAL, risk_scale=math.nan)


def test_a_bad_stop_or_capital_sizes_nothing() -> None:
    for stop in (0.0, -0.01, math.nan):
        decision = RiskManager(_defaults(), CAPITAL).sizing(_row(stop=stop))
        assert decision.size_usd == 0.0 and decision.binding == "risk"
    refused = _verdict(RiskManager(_defaults(), CAPITAL, 0.0), _row())
    assert not refused.allowed and "exposição" in refused.reason


def _alternating(sigma_annual: float, n: int = 64, sign: float = 1.0) -> list[float]:
    """Hourly returns of ±s: their zero-mean EWMA variance is exactly s², so the annual σ is exact."""
    step = sigma_annual / math.sqrt(8760)
    return [sign * (step if i % 2 == 0 else -step) for i in range(n)]


def test_vol_target_caps_new_exposure_when_portfolio_vol_is_high() -> None:
    limits = _defaults()  # 60% a year on the capital: 6,000 USD of annual σ.
    returns = {"ETH/USDT": _alternating(1.5), "SOL/USDT": _alternating(1.5)}
    assert portfolio_vol({"ETH/USDT": 3_000.0}, returns) == pytest.approx(4_500.0)
    # SOL moves with ETH: σ = 1.5·(3000 + x) ≤ 6000 leaves 1,000 USD of new exposure.
    cap = vol_target_cap(limits, CAPITAL, {"ETH/USDT": 3_000.0}, "SOL/USDT", 2_250.0, returns)
    assert cap == pytest.approx(4_000.0)
    manager = RiskManager(limits, CAPITAL)
    verdict = _verdict(manager, _row("SOL/USDT"), exposure_usd=3_000.0, vol_target_cap_usd=cap)
    assert verdict.allowed and verdict.size_usd == pytest.approx(1_000.0) and verdict.size_limit == "vol_target"
    # A book already above the target gets no new exposure; nothing held is sold.
    full = vol_target_cap(limits, CAPITAL, {"ETH/USDT": 5_000.0}, "SOL/USDT", 2_250.0, returns)
    assert full == pytest.approx(5_000.0)
    refused = _verdict(manager, _row("SOL/USDT"), exposure_usd=5_000.0, vol_target_cap_usd=full)
    assert not refused.allowed and "alvo de volatilidade" in refused.reason and refused.size_limit == "vol_target"
    # A pair moving against the book lowers its volatility: the target does not bind.
    hedge = {"ETH/USDT": _alternating(1.5), "SOL/USDT": _alternating(1.5, sign=-1.0)}
    assert vol_target_cap(limits, CAPITAL, {"ETH/USDT": 3_000.0}, "SOL/USDT", 2_250.0, hedge) is None


def test_a_hedge_gets_room_on_a_book_above_the_target_only_until_vol_returns_to_it() -> None:
    limits = _defaults()  # 6,000 USD of annual σ.
    hedge = {"ETH/USDT": _alternating(1.5), "SOL/USDT": _alternating(1.5, sign=-1.0)}
    held = {"ETH/USDT": 5_000.0}
    assert portfolio_vol(held, hedge) == pytest.approx(7_500.0)  # above the target
    # SOL moves against ETH: σ = 1.5·|5000 − x| ≤ 6000 for x in [1000, 9000]; the room is 9,000.
    cap = vol_target_cap(limits, CAPITAL, held, "SOL/USDT", 500.0, hedge)
    assert cap == pytest.approx(14_000.0)
    room = cap - 5_000.0
    assert portfolio_vol({**held, "SOL/USDT": room}, hedge) == pytest.approx(6_000.0)
    assert portfolio_vol({**held, "SOL/USDT": room + 100.0}, hedge) > 6_000.0
    # Every order within the room leaves the book below its current σ.
    for size in (100.0, 1_000.0, 5_000.0, room):
        assert portfolio_vol({**held, "SOL/USDT": size}, hedge) < 7_500.0
    # A planned order past the room is cut to it.
    assert vol_target_cap(limits, CAPITAL, held, "SOL/USDT", 10_000.0, hedge) == pytest.approx(14_000.0)


def test_mc_drawdown_reads_p95_in_r_at_the_risk_per_trade() -> None:
    limits = RiskLimits(risk_per_trade_pct=0.01, max_drawdown_pct=0.10)
    assert mc_drawdown(5.0, limits) == pytest.approx(0.05)
    assert mc_drawdown(7, limits) == pytest.approx(0.07)
    for unreadable in (None, True, False, "5", math.nan, math.inf, -math.inf):
        assert mc_drawdown(unreadable, limits) is None


def test_mc_drawdown_ok_needs_a_readable_p95_strictly_under_the_limit() -> None:
    limits = RiskLimits(risk_per_trade_pct=0.01, max_drawdown_pct=0.10)
    assert mc_drawdown_ok(5.0, limits) and mc_drawdown_ok(9.99, limits) and mc_drawdown_ok(0, limits)
    assert not mc_drawdown_ok(10.0, limits)  # at the limit
    assert not mc_drawdown_ok(12.0, limits)
    # A missing p95 proves nothing: never ok (it used to read as 0 in the panel).
    for unreadable in (None, True, "5", math.nan, math.inf):
        assert not mc_drawdown_ok(unreadable, limits)
    # The same p95 fails once the risk per trade grows.
    assert not mc_drawdown_ok(5.0, RiskLimits(risk_per_trade_pct=0.02, max_drawdown_pct=0.10))


def test_vol_target_off_or_short_history_never_binds() -> None:
    returns = {"ETH/USDT": _alternating(1.5), "SOL/USDT": _alternating(1.5)}
    held = {"ETH/USDT": 3_000.0}
    assert vol_target_cap(_defaults(target_vol_annual=0.0), CAPITAL, held, "SOL/USDT", 2_250.0, returns) is None
    short = {"ETH/USDT": _alternating(1.5), "SOL/USDT": _alternating(1.5, n=47)}
    assert vol_target_cap(_defaults(), CAPITAL, held, "SOL/USDT", 2_250.0, short) is None
    assert vol_target_cap(_defaults(), CAPITAL, held, "SOL/USDT", 2_250.0, {"ETH/USDT": _alternating(1.5)}) is None
    assert vol_target_cap(_defaults(), CAPITAL, held, "SOL/USDT", 2_250.0, {"SOL/USDT": _alternating(1.5)}) is None
    # Unreadable hours are dropped for every series; too few aligned ones left is a short history.
    holed = _alternating(1.5, n=60)
    for index in range(0, 26, 2):
        holed[index] = math.nan
    assert vol_target_cap(_defaults(), CAPITAL, held, "SOL/USDT", 2_250.0, {**returns, "SOL/USDT": holed}) is None
    assert portfolio_vol(held, {"ETH/USDT": holed}) is None
    # A pair with no volatility adds none; an empty book has none.
    flat = {**returns, "SOL/USDT": [0.0] * 64}
    assert vol_target_cap(_defaults(), CAPITAL, held, "SOL/USDT", 2_250.0, flat) is None
    assert portfolio_vol({}, returns) == 0.0
    assert "vol_target" not in _verdict(RiskManager(_defaults(), CAPITAL), _row(), vol_target_cap_usd=None).caps


def _closes(returns: list[float]) -> list[float]:
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1 + value))
    return closes


def test_vol_throttle_halves_the_size_when_7d_vol_doubles() -> None:
    calm = [0.01 if i % 2 == 0 else -0.01 for i in range(257)]
    hot = [0.04 if i % 2 == 0 else -0.04 for i in range(42)]
    # The last seven days of 4h returns run at more than twice the volatility of the whole history.
    assert vol_throttle(_closes(calm + hot), 2.0) == 0.5
    assert vol_throttle(_closes(calm + calm[:42]), 2.0) == 1.0
    assert vol_throttle(_closes(calm + hot), 0.0) == 1.0
    assert vol_throttle(_closes(hot + hot[:30]), 2.0) == 1.0  # under two weeks of returns
    assert vol_throttle([100.0] * 300, 2.0) == 1.0
    holed = _closes(calm + hot)
    holed[10] = math.nan
    assert vol_throttle(holed, 2.0) == 0.5
    decision = RiskManager(_defaults(), CAPITAL).sizing(_row(stop=0.02), throttle=0.5)
    assert decision.size_usd == pytest.approx(1_125.0) and decision.binding == "throttle"
    assert decision.caps["safety"] == 2_250.0 and decision.caps["throttle"] == 1_125.0
    assert RiskManager(_defaults(), CAPITAL).sizing(_row(stop=0.02), throttle=math.nan).binding == "safety"


def test_exposure_shrinks_then_refuses_and_open_risk_now_binds() -> None:
    manager = RiskManager(_defaults(), CAPITAL)
    shrunk = _verdict(manager, _row(stop=0.02), exposure_usd=5_000.0)
    assert shrunk.allowed and shrunk.size_usd == pytest.approx(1_000.0) and shrunk.size_limit == "exposure"
    full = _verdict(manager, _row(stop=0.02), exposure_usd=6_000.0)
    assert not full.allowed and "exposição" in full.reason and full.size_limit == "exposure"
    assert not _verdict(manager, _row(stop=0.02), exposure_usd=math.nan).allowed
    # Risk-sized orders each risk the full 1%: the 6% open risk now binds at the sixth entry.
    rows = [_row(f"P{index}/USDT", stop=0.0625, size=160.0, score=10.0 - index) for index in range(7)]
    limits = _defaults(max_positions=10, max_entries_per_cycle=10)
    accepted = allocate(rows, cash=10_000.0, live=[], capital=1_000.0, limits=limits, min_notional=lambda _symbol: 5.0)
    assert len(accepted) == 6
    assert rows[6].status == "bloqueada" and "risco da carteira" in rows[6].reason


def test_planned_notional_is_the_stop_risk_size_under_the_ceiling() -> None:
    assert planned_notional(RiskLimits(max_order_notional_pct=0.02), CAPITAL, 0.01) == pytest.approx(180.0)
    assert planned_notional(_defaults(), CAPITAL, 0.08) == pytest.approx(1_250.0)
    assert planned_notional(_defaults(), CAPITAL, 0.0) == pytest.approx(2_250.0)
    assert planned_notional(_defaults(), math.nan, 0.02) == 0.0


# -- drawdown steps (6.7) ----------------------------------------------------


@pytest.mark.parametrize(
    ("equity", "level", "scale", "entries"),
    [
        (10_000, "normal", 1.0, None),
        (9_600, "normal", 1.0, None),
        (9_500, "cautious", 0.5, 1),
        (9_000, "reducing", 0.0, 0),
        (8_500, "halt", 0.0, 0),
    ],
)
def test_portfolio_state_levels(equity: float, level: str, scale: float, entries: int | None) -> None:
    state = _manager().portfolio_state(day_change=0.0, peak_equity=10_000, equity=equity)
    assert (state.level, state.risk_scale, state.max_entries) == (level, scale, entries)
    assert state.drawdown == pytest.approx((10_000 - equity) / 10_000)
    assert not state.pause_day
    if level == "reducing":
        assert state.reason.startswith("modo só reduzir: drawdown de 10%")
    if level == "cautious":
        assert "modo cauteloso" in state.reason


def test_the_daily_loss_and_unreadable_equity() -> None:
    manager = _manager()
    paused = manager.portfolio_state(day_change=-300.0, peak_equity=10_000, equity=10_000)
    assert paused.pause_day and paused.level == "normal" and "perda diária" in paused.reason
    assert not manager.portfolio_state(day_change=-299.0, peak_equity=10_000, equity=10_000).pause_day
    for bad in (math.nan, math.inf):
        blind = manager.portfolio_state(day_change=0.0, peak_equity=10_000, equity=bad)
        assert blind.level == "reducing" and blind.max_entries == 0
    assert manager.portfolio_state(day_change=0.0, peak_equity=0.0, equity=10_000).level == "normal"


def test_reducing_is_kept_until_drawdown_recovers_below_the_first_step() -> None:
    manager = _manager()

    def level(equity: float, previous: Any) -> str:
        return manager.portfolio_state(day_change=0.0, peak_equity=10_000, equity=equity, previous=previous).level

    reducing = manager.portfolio_state(day_change=0.0, peak_equity=10_000, equity=9_000)
    assert level(9_300, reducing) == "reducing"
    assert level(9_300, {"level": "reducing", "since": "2026-10-20T00:00:00+00:00"}) == "reducing"
    assert level(9_300, "halt") == "reducing"
    assert level(9_300, None) == "cautious" and level(9_300, "cautious") == "cautious"
    assert level(9_510, reducing) == "normal"
    # With the cautious step off, reducing clears below the reducing step itself.
    off = RiskManager(RiskLimits(dd_reduce_pct=0.0), 10_000)
    state = off.portfolio_state(day_change=0.0, peak_equity=10_000, equity=9_100, previous="reducing")
    assert state.level == "normal"


def test_assess_obeys_the_state_not_the_account_figures() -> None:
    manager = _manager()
    # The account figures are not an input: the state is the only account limit.
    assert not set(ACCOUNT_FIGURES) & set(inspect.signature(RiskManager.assess).parameters)
    # A normal state lets a row through.
    normal = RiskState("normal", 0.0, 1.0, None, "")
    assert _assess(manager, state=normal).allowed
    # 10% reduces, and 15% halts.
    reducing = manager.portfolio_state(day_change=0.0, peak_equity=10_000, equity=9_000)
    verdict = _assess(manager, state=reducing)
    assert not verdict.allowed and verdict.reducing and not verdict.halt and "só reduzir" in verdict.reason
    halt = manager.portfolio_state(day_change=0.0, peak_equity=10_000, equity=8_500)
    assert _assess(manager, state=halt).halt
    paused_state = manager.portfolio_state(day_change=-300.0, peak_equity=10_000, equity=10_000)
    pause = _assess(manager, state=paused_state)
    assert pause.pause_day and not pause.allowed and not pause.halt


def test_the_cautious_state_halves_the_risk_size() -> None:
    manager = RiskManager(_uncapped(), CAPITAL)
    cautious = manager.portfolio_state(day_change=0.0, peak_equity=CAPITAL, equity=9_400)
    assert cautious.level == "cautious"
    assert _verdict(manager, _row(stop=0.02)).size_usd == pytest.approx(5_000.0)
    halved = _verdict(manager, _row(stop=0.02), state=cautious)
    assert halved.allowed and halved.size_usd == pytest.approx(2_500.0) and halved.size_limit == "risk"
    assert RiskState("normal", 0.0, 1.0, None, "").max_entries is None


# -- correlation and the BTC cluster (6.8) -----------------------------------

# Three orthogonal zero-mean series over 64 hours: periods of 2, 4 and 8 hours.
_X = [0.01 if i % 2 == 0 else -0.01 for i in range(64)]
_Y = [0.01 if (i // 2) % 2 == 0 else -0.01 for i in range(64)]
_Z = [0.01 if (i // 4) % 2 == 0 else -0.01 for i in range(64)]
#: ETH and SOL each follow BTC at ρ = 0.78: above the 0.70 cluster line, below the 0.80
#: correlation threshold, and only 0.22 with each other.
_RETURNS = {
    "BTC/USDT": _X,
    "ETH/USDT": [x + 0.8 * y for x, y in zip(_X, _Y)],
    "SOL/USDT": [x - 0.8 * y for x, y in zip(_X, _Y)],
    "DOGE/USDT": _Z,
}
_BTC_OPEN = {"symbol": "BTC/USDT", "status": "open", "qty": 1, "avg_price": 100.0, "stop_price": 85.0}


def _cluster_rows() -> list[Opportunity]:
    return [_row(symbol, size=500.0, score=score) for symbol, score in (("ETH/USDT", 3.0), ("SOL/USDT", 2.0), ("DOGE/USDT", 1.0))]


def _allocate_rows(rows: list[Opportunity], *, live: list[dict[str, Any]] | None = None, **overrides: Any) -> list[Opportunity]:
    limits = overrides.pop("limits", RiskLimits(max_entries_per_cycle=5))
    return allocate(rows, cash=10_000.0, live=live or [], capital=1_000.0, limits=limits,
                    min_notional=lambda _symbol: 5.0, **overrides)


def test_two_correlated_rows_of_the_same_cycle_are_not_both_accepted() -> None:
    rows = [_row("ETH/USDT", size=100.0, score=2.0), _row("SOL/USDT", size=100.0, score=1.0)]
    returns = {"BTC/USDT": _X, "ETH/USDT": _Y, "SOL/USDT": _Y}
    accepted = _allocate_rows(rows, returns_by_symbol=returns, limits=RiskLimits(max_correlated=1, max_entries_per_cycle=5))
    assert [row.symbol for row in accepted] == ["ETH/USDT"]
    assert rows[1].status == "bloqueada" and "correlacionadas" in rows[1].reason and "deste ciclo" in rows[1].reason
    # A held pair counts the same as one accepted in this cycle.
    held = [_row("SOL/USDT", size=100.0)]
    live = [{"symbol": "ETH/USDT", "status": "open", "qty": 1, "avg_price": 100.0, "stop_price": 99.0}]
    limits = RiskLimits(max_correlated=1)
    assert _allocate_rows(held, live=live, returns_by_symbol=returns, limits=limits) == []


def test_btc_cluster_risk_is_capped_at_half_the_open_risk() -> None:
    rows = _cluster_rows()
    # BTC risks 15 to its stop; each row 500 × 2% = 10. The cluster may risk 3% of 1,000 = 30.
    accepted = _allocate_rows(rows, live=[_BTC_OPEN], returns_by_symbol=_RETURNS)
    assert [row.symbol for row in accepted] == ["ETH/USDT", "DOGE/USDT"]
    assert rows[1].status == "bloqueada" and rows[1].reason == "risco do cluster BTC no limite de 3% do capital até os stops"
    # Without BTC held the cluster has room for both members.
    again = _cluster_rows()
    assert len(_allocate_rows(again, returns_by_symbol=_RETURNS)) == 3


def test_rows_report_their_cluster() -> None:
    rows = _cluster_rows()
    _allocate_rows(rows, live=[_BTC_OPEN], returns_by_symbol=_RETURNS)
    assert [row.cluster for row in rows] == ["BTC", "BTC", None]
    btc = [_row("BTC/USDT", size=100.0)]
    _allocate_rows(btc, returns_by_symbol={})
    assert btc[0].cluster == "BTC" and btc[0].status == "pronta"


def test_without_returns_allocation_is_unchanged() -> None:
    plain, explicit = _cluster_rows(), _cluster_rows()
    first = _allocate_rows(plain, live=[_BTC_OPEN])
    second = _allocate_rows(explicit, live=[_BTC_OPEN], returns_by_symbol=None)
    assert [row.symbol for row in first] == [row.symbol for row in second] == ["ETH/USDT", "SOL/USDT", "DOGE/USDT"]
    assert all(row.cluster is None for row in plain + explicit)
    assert [row.size_usd for row in plain] == [row.size_usd for row in explicit] == [500.0, 500.0, 500.0]

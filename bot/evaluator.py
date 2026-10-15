"""Opportunity score. Net of fees, slippage and funding, per unit of risk."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from bot.strategies.base import Setup

SPOT_TAKER = 0.001
FUTURES_TAKER = 0.0005
SCORE_READY = 0.3
COST_MULTIPLE = 2.0
MIN_REWARD_RISK = 1.5


def regime_factor(strategy_id: str, regime: str, direction: float) -> float:
    if strategy_id == "trend":
        if regime == "range":
            return 0.5
        if (direction > 0 and regime == "trend_up") or (direction < 0 and regime == "trend_down"):
            return 1.0
        return 0.0
    if strategy_id == "mean_reversion":
        if regime == "range":
            return 1.0
        if regime in {"trend_up", "trend_down"}:
            return 0.0
        return 0.5
    if strategy_id == "funding_carry":
        return 1.0 if regime == "funding" else 0.0
    return 0.0


def round_trip_cost(setup: Setup, *, slippage: float = 0.0005, funding: float = 0.0) -> float:
    fee = FUTURES_TAKER if setup.market == "usdm" else SPOT_TAKER
    legs = 2 if setup.strategy_id == "funding_carry" else 1
    return legs * 2 * fee + slippage + max(funding, 0.0)


def score_setup(setup: Setup, *, slippage: float = 0.0005, funding: float = 0.0) -> float:
    cost = round_trip_cost(setup, slippage=slippage, funding=funding)
    sigma = setup.sigma if setup.sigma > 0 else 1.0
    factor = regime_factor(setup.strategy_id, setup.regime, setup.direction)
    net = setup.expected_return - cost
    if sigma <= 0:
        return 0.0
    return (net / sigma) * setup.confidence * factor


@dataclass
class Opportunity:
    symbol: str
    strategy: str
    action: str
    score: float
    net_return: float
    risk_pct: float
    status: str
    reason: str
    timeframe: str
    direction: float
    cost: float
    gross_return: float
    data_age_seconds: float | None = None
    size_usd: float = 0.0
    alphas: dict[str, float] | None = None
    stop_price: float | None = None
    target_price: float | None = None
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(
    setup: Setup,
    *,
    slippage: float = 0.0005,
    funding: float = 0.0,
    futures_enabled: bool = False,
    blocked_reason: str | None = None,
    ignored: bool = False,
    in_position: bool = False,
    score_threshold: float = SCORE_READY,
    cost_multiple: float = COST_MULTIPLE,
    min_reward_risk: float = MIN_REWARD_RISK,
) -> Opportunity:
    cost = round_trip_cost(setup, slippage=slippage, funding=funding)
    net = setup.expected_return - cost
    value = score_setup(setup, slippage=slippage, funding=funding)
    reward_risk = net / setup.stop_pct if setup.stop_pct > 0 else 0.0
    reason = setup.reason
    status = "potencial"
    if ignored:
        status = "ignorada"
        reason = "Ignorada pelo operador até o prazo configurado"
    elif in_position:
        status = "em_posicao"
    elif setup.strategy_id == "funding_carry" and not futures_enabled:
        status = "potencial"
        reason = "Sinal válido; futuros ainda não habilitados nesta fase"
    elif setup.market == "usdm" and not futures_enabled:
        status = "potencial"
        reason = "Sinal válido; futuros ainda não habilitados nesta fase"
    elif blocked_reason:
        status = "bloqueada"
        reason = blocked_reason
    elif (
        value > score_threshold
        and net > cost_multiple * cost
        and reward_risk >= min_reward_risk
        and regime_factor(setup.strategy_id, setup.regime, setup.direction) > 0
    ):
        status = "pronta"
    elif regime_factor(setup.strategy_id, setup.regime, setup.direction) == 0:
        reason = f"{setup.reason}; regime contrário à estratégia"
    elif value <= score_threshold:
        reason = f"{setup.reason}; score abaixo do limiar de {score_threshold}"
    return Opportunity(
        symbol=setup.symbol,
        strategy=setup.strategy_id,
        action=setup.action,
        score=round(value, 4),
        net_return=net,
        risk_pct=setup.stop_pct,
        status=status,
        reason=reason,
        timeframe=setup.timeframe,
        direction=setup.direction,
        cost=cost,
        gross_return=setup.expected_return,
        alphas=setup.alphas,
    )

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
    if strategy_id == "momentum":
        if direction <= 0 or regime == "trend_down":
            return 0.0
        return 1.0 if regime == "trend_up" else 0.5
    if strategy_id == "funding_carry":
        return 1.0 if regime == "funding" else 0.0
    if strategy_id in ("intraday_momentum", "vwap_pullback"):
        # The setup already requires the closed 1h bars to trend up; the regime is
        # read on them, so "range" means a trend without ADX strength.
        if direction <= 0 or regime == "trend_down":
            return 0.0
        return 1.0 if regime == "trend_up" else 0.7
    return 0.0


def round_trip_cost(
    setup: Setup,
    *,
    slippage: float = 0.0005,
    funding: float = 0.0,
    taker: float = SPOT_TAKER,
) -> float:
    """Fees for every leg in and out, plus slippage and any funding paid.

    ``taker`` is the spot taker fee. The runtime passes
    ``max(SPOT_TAKER, account taker)``, so a discounted account never scores
    a setup cheaper than the backtest that validated it. USD-M legs keep
    ``FUTURES_TAKER``.
    """
    fee = FUTURES_TAKER if setup.market == "usdm" else taker
    legs = 2 if setup.strategy_id == "funding_carry" else 1
    return legs * 2 * fee + slippage + max(funding, 0.0)


def score_setup(
    setup: Setup,
    *,
    slippage: float = 0.0005,
    funding: float = 0.0,
    taker: float = SPOT_TAKER,
) -> float:
    cost = round_trip_cost(setup, slippage=slippage, funding=funding, taker=taker)
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
    #: Estimated share of winning trades of this strategy, or None without enough history.
    win_rate: float | None = None
    win_rate_n: int = 0
    #: Why the row is not ready: score, cost, reward_risk, regime, short, futures,
    #: ignored, in_position or blocked. None when it is ready.
    blocker: str | None = None
    #: Ready only through the Spot Testnet test mode; manual execution only.
    test_only: bool = False
    #: The limit that set ``size_usd`` (a ``risk.SIZE_LIMITS`` key), and every cap in USD.
    size_limit: str | None = None
    size_caps: dict[str, float] | None = None
    #: Seconds since the candle that produced the signal closed.
    signal_age_seconds: float | None = None
    #: Correlation cluster of the pair (for example "BTC"), or None when it trades on its own.
    cluster: str | None = None
    #: Slippage share of the round-trip cost.
    slippage_est: float | None = None
    #: Target distance above the entry; the executor re-applies it on the average fill.
    plan_target_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_win_rate(
    params: dict[str, Any] | None,
    live: tuple[int, int],
    *,
    exit_policy: str,
    min_sample: int,
) -> tuple[float | None, int]:
    """Laplace estimate (wins + 1) / (trades + 2) over backtest and live trades.

    Backtest trades count only when they were replayed with ``exit_policy``,
    and ``live`` must already hold only positions opened under it (see
    ``Store.strategy_outcomes``): a different exit rule describes a different
    trade. Returns ``(None, n)``
    below ``min_sample`` trades. This is a historical hit rate of the strategy,
    not a probability for one setup.
    """
    wins, total = live
    if params and params.get("exit_policy") == exit_policy:
        wins += int(params.get("wins") or 0)
        total += int(params.get("trades") or 0)
    if total < min_sample:
        return None, total
    return (wins + 1) / (total + 2), total


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
    taker: float = SPOT_TAKER,
) -> Opportunity:
    cost = round_trip_cost(setup, slippage=slippage, funding=funding, taker=taker)
    net = setup.expected_return - cost
    value = score_setup(setup, slippage=slippage, funding=funding, taker=taker)
    reward_risk = net / setup.stop_pct if setup.stop_pct > 0 else 0.0
    reason = setup.reason
    status = "potencial"
    blocker: str | None = None
    if ignored:
        status, blocker = "ignorada", "ignored"
        reason = "Ignorada pelo operador até o prazo configurado"
    elif in_position:
        status, blocker = "em_posicao", "in_position"
    elif setup.direction < 0 and setup.market == "spot":
        blocker = "short"
        reason = f"{setup.reason}; venda a descoberto não existe no spot"
    elif setup.strategy_id == "funding_carry" and not futures_enabled:
        blocker = "futures"
        reason = "Sinal válido; futuros ainda não habilitados nesta fase"
    elif setup.market == "usdm" and not futures_enabled:
        blocker = "futures"
        reason = "Sinal válido; futuros ainda não habilitados nesta fase"
    elif blocked_reason:
        status, blocker = "bloqueada", "blocked"
        reason = blocked_reason
    elif regime_factor(setup.strategy_id, setup.regime, setup.direction) == 0:
        blocker = "regime"
        reason = f"{setup.reason}; regime contrário à estratégia"
    elif value <= score_threshold:
        blocker = "score"
        reason = f"{setup.reason}; score abaixo do limiar de {score_threshold}"
    elif net <= cost_multiple * cost:
        blocker = "cost"
        reason = f"{setup.reason}; retorno líquido não cobre {cost_multiple}x o custo"
    elif reward_risk < min_reward_risk:
        blocker = "reward_risk"
        reason = f"{setup.reason}; ganho/risco abaixo de {min_reward_risk}"
    else:
        status = "pronta"
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
        blocker=blocker,
        slippage_est=slippage,
    )

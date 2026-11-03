"""Strategy families. Each one is code the backtest and the live engine share."""

import hashlib
import math
from collections.abc import Iterable, Mapping
from typing import Any

from bot.strategies.day import DAY
from bot.strategies.fixed import is_fixed
from bot.strategies.funding import FundingCarry
from bot.strategies.intraday import IntradayMomentum, VwapPullback
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trailing import TRAIL
from bot.strategies.trend import TrendBreakout

Strategy = TrendBreakout | MeanReversion | Momentum | FundingCarry | IntradayMomentum | VwapPullback

STRATEGIES: dict[str, Strategy] = {
    "trend": TrendBreakout(),
    "mean_reversion": MeanReversion(),
    "momentum": Momentum(),
    "funding_carry": FundingCarry(),
    "intraday_momentum": IntradayMomentum(),
    "vwap_pullback": VwapPullback(),
}
#: Strategies that day trade: 15m entries, first take-profit, break-even stop and a time stop.
DAY_STRATEGIES = frozenset(key for key, strategy in STRATEGIES.items() if strategy.style == "day")


def exit_policy_for(strategy_id: str, swing_policy: str) -> str:
    """The exit rule of a strategy's positions.

    The operator's fixed take-profit and stop-loss apply to every strategy; otherwise
    day strategies trade ``day`` and the others the operator's swing rule.
    """
    if is_fixed(swing_policy):
        return swing_policy
    return "day" if strategy_id in DAY_STRATEGIES else swing_policy


#: Version of the backtest gate. A result of an older gate is stale and is re-validated.
#: 3: portfolio limits replayed, a hold-out with trades and pace, the liquid universe under
#: automatic selection, and trades marked by ``classify``.
GATE_VERSION = 3
#: Version of each strategy's entry and exit code; bump one when its replay changes.
STRATEGY_VERSIONS: dict[str, int] = {key: 1 for key in STRATEGIES}
#: Strategies whose own exit (EMA 20/50 cross) has the hysteresis switch.
HYSTERESIS_STRATEGIES = frozenset({"trend", "momentum"})
#: The ``classify`` thresholds a replay marks its trades with, in this order.
THRESHOLD_KEYS = ("score_threshold", "cost_multiple", "min_reward_risk")


def pairset_hash(symbols: Iterable[str]) -> str:
    """Short fingerprint of a pair set: sha256 of the sorted symbols joined by commas."""
    return hashlib.sha256(",".join(sorted(symbols)).encode()).hexdigest()[:12]


def uses_hysteresis(strategy_id: str, policy: str) -> bool:
    """Whether the exit hysteresis changes this strategy's trades under ``policy``.

    Only trend and momentum exit on the EMA cross, and the fixed rule replaces
    every strategy exit.
    """
    return strategy_id in HYSTERESIS_STRATEGIES and not is_fixed(policy)


def validation_params(
    strategy_id: str,
    exit_policy: str,
    *,
    timeframes: Mapping[str, Mapping[str, Any]],
    symbols: Iterable[str],
    market_filter: str,
    hysteresis: bool,
) -> dict[str, Any]:
    """The key of a validation (``strategy_stats.params``) and its per-timeframe results.

    ``exit_policy`` may be the operator's swing rule: it is resolved with
    ``exit_policy_for``. ``hysteresis`` is stored as replayed, so it is False
    for a strategy whose trades it cannot change.
    """
    policy = exit_policy_for(strategy_id, exit_policy)
    pairs = sorted(symbols)
    return {
        "gate": GATE_VERSION,
        "version": STRATEGY_VERSIONS.get(strategy_id, 1),
        "exit_policy": policy,
        # A day validation holds only for the day rules it replayed (quiet hours and
        # slippage replay under a fixed rule too).
        **({"day": DAY.params} if policy == "day" or strategy_id in DAY_STRATEGIES else {}),
        **({"trailing": TRAIL.params} if policy == "trailing_tp" else {}),
        "market_filter": market_filter,
        "hysteresis": bool(hysteresis) and uses_hysteresis(strategy_id, policy),
        "symbols": pairs,
        "pairset": pairset_hash(pairs),
        "timeframes": {timeframe: dict(result) for timeframe, result in timeframes.items()},
    }


def live_thresholds(score_threshold: float, cost_multiple: float, min_reward_risk: float, swing_policy: str) -> dict[str, float]:
    """The ``classify`` thresholds the live cycle judges with under ``swing_policy``: a fixed
    rule's payoff is the operator's choice, so its reward-to-risk floor is 0 (as in scoring)."""
    floor = 0.0 if is_fixed(swing_policy) else min_reward_risk
    return {"score_threshold": float(score_threshold), "cost_multiple": float(cost_multiple), "min_reward_risk": float(floor)}


def stored_thresholds(params: Mapping[str, Any] | None) -> dict[str, float] | None:
    """The thresholds a stored validation ran with: its ``thresholds``, or, for a record from
    before that key, those of any timeframe result. None when it never recorded them."""
    params = params or {}
    results = params.get("timeframes")
    candidates = [params.get("thresholds"), *(results.values() if isinstance(results, Mapping) else ())]
    for found in candidates:
        if isinstance(found, Mapping) and all(isinstance(found.get(name), (int, float)) for name in THRESHOLD_KEYS):
            return {name: float(found[name]) for name in THRESHOLD_KEYS}
    return None


def thresholds_hold(params: Mapping[str, Any] | None, thresholds: Mapping[str, float]) -> bool:
    """Whether a stored validation ran with ``thresholds`` (``live_thresholds``). They decide
    which trades compete for the portfolio slots, so they change the verdict. A record that
    never stored them holds, like an unknown universe."""
    stored = stored_thresholds(params)
    return stored is None or all(math.isclose(stored[name], float(thresholds[name]), abs_tol=1e-9) for name in THRESHOLD_KEYS)


def timeframe_result(params: Mapping[str, Any] | None, timeframe: str) -> dict[str, Any] | None:
    """The stored result of one timeframe, or None when that timeframe was not replayed."""
    results = (params or {}).get("timeframes")
    if not isinstance(results, Mapping):
        return None
    result = results.get(timeframe)
    return dict(result) if isinstance(result, Mapping) else None


def timeframe_passed(params: Mapping[str, Any] | None, timeframe: str) -> bool:
    result = timeframe_result(params, timeframe)
    return bool(result and result.get("passed"))


def validated_for(
    params: Mapping[str, Any] | None,
    strategy_id: str,
    swing_policy: str,
    *,
    market_filter: str = "off",
    hysteresis: bool = True,
    timeframe: str | None = None,
    symbol: str | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> bool:
    """Whether a backtest's ``params`` replayed the rules this strategy trades with now.

    The key is the gate and strategy versions, the exit rule (with the day or
    trailing parameters it depends on), the market filter, the exit hysteresis
    of trend and momentum, and, when given, the ``classify`` thresholds
    (``thresholds_hold``), the setup's timeframe (it must have been replayed;
    ``timeframe_passed`` says whether it passed) and the pair (it must belong to
    the validated set). Whether the gate passed is not part of the key. The
    validation's staleness, the cycle and ``/bot/status`` all pass the config's
    thresholds, so a pass under other thresholds is signal-only until re-validated.
    """
    if not params:
        return False
    if params.get("gate") != GATE_VERSION or params.get("version") != STRATEGY_VERSIONS.get(strategy_id):
        return False
    policy = exit_policy_for(strategy_id, swing_policy)
    if params.get("exit_policy") != policy:
        return False
    if policy == "trailing_tp" and params.get("trailing") != TRAIL.params:
        return False
    # A day strategy replays its quiet hours and slippage under a fixed rule too.
    if (policy == "day" or strategy_id in DAY_STRATEGIES) and params.get("day") != DAY.params:
        return False
    if params.get("market_filter") != market_filter:
        return False
    if uses_hysteresis(strategy_id, policy) and params.get("hysteresis") != bool(hysteresis):
        return False
    if thresholds is not None and not thresholds_hold(params, thresholds):
        return False
    if timeframe is not None and timeframe_result(params, timeframe) is None:
        return False
    if symbol is not None and symbol not in (params.get("symbols") or ()):
        return False
    return True


__all__ = [
    "DAY_STRATEGIES",
    "GATE_VERSION",
    "HYSTERESIS_STRATEGIES",
    "STRATEGY_VERSIONS",
    "THRESHOLD_KEYS",
    "exit_policy_for",
    "live_thresholds",
    "pairset_hash",
    "stored_thresholds",
    "thresholds_hold",
    "timeframe_passed",
    "timeframe_result",
    "uses_hysteresis",
    "validated_for",
    "validation_params",
    "STRATEGIES",
    "Strategy",
    "TrendBreakout",
    "MeanReversion",
    "Momentum",
    "FundingCarry",
    "IntradayMomentum",
    "VwapPullback",
]

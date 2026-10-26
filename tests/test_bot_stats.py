from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from statistics import NormalDist

import pandas as pd
import pytest

from bot.backtest import stats

RETURNS = [0.02, -0.01, 0.03, 0.01, -0.02, 0.04, 0.0, 0.015, -0.005, 0.025]


def _psr_by_hand(returns: list[float], sr0: float) -> float:
    count = len(returns)
    mean = sum(returns) / count
    sharpe = mean / statistics.stdev(returns)
    moment = [sum((value - mean) ** power for value in returns) / count for power in (2, 3, 4)]
    skew = moment[1] / moment[0] ** 1.5
    kurtosis = moment[2] / moment[0] ** 2
    z = (sharpe - sr0) * math.sqrt(count - 1) / math.sqrt(1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe**2)
    return NormalDist().cdf(z)


def test_psr_and_dsr_match_hand_computed_values() -> None:
    assert stats.per_trade_sharpe(RETURNS) == pytest.approx(statistics.mean(RETURNS) / statistics.stdev(RETURNS))
    assert stats.probabilistic_sharpe(RETURNS) == pytest.approx(_psr_by_hand(RETURNS, 0.0))
    assert stats.probabilistic_sharpe(RETURNS, 0.2) == pytest.approx(_psr_by_hand(RETURNS, 0.2))
    # E[max SR] of 10 trials with variance 0.04: 0.2 * (0.4228 * 1.2816 + 0.5772 * 1.7894).
    assert stats.expected_max_sharpe(10, 0.04) == pytest.approx(0.31494, abs=1e-4)
    assert stats.expected_max_sharpe(1, 0.04) == 0.0
    earlier = [0.1, 0.4, -0.2]
    var = statistics.variance([*earlier, stats.per_trade_sharpe(RETURNS)])
    sr0 = stats.expected_max_sharpe(4, var)
    assert sr0 > 0
    assert stats.deflated_sharpe(RETURNS, 4, earlier) == pytest.approx(_psr_by_hand(RETURNS, sr0))
    # One trial: SR0 is 0 and the deflated Sharpe is the probabilistic one.
    assert stats.deflated_sharpe(RETURNS, 1, []) == pytest.approx(stats.probabilistic_sharpe(RETURNS))


def test_psr_edge_cases() -> None:
    assert stats.probabilistic_sharpe([0.01]) == 0.0
    assert stats.probabilistic_sharpe([0.01] * 5) == 1.0
    assert stats.probabilistic_sharpe([-0.01] * 5) == 0.0


def _stop_outs(count: int) -> list[float]:
    """Equal 2% stop-outs after a 0.2% round trip, entered at alternating prices: equal up to the last digits."""
    entries = [101.37 if index % 2 == 0 else 98.12 for index in range(count)]
    return [(entry * (1 - 0.02) - entry) / entry - 0.002 for entry in entries]


def test_returns_equal_up_to_rounding_have_no_dispersion() -> None:
    stop_outs = _stop_outs(30)
    assert 0 < max(stop_outs) - min(stop_outs) < 1e-15  # the case is float noise, not equal floats
    assert stats.per_trade_sharpe(stop_outs) == 0.0
    assert stats.probabilistic_sharpe(stop_outs) == 0.0
    # A positive mean without dispersion is the limit 1.0 whatever the length (47 used to give 0.0).
    for count in (30, 47):
        assert stats.per_trade_sharpe([0.1] * count) == 0.0
        assert stats.probabilistic_sharpe([0.1] * count) == 1.0
    noisy = [math.nextafter(0.0213, 1.0) if index % 3 else 0.0213 for index in range(40)]
    assert stats.per_trade_sharpe(noisy) == 0.0
    assert stats.probabilistic_sharpe(noisy) == 1.0
    # Real dispersion, however small next to the mean, is still measured.
    assert stats.per_trade_sharpe([0.0213, 0.0214, 0.0212]) == pytest.approx(213.0)


def test_dsr_leaves_degenerate_trials_out_of_the_variance() -> None:
    earlier = [0.1, 0.4, -0.2]
    clean = stats.deflated_sharpe(RETURNS, 6, earlier)
    poisoned = [*earlier, -2e15, math.inf, math.nan, stats.MAX_TRIAL_SHARPE + 0.01]
    # Same trial count, same deflated Sharpe: the bad values change only N's variance, and they are left out.
    assert stats.deflated_sharpe(RETURNS, 6, poisoned) == pytest.approx(clean)
    # The bound is inclusive: a trial at MAX_TRIAL_SHARPE still counts.
    assert stats.deflated_sharpe(RETURNS, 6, [*earlier, stats.MAX_TRIAL_SHARPE]) < clean


def test_a_poisoned_trial_does_not_lock_out_a_clean_strategy() -> None:
    strong = RETURNS * 8
    earlier = [0.05, -0.1, 0.12]
    passed = stats.deflated_sharpe(strong, 5, earlier)
    assert passed > 0.95  # the gate's MIN_DSR
    # The stored Sharpe of 30 equal stop-outs before the tolerance: about -2e15.
    assert stats.deflated_sharpe(strong, 5, [*earlier, -2e15]) == pytest.approx(passed)


def test_dsr_falls_as_trials_grow() -> None:
    earlier = [0.05, 0.3, -0.1, 0.2]
    values = [stats.deflated_sharpe(RETURNS, trials, earlier) for trials in (1, 2, 5, 20, 100)]
    assert values == sorted(values, reverse=True) and values[0] > values[-1]


def test_mc_drawdown_p95_is_deterministic() -> None:
    r = [2.1, -0.9, -1.3, 1.6, -1.1, 3.2, -0.7, -1.2, -0.95, 2.05, -1.0, 0.4]
    first = stats.mc_drawdown_r(r, runs=1000, seed=0)
    assert first == stats.mc_drawdown_r(r, runs=1000, seed=0)
    assert first != stats.mc_drawdown_r(r, runs=1000, seed=1)
    assert first >= stats.mc_drawdown_r(r, runs=1000, seed=0, q=0.5) > 0
    assert stats.mc_drawdown_r([1.0, 0.5]) == 0.0 and stats.mc_drawdown_r([]) == 0.0
    # Cumulative 1, -1, 0, -1, 2: the drop from the first peak of 1 to -1 is 2R.
    assert stats.max_drawdown_r([1.0, -2.0, 1.0, -1.0, 3.0]) == 2.0
    # The peak starts at zero: an opening loss is a drawdown.
    assert stats.max_drawdown_r([-1.5, 1.0]) == 1.5


@dataclass
class _Trade:
    exit_time: pd.Timestamp
    net_return: float


def test_walk_forward_splits_by_exit_time_into_4_windows_and_a_holdout() -> None:
    start = pd.Timestamp("2026-01-01", tz="UTC")
    at = lambda days: start + pd.Timedelta(days=days)  # noqa: E731
    trades = [_Trade(at(95), -0.01), _Trade(at(5), 0.02), _Trade(at(-1), 0.01), _Trade(at(20), -0.03), _Trade(at(45), 0.01),
              _Trade(at(65), 0.02), _Trade(at(85), 0.03), _Trade(at(79.9), 0.01)]
    split = stats.walk_forward(trades, start, at(100), windows=4, holdout=0.2)
    windows = split["windows"]
    assert [window["start"] for window in windows] == [at(days).isoformat() for days in (0, 20, 40, 60)]
    assert windows[-1]["end"] == at(80).isoformat()
    # A trade before the start counts in the first window; one on an edge belongs to the later window.
    assert [window["trades"] for window in windows] == [2, 1, 1, 2]
    assert [window["net"] for window in windows] == [pytest.approx(0.03), pytest.approx(-0.03), pytest.approx(0.01), pytest.approx(0.03)]
    assert [window["profitable"] for window in windows] == [True, False, True, True]
    assert split["holdout"] == {"start": at(80).isoformat(), "trades": 2, "net": pytest.approx(0.02), "passed": True}
    assert [trade.exit_time for trade in split["in_sample"]] == [at(days) for days in (-1, 5, 20, 45, 65, 79.9)]
    losing = stats.walk_forward([_Trade(at(90), -0.01)], start, at(100))
    assert losing["holdout"]["passed"] is False and losing["in_sample"] == []


def _windows(nets: list[float], days: int = 10) -> list[dict]:
    start = pd.Timestamp("2026-01-01", tz="UTC")
    edges = [(start + pd.Timedelta(days=days * k)).isoformat() for k in range(len(nets) + 1)]
    return [{"start": edges[k], "end": edges[k + 1], "net": net} for k, net in enumerate(nets)]


def test_wfe_is_the_median_of_anchored_folds() -> None:
    # Rates 0.1, 0.2, 0.1, 0.2 per day: folds 0.2/0.1, 0.1/0.15, 0.2/(0.4/3) -> 2, 0.667, 1.5; median 1.5.
    assert stats.walk_forward_efficiency(_windows([1.0, 2.0, 1.0, 2.0])) == pytest.approx(1.5)
    # The fold anchored on the first window's loss is skipped; the others are 0.1/0.05 and 0.1/(2/30).
    assert stats.walk_forward_efficiency(_windows([-1.0, 2.0, 1.0, 1.0])) == pytest.approx(
        statistics.median([1.0 / (1.0 / 2), 1.0 / (2.0 / 3)])
    )
    assert stats.walk_forward_efficiency(_windows([-1.0, -1.0, -1.0, 1.0])) == 0.0
    assert stats.walk_forward_efficiency(_windows([1.0, 1.0, 1.0, 1.0])) == pytest.approx(1.0)


def test_profit_factor_and_sortino() -> None:
    assert stats.profit_factor([0.04, -0.02, 0.06, -0.01]) == pytest.approx(10 / 3)
    assert stats.profit_factor([0.01, 0.02]) == stats.RATIO_CAP
    assert stats.profit_factor([-0.01, 0.0]) == 0.0
    returns = [0.02, -0.01, 0.03, -0.02]
    downside = math.sqrt((0.01**2 + 0.02**2) / 4)
    assert stats.sortino(returns, 52) == pytest.approx(0.005 / downside * math.sqrt(52))
    assert stats.sortino([0.01, 0.02], 52) == stats.RATIO_CAP
    assert stats.sortino([0.01], 52) == 0.0

"""Statistics of the backtest gate: deflated Sharpe, Monte Carlo drawdown and walk-forward.

Pure numpy and the standard library. Returns are per trade unless a function
says otherwise; R is a trade's net return divided by its entry stop distance.

* Probabilistic Sharpe (Bailey and López de Prado 2012): the probability that
  the true per-trade Sharpe is above ``sr0``, given the sample's length, skew
  and kurtosis.
* Deflated Sharpe (Bailey and López de Prado 2014): the same probability with
  ``sr0`` the Sharpe the best of N unskilled trials would reach by chance.
* Monte Carlo drawdown: bootstrap of the trades' R, the ``q`` quantile of the
  largest cumulative-R drawdown.
* Walk-forward without refit (nothing is fitted): the span's last share is a
  hold-out, the rest is cut into equal windows by exit time.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime
from statistics import NormalDist
from typing import Any

import numpy as np

#: Euler-Mascheroni constant, in the expected maximum of N Gaussian Sharpe ratios.
EULER_GAMMA = 0.5772156649015329
#: Profit factor and Sortino of a sample without losses.
RATIO_CAP = 99.0

_NORMAL = NormalDist()


def per_trade_sharpe(returns: Sequence[float] | np.ndarray) -> float:
    """Mean over sample standard deviation, not annualised. 0.0 below two returns or without dispersion."""
    values = np.asarray(returns, dtype=float)
    if len(values) < 2:
        return 0.0
    sigma = float(values.std(ddof=1))
    return 0.0 if sigma == 0 else float(values.mean()) / sigma


def probabilistic_sharpe(returns: Sequence[float] | np.ndarray, sr0: float = 0.0) -> float:
    """P(true Sharpe > ``sr0``) = Φ((SR − SR0)·√(n−1) / √(1 − γ3·SR + (γ4 − 1)/4·SR²)).

    SR is the per-trade Sharpe (sample standard deviation); γ3 and γ4 are the
    sample skewness and (non-excess) kurtosis. Fewer than two returns, or a
    non-positive denominator, give 0.0. Returns without dispersion give the
    limit: 1.0 when their mean is above zero and ``sr0`` is finite, else 0.0.
    """
    values = np.asarray(returns, dtype=float)
    count = len(values)
    if count < 2:
        return 0.0
    mean = float(values.mean())
    if float(values.std(ddof=1)) == 0:
        return 1.0 if mean > 0 else 0.0
    sharpe = per_trade_sharpe(values)
    centred = values - mean
    population = float(np.sqrt(np.mean(centred**2)))
    skew = float(np.mean(centred**3)) / population**3
    kurtosis = float(np.mean(centred**4)) / population**4
    denominator = 1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe**2
    if denominator <= 0:
        return 0.0
    return _NORMAL.cdf((sharpe - sr0) * math.sqrt(count - 1) / math.sqrt(denominator))


def expected_max_sharpe(n: int, var: float) -> float:
    """E[max SR] of ``n`` unskilled trials whose Sharpe ratios have variance ``var``.

    √V·((1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))); 0.0 for a single trial.
    """
    if n <= 1 or var <= 0:
        return 0.0
    return math.sqrt(var) * (
        (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n) + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (n * math.e))
    )


def deflated_sharpe(returns: Sequence[float] | np.ndarray, trials: int, trial_sharpes: Sequence[float]) -> float:
    """Probabilistic Sharpe against the best of ``trials`` unskilled trials.

    The variance of the trials' per-trade Sharpe is taken over ``trial_sharpes``
    (the earlier trials) plus this sample's own Sharpe, with the sample variance;
    with one value it is 0, and so is SR0.
    """
    sharpes = [float(value) for value in trial_sharpes] + [per_trade_sharpe(returns)]
    var = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
    return probabilistic_sharpe(returns, expected_max_sharpe(int(trials), var))


def _drawdowns(paths: np.ndarray) -> np.ndarray:
    """Largest drop of each row's cumulative sum from its running peak; the peak starts at 0."""
    cumulative = np.cumsum(paths, axis=-1)
    peaks = np.maximum.accumulate(np.maximum(cumulative, 0.0), axis=-1)
    return np.asarray((peaks - cumulative).max(axis=-1), dtype=float)


def max_drawdown_r(r: Sequence[float] | np.ndarray) -> float:
    """Largest drawdown of the cumulative R, in R (a positive number)."""
    values = np.asarray(r, dtype=float)
    if len(values) == 0:
        return 0.0
    return float(_drawdowns(values))


def mc_drawdown_r(r: Sequence[float] | np.ndarray, runs: int = 1000, seed: int = 0, q: float = 0.95) -> float:
    """The ``q`` quantile of the cumulative-R drawdown over ``runs`` bootstrap resamples.

    Each resample draws as many trades as the sample, with replacement, from a
    generator seeded with ``seed``: the same trades give the same number.
    """
    values = np.asarray(r, dtype=float)
    if len(values) == 0 or runs <= 0:
        return 0.0
    rng = np.random.default_rng(seed)
    paths = values[rng.integers(0, len(values), size=(runs, len(values)))]
    return float(np.quantile(_drawdowns(paths), q))


def walk_forward(
    trades: Sequence[Any], start: datetime, end: datetime, windows: int = 4, holdout: float = 0.2
) -> dict[str, Any]:
    """Split trades by exit time into ``windows`` equal in-sample windows and a hold-out.

    The hold-out is the last ``holdout`` share of ``[start, end]``; the rest is
    cut into equal windows. A trade is any object with ``exit_time`` and
    ``net_return``; one that exits before ``start`` counts in the first window.
    A window is profitable when its net (the sum of its trades' returns) is
    above zero; the hold-out passes with a positive net or no trades.

    Returns ``{"windows": [{start, end, trades, net, profitable}], "holdout":
    {start, trades, net, passed}, "in_sample": [trades]}``, with ISO times.
    """
    held_from = start + (end - start) * (1 - holdout)
    width = (held_from - start) / windows
    edges = [start + width * k for k in range(windows + 1)]
    edges[-1] = held_from
    buckets: list[list[Any]] = [[] for _ in range(windows + 1)]
    for trade in sorted(trades, key=lambda item: item.exit_time):
        buckets[bisect_right(edges[1:], trade.exit_time)].append(trade)
    report = [
        {
            "start": edges[k].isoformat(),
            "end": edges[k + 1].isoformat(),
            "trades": len(buckets[k]),
            "net": float(sum(trade.net_return for trade in buckets[k])),
            "profitable": bool(sum(trade.net_return for trade in buckets[k]) > 0),
        }
        for k in range(windows)
    ]
    held = buckets[windows]
    held_net = float(sum(trade.net_return for trade in held))
    return {
        "windows": report,
        "holdout": {"start": held_from.isoformat(), "trades": len(held), "net": held_net, "passed": bool(held_net > 0 or not held)},
        "in_sample": [trade for bucket in buckets[:windows] for trade in bucket],
    }


def walk_forward_efficiency(windows: Sequence[dict[str, Any]]) -> float:
    """Median over k = 2..n of rate(window k) / rate(windows before k); rate = net per day.

    A fold whose earlier windows did not make money has no efficiency and is
    skipped; with no fold left the efficiency is 0.0.
    """
    days = [
        (datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).total_seconds() / 86400
        for window in windows
    ]
    ratios = []
    for k in range(1, len(windows)):
        anchor_days = sum(days[:k])
        anchor = sum(window["net"] for window in windows[:k]) / anchor_days if anchor_days > 0 else 0.0
        if anchor <= 0 or days[k] <= 0:
            continue
        ratios.append(windows[k]["net"] / days[k] / anchor)
    return float(np.median(ratios)) if ratios else 0.0


def profit_factor(returns: Sequence[float] | np.ndarray) -> float:
    """Gross gains over gross losses, capped at ``RATIO_CAP``; 0.0 without gains."""
    values = np.asarray(returns, dtype=float)
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    if gains <= 0:
        return 0.0
    return RATIO_CAP if losses <= 0 else min(RATIO_CAP, gains / losses)


def sortino(returns: Sequence[float] | np.ndarray, periods_per_year: float) -> float:
    """Mean over downside deviation (root mean square of the negative returns), annualised.

    Capped at ``RATIO_CAP`` when nothing lost; 0.0 below two returns.
    """
    values = np.asarray(returns, dtype=float)
    if len(values) < 2 or periods_per_year <= 0:
        return 0.0
    mean = float(values.mean())
    downside = float(np.sqrt(np.mean(np.minimum(values, 0.0) ** 2)))
    if downside == 0:
        return RATIO_CAP if mean > 0 else 0.0
    return float(np.clip(mean / downside * math.sqrt(periods_per_year), -RATIO_CAP, RATIO_CAP))

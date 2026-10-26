"""Live evidence against the backtest: demote a strategy, a strategy on one pair, or block a pair.

Like with like (D26): the live daily Sharpe is measured over the last 30 calendar
UTC days (fewer when the strategy was validated within them), a day without a
close counting 0, and compared with the backtest's
``daily_sharpe``, which counts idle days as 0 too and uses the same √365 scale.
The backtest side is the best passed timeframe's value. Every check reads closed
strategy trades only (``Store.closed_trades``: no test-mode, DCA or grid rows) and
takes ``now``, so it is replayable in a test.

A strategy's evidence starts at its current validation (``validated_at``): a
revalidation gives it a fresh start instead of being demoted again by the trades
that demoted it, which a signal-only strategy could never outgrow. The per-pair
checks age out by time instead (90 days for expectancy, 30 for slippage).

``apply_decay`` is the previous rule (Sharpe of the days that closed a trade,
against ``backtest_sharpe``) and stays for the old runtime path.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from bot.store import Store

#: Calendar days of the live daily Sharpe.
LIVE_DAYS = 30
#: Closed trades the live window needs before its Sharpe is judged.
MIN_WINDOW_TRADES = 10
#: A live Sharpe below this share of the backtest's demotes the strategy.
SHARPE_FLOOR = 0.5
#: Last closed trades whose mean R below 0 demotes the strategy.
RECENT_TRADES = 20
#: Last trades of a strategy on one pair, within ``PAIR_DAYS``, whose mean R below 0 demotes it there.
PAIR_TRADES = 10
PAIR_DAYS = 90
#: A pair's last trades within ``SLIPPAGE_DAYS`` measured against the slippage budget.
SLIPPAGE_TRADES = 20
#: Measured trades the slippage mean needs; fewer say nothing.
SLIPPAGE_MIN_TRADES = 10
SLIPPAGE_DAYS = 30

PAIR_DEMOTED = f"rebaixada neste par: expectativa negativa em {PAIR_TRADES} trades"
SLIPPAGE_BLOCKED = "slippage realizado acima do orçamento"


def live_sharpe(daily_returns: list[float]) -> float:
    """Annualised Sharpe (√365) of daily returns; 0 without two days or without variance."""
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((value - mean) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
    sigma = float(var) ** 0.5
    if sigma == 0:
        return 0.0
    return float(mean / sigma * (365 ** 0.5))


def backtest_daily_sharpe(row: Mapping[str, Any]) -> float | None:
    """The best passed timeframe's backtest daily Sharpe, or None when no timeframe passed."""
    results = (row.get("params") or {}).get("timeframes")
    if not isinstance(results, Mapping):
        return None
    values = []
    for result in results.values():
        if not isinstance(result, Mapping) or not result.get("passed"):
            continue
        value = result.get("daily_sharpe")
        if isinstance(value, (int, float)) and math.isfinite(value):
            values.append(float(value))
    return max(values) if values else None


def review_strategy(store: Store, strategy_id: str, *, capital: float, now: datetime) -> str | None:
    """Demote the strategy to signal-only when its live evidence contradicts the backtest.

    Two triggers, both on trades closed since the current validation:
    - with at least ``MIN_WINDOW_TRADES`` of them in the last ``LIVE_DAYS`` calendar
      days, the zero-filled live daily Sharpe is below ``SHARPE_FLOOR`` times the best
      passed timeframe's backtest daily Sharpe (when that is positive). Its days run
      from the later of the window's first day and ``validated_at`` to ``now``'s day,
      with the trades closed before ``validated_at`` left out, and it needs two days;
    - the last ``RECENT_TRADES`` closed trades, every one with a recorded R, have a
      mean R below 0.

    The measured live Sharpe is saved either way. A demotion saves ``signal_only=True``
    and ``params["demoted"] = {"reason", "at"}``. Returns the reason when this call
    demoted the strategy; None when it did not (also when it already was signal-only).
    """
    row = store.strategy(strategy_id)
    if row is None:
        return None
    moment = _utc(now)
    validated = _iso_utc(row.get("validated_at"))
    window_start = (moment.date() - timedelta(days=LIVE_DAYS - 1)).isoformat()
    since = max(window_start, validated) if validated else window_start

    live: float | None = None
    trades, returns = _live_days(store, strategy_id, since, moment.date(), capital)
    if trades >= MIN_WINDOW_TRADES and len(returns) >= 2:
        live = live_sharpe(returns)

    reason: str | None = None
    backtest = backtest_daily_sharpe(row)
    if live is not None and backtest is not None and backtest > 0 and live < backtest * SHARPE_FLOOR:
        reason = (
            f"Sharpe diário dos últimos {len(returns)} dias ({live:.2f}) abaixo de metade "
            f"do backtest ({backtest:.2f})"
        )
    if reason is None:
        recent = _mean_r(store.closed_trades(strategy=strategy_id, since=validated, limit=RECENT_TRADES), RECENT_TRADES)
        if recent is not None and recent < 0:
            reason = f"média de R dos últimos {RECENT_TRADES} trades negativa ({recent:+.2f} R)"

    demote = reason is not None and not row.get("signal_only")
    if live is None and not demote:
        return None
    updated = dict(row)
    if live is not None:
        updated["live_sharpe"] = live
    if demote:
        updated["signal_only"] = True
        updated["params"] = {**(row.get("params") or {}), "demoted": {"reason": reason, "at": moment.isoformat()}}
    store.save_strategy(updated)
    return reason if demote else None


def pair_demoted(store: Store, strategy_id: str, symbol: str, *, now: datetime) -> str | None:
    """Why the strategy is signal-only on this pair, or None.

    The last ``PAIR_TRADES`` closed trades of the strategy on the pair within
    ``PAIR_DAYS``, every one with a recorded R, have a mean R below 0. Read-only:
    it ages out as those trades leave the window.
    """
    since = (_utc(now) - timedelta(days=PAIR_DAYS)).isoformat()
    trades = store.closed_trades(strategy=strategy_id, symbol=symbol, since=since, limit=PAIR_TRADES)
    mean = _mean_r(trades, PAIR_TRADES)
    return PAIR_DEMOTED if mean is not None and mean < 0 else None


def pair_slippage_blocked(store: Store, symbol: str, budget: float, *, now: datetime) -> str | None:
    """Why the pair takes no entry because of its realized slippage, or None.

    Over the pair's last ``SLIPPAGE_TRADES`` closed trades within ``SLIPPAGE_DAYS``
    (any strategy), the mean of ``entry_slippage + exit_slippage`` of those with
    both recorded is above ``budget``. Needs ``SLIPPAGE_MIN_TRADES`` measured
    trades; a budget of 0 or less is off. Read-only.
    """
    if budget <= 0:
        return None
    since = (_utc(now) - timedelta(days=SLIPPAGE_DAYS)).isoformat()
    costs = [
        float(trade["entry_slippage"]) + float(trade["exit_slippage"])
        for trade in store.closed_trades(symbol=symbol, since=since, limit=SLIPPAGE_TRADES)
        if trade.get("entry_slippage") is not None and trade.get("exit_slippage") is not None
    ]
    if len(costs) < SLIPPAGE_MIN_TRADES:
        return None
    return SLIPPAGE_BLOCKED if sum(costs) / len(costs) > budget else None


def apply_decay(store: Store, strategy_id: str, daily_returns: list[float]) -> bool:
    """Return True when the strategy was demoted to signal-only."""
    row = store.strategy(strategy_id)
    if row is None or row.get("backtest_sharpe") in (None, 0):
        return False
    if len(daily_returns) < 30:
        return False
    window = daily_returns[-30:]
    current = live_sharpe(window)
    backtest = float(row["backtest_sharpe"])
    demoted = current < backtest * 0.5
    updated = {**row, "live_sharpe": current}
    if demoted:
        updated["signal_only"] = True
    store.save_strategy(updated)
    return demoted


def _live_days(store: Store, strategy_id: str, since: str, today: date, capital: float) -> tuple[int, list[float]]:
    """The strategy's closed trades at or after ``since`` up to ``today``, and their PnL per UTC day.

    Returns the trade count and one value per calendar day from ``since``'s day to
    ``today``, oldest first, as a fraction of ``capital``; a day without a close
    counts 0. Only trades closed at or after ``since`` count, also on its first
    day. A capital of 0 or less measures no day.
    """
    first = date.fromisoformat(since[:10])
    pnl = {(first + timedelta(days=offset)).isoformat(): 0.0 for offset in range((today - first).days + 1)}
    count = 0
    for trade in store.closed_trades(strategy=strategy_id, since=since):
        day = str(trade["closed_at"])[:10]
        if day in pnl:
            pnl[day] += float(trade.get("realized_pnl") or 0.0)
            count += 1
    return count, [value / capital for value in pnl.values()] if capital > 0 else []


def _mean_r(trades: Sequence[Mapping[str, Any]], count: int) -> float | None:
    """Mean R of exactly ``count`` trades, or None when there are fewer or one has no R."""
    if len(trades) < count:
        return None
    values = [trade.get("r_multiple") for trade in trades[:count]]
    if any(value is None for value in values):
        return None
    return sum(float(value) for value in values if value is not None) / count


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _iso_utc(value: Any) -> str | None:
    """An ISO time as the store writes it (UTC, ``+00:00``), or None when absent or unreadable."""
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value))).isoformat()
    except ValueError:
        return None

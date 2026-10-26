"""Replay of a DCA or grid plan on closed bars, for the panel's preview.

It answers what Binance's "historical profits" chart answers: how the same
plan would have done on the last days of the pair. It is no gate, and like
any backtest it does not promise the future. It trades as the live cycle:

* buys, safety orders, grid legs and take-profits fill at the bar's close,
  because the live cycle reads the last close and sends a market buy or an
  IOC sell (each pays the taker fee and half the spread);
* the stop rests at the broker, so it fills on the bar's low, at the stop or
  at the open when the bar gaps through it, and it is checked before the
  close's decisions;
* DCA rounds repeat after each take-profit, so the replay shows several
  rounds; the stop ends the replay, as it ends the live instance;
* with ``resting_buys`` the safety orders and grid buys rest on the book: each
  fills at its own price when the bar's low reaches it, paying the fee and no
  spread (Spot VIP0 maker = taker). The live cycle rests one buy at a time and
  places the next a minute after a fill, so a bar whose low crosses several
  levels fills each of them. Every level rests above the stop, so the fills
  come before the stop check: a bar that falls to the stop first buys every
  level over it, and the stop sells that larger quantity.

The preview replays the last 30, 90 and 180 days of 1h bars (``replay_windows``)
and says how long a grid's close stayed outside its range: a grid only earns
while the price moves sideways inside it. On 1h bars a new round starts on the
next bar, so a ``cooldown_minutes`` above 60 is not modelled.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from bot.instances import (
    DcaParams,
    GridParams,
    Kind,
    dca_ladder,
    dca_stop,
    grid_levels,
    grid_next,
    grid_qty,
    grid_start_k,
)

#: The preview's windows, in days of 1h bars.
REPLAY_WINDOW_DAYS: tuple[int, ...] = (30, 90, 180)
#: A grid whose close spent more than this share of the window outside its range gets a warning.
OUT_OF_RANGE_WARN = 0.30


def _drawdown(equity: list[float]) -> float:
    peak = worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def replay_dca(params: DcaParams, frame: pd.DataFrame, *, taker: float, spread: float = 0.0) -> dict[str, Any]:
    cost = taker + spread / 2
    budget = sum(params.base_usd * params.size_mult**index for index in range(params.so_count + 1))
    cash, held, spent = budget, 0.0, 0.0
    base = stop = 0.0
    filled = rounds = wins = 0
    ended: str | None = None
    equity: list[float] = []
    in_round = False
    for opened, low, close in zip(frame["open"], frame["low"], frame["close"]):
        opened, low, close = float(opened), float(low), float(close)
        if not in_round:
            base, filled, in_round = close, 0, True
            stop = dca_stop(params, base)
            ladder = dca_ladder(params, base)
            held = params.base_usd * (1 - cost) / close
            spent = params.base_usd
            cash -= params.base_usd
            rounds += 1
        else:
            while params.resting_buys and filled < params.so_count and low <= ladder[filled + 1]["price"]:
                # The resting safety order fills at its own price, above the stop: before it.
                filled += 1
                usd = ladder[filled]["usd"]
                held += usd * (1 - taker) / ladder[filled]["price"]
                spent += usd
                cash -= usd
            if low <= stop:
                cash += held * min(opened, stop) * (1 - cost)
                held, ended = 0.0, "stop"
                equity.append(cash)
                break
            if held and close >= spent / held * (1 + params.take_profit_pct):
                cash += held * close * (1 - cost)
                wins += 1
                held, in_round = 0.0, False
            elif not params.resting_buys and filled < params.so_count and close <= ladder[filled + 1]["price"]:
                filled += 1
                usd = ladder[filled]["usd"]
                held += usd * (1 - cost) / close
                spent += usd
                cash -= usd
        equity.append(cash + held * close)
    last = float(frame["close"].iloc[-1]) if len(frame) else 0.0
    final = cash + held * last
    return {
        "bars": len(frame),
        "rounds": rounds,
        "wins": wins,
        "ended": ended,
        "pnl_usd": final - budget,
        "pnl_pct": (final - budget) / budget if budget else 0.0,
        "max_drawdown_pct": _drawdown(equity),
    }


def replay_grid(params: GridParams, frame: pd.DataFrame, *, taker: float, spread: float = 0.0) -> dict[str, Any]:
    cost = taker + spread / 2
    levels = grid_levels(params)
    qtys = grid_qty(params)
    cash = params.invest_usd
    k: int | None = None
    paid: dict[int, float] = {}
    trades = 0
    realized = 0.0
    ended: str | None = None
    equity: list[float] = []
    for opened, low, close in zip(frame["open"], frame["low"], frame["close"]):
        opened, low, close = float(opened), float(low), float(close)
        if k is None:
            if not params.lower < close < params.upper:
                continue
            k = grid_start_k(levels, close)
            for index in range(k, params.grids):
                paid[index] = qtys[index] * close * (1 + cost)
                cash -= paid[index]
            equity.append(params.invest_usd)
            continue
        while params.resting_buys and k > 0 and low <= levels[k - 1]:
            # The resting buy of the level under the lowest held fills at the level, above the stop: before it.
            k -= 1
            paid[k] = qtys[k] * levels[k] * (1 + taker)
            cash -= paid[k]
        held = sum(qtys[index] for index in paid)
        if low <= params.stop_price:
            fill = min(opened, params.stop_price)
            proceeds = held * fill * (1 - cost)
            realized += proceeds - sum(paid.values())
            cash += proceeds
            paid, ended = {}, "stop"
            equity.append(cash)
            break
        if params.take_profit_price is not None and close >= params.take_profit_price:
            proceeds = held * close * (1 - cost)
            realized += proceeds - sum(paid.values())
            cash += proceeds
            paid, ended = {}, "take_profit"
            equity.append(cash)
            break
        step = grid_next(levels, k, close)
        if step is not None:
            side, new_k = step
            if side == "buy":
                for index in range(new_k, k):
                    paid[index] = qtys[index] * close * (1 + cost)
                    cash -= paid[index]
            else:
                for index in range(k, new_k):
                    proceeds = qtys[index] * close * (1 - cost)
                    realized += proceeds - paid.pop(index, 0.0)
                    cash += proceeds
                    trades += 1
            k = new_k
        equity.append(cash + sum(qtys[index] for index in paid) * close)
    last = float(frame["close"].iloc[-1]) if len(frame) else 0.0
    final = cash + sum(qtys[index] for index in paid) * last
    return {
        "bars": len(frame),
        "started": k is not None,
        "grid_trades": trades,
        "realized_usd": realized,
        "ended": ended,
        "pnl_usd": final - params.invest_usd,
        "pnl_pct": (final - params.invest_usd) / params.invest_usd,
        "max_drawdown_pct": _drawdown(equity),
    }


def out_of_range_share(params: DcaParams | GridParams, frame: pd.DataFrame) -> float:
    """Share of the bars whose close is outside the grid's ``[lower, upper]``; 0.0 for DCA or no bars."""
    if not isinstance(params, GridParams) or frame.empty:
        return 0.0
    closes = frame["close"].astype(float)
    outside = (closes < params.lower) | (closes > params.upper)
    return float(outside.mean())


def replay_windows(
    kind: Kind,
    params: DcaParams | GridParams,
    frame_1h: pd.DataFrame,
    *,
    taker: float,
    spread: float,
    days: Sequence[int] = REPLAY_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """The plan replayed on the last ``days`` of ``frame_1h``, one row per window, in the order given.

    Each window is the bars after ``last bar - N days``, so a pair younger than
    the window replays its whole history (``bars`` tells). ``warn`` is set for a
    grid out of range more than ``OUT_OF_RANGE_WARN`` of the window.
    """
    expected = DcaParams if kind == "dca" else GridParams
    if not isinstance(params, expected):
        raise TypeError(f"{kind} replay needs {expected.__name__}, got {type(params).__name__}")
    if not isinstance(frame_1h.index, pd.DatetimeIndex):
        raise TypeError("frame_1h needs a DatetimeIndex")
    if any(n <= 0 for n in days):
        raise ValueError("every window needs a positive number of days")
    end = frame_1h.index[-1] if len(frame_1h) else None
    rows: list[dict[str, Any]] = []
    for n in days:
        window = frame_1h if end is None else frame_1h[frame_1h.index > end - pd.Timedelta(days=n)]
        if isinstance(params, DcaParams):
            result = replay_dca(params, window, taker=taker, spread=spread)
        else:
            result = replay_grid(params, window, taker=taker, spread=spread)
        share = out_of_range_share(params, window)
        rows.append(
            {
                "days": n,
                **result,
                "out_of_range_share": share,
                "warn": kind == "grid" and share > OUT_OF_RANGE_WARN,
            }
        )
    return rows

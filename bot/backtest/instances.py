"""Replay of a DCA or grid plan on closed bars, for the panel's preview.

It answers what Binance's "historical profits" chart answers: how the same
plan would have done on the last days of the pair. It is no gate, and like
any backtest it does not promise the future. It trades as the live cycle:

* buys, safety orders, grid legs and take-profits fill at the bar's close,
  because the live cycle reads the last close and sends a market buy or an
  IOC sell (each pays the taker fee and half the spread);
* the stop rests at the broker, so it fills on the bar's low, at the stop or
  at the open when the bar gaps through it, and it is checked first;
* DCA rounds repeat after each take-profit, so the replay shows several
  rounds; the stop ends the replay, as it ends the live instance.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from bot.instances import DcaParams, GridParams, dca_ladder, dca_stop, grid_levels, grid_next, grid_qty, grid_start_k


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
        elif low <= stop:
            cash += held * min(opened, stop) * (1 - cost)
            held, ended = 0.0, "stop"
            equity.append(cash)
            break
        elif held and close >= spent / held * (1 + params.take_profit_pct):
            cash += held * close * (1 - cost)
            wins += 1
            held, in_round = 0.0, False
        elif filled < params.so_count and close <= ladder[filled + 1]["price"]:
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

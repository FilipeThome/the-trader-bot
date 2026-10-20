"""DCA and grid bots, as Binance's Spot DCA and Spot Grid run them, in pure functions.

An instance is configured by the operator, not found by a strategy, so the
backtest gate does not apply: its creation checks do. Every check works on the
whole plan up front, like a Binance bot reserves its investment:

* **DCA**: a market base order, then up to ``so_count`` safety buys at growing
  distances below the base price (``step_mult``) with growing sizes
  (``size_mult``). The whole position sells at ``take_profit_pct`` above its
  average. One stop rests ``stop_pct`` under the last safety level, so the
  loss with every safety order filled is known before the first buy.
* **Grid**: ``grids`` intervals between ``lower`` and ``upper``. Level ``i``
  buys at ``L[i]`` and sells at ``L[i+1]``. What is held is always the top of
  the grid, from the lowest held level ``k`` up, so the state is one index,
  read back from the quantity the position holds (``grid_k_from_qty``): an
  order whose result was unknown can never desynchronise it.

The live cycle runs every minute and trades with market buys and IOC sells,
because a spot stop locks the base asset a resting sell limit would need: round
trips inside one minute are missed, and each leg pays the taker fee and the
spread. The profit floor per grid accounts for both.

A new round waits ``cooldown_minutes`` after the previous one closed, as
Binance waits 60 s: a DCA repeat after its take-profit, and a grid restart from
an empty inventory after a full sell-out (``cooldown_ends``).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal["dca", "grid"]
#: Net profit a grid interval or a DCA take-profit must keep after both taker fees and the spread.
MIN_NET_MARGIN = 0.002
#: Orders sized this far under the per-order cap, as the executor does.
CAP_HEADROOM = 0.95
#: A grid level must be worth this much more than the exchange minimum: its sale is
#: smaller than its buy (fees, the sell margin) and goes out below the bid.
MIN_NOTIONAL_HEADROOM = 1.05
#: Orders one DCA round may need: base buy, its stop, and the exit.
DCA_FIXED_ORDERS = 3
#: Each safety buy or grid leg costs the order plus the re-placed stop.
ORDERS_PER_LEG = 2
#: Minutes a new round waits after the previous one closed (Binance's 60 s).
DEFAULT_COOLDOWN_MINUTES = 1


class DcaParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    base_usd: float = Field(gt=0)
    so_count: int = Field(ge=1, le=8)
    #: Distance of the first safety order below the base price.
    step_pct: float = Field(gt=0, le=0.2)
    #: Each further distance is the previous one times this (Binance's price deviation multiplier).
    step_mult: float = Field(default=1.0, ge=1.0, le=3.0)
    #: Each safety order is the base order times this to the power of its index.
    size_mult: float = Field(default=1.0, ge=1.0, le=3.0)
    take_profit_pct: float = Field(gt=0, le=0.5)
    #: Stop distance under the last safety level.
    stop_pct: float = Field(gt=0, le=0.5)
    #: Start a new round after a take-profit.
    repeat: bool = False
    max_orders_per_day: int = Field(default=20, ge=4, le=200)
    #: Pause between a take-profit and the next round's base buy. Absent from older stored params.
    cooldown_minutes: int = Field(default=DEFAULT_COOLDOWN_MINUTES, ge=0, le=1440)

    @model_validator(mode="after")
    def _ladder_above_zero(self) -> "DcaParams":
        if _deviation(self, self.so_count) >= 0.9:
            raise ValueError("as ordens de segurança passam de 90% abaixo da base")
        return self


class GridParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    grids: int = Field(ge=2, le=30)
    mode: Literal["arithmetic", "geometric"] = "arithmetic"
    invest_usd: float = Field(gt=0)
    #: Below the range: the resting stop for everything held, and the end of the grid.
    stop_price: float = Field(gt=0)
    #: Above the range: sell everything and end the grid. None = no take-profit.
    take_profit_price: float | None = Field(default=None, gt=0)
    max_orders_per_day: int = Field(default=20, ge=4, le=200)
    #: Pause between a full sell-out and the restart from an empty inventory. Absent from older stored params.
    cooldown_minutes: int = Field(default=DEFAULT_COOLDOWN_MINUTES, ge=0, le=1440)

    @model_validator(mode="after")
    def _ordered(self) -> "GridParams":
        if not self.stop_price < self.lower < self.upper:
            raise ValueError("é preciso stop < inferior < superior")
        if self.take_profit_price is not None and self.take_profit_price <= self.upper:
            raise ValueError("o take-profit fica acima do limite superior")
        return self


# -- DCA ----------------------------------------------------------------------


def _deviation(params: DcaParams, index: int) -> float:
    """Distance below the base price of safety order ``index`` (1-based)."""
    return params.step_pct * sum(params.step_mult**j for j in range(index))


def dca_ladder(params: DcaParams, base_price: float) -> list[dict[str, float]]:
    """The base order (index 0) and every safety order: price, quote size and the average after it fills."""
    rows: list[dict[str, float]] = []
    spent = held = 0.0
    for index in range(params.so_count + 1):
        price = base_price * (1 - _deviation(params, index))
        usd = params.base_usd * params.size_mult**index
        spent += usd
        held += usd / price
        rows.append({"index": index, "price": price, "usd": usd, "avg": spent / held, "qty": held})
    return rows


def dca_stop(params: DcaParams, base_price: float) -> float:
    return dca_ladder(params, base_price)[-1]["price"] * (1 - params.stop_pct)


def dca_plan(params: DcaParams, base_price: float, *, taker: float) -> dict[str, Any]:
    ladder = dca_ladder(params, base_price)
    stop = dca_stop(params, base_price)
    last = ladder[-1]
    total = sum(row["usd"] for row in ladder)
    # Every order filled, then the stop: the whole ladder sold at the stop, both fees paid.
    loss = total - last["qty"] * stop + (total + last["qty"] * stop) * taker
    return {
        "ladder": ladder,
        "stop_price": stop,
        "total_usd": total,
        "loss_at_stop_usd": loss,
        "take_profit_from_base": base_price * (1 + params.take_profit_pct),
        "orders_per_round": DCA_FIXED_ORDERS + ORDERS_PER_LEG * params.so_count,
    }


def dca_next(params: DcaParams, base_price: float, filled: int, price: float, avg: float) -> tuple[str, int] | None:
    """``("take_profit", 0)``, ``("safety", index)`` of the next safety order reached, or None."""
    if price >= avg * (1 + params.take_profit_pct):
        return ("take_profit", 0)
    if filled < params.so_count:
        level = dca_ladder(params, base_price)[filled + 1]
        if price <= level["price"]:
            return ("safety", filled + 1)
    return None


# -- grid ---------------------------------------------------------------------


def grid_levels(params: GridParams) -> list[float]:
    """The ``grids + 1`` prices from ``lower`` to ``upper``."""
    n = params.grids
    if params.mode == "geometric":
        ratio = (params.upper / params.lower) ** (1 / n)
        return [params.lower * ratio**i for i in range(n + 1)]
    step = (params.upper - params.lower) / n
    return [params.lower + step * i for i in range(n + 1)]


def grid_qty(params: GridParams) -> list[float]:
    """Base quantity of each level: the same quote at each buy price."""
    per_level = params.invest_usd / params.grids
    return [per_level / price for price in grid_levels(params)[:-1]]


def grid_start_k(levels: list[float], price: float) -> int:
    """Lowest level held when the grid starts at ``price``: every level whose sell price is above it."""
    return bisect_right(levels[1:], price)


def grid_k_from_qty(qtys: list[float], held: float) -> int:
    """Lowest held level whose quantities from there up are closest to ``held``."""
    sums = [sum(qtys[k:]) for k in range(len(qtys) + 1)]
    return min(range(len(sums)), key=lambda k: abs(sums[k] - held))


def grid_next(levels: list[float], k: int, price: float) -> tuple[str, int] | None:
    """``("buy", new_k)`` for the levels ``price`` fell to, ``("sell", new_k)`` for those it rose past."""
    buy_from = bisect_left(levels[:-1], price)
    if buy_from < k:
        return ("buy", buy_from)
    sell_to = bisect_right(levels[1:], price)
    if sell_to > k:
        return ("sell", sell_to)
    return None


def grid_plan(params: GridParams, price: float, *, taker: float, spread: float) -> dict[str, Any]:
    levels = grid_levels(params)
    qtys = grid_qty(params)
    k0 = grid_start_k(levels, price)
    gross = min(levels[i + 1] / levels[i] - 1 for i in range(params.grids))
    net = gross - 2 * taker - spread
    first_buy_qty = sum(qtys[k0:])
    # Every level held when the price reaches the stop: the start levels were bought at ``price``.
    loss = sum(qty * (max(level, price) - params.stop_price) for qty, level in zip(qtys, levels))
    loss += sum(qty * (max(level, price) + params.stop_price) for qty, level in zip(qtys, levels)) * taker
    return {
        "levels": levels,
        "qty_per_level": qtys,
        "start_k": k0,
        "first_buy_usd": first_buy_qty * price,
        "first_buy_qty": first_buy_qty,
        "profit_per_grid_gross": gross,
        "profit_per_grid_net": net,
        "loss_at_stop_usd": loss,
        "max_held_usd": max(params.invest_usd, first_buy_qty * price),
    }


# -- creation checks ------------------------------------------------------------


def creation_problems(
    kind: Kind,
    plan: dict[str, Any],
    *,
    min_notional: float,
    order_cap: float,
    sizing_capital: float,
    exposure_room: float,
    risk_per_trade_pct: float,
    taker: float,
    spread: float,
    params: DcaParams | GridParams,
    live_instances: int = 0,
    max_instances: int | None = None,
) -> list[str]:
    """Why the plan cannot run, in the operator's words; empty when it can.

    ``live_instances`` counts the robots already running, without this one;
    ``max_instances`` None leaves their number unchecked.
    """
    problems: list[str] = []
    if max_instances is not None and live_instances >= max_instances:
        problems.append(f"já há {live_instances} robôs ativos (máximo de {max_instances})")
    cap = order_cap * CAP_HEADROOM
    risk_cap = sizing_capital * risk_per_trade_pct
    if plan["loss_at_stop_usd"] > risk_cap:
        problems.append(
            f"perda no stop de US$ {plan['loss_at_stop_usd']:.2f} passa do risco por trade de US$ {risk_cap:.2f}"
        )
    if kind == "dca":
        assert isinstance(params, DcaParams)
        sizes = [row["usd"] for row in plan["ladder"]]
        if min(sizes) < min_notional:
            problems.append(f"ordem de US$ {min(sizes):.2f} abaixo do mínimo da corretora de US$ {min_notional:.2f}")
        if plan["total_usd"] > cap:
            problems.append(f"a posição cheia de US$ {plan['total_usd']:.2f} passa do teto por ordem de US$ {cap:.2f} (o stop é uma ordem só)")
        if plan["total_usd"] > exposure_room:
            problems.append(f"investimento de US$ {plan['total_usd']:.2f} passa da exposição livre de US$ {exposure_room:.2f}")
        if params.take_profit_pct < 2 * taker + spread + MIN_NET_MARGIN:
            problems.append(f"take-profit abaixo de {2 * taker + spread + MIN_NET_MARGIN:.2%}: não paga taxas e spread")
    else:
        assert isinstance(params, GridParams)
        per_level = params.invest_usd / params.grids
        if per_level < min_notional * MIN_NOTIONAL_HEADROOM:
            problems.append(
                f"US$ {per_level:.2f} por grade: a venda de uma grade ficaria abaixo do mínimo da corretora de US$ {min_notional:.2f}"
                f" (é preciso ao menos US$ {min_notional * MIN_NOTIONAL_HEADROOM:.2f})"
            )
        if plan["max_held_usd"] > cap:
            problems.append(f"o estoque cheio de US$ {plan['max_held_usd']:.2f} passa do teto por ordem de US$ {cap:.2f} (o stop é uma ordem só)")
        if plan["max_held_usd"] > exposure_room:
            problems.append(f"investimento de US$ {plan['max_held_usd']:.2f} passa da exposição livre de US$ {exposure_room:.2f}")
        if plan["profit_per_grid_net"] < MIN_NET_MARGIN:
            problems.append(
                f"lucro por grade de {plan['profit_per_grid_net']:.2%} depois de taxas e spread; o mínimo é {MIN_NET_MARGIN:.2%}"
            )
    return problems


def cooldown_ends(closed_at_iso: str, minutes: int) -> datetime:
    """When the next round may start: ``minutes`` after the previous round closed, in UTC.

    The store writes aware UTC timestamps; a naive one is read as UTC.
    """
    closed_at = datetime.fromisoformat(closed_at_iso)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    return closed_at.astimezone(timezone.utc) + timedelta(minutes=minutes)


def worst_case_loss(instance: dict[str, Any], *, taker: float) -> float:
    """The loss an instance reserved at creation: the open risk it may still add."""
    kind = instance["kind"]
    state = instance["state"]
    if kind == "dca":
        params = DcaParams.model_validate(instance["params"])
        return float(dca_plan(params, float(state["base_price"]), taker=taker)["loss_at_stop_usd"])
    params_grid = GridParams.model_validate(instance["params"])
    return float(grid_plan(params_grid, float(state["start_price"]), taker=taker, spread=0.0)["loss_at_stop_usd"])


def instance_budget(instance: dict[str, Any]) -> float:
    """Quote an instance may hold at most: the whole DCA ladder, or the grid investment."""
    if instance["kind"] == "dca":
        params = DcaParams.model_validate(instance["params"])
        return sum(params.base_usd * params.size_mult**index for index in range(params.so_count + 1))
    return float(GridParams.model_validate(instance["params"]).invest_usd)

"""The day-trade preset: the config it builds, what it changes and the order it sizes."""

from __future__ import annotations

from typing import Any

from bot import risk
from bot.config import BotConfig
from bot.evaluator import Opportunity
from bot.mandate_io import mandate_caps_for
from bot.strategies import DAY_STRATEGIES
from bot.strategies.day import DAY

#: Stop distance of the day preset's sizing example.
PRESET_STOP_PCT = 0.01
#: The day-trade preset: research-backed defaults (docs/plano-day-trade.md), editable afterwards.
DAY_PRESET_LIMITS: dict[str, float | int] = {
    "risk_per_trade_pct": 0.005,
    "max_daily_loss_pct": 0.02,
    "max_trades_per_day": 8,
    "pair_cooldown_minutes": 60,
    "loss_guard_trades": 3,
}
DAY_PRESET_FIELDS: dict[str, float] = {"cost_multiple": 3.0, "min_reward_risk": 1.2}


def _day_preset(current: BotConfig) -> BotConfig:
    """The current config with the day-trade limits, the day strategies on and the swing ones off."""
    data = current.model_dump()
    data["limits"] = {**data["limits"], **DAY_PRESET_LIMITS}
    data.update(DAY_PRESET_FIELDS)
    data["strategies"] = {name: name in DAY_STRATEGIES for name in data["strategies"]}
    return BotConfig.model_validate(data)


def _preset_preview(current: BotConfig, preset: BotConfig) -> dict[str, Any]:
    """What the preset changes, and what an order can really be under it."""
    before, after = current.model_dump(), preset.model_dump()
    changes = [
        {"field": f"limits.{name}", "from": before["limits"][name], "to": after["limits"][name]}
        for name in DAY_PRESET_LIMITS
        if before["limits"][name] != after["limits"][name]
    ]
    changes += [
        {"field": name, "from": before[name], "to": after[name]} for name in DAY_PRESET_FIELDS if before[name] != after[name]
    ]
    changes += [
        {"field": f"strategies.{name}", "from": before["strategies"].get(name), "to": on}
        for name, on in after["strategies"].items()
        if before["strategies"].get(name) != on
    ]
    capital = preset.capital_for_sizing()
    order_cap = capital * preset.limits.max_order_notional_pct
    # At the narrowest day stop, the risk per trade asks for this notional; the order cap may cut it.
    wanted = capital * preset.limits.risk_per_trade_pct / DAY.min_stop_pct
    return {
        "changes": changes,
        "order_cap_usd": round(order_cap, 2),
        "risk_notional_usd": round(wanted, 2),
        "capped_by_order_cap": wanted > order_cap,
        "mandate_orders_per_day": mandate_caps_for(preset)["max_trades_per_day"],
        "sizing": _size_example(preset, PRESET_STOP_PCT),
    }


def _size_example(config: BotConfig, stop_pct: float) -> dict[str, Any]:
    """The order one entry at a ``stop_pct`` stop would place under ``config`` (``RiskManager.sizing``).

    No pair is named, so neither the venue nor the volatility cap applies, and no hit
    rate is known, so Kelly does not cut the share.
    """
    manager = risk.RiskManager(config.limits, config.capital_for_sizing())
    opportunity = Opportunity(
        symbol="",
        strategy="preset",
        action="Comprar",
        score=0.0,
        net_return=0.0,
        risk_pct=stop_pct,
        status="potencial",
        reason="",
        timeframe="15m",
        direction=1.0,
        cost=0.0,
        gross_return=0.0,
    )
    decision = manager.sizing(opportunity)
    return {
        "stop_pct": stop_pct,
        "size_usd": round(decision.size_usd, 2),
        "binding": decision.binding,
        "label": risk.SIZE_LIMITS[decision.binding],
        "caps": {name: round(value, 2) for name, value in decision.caps.items()},
    }

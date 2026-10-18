"""Operator configuration.

The mandate is written from these limits (``bot.mandate_io``), so every limit
has a fixed server-side ceiling here. The mandate gate then enforces them on
each order, and the bot's own risk checks use the tighter of the two.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bot.paths import base_runtime_root

Mode = Literal["testnet", "production"]
#: ``manual``: the cycle only manages exits of positions opened from the panel.
#: ``auto``: the cycle also enters ready rows whose estimated hit rate clears ``min_win_rate``
#: (swing) or the first-target break-even plus ``DAY_WIN_RATE_MARGIN`` (day).
EntryMode = Literal["manual", "auto"]
DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT")
#: Each symbol costs about seven REST reads per cycle.
MAX_SYMBOLS = 20
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}/USDT$")
#: Every strategy id the engine knows, off until the operator turns it on.
DEFAULT_STRATEGIES = {
    "trend": False,
    "mean_reversion": False,
    "momentum": False,
    "funding_carry": False,
    "intraday_momentum": False,
    "vwap_pullback": False,
}


def is_usdt_pair(symbol: str) -> bool:
    return bool(_SYMBOL.match(symbol))


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    max_order_notional_pct: float = 0.02
    max_exposure_pct: float = 0.60
    #: Spot has no leverage. A saved value above 1 is read as 1.
    max_leverage: float = 1.0
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    #: New positions per UTC day. The mandate allows four orders per entry.
    max_trades_per_day: int = Field(default=20, ge=1, le=100)
    max_positions: int = Field(default=5, ge=1, le=20)
    max_correlated: int = Field(default=2, ge=1)
    correlation_threshold: float = Field(default=0.80, gt=0, le=1)
    #: Loss at the stop of one new position, as a share of the sizing capital.
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.02)
    #: Sum of the loss to the stop of every live position plus a new one.
    max_open_risk_pct: float = Field(default=0.06, gt=0, le=0.20)
    #: No new entry on a pair for this many minutes after a losing close on it (0 = off).
    pair_cooldown_minutes: int = Field(default=120, ge=0, le=1440)
    #: Losing closes in the last 6 hours that pause every new entry (0 = off).
    loss_guard_trades: int = Field(default=3, ge=0, le=20)
    #: Share of full Kelly a sized row may risk at its stop, when its hit rate is known (0 = off).
    kelly_fraction: float = Field(default=0.25, ge=0, le=1)
    #: New positions one cycle may open; the rest wait for the next cycle.
    max_entries_per_cycle: int = Field(default=2, ge=1, le=20)
    #: Daily orders the mandate reserves for DCA and grid instances, on top of the
    #: strategies' budget. The instances' own daily caps must fit in it (0 = no instances).
    instance_orders_per_day: int = Field(default=0, ge=0, le=400)

    @field_validator("max_order_notional_pct", "max_exposure_pct", "max_daily_loss_pct", "max_drawdown_pct")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("percentage limits must be in (0, 1]")
        return value

    @field_validator("max_leverage")
    @classmethod
    def _spot_leverage(cls, value: float) -> float:
        if not value > 0:
            raise ValueError("max_leverage must be positive")
        return min(value, 1.0)

    @model_validator(mode="after")
    def _open_risk_covers_one_trade(self) -> "RiskLimits":
        if self.risk_per_trade_pct > self.max_open_risk_pct:
            raise ValueError("risk_per_trade_pct must not exceed max_open_risk_pct")
        return self


class BotConfig(BaseModel):
    # json.loads accepts Infinity; an infinite capital would be saved as null and break the next load.
    model_config = ConfigDict(allow_inf_nan=False)

    mode: Mode = "testnet"
    allocated_capital_usd: float = Field(default=1000.0, gt=0, le=1_000_000_000)
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_SYMBOLS), min_length=1, max_length=MAX_SYMBOLS)
    strategies: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_STRATEGIES))
    flatten_on_halt: bool = False
    entry_mode: EntryMode = "manual"
    #: Laplace-estimated share of winning trades a strategy needs for an automatic entry.
    min_win_rate: float = Field(default=0.60, ge=0.5, le=0.95)
    #: Backtest plus live trades below this count give no estimate, so no automatic entry.
    min_win_rate_sample: int = Field(default=20, ge=10, le=1000)
    #: The stop follows the highest price at the entry's stop distance; the fixed target is not used.
    trailing_stop: bool = True
    #: With ``trailing_stop``: the entry stop stays until the gain reaches the activation
    #: (``bot.strategies.trailing.TRAIL``); then the stop follows the highest price closer.
    trailing_activation: bool = False
    #: Spot Testnet only: buy setups that miss a quality filter or validation can be
    #: executed by hand, for testing the order flow. Production ignores it.
    testnet_test_mode: bool = False
    #: About once an hour, replace ``symbols`` by the liquid USDT pairs with the best
    #: risk-adjusted 28-day momentum. Pairs with a live position always stay.
    auto_select: bool = False
    auto_select_count: int = Field(default=15, ge=5, le=MAX_SYMBOLS)
    limits: RiskLimits = Field(default_factory=RiskLimits)
    score_threshold: float = 0.3
    min_reward_risk: float = 1.5
    #: An entry needs this expected profit in USD at its target, net of costs, at the size it gets.
    min_profit_usd: float = Field(default=0.0, ge=0, le=1000)
    cost_multiple: float = 2.0
    funding_annualized_threshold: float = 0.10
    min_quote_volume_usd: float = 5_000_000.0
    tick_seconds: int = 60
    production_capital_fraction: float = Field(default=0.10, gt=0, le=1)
    #: A production pilot without 30 days of testnet history is allowed while the production
    #: capital stays at or below this (0 = no pilot). Like the gates, only a hand edit of
    #: bot.json sets it; once in production the cap recorded at the switch is the one enforced.
    pilot_max_production_capital_usd: float = Field(default=0.0, ge=0, le=50)
    #: Operator attestations, edited in bot.json only. The API never writes them.
    gates: dict[str, bool] = Field(
        default_factory=lambda: {"g1": False, "g2": False, "g3": False, "g4": False}
    )

    @field_validator("symbols")
    @classmethod
    def _usdt_pairs(cls, value: list[str]) -> list[str]:
        clean = [item.strip().upper() for item in value]
        bad = [item for item in clean if not _SYMBOL.match(item)]
        if bad:
            raise ValueError(f"symbols must be BASE/USDT spot pairs: {bad}")
        return list(dict.fromkeys(clean))

    @field_validator("strategies")
    @classmethod
    def _known_strategies(cls, value: dict[str, bool]) -> dict[str, bool]:
        """A bot.json saved before a strategy existed lists it as off."""
        return {**DEFAULT_STRATEGIES, **value}

    def config_path(self) -> Path:
        return base_runtime_root() / "bot.json"

    def save(self) -> Path:
        """Write bot.json atomically: a crash mid-write leaves the previous file, never a truncated one."""
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "BotConfig":
        target = path or (base_runtime_root() / "bot.json")
        if not target.is_file():
            return cls()
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bot.json must be an object")
        return cls.model_validate(data)

    @property
    def test_mode_active(self) -> bool:
        return self.mode == "testnet" and self.testnet_test_mode

    @property
    def exit_policy(self) -> str:
        """The exit rule live trading and the backtest gate both follow."""
        if not self.trailing_stop:
            return "target"
        return "trailing_tp" if self.trailing_activation else "trailing"

    def production_capital(self) -> float:
        """The capital production trades with: ``production_capital_fraction`` of the allocated capital."""
        return self.allocated_capital_usd * self.production_capital_fraction

    def capital_for_sizing(self) -> float:
        """Production starts at ``production_capital_fraction`` of the allocated capital."""
        if self.mode == "production":
            return self.production_capital()
        return self.allocated_capital_usd

    def tightened(self, mandate_caps: Any | None) -> RiskLimits:
        """Return limits that never exceed the mandate."""
        limits = self.limits.model_copy()
        if mandate_caps is None:
            return limits
        capital = self.capital_for_sizing()
        order_cap = float(mandate_caps.max_order_notional_usd)
        exposure_cap = float(mandate_caps.max_total_exposure_usd)
        if capital > 0:
            limits.max_order_notional_pct = min(limits.max_order_notional_pct, order_cap / capital)
            limits.max_exposure_pct = min(limits.max_exposure_pct, exposure_cap / capital)
        limits.max_leverage = min(limits.max_leverage, float(mandate_caps.max_leverage))
        return limits

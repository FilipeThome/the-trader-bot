"""Operator configuration. Limits here can only tighten the mandate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from bot.paths import base_runtime_root

Mode = Literal["testnet", "production"]
DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT")


class RiskLimits(BaseModel):
    max_order_notional_pct: float = 0.02
    max_exposure_pct: float = 0.60
    max_leverage: float = 3.0
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    max_trades_per_day: int = 20
    max_positions: int = 5
    max_correlated: int = 2
    correlation_threshold: float = 0.80

    @field_validator("max_order_notional_pct", "max_exposure_pct", "max_daily_loss_pct", "max_drawdown_pct")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("percentage limits must be in (0, 1]")
        return value


class BotConfig(BaseModel):
    mode: Mode = "testnet"
    allocated_capital_usd: float = Field(default=1000.0, gt=0)
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    strategies: dict[str, bool] = Field(
        default_factory=lambda: {
            "trend": False,
            "mean_reversion": False,
            "funding_carry": False,
        }
    )
    flatten_on_halt: bool = False
    limits: RiskLimits = Field(default_factory=RiskLimits)
    score_threshold: float = 0.3
    min_reward_risk: float = 1.5
    cost_multiple: float = 2.0
    funding_annualized_threshold: float = 0.10
    min_quote_volume_usd: float = 5_000_000.0
    tick_seconds: int = 60
    production_capital_fraction: float = 0.10
    gates: dict[str, bool] = Field(
        default_factory=lambda: {"g1": False, "g2": False, "g3": False, "g4": False}
    )

    def config_path(self) -> Path:
        return base_runtime_root() / "bot.json"

    def save(self) -> Path:
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "BotConfig":
        target = path or (base_runtime_root() / "bot.json")
        if not target.is_file():
            cfg = cls()
            return cfg
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bot.json must be an object")
        return cls.model_validate(data)

    def capital_for_sizing(self) -> float:
        """Production starts at 10% of the allocated capital."""
        if self.mode == "production":
            return self.allocated_capital_usd * self.production_capital_fraction
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
        limits.max_trades_per_day = min(limits.max_trades_per_day, int(mandate_caps.max_trades_per_day))
        return limits

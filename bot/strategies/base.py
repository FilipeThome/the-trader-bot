"""Shared strategy result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Setup:
    strategy_id: str
    symbol: str
    timeframe: str
    action: str
    direction: float
    expected_return: float
    sigma: float
    stop_pct: float
    target_pct: float
    reason: str
    regime: str
    market: str = "spot"
    confidence: float = 0.0
    alphas: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

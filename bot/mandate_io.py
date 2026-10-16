"""Operator consent path. The agent loop cannot import a writer; this module is the UI's."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bot.live.mandate.model import (
    MANDATE_SCHEMA_VERSION,
    AssetClass,
    InstrumentType,
)
from bot.live.paths import broker_dir
from bot.paths import get_runtime_root

if TYPE_CHECKING:
    from bot.config import BotConfig


def commit_binance_mandate(
    *,
    capital_usd: float,
    max_order_notional_usd: float,
    max_total_exposure_usd: float,
    max_leverage: float = 1.0,
    max_trades_per_day: int = 20,
    flatten_on_halt: bool = False,
    days: int = 30,
) -> Path:
    """Write ``mandate.json`` for the Binance broker. Instruments are crypto only."""
    now = datetime.now(timezone.utc)
    token = hashlib.sha256(f"binance|{capital_usd}|{now.isoformat()}".encode()).hexdigest()
    payload = {
        "schema_version": MANDATE_SCHEMA_VERSION,
        "hard_caps": {
            "account_funding_usd": capital_usd,
            "max_order_notional_usd": max_order_notional_usd,
            "max_total_exposure_usd": max_total_exposure_usd,
            "max_leverage": max_leverage,
            "allowed_instruments": [InstrumentType.CRYPTO.value],
            "max_trades_per_day": max_trades_per_day,
        },
        "universe": {
            "asset_classes": [AssetClass.CRYPTO.value],
            "min_market_cap_usd": None,
            "min_avg_daily_volume_usd": None,
            "exclude_symbols": [],
        },
        "consent": {
            "created_at": now.isoformat(),
            "consent_token_sha256": token,
            "broker": "binance",
            "account_ref": "bot-operator",
            "expires_at": (now + timedelta(days=days)).isoformat(),
        },
        "flatten_on_halt": flatten_on_halt,
    }
    path = broker_dir("binance") / "mandate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


#: Orders one bot entry may need: entry, stop, exit and one re-placed stop.
ORDERS_PER_ENTRY = 4
#: A day trade adds the first-target sale and the break-even stop.
ORDERS_PER_DAY_ENTRY = 6


def commit_mandate_for(config: "BotConfig") -> Path:
    """Write the mandate for the active mode from the operator limits."""
    from bot.strategies import DAY_STRATEGIES

    capital = config.capital_for_sizing()
    limits = config.limits
    day = any(config.strategies.get(strategy_id, False) for strategy_id in DAY_STRATEGIES)
    per_entry = ORDERS_PER_DAY_ENTRY if day else ORDERS_PER_ENTRY
    return commit_binance_mandate(
        capital_usd=capital,
        max_order_notional_usd=capital * limits.max_order_notional_pct,
        max_total_exposure_usd=capital * limits.max_exposure_pct,
        max_leverage=limits.max_leverage,
        max_trades_per_day=per_entry * limits.max_trades_per_day,
        flatten_on_halt=config.flatten_on_halt,
    )


def mandate_is_restricted() -> bool:
    from bot.live.mandate.store import load_mandate

    mandate = load_mandate("binance")
    if mandate is None:
        return False
    instruments = set(mandate.hard_caps.allowed_instruments)
    return instruments == {InstrumentType.CRYPTO} and AssetClass.CRYPTO in mandate.universe.asset_classes


def active_root_hint() -> Path:
    return get_runtime_root()

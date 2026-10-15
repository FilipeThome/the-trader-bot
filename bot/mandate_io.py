"""Operator consent path. The agent loop cannot import a writer; this module is the UI's."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.live.mandate.model import (
    MANDATE_SCHEMA_VERSION,
    AssetClass,
    InstrumentType,
)
from bot.live.paths import broker_dir
from bot.paths import get_runtime_root


def commit_binance_mandate(
    *,
    capital_usd: float,
    max_order_notional_usd: float,
    max_total_exposure_usd: float,
    max_leverage: float = 3.0,
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


def mandate_is_restricted() -> bool:
    from bot.live.mandate.store import load_mandate

    mandate = load_mandate("binance")
    if mandate is None:
        return False
    instruments = set(mandate.hard_caps.allowed_instruments)
    return instruments == {InstrumentType.CRYPTO} and AssetClass.CRYPTO in mandate.universe.asset_classes


def active_root_hint() -> Path:
    return get_runtime_root()

"""Runtime root for the bot.

Ported modules call :func:`get_runtime_root`. A mode pin separates testnet and
production state (store, journal, mandate, halt, ledger). The pin is process
state, not a context variable, so the tick thread and every API thread see the
same environment. ``binance.json`` and the API token stay at the base root
(:func:`base_runtime_root`). Tests that set ``VIBE_TRADING_HOME`` keep the
Vibe-Trading contract when no pin is active.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

_PIN_LOCK = threading.Lock()
_MODE_ROOT: Path | None = None


@dataclass(frozen=True)
class PinToken:
    """The pin that was active before :func:`pin_mode_root`."""

    previous: Path | None


def pin_mode_root(path: Path | None) -> PinToken:
    """Point ported modules at one environment directory for the whole process."""
    global _MODE_ROOT
    with _PIN_LOCK:
        token = PinToken(_MODE_ROOT)
        _MODE_ROOT = path
    return token


def reset_mode_root(token: PinToken) -> None:
    global _MODE_ROOT
    with _PIN_LOCK:
        _MODE_ROOT = token.previous


def base_runtime_root() -> Path:
    """Operator root: credentials, ``bot.json`` and the API token. Ignores the mode pin."""
    for var in ("BOT_RUNTIME_ROOT", "VIBE_TRADING_HOME"):
        env_root = os.environ.get(var, "").strip()
        if env_root:
            if env_root.startswith(("//", "\\\\")):
                raise ValueError(f"{var} must not be a UNC path: {env_root!r}")
            return Path(env_root).expanduser()
    return Path.home() / ".the-trader-bot"


def get_runtime_root(config_path: Path | None = None) -> Path:
    """Return the directory that holds the journal, the halt flag and the mandate."""
    if config_path is not None:
        return config_path.expanduser().parent
    with _PIN_LOCK:
        pinned = _MODE_ROOT
    if pinned is not None:
        return pinned
    return base_runtime_root()

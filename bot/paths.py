"""Runtime root for the bot.

Ported modules call :func:`get_runtime_root`. A mode pin separates testnet and
production state (store, journal, mandate, halt). Tests that set
``VIBE_TRADING_HOME`` keep the Vibe-Trading contract when no pin is active.
"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path

_MODE_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "bot_mode_root", default=None
)


def pin_mode_root(path: Path | None) -> contextvars.Token[Path | None]:
    """Point ported modules at one environment directory for this context."""
    return _MODE_ROOT.set(path)


def reset_mode_root(token: contextvars.Token[Path | None]) -> None:
    _MODE_ROOT.reset(token)


def base_runtime_root() -> Path:
    """Operator config root. Ignores the per-mode pin."""
    token = _MODE_ROOT.set(None)
    try:
        return get_runtime_root()
    finally:
        _MODE_ROOT.reset(token)


def get_runtime_root(config_path: Path | None = None) -> Path:
    """Return the directory that holds credentials, the journal and the halt flag."""
    if config_path is not None:
        return config_path.expanduser().parent
    pinned = _MODE_ROOT.get()
    if pinned is not None:
        return pinned
    for var in ("BOT_RUNTIME_ROOT", "VIBE_TRADING_HOME"):
        env_root = os.environ.get(var, "").strip()
        if env_root:
            if env_root.startswith(("//", "\\\\")):
                raise ValueError(f"{var} must not be a UNC path: {env_root!r}")
            return Path(env_root).expanduser()
    return Path.home() / ".the-trader-bot"

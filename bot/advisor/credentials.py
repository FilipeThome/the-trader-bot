"""The DeepSeek key: ``deepseek.json`` at the operator's base root, next to ``binance.json``.

``{"api_key": "...", "base_url": "https://api.deepseek.com"}``. ``DEEPSEEK_API_KEY`` counts only
when the file holds no key. On POSIX a file readable by the group or others is refused, as an
SSH client refuses a loose private key. The key never goes to ``bot.json``, an API response, a
log, the ledger or ``advice_log``; ``python -m bot.ops advisor-key`` writes it from stdin.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from bot.paths import base_runtime_root

FILENAME = "deepseek.json"
DEFAULT_BASE_URL = "https://api.deepseek.com"
ENV_KEY = "DEEPSEEK_API_KEY"


class CredentialsError(RuntimeError):
    """The key file exists but cannot be used. The message names the file, never its content."""


@dataclass(frozen=True)
class Credentials:
    #: Hidden from ``repr`` so a logged object never shows it.
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL


def path() -> Path:
    return base_runtime_root() / FILENAME


def load() -> Credentials | None:
    """The key and base URL, or None when neither the file nor ``DEEPSEEK_API_KEY`` has a key.

    Raises ``CredentialsError`` for a file that is unreadable, not an object, or open to the
    group or others on POSIX.
    """
    target = path()
    data: dict[str, object] = {}
    if target.is_file():
        if os.name == "posix" and stat.S_IMODE(target.stat().st_mode) & 0o077:
            raise CredentialsError(f"{FILENAME} com permissão aberta demais; use chmod 600")
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialsError(f"{FILENAME} ilegível") from exc
        if not isinstance(raw, dict):
            raise CredentialsError(f"{FILENAME} deve ser um objeto")
        data = raw
    key = str(data.get("api_key") or "").strip() or os.environ.get(ENV_KEY, "").strip()
    if not key:
        return None
    base_url = str(data.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE_URL
    return Credentials(key, base_url)


def configured() -> bool:
    """Whether a usable key exists; an unusable file reads as not configured."""
    try:
        return load() is not None
    except CredentialsError:
        return False


def save_key(api_key: str) -> Path:
    """Write the key with owner-only permissions, keeping the file's other fields (``base_url``).

    Atomic like ``sdk.save_config``: a unique owner-only temporary file (``mkstemp``), fsynced,
    then ``os.replace``. On Windows 0o600 is best effort: the file inherits the folder's ACL.
    """
    key = api_key.strip()
    if not key:
        raise CredentialsError("chave vazia")
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload: dict[str, object] = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialsError(f"{FILENAME} ilegível; nada foi gravado") from exc
        if isinstance(existing, dict):
            payload = existing
    payload["api_key"] = key
    payload.setdefault("base_url", DEFAULT_BASE_URL)
    body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target

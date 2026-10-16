"""Who may call the bot API.

The API binds to loopback, but a browser on the same machine is a client too.
Three checks run on every ``/bot`` request:

* ``Host`` must name the API itself, so a DNS-rebinding page is refused;
* ``Origin``, when the browser sends one, must be the panel's dev server, and
  every write must carry ``X-Bot-Client: panel`` (a page on another origin
  cannot add that header without a CORS preflight, and the API grants none);
* ``X-Bot-Token`` must equal the per-install token in ``<base root>/api-token``.
  The Vite proxy injects it; the browser never sees it.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from bot.paths import base_runtime_root

TOKEN_HEADER = "x-bot-token"
CLIENT_HEADER = "x-bot-client"
CLIENT_VALUE = "panel"
ALLOWED_HOSTS = frozenset({"127.0.0.1:8000", "localhost:8000"})
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173"})
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_TOKEN_FILENAME = "api-token"


def token_path() -> Path:
    return base_runtime_root() / _TOKEN_FILENAME


def load_or_create_token() -> str:
    """Read the install token, creating it owner-only on first start."""
    path = token_path()
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def refusal(method: str, headers: dict[str, str], token: str | None) -> tuple[int, str] | None:
    """Return ``(status, detail)`` when the request must be refused, else ``None``.

    ``headers`` uses lower-case names.
    """
    if headers.get("host", "") not in ALLOWED_HOSTS:
        return 403, "host não permitido"
    origin = headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return 403, "origem não permitida"
    if method not in _SAFE_METHODS and headers.get(CLIENT_HEADER) != CLIENT_VALUE:
        return 403, "escrita sem o cabeçalho do painel"
    if not token:
        return 503, "token da API indisponível"
    supplied = headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        return 401, "token ausente ou inválido"
    return None

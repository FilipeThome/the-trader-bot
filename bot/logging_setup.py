"""Secret redaction for every log line, halt reason and error the bot shows.

ccxt puts the signed URL (``signature=...``) in its error messages and tracebacks, and an
HTTP client error can echo a request with its ``Authorization`` header. ``redact`` scrubs a
free-form string with the vendored ``redact_text`` (left as it is in its origin commit) plus
the shapes it does not catch here: a query-string signature, an ``api_key`` label behind a
prefix (``deepseek_api_key=``), and short ``sk-`` keys.

``RedactingFormatter`` applies it to the whole formatted record, traceback and stack included,
so no handler writes a secret whichever logger or library produced it. A filter on the root
logger would miss records of child loggers and could not see the formatted traceback.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from bot.tools.redaction import redact_text

_REDACTED = "[redacted]"

#: Value of a pair: stops at whitespace, quotes, separators and brackets, so an
#: inserted ``[redacted]`` is never matched again (the patterns stay idempotent).
_VALUE = r"[^\s\"'&,;{}\[\]()]+"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Binance's HMAC of a signed request, in a URL or a form body.
    (re.compile(r"(?i)\b(signature=)" + _VALUE), r"\1" + _REDACTED),
    # Any ``...api_key``/``...apikey`` label, prefixed or not: deepseek_api_key=, X-MBX-APIKEY: ...
    (re.compile(r"(?i)([A-Za-z0-9_]*api[_-]?key[\"']?\s*[:=]\s*[\"']?)" + _VALUE), r"\1" + _REDACTED),
    # ``Bearer <token>``, whatever label precedes it.
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1" + _REDACTED),
    # An ``sk-`` key (DeepSeek, OpenAI) shorter than ``redact_text``'s bound.
    (re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"), _REDACTED),
)

#: Loggers uvicorn configures with handlers of their own (``propagate`` off for access).
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact(text: object) -> str:
    """``text`` as a string with every credential-shaped value replaced. Never raises on input."""
    clean = redact_text(text)
    for pattern, replacement in _PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean


class RedactingFormatter(logging.Formatter):
    """Formats with the handler's own formatter, then redacts the whole line.

    The cached ``record.exc_text`` is redacted too, so another handler that reuses it never
    writes the raw traceback.
    """

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        line = self.inner.format(record)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return redact(line)


def install_redaction(logger_names: Iterable[str] = ()) -> None:
    """Wrap the formatter of every handler on the root logger and on ``logger_names``.

    Idempotent. Call it after the handlers exist (``logging.basicConfig``, uvicorn's config).
    """
    for logger in (logging.getLogger(), *(logging.getLogger(name) for name in logger_names)):
        for handler in logger.handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(handler.formatter))

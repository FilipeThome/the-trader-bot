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
    # The match starts at ``api``: only the value is replaced, so the prefix needs no class of
    # its own, and an unbounded class before the literal backtracks quadratically on a long word.
    (re.compile(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)" + _VALUE), r"\1" + _REDACTED),
    # ``Bearer <token>``, whatever label precedes it.
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1" + _REDACTED),
    # An ``sk-`` key (DeepSeek, OpenAI) shorter than ``redact_text``'s bound.
    (re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"), _REDACTED),
)

#: Loggers uvicorn configures with handlers of their own (``propagate`` off for access).
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

#: Longest text ``redact`` scans. An access line carries the request path a client chose, so the
#: patterns never run on more than this: a longer text keeps its head and the whole lines of its
#: tail (where a traceback ends), with a marker of what was left out between them.
MAX_REDACT_CHARS = 8192
_HEAD_CHARS = 4000
_TAIL_CHARS = 4000


def redact(text: object) -> str:
    """``text`` as a string with every credential-shaped value replaced, at most about
    ``MAX_REDACT_CHARS`` long (``_bounded``). Never raises on input."""
    clean = redact_text(_bounded("" if text is None else str(text)))
    for pattern, replacement in _PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean


def _bounded(text: str) -> str:
    """``text`` cut to its head, a marker and its tail from the tail's first line break on.

    A value cut at the head's end is followed by the marker's space, so the patterns still see
    its label and redact what is left of it. The tail starts at a line boundary, never inside a
    value whose label was cut away; a single line longer than the bound keeps only its head.
    """
    if len(text) <= MAX_REDACT_CHARS:
        return text
    head = text[:_HEAD_CHARS]
    tail = text[-_TAIL_CHARS:]
    newline = tail.find("\n")
    tail = tail[newline:] if newline >= 0 else ""
    return f"{head} [... {len(text) - len(head) - len(tail)} caracteres omitidos ...]{tail}"


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

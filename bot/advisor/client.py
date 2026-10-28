"""One DeepSeek chat completion in JSON mode, over an ``httpx.Client`` of its own.

The client never touches the Binance weight budget or its HTTP session. Every failure becomes
``AdvisorError(kind)``; the message names the kind and the HTTP status only, never the body (a
proxy or the API may echo the request) and never the key. An empty content, which the API
documents as possible in JSON mode, is asked once more before it counts as ``empty``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

ErrorKind = Literal["auth", "rate_limited", "timeout", "server", "empty", "invalid_json"]

#: Connect within 5 s, read within 30 s: the call runs on the advisor's thread, never the cycle's.
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
#: Low temperature: the same data should get the same verdict.
TEMPERATURE = 0.1
#: The verdict is a small JSON object; this bounds the cost of a runaway answer.
MAX_TOKENS = 400

#: Peak price per million tokens in USD: (cache hit, cache miss, output), from
#: api-docs.deepseek.com/quick_start/pricing read on 2026-10-28. An ESTIMATE: the billed
#: amount is the one in the DeepSeek console. Reasoning tokens bill as output.
PRICES_AS_OF = "2026-10-28"
PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (0.006, 0.30, 1.20),
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
}
#: Full price on weekdays within these UTC hours; half price otherwise (estimate, as above).
PEAK_HOURS_UTC = (range(1, 4), range(6, 10))
OFF_PEAK_DISCOUNT = 0.5


class AdvisorError(Exception):
    """A failed call. ``kind`` is one of ``ErrorKind``; ``str()`` never holds the body or the key."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind: ErrorKind = kind


@dataclass(frozen=True)
class Completion:
    """The answer's content and what it cost in tokens and time."""

    content: str
    prompt_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    completion_tokens: int
    latency_ms: float


def is_peak(moment: datetime) -> bool:
    """DeepSeek's full-price window: Monday to Friday, 01:00-04:00 and 06:00-10:00 UTC."""
    return moment.weekday() < 5 and any(moment.hour in hours for hours in PEAK_HOURS_UTC)


def estimate_cost(model: str, completion: Completion, moment: datetime) -> float:
    """The call's cost in USD from ``PRICES`` (an estimate); 0 for a model not in the table."""
    prices = PRICES.get(model)
    if prices is None:
        return 0.0
    hit, miss, output = prices
    missed = completion.cache_miss_tokens
    if completion.cache_hit_tokens + missed == 0:
        # A usage without the cache split is priced as all misses: an estimate never runs low.
        missed = completion.prompt_tokens
    cost = (completion.cache_hit_tokens * hit + missed * miss + completion.completion_tokens * output) / 1e6
    return cost if is_peak(moment) else cost * OFF_PEAK_DISCOUNT


class DeepSeekClient:
    """``POST {base_url}/chat/completions``. ``transport`` replaces the network in tests."""

    def __init__(self, api_key: str, base_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
            transport=transport,
        )

    def __enter__(self) -> DeepSeekClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def complete(self, model: str, system: str, user: str) -> Completion:
        """Ask once, and once more on an empty content. The system prompt goes first, so the
        API's prefix cache serves it on every call."""
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
        for _attempt in range(2):
            completion = self._post(body)
            if completion.content.strip():
                return completion
        raise AdvisorError("empty", "resposta sem conteúdo duas vezes")

    def _post(self, body: dict[str, Any]) -> Completion:
        started = time.monotonic()
        try:
            response = self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException:
            raise AdvisorError("timeout", "sem resposta no prazo") from None
        except httpx.HTTPError as exc:
            # Only the class name: a transport error's text may carry the request.
            raise AdvisorError("server", f"falha de rede ({type(exc).__name__})") from None
        latency = (time.monotonic() - started) * 1000
        status = response.status_code
        if status in (401, 402, 403):
            raise AdvisorError("auth", f"HTTP {status}")
        if status == 429:
            raise AdvisorError("rate_limited", "HTTP 429")
        if status >= 400:
            raise AdvisorError("server", f"HTTP {status}")
        try:
            # Long queues send blank lines before the body; json.loads skips the whitespace.
            payload = json.loads(response.text)
            content = payload["choices"][0]["message"].get("content") or ""
            usage = payload.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AdvisorError("invalid_json", f"corpo da resposta fora do formato (HTTP {status})") from None
        if not isinstance(content, str):
            raise AdvisorError("invalid_json", "conteúdo da resposta não é texto")
        return Completion(
            content=content,
            prompt_tokens=_count(usage.get("prompt_tokens")),
            cache_hit_tokens=_count(usage.get("prompt_cache_hit_tokens")),
            cache_miss_tokens=_count(usage.get("prompt_cache_miss_tokens")),
            completion_tokens=_count(usage.get("completion_tokens")),
            latency_ms=latency,
        )


def _count(value: object) -> int:
    """A token count, 0 when absent or not a non-negative integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

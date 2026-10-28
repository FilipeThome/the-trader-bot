"""One DeepSeek chat completion in JSON mode, over an ``httpx.Client`` of its own.

The client never touches the Binance weight budget or its HTTP session. Every failure becomes
``AdvisorError(kind)``; the message names the kind and the HTTP status only, never the body (a
proxy or the API may echo the request) and never the key. An empty content, which the API
documents as possible in JSON mode, is asked once more before it counts as ``empty``. Every
attempt the API answered is billed, the empty one included: the completion carries the usage of
all of them, and an error after a billed attempt carries that usage (``AdvisorError.usage``).
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
#: Longest one attempt may take, start to last byte. The read timeout bounds the wait for one
#: chunk only, and a long queue sends blank lines before the body, each resetting it: without
#: this bound one call could hold the advisor's single worker (and its budget lock) for minutes.
TOTAL_SECONDS = 45.0
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


@dataclass(frozen=True)
class Completion:
    """The answer's content and what it cost in tokens and time."""

    content: str
    prompt_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    completion_tokens: int
    latency_ms: float

    def after(self, earlier: Completion | None) -> Completion:
        """This answer with the tokens and time of ``earlier`` billed attempts added."""
        if earlier is None:
            return self
        return Completion(
            content=self.content,
            prompt_tokens=earlier.prompt_tokens + self.prompt_tokens,
            cache_hit_tokens=earlier.cache_hit_tokens + self.cache_hit_tokens,
            cache_miss_tokens=earlier.cache_miss_tokens + self.cache_miss_tokens,
            completion_tokens=earlier.completion_tokens + self.completion_tokens,
            latency_ms=earlier.latency_ms + self.latency_ms,
        )


class AdvisorError(Exception):
    """A failed call. ``kind`` is one of ``ErrorKind``; ``str()`` never holds the body or the key.
    ``usage`` holds what the billed attempts cost, when the API reported it: the budget counts it."""

    def __init__(self, kind: ErrorKind, message: str, *, usage: Completion | None = None) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind: ErrorKind = kind
        self.usage = usage


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
        billed: Completion | None = None
        for _attempt in range(2):
            try:
                completion = self._post(body).after(billed)
            except AdvisorError as exc:
                exc.usage = exc.usage.after(billed) if exc.usage is not None else billed
                raise
            if completion.content.strip():
                return completion
            billed = completion
        raise AdvisorError("empty", "resposta sem conteúdo duas vezes", usage=billed)

    def _post(self, body: dict[str, Any]) -> Completion:
        started = time.monotonic()
        try:
            with self._client.stream("POST", "/chat/completions", json=body) as response:
                status = response.status_code
                if status in (401, 402, 403):
                    raise AdvisorError("auth", f"HTTP {status}")
                if status == 429:
                    raise AdvisorError("rate_limited", "HTTP 429")
                if status >= 400:
                    raise AdvisorError("server", f"HTTP {status}")
                raw = _read_within(response, started)
        except httpx.TimeoutException:
            raise AdvisorError("timeout", "sem resposta no prazo") from None
        except httpx.HTTPError as exc:
            # Only the class name: a transport error's text may carry the request.
            raise AdvisorError("server", f"falha de rede ({type(exc).__name__})") from None
        latency = (time.monotonic() - started) * 1000
        try:
            # Long queues send blank lines before the body; json.loads skips the whitespace.
            payload = json.loads(raw)
            content = payload["choices"][0]["message"].get("content") or ""
            usage = payload.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AdvisorError("invalid_json", f"corpo da resposta fora do formato (HTTP {status})") from None
        if not isinstance(usage, dict):
            usage = {}
        completion = Completion(
            content=content if isinstance(content, str) else "",
            prompt_tokens=_count(usage.get("prompt_tokens")),
            cache_hit_tokens=_count(usage.get("prompt_cache_hit_tokens")),
            cache_miss_tokens=_count(usage.get("prompt_cache_miss_tokens")),
            completion_tokens=_count(usage.get("completion_tokens")),
            latency_ms=latency,
        )
        if not isinstance(content, str):
            # Answered, so billed: the error carries the usage.
            raise AdvisorError("invalid_json", "conteúdo da resposta não é texto", usage=completion)
        return completion


def _read_within(response: httpx.Response, started: float) -> bytes:
    """The body, read chunk by chunk until ``TOTAL_SECONDS`` after ``started``; past it, a
    ``timeout``. A chunk still waits up to the read timeout, so one attempt ends by
    ``TOTAL_SECONDS`` plus one read timeout at worst."""
    chunks: list[bytes] = []
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        if time.monotonic() - started > TOTAL_SECONDS:
            raise AdvisorError("timeout", "resposta além do prazo total")
    return b"".join(chunks)


def _count(value: object) -> int:
    """A token count, 0 when absent or not a non-negative integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

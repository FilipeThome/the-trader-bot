"""No secret in a log line, a halt reason, an API error or the ledger (plan wave 1, 1.3)."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest

from bot.executor import REST_PLACED, UnprotectedPosition
from bot.logging_setup import RedactingFormatter, install_redaction, redact
from test_bot_executor_review import _instance, _rest
from test_bot_runtime import _runtime

SIGNED_URL = "GET https://api.binance.com/api/v3/order?symbol=BTCUSDT&timestamp=1700000000000&signature=abc123def"
DEEPSEEK_KEY = "sk-" + "0123456789abcdef" * 2


@pytest.fixture
def root_output(monkeypatch) -> io.StringIO:
    """A root handler like ``logging.basicConfig``'s, alone on the root logger for the test."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    install_redaction()
    return stream


def test_a_logged_exception_never_writes_the_signature_or_the_key(root_output) -> None:
    try:
        raise RuntimeError(f"binance {SIGNED_URL} failed with key {DEEPSEEK_KEY}")
    except RuntimeError:
        logging.getLogger("bot.x").exception("order failed: signature=abc %s", DEEPSEEK_KEY)
    text = root_output.getvalue()
    assert "order failed" in text and "RuntimeError" in text and "Traceback" in text
    assert "abc123def" not in text and "signature=abc " not in text
    assert DEEPSEEK_KEY not in text and "0123456789abcdef" not in text


def test_the_stack_and_every_named_handler_are_redacted(root_output, monkeypatch) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "handlers", [handler])
    install_redaction(["uvicorn.error"])
    install_redaction(["uvicorn.error"])
    assert isinstance(handler.formatter, RedactingFormatter)
    assert not isinstance(handler.formatter.inner, RedactingFormatter)
    logging.getLogger("uvicorn.error").error("request %s", SIGNED_URL, stack_info=True)
    assert "request GET" in stream.getvalue() and "abc123def" not in stream.getvalue()


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("free text with signature=abc123&recvWindow=10000", "abc123"),
        (f"deepseek_api_key={DEEPSEEK_KEY}", DEEPSEEK_KEY),
        ("deepseek_api_key: short-secret", "short-secret"),
        (f"Authorization: Bearer {DEEPSEEK_KEY}", DEEPSEEK_KEY),
        ("{'X-MBX-APIKEY': 'binancekey123'}", "binancekey123"),
        ("bearer eyJhbGciOi.payload", "eyJhbGciOi"),
        ("short key sk-0123456789abcdef", "sk-0123456789abcdef"),
    ],
)
def test_free_text_is_cleaned(raw: str, secret: str) -> None:
    clean = redact(raw)
    assert secret not in clean and "[redacted]" in clean
    assert redact(clean) == clean


def test_ordinary_text_is_left_alone() -> None:
    text = "stop recusado: exchange rejected the order (InvalidOrder); token bucket 3; api_keys: 3"
    assert redact("relógio fora de 1s") == "relógio fora de 1s"
    assert redact(text).startswith("stop recusado: exchange rejected the order (InvalidOrder)")


def test_a_halt_with_the_signed_url_keeps_a_clean_reason(tmp_path, monkeypatch) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("bot.runtime.append_ledger", lambda event: events.append(event))
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.store.set_state("running", True)
    executor.raise_on_sync = UnprotectedPosition(f"BTC/USDT: stop recusado: {SIGNED_URL}")
    result = runtime.tick()
    assert runtime.halted is True
    reason = runtime.store.get_state("halt_reason")
    assert reason.startswith("posição sem proteção: BTC/USDT: stop recusado") and "abc123def" not in reason
    assert "abc123def" not in result["error"]
    assert all("abc123def" not in str(event) for event in events)


def test_a_failed_cycle_ledgers_a_clean_detail(tmp_path, monkeypatch) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("bot.runtime.append_ledger", lambda event: events.append(event))
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.store.set_state("running", True)
    executor.raise_on_sync = RuntimeError(f"binance {SIGNED_URL}")
    assert runtime.tick()["status"] == "error"
    (error,) = [event for event in events if event["kind"] == "tick_error"]
    assert error["error"] == "RuntimeError" and "api.binance.com" in error["detail"]
    assert "abc123def" not in error["detail"]


def test_an_unprotected_resting_buy_keeps_the_stop_response_clean(tmp_path, venue, monkeypatch) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("bot.runtime.append_ledger", lambda event: events.append(event))
    executor, position = _instance(tmp_path)
    assert _rest(executor, position) == REST_PLACED
    # The runtime comes after: its fakes take the market rules away from the instance's stop.
    runtime, _collector, _fake = _runtime(tmp_path, monkeypatch)

    def unprotected(_position_id: int) -> str:
        raise UnprotectedPosition(f"BTC/USDT: stop recusado: {SIGNED_URL}")

    monkeypatch.setattr(executor, "cancel_rest", unprotected)
    runtime.executor = executor
    runtime.store.set_state("running", True)
    result = runtime.stop()
    assert runtime.halted is True and result["error"].startswith("posição sem proteção: BTC/USDT: stop recusado")
    assert "abc123def" not in result["error"] and "abc123def" not in str(result["resting"])
    assert all("abc123def" not in str(event) for event in events)

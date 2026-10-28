"""How an exchange error becomes a journal outcome, and the one resend after -1021 (plan wave 1, 1.1 and 1.2)."""

from __future__ import annotations

import json
from typing import Any, Callable

import ccxt
import pytest

from bot.exchange import sdk
from bot.exchange.execution import orchestrate
from bot.exchange.execution.journal import append_event, begin_submission, new_event
from bot.executor import Executor
from bot.live.halt import trip_halt
from bot.store import Store

AHEAD = 'binance {"code":-1021,"msg":"Timestamp for this request was 1000ms ahead of the server\'s time."}'
OUTSIDE = 'binance {"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'
BAD_SIGNATURE = 'binance {"code":-1022,"msg":"Signature for this request is not valid."}'


class InvalidOrder(Exception):
    """Stands in for ccxt's: a double is classified by the class names along its MRO."""


class OrderImmediatelyFillable(InvalidOrder):
    pass


class Exchange:
    """A ccxt client double: each write pops the next scripted error, then answers."""

    def __init__(self, *errors: BaseException, on_raise: Callable[[], None] | None = None) -> None:
        self.errors = list(errors)
        self.on_raise = on_raise
        self.options: dict[str, Any] = {"timeDifference": 0}
        self.creates: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.clock_reads = 0

    def load_markets(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def load_time_difference(self) -> int:
        self.clock_reads += 1
        self.options["timeDifference"] = 500
        return 500

    def _next(self) -> None:
        if self.errors:
            error = self.errors.pop(0)
            if self.on_raise is not None:
                self.on_raise()
            raise error

    def create_order(self, symbol: str, type_: str, side: str, amount: Any, price: Any, params: dict[str, Any]) -> Any:
        self.creates.append({"symbol": symbol, "type": type_, "amount": amount, "price": price, **params})
        self._next()
        return {"id": "42", "clientOrderId": params.get("newClientOrderId"), "symbol": symbol, "status": "open"}

    def cancel_order(self, order_id: str, symbol: str) -> Any:
        self.cancels.append(order_id)
        self._next()
        return {"id": order_id, "symbol": symbol, "status": "canceled"}


@pytest.fixture
def paper(tmp_path, monkeypatch) -> sdk.BinanceConfig:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    (tmp_path / "binance.json").write_text(
        json.dumps({"profile": "paper", "api_key": "k", "api_secret": "s", "testnet_audit_only": False}),
        encoding="utf-8",
    )
    # An unknown outcome is looked up once by client id; here Binance does not know it yet.
    monkeypatch.setattr(sdk, "get_order", lambda *_args, **_kwargs: {"status": "not_found"})
    return sdk.BinanceConfig(profile="paper", api_key="k", api_secret="s")


def _journaled_stop(config: sdk.BinanceConfig, exchange: Exchange, monkeypatch) -> tuple[dict[str, Any], str | None]:
    """One stop sell through the journal: PREFLIGHT_ALLOWED, SUBMISSION_PENDING, the adapter, the outcome."""
    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: exchange)
    state_key = "order:stop1"
    common = {
        "profile_id": "binance-paper-trade",
        "environment": "paper",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "side": "sell",
        "order_type": "stop_loss_limit",
        "quantity": "0.5",
        "client_order_id": "stop1",
        "intent_id": "stop1",
    }
    append_event(new_event("PREFLIGHT_ALLOWED", state_key, **common))
    begin_submission(new_event("SUBMISSION_PENDING", state_key, **common))
    result = orchestrate._submit(
        config,
        state_key,
        common,
        {
            "symbol": "BTC/USDT",
            "side": "sell",
            "order_type": "stop_loss_limit",
            "quantity": "0.5",
            "stop_price": "98",
            "limit_price": "97.5",
            "time_in_force": "gtc",
            "client_order_id": "stop1",
        },
    )
    return result, orchestrate._latest_event(state_key)


# -- 1.1: a definite reject by the ccxt hierarchy -----------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ccxt.OrderImmediatelyFillable("binance -2010 Stop price would trigger immediately."),
        ccxt.OrderNotFillable("binance"),
        ccxt.RateLimitExceeded("binance 429"),
        ccxt.DDoSProtection("binance 418"),
        ccxt.BadSymbol("binance"),
        ccxt.AuthenticationError(BAD_SIGNATURE),
        OrderImmediatelyFillable("Stop price would trigger immediately."),
    ],
)
def test_a_definite_reject_follows_the_ccxt_hierarchy(error: BaseException) -> None:
    assert sdk._exchange_outcome(error)["status"] == "error"


@pytest.mark.parametrize(
    "error",
    [
        ccxt.RequestTimeout("binance"),
        ccxt.ExchangeNotAvailable("binance"),
        ccxt.InvalidNonce(AHEAD),
        ccxt.NetworkError("binance"),
        RuntimeError("socket closed"),
    ],
)
def test_a_network_or_unknown_error_stays_unknown(error: BaseException) -> None:
    assert sdk._exchange_outcome(error)["status"] == "unknown"


def test_a_stop_that_would_trigger_immediately_is_rejected_and_exits_in_the_same_cycle(
    paper, monkeypatch, tmp_path, venue
) -> None:
    exchange = Exchange(OrderImmediatelyFillable("binance -2010 Stop price would trigger immediately."))
    result, event = _journaled_stop(paper, exchange, monkeypatch)
    assert event == "ORDER_REJECTED" and result["status"] == "error" and result["request_sent"] is True
    # Executor.protect gets that answer for the stop of a fresh entry: it sells at once.
    venue.responses["stop_loss_limit"] = [result]
    executor = Executor(Store(tmp_path / "bot.sqlite"), "binance-paper-trade")
    executor.enter(_ready())
    assert [(call["side"], call["order_type"]) for call in venue.calls] == [
        ("buy", "limit"),
        ("sell", "stop_loss_limit"),
        ("sell", "limit"),
    ]
    assert executor.store.open_positions() == []


def test_a_timed_out_stop_stays_unknown_and_keeps_the_position(paper, monkeypatch, tmp_path, venue) -> None:
    result, event = _journaled_stop(paper, Exchange(ccxt.RequestTimeout("binance")), monkeypatch)
    assert event == "SUBMISSION_UNKNOWN" and result["status"] == "unknown"
    venue.responses["stop_loss_limit"] = [result]
    executor = Executor(Store(tmp_path / "bot.sqlite"), "binance-paper-trade")
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    assert position["stop_client_order_id"] == "stop1"
    assert [call["order_type"] for call in venue.calls] == ["limit", "stop_loss_limit"]


# -- 1.2: one resend after -1021 ------------------------------------------------------------------


@pytest.mark.parametrize(("message", "offset"), [(AHEAD, 1500), (OUTSIDE, 500)])
def test_a_clock_reject_is_resent_once_with_the_same_client_id(paper, monkeypatch, message, offset) -> None:
    exchange = Exchange(ccxt.InvalidNonce(message))
    result, event = _journaled_stop(paper, exchange, monkeypatch)
    assert event == "ORDER_SUBMITTED" and result["status"] == "ok" and result["order_id"] == "42"
    assert [call["newClientOrderId"] for call in exchange.creates] == ["stop1", "stop1"]
    assert exchange.creates[0] == exchange.creates[1]
    # Measured again; the extra second only when Binance said the request was ahead of it.
    assert exchange.clock_reads == 1 and exchange.options["timeDifference"] == offset


def test_a_second_clock_reject_stays_unknown(paper, monkeypatch) -> None:
    exchange = Exchange(ccxt.InvalidNonce(AHEAD), ccxt.InvalidNonce(AHEAD))
    result, event = _journaled_stop(paper, exchange, monkeypatch)
    assert event == "SUBMISSION_UNKNOWN" and result["status"] == "unknown"
    assert len(exchange.creates) == 2


def test_an_invalid_signature_is_not_resent(paper, monkeypatch) -> None:
    exchange = Exchange(ccxt.AuthenticationError(BAD_SIGNATURE))
    result, event = _journaled_stop(paper, exchange, monkeypatch)
    assert event == "ORDER_REJECTED" and result["status"] == "error"
    assert len(exchange.creates) == 1 and exchange.clock_reads == 0


def test_an_invalid_nonce_without_the_code_is_not_resent(paper, monkeypatch) -> None:
    exchange = Exchange(ccxt.InvalidNonce("binance nonce"))
    _result, event = _journaled_stop(paper, exchange, monkeypatch)
    assert event == "SUBMISSION_UNKNOWN" and len(exchange.creates) == 1


def test_a_halt_between_the_attempts_blocks_the_resend(paper, monkeypatch) -> None:
    exchange = Exchange(ccxt.InvalidNonce(AHEAD), on_raise=lambda: trip_halt("file", "teste", broker="binance"))
    result, event = _journaled_stop(paper, exchange, monkeypatch)
    assert len(exchange.creates) == 1
    # The first attempt's answer, as sent: the journal keeps it unresolved until reconciled.
    assert result["status"] == "unknown" and result["request_sent"] is True
    assert event == "SUBMISSION_UNKNOWN"


def test_a_cancel_rejected_by_the_clock_is_resent_once_even_under_a_halt(paper, monkeypatch) -> None:
    exchange = Exchange(ccxt.InvalidNonce(AHEAD), on_raise=lambda: trip_halt("file", "teste", broker="binance"))
    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: exchange)
    with sdk.authorize_exchange_write():
        result = sdk.cancel_order(paper, "7", symbol="BTC/USDT")
    assert result["status"] == "ok" and exchange.cancels == ["7", "7"]


def _ready() -> Any:
    from bot.evaluator import Opportunity

    return Opportunity(
        symbol="BTC/USDT",
        strategy="trend",
        action="Comprar",
        score=0.82,
        net_return=0.02,
        risk_pct=0.02,
        status="pronta",
        reason="rompimento",
        timeframe="1h",
        direction=1,
        cost=0.002,
        gross_return=0.04,
        size_usd=100,
        stop_price=98.0,
        target_price=104.0,
    )

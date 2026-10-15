from __future__ import annotations

from bot.evaluator import Opportunity
from bot.executor import Executor
from bot.store import Store


def _ready() -> Opportunity:
    return Opportunity(
        symbol="BTC/USDT",
        strategy="trend",
        action="Comprar",
        score=0.82,
        net_return=0.02,
        risk_pct=0.01,
        status="pronta",
        reason="rompimento",
        timeframe="4h",
        direction=1,
        cost=0.002,
        gross_return=0.022,
        size_usd=100,
    )


def test_executor_never_calls_the_connector_except_through_the_gate(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    def gate(**kwargs):
        calls.append(kwargs)
        return {"status": "blocked", "decision": "DENY", "reasons": ["mandate"]}

    monkeypatch.setattr("bot.exchange.service.execute_live_order", gate)

    def boom(*_args, **_kwargs):
        raise AssertionError("connector write bypassed the order gate")

    monkeypatch.setattr("bot.exchange.sdk.place_order", boom)
    monkeypatch.setattr("bot.exchange.sdk.cancel_order", boom)
    store = Store(tmp_path / "bot.sqlite")
    Executor(store, "binance-paper-trade").act([_ready()], clock_blocked=False)
    assert len(calls) == 1
    assert calls[0]["broker"] == "binance"
    assert calls[0]["place_kwargs"]["idempotency_key"]


def test_repeated_idempotency_key_is_stable(tmp_path, monkeypatch) -> None:
    keys: list[str] = []

    def gate(**kwargs):
        keys.append(kwargs["place_kwargs"]["idempotency_key"])
        return {"status": "ok", "order_id": "1", "price": "100", "amount": "1", "client_order_id": "abc"}

    monkeypatch.setattr("bot.exchange.service.execute_live_order", gate)
    store = Store(tmp_path / "bot.sqlite")
    executor = Executor(store, "binance-paper-trade")
    executor.act([_ready()], clock_blocked=False)
    executor.act([_ready()], clock_blocked=False)
    assert keys[0] == keys[1]
    assert len(keys[0]) == 32

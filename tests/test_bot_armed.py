from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.collector import Series
from test_bot_runtime import _ready_cycle


def _price(runtime, symbol: str, close: float) -> None:
    """Set the last 1h and 15m close of ``symbol`` in the collector's snapshot."""
    now = datetime.now(timezone.utc)
    index = pd.DatetimeIndex([pd.Timestamp(now).floor("h")])
    frame = pd.DataFrame({"open": [close], "high": [close], "low": [close], "close": [close], "volume": [10.0]}, index=index)
    for interval in ("1h", "15m"):
        runtime.collector.snapshot.candles[(symbol, interval)] = Series(frame, now, "test")


def test_an_armed_pullback_buys_only_once_the_price_reaches_it(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch)
    _price(runtime, "BTC/USDT", 100.0)
    runtime.tick()
    armed = runtime.arm_entry("BTC/USDT", "trend", 95.0, 24)
    assert armed["status"] == "ok" and armed["condition"] == "below"
    assert runtime.arm_entry("BTC/USDT", "trend", 90.0, 24)["error"] == "já existe entrada armada neste par e estratégia"
    runtime.tick()
    assert not any(event.startswith("act:") for event in executor.events)
    _price(runtime, "BTC/USDT", 94.5)
    runtime.tick()
    assert executor.events.count("act:1") == 1
    row = runtime.store.armed_entries()[0]
    assert row["status"] == "fired" and "94.5" in row["reason"]
    runtime.tick()
    assert executor.events.count("act:1") == 1


def test_an_armed_breakout_waits_above_the_price(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch)
    _price(runtime, "BTC/USDT", 100.0)
    runtime.tick()
    assert runtime.arm_entry("BTC/USDT", "trend", 105.0, 24)["condition"] == "above"
    _price(runtime, "BTC/USDT", 104.0)
    runtime.tick()
    assert "act:1" not in executor.events
    _price(runtime, "BTC/USDT", 105.5)
    runtime.tick()
    assert "act:1" in executor.events


def test_a_reached_trigger_waits_while_the_setup_is_not_ready(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch)
    _price(runtime, "BTC/USDT", 100.0)
    runtime.tick()
    runtime.arm_entry("BTC/USDT", "trend", 95.0, 24)
    monkeypatch.setattr("bot.runtime._setup", lambda *_args: None)
    _price(runtime, "BTC/USDT", 94.0)
    runtime.tick()
    row = runtime.store.armed_entries()[0]
    assert row["status"] == "armed" and row["reason"] == "gatilho atingido; o setup não está pronto agora"
    assert "act:1" not in executor.events


def test_armed_entries_expire_and_can_be_cancelled(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch, symbols=["BTC/USDT", "ETH/USDT"])
    for symbol in ("BTC/USDT", "ETH/USDT"):
        _price(runtime, symbol, 100.0)
    runtime.tick()
    first = runtime.arm_entry("BTC/USDT", "trend", 95.0, 1)["id"]
    second = runtime.arm_entry("ETH/USDT", "trend", 95.0, 24)["id"]
    assert runtime.disarm_entry(second)["status"] == "ok"
    assert runtime.disarm_entry(second)["status"] == "error"
    runtime.tick(now=datetime.now(timezone.utc) + timedelta(hours=2))
    status = {row["id"]: row["status"] for row in runtime.store.armed_entries()}
    assert status == {first: "expired", second: "cancelled"}


def test_arming_is_refused_on_bad_input(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    _price(runtime, "BTC/USDT", 100.0)
    assert runtime.arm_entry("ETH/USDT", "trend", 95.0, 24)["error"] == "par fora da lista analisada"
    assert runtime.arm_entry("BTC/USDT", "momentum", 95.0, 24)["error"] == "estratégia desligada"
    assert "prazo" in runtime.arm_entry("BTC/USDT", "trend", 95.0, 500)["error"]
    assert runtime.arm_entry("BTC/USDT", "trend", 100.0, 24)["error"].startswith("gatilho igual")


def test_arming_starts_the_cycle_in_manual_mode(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    runtime.store.set_state("running", False)
    _price(runtime, "BTC/USDT", 100.0)
    assert runtime.arm_entry("BTC/USDT", "trend", 95.0, 24)["cycle_started"] is True
    assert runtime.running

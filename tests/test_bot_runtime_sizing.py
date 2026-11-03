"""Sizing inputs the runtime feeds the risk manager (6.1, 6.4, 6.14): caps, slippage, fees, BTC read."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest

from bot.collector import BookTop, Series
from bot.config import RiskLimits
from bot.exchange import service
from bot.runtime import Runtime
from test_bot_runtime import _ready_cycle, _runtime


def _row(runtime: Runtime) -> dict[str, Any]:
    return runtime.store.opportunities()[0]


def test_rows_carry_the_binding_limit_and_caps(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    runtime.tick()
    row = _row(runtime)
    # 1% risk at a 1% stop is the whole capital; the 25% safety cap (less 10% headroom) binds.
    assert row["size_usd"] == pytest.approx(225.0) and row["size_limit"] == "safety"
    assert row["size_caps"]["risk"] == pytest.approx(1000.0)
    assert row["size_caps"]["safety"] == pytest.approx(225.0)
    assert row["size_caps"]["exposure"] == pytest.approx(600.0)
    assert row["cluster"] == "BTC"
    assert row["plan_target_pct"] == pytest.approx(0.05) and row["timeframe"] == "1h"


def test_testnet_cap_bounds_the_entry_size(tmp_path, monkeypatch) -> None:
    from bot.mandate_io import commit_mandate_for

    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    # The Spot Testnet refuses orders above 100 USDT: binance.json says so, below the mandate's 250.
    (tmp_path / "binance.json").write_text(json.dumps({"testnet_max_order_notional": {"USDT": "100"}}), encoding="utf-8")
    commit_mandate_for(runtime.config)
    assert service.order_notional_cap("BTC/USDT", "binance-paper-trade") == 100
    runtime.tick()
    row = _row(runtime)
    assert row["size_usd"] == pytest.approx(90.0) and row["size_limit"] == "venue"
    assert row["size_caps"]["venue"] == pytest.approx(90.0) and row["size_caps"]["safety"] == pytest.approx(225.0)
    assert "act:1" in executor.events


def test_mandate_cap_tightens_until_reconfirmed(tmp_path, monkeypatch) -> None:
    from bot.mandate_io import commit_mandate_for

    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    # A mandate signed under the old 2% cap: the new 25% default waits for the operator.
    commit_mandate_for(runtime.config.model_copy(update={"limits": RiskLimits(max_order_notional_pct=0.02)}))
    runtime.tick()
    assert _row(runtime)["size_usd"] == pytest.approx(18.0) and _row(runtime)["size_limit"] == "safety"
    commit_mandate_for(runtime.config)
    runtime.tick()
    assert _row(runtime)["size_usd"] == pytest.approx(225.0)


def test_slippage_estimate_uses_the_planned_notional(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    runtime.collector.snapshot.books["BTC/USDT"] = BookTop(100.0, 100.0, 100.0, 100.0, now)  # type: ignore[attr-defined]
    runtime.tick()
    # The planned order is 225 (not a fixed 1000) against 20 000 USDT at the top of the book.
    row = _row(runtime)
    assert row["slippage_est"] == pytest.approx(225 / 20_000)
    assert row["cost"] == pytest.approx(2 * 0.001 + 225 / 20_000)


@pytest.mark.parametrize(("account", "taker"), [(0.002, 0.002), (0.0005, 0.001), (None, 0.001)])
def test_scoring_uses_the_account_taker_but_never_below_spot(tmp_path, monkeypatch, account: float | None, taker: float) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    fees = None if account is None else (0.0, account)
    monkeypatch.setattr(service, "trading_fees", lambda symbol, profile_id: fees)
    runtime.tick()
    assert _row(runtime)["cost"] == pytest.approx(2 * taker + 0.0005)


def test_collect_reads_btc_and_warms_the_account_fees(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, ["ETH/USDT"])
    asked: list[list[str]] = []
    warmed: list[str] = []
    inner = collector.refresh
    collector.refresh = lambda symbols: (asked.append(list(symbols)), inner(symbols))[1]  # type: ignore[method-assign]
    monkeypatch.setattr(service, "trading_fees", lambda symbol, profile_id: warmed.append(symbol))
    runtime.tick()
    assert asked == [["ETH/USDT", "BTC/USDT"]] and warmed == ["ETH/USDT", "BTC/USDT"]
    # Under API weight pressure no fee is read.
    monkeypatch.setattr("bot.cycle.market.weight_pressure", lambda: True)
    runtime.tick()
    assert warmed == ["ETH/USDT", "BTC/USDT"]


def test_a_volatile_pair_is_sized_by_its_daily_atr(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    index = pd.date_range(end=pd.Timestamp(now).floor("4h") - pd.Timedelta(hours=4), periods=120, freq="4h")
    # Every day spans 95 to 105 around a flat 100: a 10% daily ATR.
    frame = pd.DataFrame({"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0, "volume": 1.0}, index=index)
    runtime.collector.snapshot.candles[("BTC/USDT", "4h")] = Series(frame, now, "test")  # type: ignore[attr-defined]
    runtime.tick()
    row = _row(runtime)
    # One daily ATR of the order costs at most 1% of the capital: 1000 x 1% / 10%.
    assert row["size_usd"] == pytest.approx(100.0) and row["size_limit"] == "volatility"


def test_a_hot_week_halves_the_size(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    # The last seven days of 4h returns run far above the whole history's volatility.
    calm = [100.0 * (1.001 if index % 2 else 1.0) for index in range(250)]
    hot = [100.0 * (1.02 if index % 2 else 1.0) for index in range(42)]
    closes = calm + hot
    index = pd.date_range(end=pd.Timestamp(now).floor("4h") - pd.Timedelta(hours=4), periods=len(closes), freq="4h")
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
    runtime.collector.snapshot.candles[("BTC/USDT", "4h")] = Series(frame, now, "test")  # type: ignore[attr-defined]
    runtime.tick()
    row = _row(runtime)
    assert row["size_limit"] == "throttle" and row["size_usd"] == pytest.approx(112.5)

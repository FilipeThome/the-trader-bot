"""Live guards of the runtime (6.5, 6.6, 6.9, 6.12, 6.13): validation per timeframe and pair set, Monte Carlo,
pair demotion and slippage, the position's exit frame and hysteresis, signal age and the macro calendar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from bot.collector import MarketSnapshot, Series
from bot.config import BotConfig
from bot.cycle import exits as exits_module
from bot.cycle.exits import _exit_reason
from bot.cycle.scoring import _setup
from bot.decay import PAIR_DEMOTED, SLIPPAGE_BLOCKED
from bot.evaluator import Opportunity
from bot.runtime import Runtime
from bot.store import Store
from bot.strategies import IntradayMomentum, validation_params
from bot.strategies.base import Setup
from test_bot_runtime import FakeCollector, _ready_cycle, _runtime, _snapshot
from test_bot_runtime import _setup as _trend_setup

MOMENT = datetime(2026, 10, 15, 12, 5, tzinfo=timezone.utc)
_FREQ = {"15m": "15min", "1h": "1h", "4h": "4h"}


def _series(closes: list[float], interval: str, moment: datetime = MOMENT) -> Series:
    """``closes`` as closed bars of ``interval`` ending right before the bar forming at ``moment``, plus that bar."""
    width = pd.Timedelta(_FREQ[interval])
    end = pd.Timestamp(moment).floor(width)
    values = [*closes, closes[-1]]
    index = pd.date_range(end=end, periods=len(values), freq=width)
    frame = pd.DataFrame({"open": values, "high": values, "low": values, "close": values, "volume": 1.0}, index=index)
    return Series(frame, moment, "test")


RISING = [100 * 1.003**i for i in range(260)]
TURNED = [100 * 1.003**i for i in range(200)] + [100 * 1.003**199 * 0.99**i for i in range(1, 61)]
FALLING = [100 * 0.997**i for i in range(260)]


def _ledger(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("bot.runtime.append_ledger", lambda payload: events.append(dict(payload)))
    return events


def _position(strategy: str, timeframe: str | None, hysteresis: int | None = None) -> dict[str, Any]:
    return {"symbol": "BTC/USDT", "strategy": strategy, "timeframe": timeframe, "exit_hysteresis": hysteresis, "target_price": None}


# -- exit frame and hysteresis (6.6, 6.9) ---------------------------------------


def test_a_4h_trend_position_exits_on_4h_bars() -> None:
    snapshot = MarketSnapshot(candles={("BTC/USDT", "4h"): _series(TURNED, "4h"), ("BTC/USDT", "1h"): _series(RISING, "1h")})
    assert _exit_reason(_position("trend", "4h"), snapshot, MOMENT, use_target=False) is not None
    assert _exit_reason(_position("trend", "1h"), snapshot, MOMENT, use_target=False) is None


def test_a_1h_mean_reversion_position_exits_on_the_1h_ema() -> None:
    snapshot = MarketSnapshot(candles={("BTC/USDT", "1h"): _series(RISING, "1h"), ("BTC/USDT", "15m"): _series(FALLING, "15m")})
    assert _exit_reason(_position("mean_reversion", "1h"), snapshot, MOMENT, use_target=False) == "preço voltou à média"
    assert _exit_reason(_position("mean_reversion", "15m"), snapshot, MOMENT, use_target=False) is None


def test_a_legacy_row_without_timeframe_uses_the_old_frames() -> None:
    # Old rows: trend exits on 1h, momentum on 4h, mean reversion on 15m.
    snapshot = MarketSnapshot(
        candles={
            ("BTC/USDT", "1h"): _series(TURNED, "1h"),
            ("BTC/USDT", "4h"): _series(RISING, "4h"),
            ("BTC/USDT", "15m"): _series(RISING, "15m"),
        }
    )
    assert _exit_reason(_position("trend", None), snapshot, MOMENT, use_target=False) is not None
    assert _exit_reason(_position("momentum", None), snapshot, MOMENT, use_target=False) is None
    assert _exit_reason(_position("mean_reversion", None), snapshot, MOMENT, use_target=False) == "preço voltou à média"


def test_position_records_hysteresis_and_legacy_rows_exit_without_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    plain = Runtime(BotConfig(exit_hysteresis=False), Store(tmp_path / "a.sqlite"), FakeCollector(_snapshot(["BTC/USDT"])))  # type: ignore[arg-type]
    default = Runtime(BotConfig(), Store(tmp_path / "b.sqlite"), FakeCollector(_snapshot(["BTC/USDT"])))  # type: ignore[arg-type]
    # The executor writes the flag on every strategy position and logs through the runtime's ledger.
    assert plain.executor.exit_hysteresis is False and default.executor.exit_hysteresis is True
    assert default.executor.ledger == default._ledger
    seen: list[bool] = []
    monkeypatch.setattr(exits_module._TREND, "exit_reason", lambda _frame, _side, *, hysteresis=True: seen.append(hysteresis))
    snapshot = MarketSnapshot(candles={("BTC/USDT", "1h"): _series(RISING, "1h")})
    for flag in (1, 0, None):
        _exit_reason(_position("trend", "1h", flag), snapshot, MOMENT, use_target=False)
    assert seen == [True, False, False]


# -- validation per timeframe, pair set and Monte Carlo (6.5) ------------------


def _params(timeframes: dict[str, dict[str, Any]], symbols: list[str] | None = None) -> dict[str, Any]:
    return validation_params(
        "trend", "trailing", timeframes=timeframes, symbols=symbols or ["BTC/USDT"], market_filter="off", hysteresis=True
    )


def _scored(tmp_path, monkeypatch, params: dict[str, Any], **overrides: Any) -> tuple[Runtime, Any]:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, **{"entry_mode": "auto", "min_win_rate": 0.70, **overrides})
    runtime.store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": params})
    return runtime, executor


PASSED = {"passed": True, "wins": 30, "trades": 40, "mc_p95_dd_r": 0.0}


def test_a_setup_on_an_unvalidated_timeframe_is_signal_only(tmp_path, monkeypatch) -> None:
    runtime, executor = _scored(tmp_path, monkeypatch, _params({"4h": PASSED, "1h": {"passed": False, "wins": 30, "trades": 40}}))
    asked: list[tuple[str, ...]] = []
    monkeypatch.setattr("bot.cycle.scoring._setup", lambda *args: (asked.append(args[5]), _trend_setup(*args))[1])
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and "timeframe 1h não passou" in row["reason"]
    assert "act:0" in executor.events
    # The validated timeframe is tried first.
    assert asked == [("4h",)]


def test_setup_tries_validated_timeframes_first() -> None:
    class Both:
        timeframes = ("1h", "4h")

        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> Setup:
            return Setup("trend", symbol, timeframe, "Comprar", 1, 0.05, 0.01, 0.01, 0.05, "t", "trend_up")

    snapshot = MarketSnapshot(candles={("BTC/USDT", "1h"): _series(RISING, "1h"), ("BTC/USDT", "4h"): _series(RISING, "4h")})
    assert _setup(Both(), "BTC/USDT", snapshot, 0.1, MOMENT).timeframe == "1h"  # type: ignore[arg-type, union-attr]
    assert _setup(Both(), "BTC/USDT", snapshot, 0.1, MOMENT, ("4h",)).timeframe == "4h"  # type: ignore[arg-type, union-attr]


def test_a_pair_outside_the_validated_set_is_signal_only(tmp_path, monkeypatch) -> None:
    runtime, executor = _scored(tmp_path, monkeypatch, _params({"1h": PASSED}), symbols=["BTC/USDT", "ETH/USDT"])
    runtime.collector.snapshot = _snapshot(["BTC/USDT", "ETH/USDT"])  # type: ignore[attr-defined]
    runtime.tick()
    rows = {row["symbol"]: row for row in runtime.store.opportunities()}
    assert rows["ETH/USDT"]["status"] == "bloqueada" and "fora do conjunto de pares validado" in rows["ETH/USDT"]["reason"]
    assert rows["BTC/USDT"]["status"] == "pronta" and "act:1" in executor.events


@pytest.mark.parametrize(("mc", "blocked"), [(12.0, True), (10.0, True), (5.0, False), (None, True), (float("nan"), True)])
def test_mc_drawdown_over_the_limit_makes_rows_signal_only(tmp_path, monkeypatch, mc: float | None, blocked: bool) -> None:
    # 12 R of p95 drawdown at 1% risk per trade is 12%, past the 10% limit.
    runtime, executor = _scored(tmp_path, monkeypatch, _params({"1h": {**PASSED, "mc_p95_dd_r": mc}}))
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert (row["status"] == "bloqueada") is blocked
    if blocked and mc is not None and mc == mc:
        assert "Monte Carlo: drawdown p95 de" in row["reason"] and "passa do limite de 10%" in row["reason"]
    elif blocked:
        # A record without a readable Monte Carlo drawdown never trades (review F4).
        assert "Monte Carlo: drawdown p95 ausente ou ilegível" in row["reason"]
    assert ("act:0" if blocked else "act:1") in executor.events


# -- pair demotion and slippage (6.4, 6.12) ------------------------------------


def _closed_trade(store: Store, symbol: str, strategy: str, *, r: float, slippage: float | None = None) -> None:
    position_id = store.open_pending(symbol, strategy, exit_policy="trailing")
    assert position_id is not None
    store.update_position(position_id, status="open", qty=1.0, avg_price=100.0, stop_price=90.0)
    store.close_position(position_id, exit_price=100.0, realized_pnl=0.0, fees=0.0)
    store.update_position(position_id, r_multiple=r, entry_slippage=slippage, exit_slippage=slippage)


def test_a_demoted_pair_is_signal_only_and_logs_the_change_once(tmp_path, monkeypatch) -> None:
    events = _ledger(monkeypatch)
    runtime, executor = _scored(tmp_path, monkeypatch, _params({"1h": PASSED}))
    for _ in range(10):
        _closed_trade(runtime.store, "BTC/USDT", "trend", r=-0.5)
    runtime.tick()
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and row["reason"] == PAIR_DEMOTED and "act:0" in executor.events
    blocked = [event for event in events if event["kind"] == "pair_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["symbol"] == "BTC/USDT" and blocked[0]["strategy"] == "trend" and blocked[0]["reason"] == "expectancy"
    assert blocked[0]["blocked"] is True


def test_a_pair_over_its_slippage_budget_takes_no_entry(tmp_path, monkeypatch) -> None:
    events = _ledger(monkeypatch)
    runtime, executor = _scored(tmp_path, monkeypatch, _params({"1h": PASSED}))
    # Any strategy's trades count: 0.4% round trip against the 0.3% budget.
    for _ in range(10):
        _closed_trade(runtime.store, "BTC/USDT", "mean_reversion", r=1.0, slippage=0.002)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and row["reason"] == SLIPPAGE_BLOCKED and "act:0" in executor.events
    blocked = [event for event in events if event["kind"] == "pair_blocked"]
    assert [(event["symbol"], event["strategy"], event["reason"]) for event in blocked] == [("BTC/USDT", None, "slippage")]


def test_review_strategy_runs_in_the_daily_decay_and_records_the_demotion(tmp_path, monkeypatch) -> None:
    runtime, _executor = _scored(tmp_path, monkeypatch, _params({"1h": PASSED}))
    reasons = {"trend": "média de R dos últimos 20 trades negativa (-0.30 R)"}
    monkeypatch.setattr("bot.runtime.review_strategy", lambda _store, strategy_id, **_kwargs: reasons.get(strategy_id))
    runtime.tick()
    demoted = [row for row in runtime.store.decisions() if row.get("decision") == "rebaixada"]
    assert [(row["strategy"], row["reason"]) for row in demoted] == [("trend", reasons["trend"])]


# -- signal age and macro calendar (6.13) --------------------------------------


def _aged(tmp_path, monkeypatch, entry_mode: str, live: float = 100.0, moment: datetime = MOMENT) -> tuple[Runtime, Any]:
    """Closed 1h bars at 100 and the bar forming at ``moment`` trading at ``live`` (stop 1%, so 1 R = 1.0)."""
    runtime, executor = _scored(tmp_path, monkeypatch, _params({"1h": PASSED}), entry_mode=entry_mode)
    series = _series([100.0] * 80, "1h", moment)
    series.frame.iloc[-1, series.frame.columns.get_loc("close")] = live
    runtime.collector.snapshot = MarketSnapshot(candles={("BTC/USDT", "1h"): series})  # type: ignore[attr-defined]
    return runtime, executor


def test_a_fresh_signal_enters_automatically(tmp_path, monkeypatch) -> None:
    runtime, executor = _aged(tmp_path, monkeypatch, "auto", live=103.0, moment=MOMENT.replace(minute=1))
    runtime.tick(now=MOMENT.replace(minute=1))
    row = runtime.store.opportunities()[0]
    # 60 s after the close the price is not judged, even 3 R above the signal.
    assert row["signal_age_seconds"] == pytest.approx(60.0) and row["entry_drift_r"] == pytest.approx(3.0)
    assert executor.events[-1] == "act:1"


def test_a_late_signal_enters_automatically_while_the_price_stays_near_it(tmp_path, monkeypatch) -> None:
    # The 11:00 bar closed at 12:00: at 12:05 its signal is 300 s old, but the price is still 0.3 R above it.
    runtime, executor = _aged(tmp_path, monkeypatch, "auto", live=100.3)
    runtime.tick(now=MOMENT)
    row = runtime.store.opportunities()[0]
    assert row["signal_age_seconds"] == pytest.approx(300.0) and row["entry_drift_r"] == pytest.approx(0.3)
    assert "atraso" not in row["reason"]
    assert executor.events[-1] == "act:1"


@pytest.mark.parametrize(
    ("live", "why"),
    [(100.6, "preço 0.60 R acima do sinal"), (99.0, "preço 1.00 R abaixo do sinal, além do stop")],
)
def test_a_late_signal_the_price_left_is_not_entered_automatically(tmp_path, monkeypatch, live, why) -> None:
    runtime, executor = _aged(tmp_path, monkeypatch, "auto", live=live)
    runtime.tick(now=MOMENT)
    row = runtime.store.opportunities()[0]
    assert row["status"] == "pronta"
    assert f"sinal com 300 s de atraso e {why}: sem entrada automática" in row["reason"]
    assert executor.events[-1] == "act:0"


def test_a_late_signal_without_a_live_price_is_not_entered_automatically(tmp_path, monkeypatch) -> None:
    runtime, executor = _aged(tmp_path, monkeypatch, "auto")
    monkeypatch.setattr("bot.cycle.market._last_price", lambda *_args: None)
    runtime.tick(now=MOMENT)
    row = runtime.store.opportunities()[0]
    assert row["entry_drift_r"] is None
    assert "atraso e preço atual sem leitura" in row["reason"]
    assert executor.events[-1] == "act:0"


def test_manual_entry_shows_the_age_but_is_allowed(tmp_path, monkeypatch) -> None:
    runtime, executor = _aged(tmp_path, monkeypatch, "manual")
    runtime.tick(now=MOMENT)
    row = runtime.store.opportunities()[0]
    assert row["status"] == "pronta" and row["signal_age_seconds"] == pytest.approx(300.0)
    manual = Opportunity(
        symbol="BTC/USDT", strategy="trend", action="Comprar", score=1, net_return=0.02, risk_pct=0.01, status="pronta",
        reason="r", timeframe="1h", direction=1, cost=0.002, gross_return=0.03, size_usd=50, signal_age_seconds=300.0,
    )
    assert runtime.execute_manual(manual)["results"][0]["decision"] == "operar"
    assert "act:1" in executor.events


def _both(strategy: Any, symbol: str, *_args: Any) -> Setup:
    if isinstance(strategy, IntradayMomentum):
        return Setup(
            "intraday_momentum", symbol, "15m", "Comprar", 1.0, 0.015, 0.004, 0.01, 0.015, "rompimento", "trend_up", confidence=0.9
        )
    return _trend_setup(strategy, symbol)


def _macro(tmp_path, monkeypatch, events: list[str]) -> Runtime:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, macro_events_utc=events)
    runtime.config.strategies.update(trend=True, intraday_momentum=True)
    runtime.collector.snapshot = MarketSnapshot(  # type: ignore[attr-defined]
        candles={("BTC/USDT", "1h"): _series([100.0] * 80, "1h"), ("BTC/USDT", "15m"): _series([100.0] * 80, "15m")}
    )
    monkeypatch.setattr("bot.cycle.scoring._setup", _both)
    monkeypatch.setattr("bot.cycle.scoring.in_quiet_hours", lambda *_a: False)
    return runtime


def test_macro_window_blocks_day_entries_only(tmp_path, monkeypatch) -> None:
    runtime = _macro(tmp_path, monkeypatch, [(MOMENT + timedelta(minutes=10)).isoformat()])
    runtime.tick(now=MOMENT)
    rows = {row["strategy"]: row for row in runtime.store.opportunities()}
    assert rows["intraday_momentum"]["status"] == "bloqueada"
    assert rows["intraday_momentum"]["reason"] == "pausa de evento macro até 12:30 UTC"
    assert "macro" not in rows["trend"]["reason"]
    # Outside the 15-minute window the day row is judged as usual.
    runtime.tick(now=MOMENT - timedelta(minutes=10))
    rows = {row["strategy"]: row for row in runtime.store.opportunities()}
    assert "macro" not in rows["intraday_momentum"]["reason"]


def test_macro_events_empty_by_default(tmp_path, monkeypatch) -> None:
    assert BotConfig().macro_events_utc == []
    runtime = _macro(tmp_path, monkeypatch, [])
    runtime.tick(now=MOMENT)
    assert all("macro" not in row["reason"] for row in runtime.store.opportunities())

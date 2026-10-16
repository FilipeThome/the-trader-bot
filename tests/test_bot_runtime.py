from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from bot.collector import MarketSnapshot, Series
from bot.config import BotConfig, RiskLimits
from bot.exchange import service
from bot.executor import UnprotectedPosition
from bot.live.halt import halt_flag_set
from bot.paths import get_runtime_root, pin_mode_root
from bot.strategies import STRATEGIES
from bot.runtime import Runtime, mode_root
from bot.store import Store
from bot.strategies.base import Setup


def _snapshot(symbols: list[str]) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    index = pd.DatetimeIndex([pd.Timestamp(now).floor("h")])
    frame = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [10.0]}, index=index)
    return MarketSnapshot(candles={(symbol, "1h"): Series(frame, now, "test") for symbol in symbols})


class FakeCollector:
    def __init__(self, snapshot: MarketSnapshot) -> None:
        self.snapshot = snapshot
        self.clock_syncs = 0

    def sync_clock(self) -> bool:
        self.clock_syncs += 1
        return True

    def refresh(self, symbols: list[str]) -> MarketSnapshot:
        return self.snapshot


class FakeExecutor:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.raise_on_sync: Exception | None = None
        self.raise_on_flatten: Exception | None = None
        self.decision = "operar"

    def sync_positions(self) -> None:
        self.events.append("sync")
        if self.raise_on_sync is not None:
            raise self.raise_on_sync

    def act(
        self, opportunities: list[Any], *, clock_blocked: bool, manual: bool = False, allow_test: bool = False
    ) -> list[dict[str, Any]]:
        self.events.append(f"act:{len(opportunities)}" + (":test" if allow_test else ""))
        return [{"decision": self.decision, "reason": "ok"} for _ in opportunities]

    def trail(self, position_id: int, last_price: Any, *, reserve: int) -> str:
        self.events.append(f"trail:{position_id}:{reserve}")
        return "movido"

    def flatten(self) -> list[dict[str, Any]]:
        self.events.append(f"flatten:halted={halt_flag_set('binance')}")
        if self.raise_on_flatten is not None:
            raise self.raise_on_flatten
        return [{"status": "ok"}]

    def exit_position(self, position_id: int, reason: str) -> dict[str, Any]:
        self.events.append(f"exit:{position_id}")
        return {"status": "ok"}


def _runtime(
    tmp_path, monkeypatch, symbols: list[str] | None = None, **overrides: Any
) -> tuple[Runtime, FakeCollector, FakeExecutor]:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    config = BotConfig(
        symbols=symbols or ["BTC/USDT"],
        strategies={"trend": True, "mean_reversion": False, "funding_carry": False},
        **overrides,
    )
    collector = FakeCollector(_snapshot(config.symbols))
    monkeypatch.setattr(service, "free_balance", lambda asset, profile_id: Decimal("100000"))
    monkeypatch.setattr(service, "market_rules", lambda symbol, profile_id: None)
    runtime = Runtime(config, Store(tmp_path / "bot.sqlite"), collector)  # type: ignore[arg-type]
    executor = FakeExecutor()
    runtime.executor = executor  # type: ignore[assignment]
    return runtime, collector, executor


def _setup(strategy: Any, symbol: str, *_args: Any) -> Setup:
    return Setup(
        strategy_id="trend",
        symbol=symbol,
        timeframe="1h",
        action="Comprar",
        direction=1,
        expected_return=0.05,
        sigma=0.01,
        stop_pct=0.01,
        target_pct=0.05,
        reason="teste",
        regime="trend_up",
        confidence=1,
    )


def test_halt_flattens_before_the_flag_and_the_flag_sticks(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.store.set_state("running", True)
    result = runtime.halt("Parar tudo", flatten=True, by="frontend")
    assert executor.events == ["flatten:halted=False"]
    assert result["halted"] is True and runtime.halted is True
    assert runtime.start()["status"] == "error"


def test_the_cycle_syncs_the_clock_and_settles_positions_before_entries(tmp_path, monkeypatch) -> None:
    runtime, collector, executor = _runtime(tmp_path, monkeypatch, entry_mode="auto")
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "ok"
    assert collector.clock_syncs == 1
    assert executor.events[0] == "sync"
    assert executor.events[-1].startswith("act:")


def test_a_drawdown_halts_once_per_cycle(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    monkeypatch.setattr("bot.runtime._setup", _setup)
    runtime.store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": {"exit_policy": "trailing"}})
    runtime.store.record_equity(1000.0, "2026-01-01T00:00:00+00:00")
    runtime.store.record_day("2026-01-01", net_pnl=-200.0)
    halts: list[str] = []
    monkeypatch.setattr(runtime, "halt", lambda reason, **_kwargs: halts.append(reason))
    runtime.store.set_state("running", True)
    runtime.tick()
    assert len(halts) == 1 and "drawdown" in halts[0]


def test_a_real_daily_loss_pauses_until_the_next_utc_day(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("bot.runtime._setup", _setup)
    runtime.store.save_strategy({"strategy_id": "trend", "signal_only": False, "params": {"exit_policy": "trailing"}})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runtime.store.record_day(today, net_pnl=-40.0)
    runtime.store.set_state("running", True)
    runtime.tick()
    assert runtime.store.get_state("running") is False
    assert runtime.store.get_state("paused_until")


def test_three_failed_cycles_halt(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.store.set_state("running", True)
    executor.raise_on_sync = RuntimeError("journal")
    for _ in range(3):
        assert runtime.tick()["status"] == "error"
    assert runtime.halted is True


def test_an_unprotected_position_halts_at_once_without_flatten(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.config = runtime.config.model_copy(update={"flatten_on_halt": True})
    runtime.store.set_state("running", True)
    executor.raise_on_sync = UnprotectedPosition("BTC/USDT: stop recusado")
    runtime.tick()
    assert runtime.halted is True
    assert not any(event.startswith("flatten") for event in executor.events)


def test_decay_runs_once_per_utc_day(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr("bot.runtime.apply_decay", lambda _store, strategy_id, _returns: calls.append(strategy_id) or False)
    runtime.tick()
    runtime.tick()
    assert sorted(calls) == sorted(STRATEGIES)


def test_the_mode_pin_is_seen_by_another_thread(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    pin_mode_root(mode_root(BotConfig(mode="production")))
    seen: list[Any] = []
    thread = threading.Thread(target=lambda: seen.append(get_runtime_root()))
    thread.start()
    thread.join()
    assert seen == [tmp_path / "production"]


def test_credentials_stay_at_the_base_root_under_a_mode_pin(tmp_path, monkeypatch) -> None:
    from bot.exchange import sdk

    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    pin_mode_root(mode_root(BotConfig()))
    assert sdk.config_path() == tmp_path / "binance.json"
    assert get_runtime_root() == tmp_path / "testnet"


@pytest.mark.parametrize("raises", [True, False])
def test_the_process_loop_logs_and_halts_after_three_failures(monkeypatch, raises: bool) -> None:
    import bot.__main__ as entry

    halts: list[str] = []

    class Stub:
        running = False

        def halt(self, reason: str) -> None:
            halts.append(reason)

    def load() -> BotConfig:
        if raises:
            raise ValueError("bot.json")
        return BotConfig()

    monkeypatch.setattr(entry.BotConfig, "load", staticmethod(load))
    monkeypatch.setattr(entry, "_runtime", lambda: Stub())
    failures = 0
    for _ in range(3):
        failures, interval = entry.run_once(failures)
        assert interval >= 5
    assert halts == (["3 falhas seguidas no laço do processo"] if raises else [])
    assert failures == (3 if raises else 0)


def test_the_halt_flag_is_written_even_when_flatten_raises(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    executor.raise_on_flatten = RuntimeError("BinanceConfigError")
    result = runtime.halt("Parar tudo", flatten=True)
    assert result["flatten"][0]["status"] == "error"
    assert runtime.halted is True


def test_a_manual_close_while_halted_is_refused(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.halt("Parar tudo", flatten=False)
    assert runtime.close_position(1)["status"] == "blocked"
    assert executor.events == []


def _ready_cycle(tmp_path, monkeypatch, **overrides: Any) -> tuple[Runtime, FakeExecutor]:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, **overrides)
    monkeypatch.setattr("bot.runtime._setup", _setup)
    runtime.store.save_strategy(
        {"strategy_id": "trend", "signal_only": False, "params": {"wins": 30, "trades": 40, "exit_policy": "trailing"}}
    )
    runtime.store.set_state("running", True)
    return runtime, executor


def test_manual_mode_never_enters_by_itself(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch)
    runtime.tick()
    assert runtime.store.opportunities()[0]["status"] == "pronta"
    assert not any(event.startswith("act:") for event in executor.events)


def test_auto_mode_enters_only_above_the_hit_rate(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["win_rate"] == pytest.approx(31 / 42) and row["win_rate_n"] == 40
    assert "act:1" in executor.events
    strict, strict_executor = _ready_cycle(tmp_path / "strict", monkeypatch, entry_mode="auto", min_win_rate=0.80)
    strict.tick()
    assert "act:0" in strict_executor.events


def test_a_backtest_with_another_exit_policy_gives_no_estimate(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", trailing_stop=False)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["win_rate"] is None
    assert "act:0" in executor.events


def _manual_opportunity() -> Any:
    from bot.evaluator import Opportunity

    return Opportunity(
        symbol="BTC/USDT", strategy="trend", action="Comprar", score=1, net_return=0.02, risk_pct=0.01,
        status="pronta", reason="r", timeframe="1h", direction=1, cost=0.002, gross_return=0.03, size_usd=50,
    )


@pytest.mark.parametrize(("decision", "mode", "starts"), [
    ("operar", "manual", True),
    ("pendente", "manual", True),
    ("bloqueado", "manual", False),
    ("operar", "auto", False),
])
def test_a_manual_trade_starts_the_cycle_only_in_manual_mode_after_an_entry(
    tmp_path, monkeypatch, decision: str, mode: str, starts: bool
) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, entry_mode=mode)
    executor.decision = decision
    result = runtime.execute_manual(_manual_opportunity())
    assert result["cycle_started"] is starts and runtime.running is starts


def test_a_halt_keeps_the_cycle_off_after_a_manual_trade(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch)
    runtime.halt("teste", flatten=False)
    assert runtime.execute_manual(_manual_opportunity())["cycle_started"] is False


def test_trailing_skips_the_fixed_target_and_reserves_one_order_per_position(tmp_path, monkeypatch) -> None:
    from bot.runtime import _exit_reason

    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    for symbol in ("BTC/USDT", "ETH/USDT"):
        position_id = runtime.store.open_pending(symbol, "mean_reversion")
        assert position_id is not None
        runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=90.0, target_price=95.0, stop_price=85.0)
    runtime.collector.snapshot = _snapshot(["BTC/USDT", "ETH/USDT"])  # type: ignore[attr-defined]
    runtime.collector.snapshot.candles[("BTC/USDT", "15m")] = runtime.collector.snapshot.candles[("BTC/USDT", "1h")]
    runtime._trail(runtime.collector.snapshot)
    assert executor.events == ["trail:1:3"]
    position = runtime.store.open_positions()[0]
    assert _exit_reason(position, runtime.collector.snapshot, datetime.now(timezone.utc), use_target=True) == "alvo atingido"
    assert _exit_reason(position, runtime.collector.snapshot, datetime.now(timezone.utc), use_target=False) is None



def test_a_strategy_validated_under_another_exit_rule_is_signal_only(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto")
    runtime.store.save_strategy(
        {"strategy_id": "trend", "signal_only": False, "params": {"wins": 30, "trades": 40, "exit_policy": "target"}}
    )
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["status"] == "bloqueada" and "somente sinal" in row["reason"]
    assert "act:0" in executor.events



def test_each_position_keeps_the_exit_rule_it_opened_under(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, trailing_stop=False)
    snapshot = _snapshot(["BTC/USDT", "ETH/USDT"])
    for symbol in ("BTC/USDT", "ETH/USDT"):
        snapshot.candles[(symbol, "15m")] = snapshot.candles[(symbol, "1h")]
    trailing_id = runtime.store.open_pending("BTC/USDT", "trend", exit_policy="trailing")
    target_id = runtime.store.open_pending("ETH/USDT", "trend", exit_policy="target")
    assert trailing_id is not None and target_id is not None
    for position_id in (trailing_id, target_id):
        runtime.store.update_position(position_id, status="open", qty=1.0, avg_price=90.0, target_price=95.0, stop_price=85.0)
    runtime._trail(snapshot)
    assert executor.events == [f"trail:{trailing_id}:3"]
    runtime._exits(snapshot, datetime.now(timezone.utc))
    assert executor.events[1:] == [f"exit:{target_id}"]



def _weak_setup(strategy: Any, symbol: str, *_args: Any) -> Setup:
    # Valid buy, but its score stays below the 0.3 threshold.
    return Setup("trend", symbol, "1h", "Comprar", 1, 0.004, 0.05, 0.002, 0.004, "fraco", "trend_up", confidence=0.5)


@pytest.mark.parametrize(("mode", "flag", "expected"), [("testnet", True, True), ("testnet", False, False), ("production", True, False)])
def test_test_rows_exist_only_in_the_spot_testnet_test_mode(tmp_path, monkeypatch, mode: str, flag: bool, expected: bool) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, mode=mode, testnet_test_mode=flag, entry_mode="auto")
    monkeypatch.setattr("bot.runtime._setup", _weak_setup)
    runtime.store.set_state("running", True)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert row["test_only"] is expected
    assert (row["status"] == "pronta") is expected
    assert row["blocker"] == "score"
    # Automatic trading takes a test row only on the Spot Testnet with the test mode on.
    assert ("act:1:test" if expected else "act:0") in executor.events


def test_a_smaller_balance_shrinks_orders_without_a_drawdown_halt(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    runtime.tick()
    # 2% of 1000 is the order cap; entries keep 10% of it as headroom for the stop.
    assert runtime.store.opportunities()[0]["size_usd"] == pytest.approx(18.0)
    monkeypatch.setattr(service, "free_balance", lambda asset, profile_id: Decimal("100"))
    for _ in range(3):
        runtime.tick()
    assert runtime.halted is False and runtime.store.peak_equity() == pytest.approx(1000.0)
    assert runtime.store.opportunities()[0]["size_usd"] == pytest.approx(1.8)


def test_auto_entries_share_the_free_balance_across_pairs(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    runtime, executor = _ready_cycle(
        tmp_path,
        monkeypatch,
        entry_mode="auto",
        min_win_rate=0.70,
        symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        limits=RiskLimits(max_order_notional_pct=0.5, max_entries_per_cycle=5),
    )
    # Orders are sized on the 30 USDT the account holds: 13.50 each (15 less headroom); 28.47 can be spent.
    monkeypatch.setattr(service, "free_balance", lambda asset, profile_id: Decimal("30"))
    monkeypatch.setattr(service, "market_rules", lambda symbol, profile_id: SimpleNamespace(min_notional=Decimal("5")))
    runtime.tick()
    assert "act:2" in executor.events
    rows = {row["symbol"]: row for row in runtime.store.opportunities()}
    assert rows["BTC/USDT"]["size_usd"] == pytest.approx(13.5)
    assert rows["ETH/USDT"]["size_usd"] == pytest.approx(13.5)
    assert rows["SOL/USDT"]["status"] == "bloqueada" and "saldo insuficiente" in rows["SOL/USDT"]["reason"]


def test_a_manual_entry_on_a_pair_already_held_is_refused(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.store.open_pending(
        "BTC/USDT", "mean_reversion", stop_price=90.0, target_price=None, exit_policy="trailing", test_only=False
    )
    result = runtime.execute_manual(_manual_opportunity())
    assert result["results"][0]["decision"] == "bloqueado" and "já em carteira" in result["results"][0]["reason"]
    assert not any(event.startswith("act:") for event in executor.events)


def test_auto_select_runs_hourly_and_keeps_pairs_with_positions(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, auto_select=True)
    runtime.store.open_pending(
        "XRP/USDT", "trend", stop_price=0.5, target_price=None, exit_policy="trailing", test_only=False
    )
    calls: list[int] = []
    monkeypatch.setattr(
        "bot.runtime.rank_promising",
        lambda _collector, count, listed: calls.append(count) or ["SOL/USDT", "XRP/USDT", "AVAX/USDT"],
    )
    monkeypatch.setattr("bot.runtime.venue_symbols", lambda _collector, mode: frozenset({"SOL/USDT"}))
    start = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    runtime.tick(now=start)
    runtime.tick(now=start.replace(minute=30))
    assert calls == [15]
    assert runtime.config.symbols == ["XRP/USDT", "SOL/USDT", "AVAX/USDT"]
    assert BotConfig.load().symbols == runtime.config.symbols
    runtime.tick(now=start.replace(hour=13, minute=1))
    assert len(calls) == 2


def test_a_failing_pair_ranking_never_halts_the_bot(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, auto_select=True)

    def broken(_collector: Any, _count: int, listed: Any) -> list[str]:
        raise TimeoutError("binance")

    monkeypatch.setattr("bot.runtime.rank_promising", broken)
    monkeypatch.setattr("bot.runtime.venue_symbols", lambda _collector, mode: None)
    start = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    for hour in range(4):
        assert runtime.tick(now=start.replace(hour=12 + hour))["status"] == "ok"
    assert runtime.halted is False and runtime.config.symbols == ["BTC/USDT"]


def test_auto_select_drops_pairs_the_testnet_cannot_trade_when_nothing_rises(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT", "STRK/USDT"], auto_select=True)
    monkeypatch.setattr("bot.runtime.rank_promising", lambda _collector, count, listed: [])
    monkeypatch.setattr("bot.runtime.venue_symbols", lambda _collector, mode: frozenset({"BTC/USDT", "ETH/USDT"}))
    runtime.tick()
    assert runtime.config.symbols == ["BTC/USDT"]


def _open(runtime: Runtime, symbol: str, strategy: str, *, qty: float, avg: float, stop: float) -> int:
    position_id = runtime.store.open_pending(
        symbol, strategy, stop_price=stop, target_price=None, exit_policy="trailing", test_only=False
    )
    assert position_id is not None
    runtime.store.update_position(position_id, status="open", qty=qty, avg_price=avg, stop_price=stop)
    return position_id


def test_open_risk_is_capped_on_the_configured_capital_not_the_balance(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    # 50 USDT at risk to the stop: under 6% of the configured 1000, far above 6% of the 200 the account funds.
    _open(runtime, "XRP/USDT", "trend", qty=100, avg=1.0, stop=0.5)
    monkeypatch.setattr(service, "free_balance", lambda asset, profile_id: Decimal("100"))
    runtime.tick()
    assert "act:1" in executor.events
    assert runtime.store.opportunities()[0]["size_usd"] == pytest.approx(3.6)


def test_an_unreadable_balance_blocks_entries_in_the_cycle(tmp_path, monkeypatch) -> None:
    runtime, executor = _ready_cycle(tmp_path, monkeypatch, entry_mode="auto", min_win_rate=0.70)
    monkeypatch.setattr(service, "free_balance", lambda asset, profile_id: None)
    runtime.tick()
    row = runtime.store.opportunities()[0]
    assert "act:0" in executor.events and row["status"] == "bloqueada" and "saldo" in row["reason"]


def test_the_cycle_exits_a_momentum_position_when_ema20_falls_under_ema50(tmp_path, monkeypatch) -> None:
    runtime, collector, executor = _runtime(tmp_path, monkeypatch)
    position_id = _open(runtime, "BTC/USDT", "momentum", qty=1, avg=100.0, stop=50.0)
    closes = [100 * 1.003**i for i in range(200)] + [100 * 1.003**199 * 0.99**i for i in range(1, 61)]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="4h", tz="UTC")
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
    collector.snapshot.candles[("BTC/USDT", "4h")] = Series(frame, datetime.now(timezone.utc), "test")
    runtime.store.set_state("running", True)
    runtime.tick()
    assert f"exit:{position_id}" in executor.events


def test_auto_select_waits_out_weight_pressure_and_survives_a_bad_timestamp(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, auto_select=True)
    calls: list[int] = []
    monkeypatch.setattr("bot.runtime.rank_promising", lambda _collector, count, listed: calls.append(count) or ["SOL/USDT"])
    monkeypatch.setattr("bot.runtime.venue_symbols", lambda _collector, mode: None)
    monkeypatch.setattr("bot.runtime.weight_pressure", lambda: True)
    runtime.tick()
    assert calls == []
    monkeypatch.setattr("bot.runtime.weight_pressure", lambda: False)
    runtime.store.set_state("auto_select_at", "not a time")
    assert runtime.tick()["status"] == "ok" and runtime.failures == 0


@pytest.mark.parametrize(("test_mode", "expected"), [(True, "act:1:test"), (False, "act:0")])
def test_testnet_auto_trading_takes_test_rows_only_with_the_test_mode(tmp_path, monkeypatch, test_mode: bool, expected: str) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, entry_mode="auto", testnet_test_mode=test_mode)
    # No validated strategy: the ready setup is a test row in test mode, signal only otherwise.
    monkeypatch.setattr("bot.runtime._setup", _setup)
    runtime.store.set_state("running", True)
    runtime.tick()
    assert expected in executor.events


def test_a_manual_entry_right_after_a_loss_on_the_pair_is_refused(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    loser = _open(runtime, "BTC/USDT", "mean_reversion", qty=1, avg=100.0, stop=90.0)
    runtime.store.close_position(loser, exit_price=95.0, realized_pnl=-5.0, fees=0.0)
    result = runtime.execute_manual(_manual_opportunity())
    assert result["results"][0]["decision"] == "bloqueado" and "pausa" in result["results"][0]["reason"]
    assert not any(event.startswith("act:") for event in executor.events)


def test_a_restart_after_a_halt_counts_failures_from_zero(tmp_path, monkeypatch) -> None:
    from bot.live.halt import clear_halt

    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.store.set_state("running", True)
    executor.raise_on_sync = RuntimeError("binance")
    for _ in range(3):
        runtime.tick()
    assert runtime.halted is True
    clear_halt("binance")
    clear_halt(None)
    assert runtime.start()["status"] == "ok"
    # One failure after the restart is one failure, not the fourth in a row.
    assert runtime.tick()["status"] == "error"
    assert runtime.halted is False and runtime.failures == 1


def test_strategies_decide_on_closed_bars_only(tmp_path, monkeypatch) -> None:
    from bot.runtime import _setup

    seen: list[int] = []

    class Recorder:
        timeframes = ("15m",)

        def evaluate(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> None:
            seen.append(len(frame))
            return None

    moment = datetime(2026, 10, 15, 12, 7, tzinfo=timezone.utc)
    index = pd.date_range(end=pd.Timestamp("2026-10-15T12:00:00Z"), periods=5, freq="15min")
    frame = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=index)
    snapshot = MarketSnapshot(candles={("BTC/USDT", "15m"): Series(frame, moment, "test")})
    _setup(Recorder(), "BTC/USDT", snapshot, 0.1, moment)  # type: ignore[arg-type]
    # The 12:00 bar is still forming at 12:07; the strategy sees the four closed ones.
    _setup(Recorder(), "BTC/USDT", snapshot, 0.1, moment.replace(minute=16))  # type: ignore[arg-type]
    assert seen == [4, 5]


def test_auto_select_keeps_hand_edits_of_bot_json_and_writes_it_whole(tmp_path, monkeypatch) -> None:
    import json

    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, auto_select=True)
    runtime.config.save()
    path = runtime.config.config_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["gates"]["g1"] = True
    raw["entry_mode"] = "auto"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("bot.runtime.rank_promising", lambda _collector, count, listed: ["SOL/USDT"])
    monkeypatch.setattr("bot.runtime.venue_symbols", lambda _collector, mode: None)
    runtime.tick()
    saved = BotConfig.load()
    assert saved.symbols == ["SOL/USDT"] and saved.gates["g1"] is True and saved.entry_mode == "auto"
    # In memory only the pairs change: the hand edit takes effect through the panel or a restart.
    assert runtime.config.symbols == ["SOL/USDT"] and runtime.config.entry_mode == "manual"
    assert [item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")] == []


def test_the_cycle_reads_binance_without_holding_the_panel_lock(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch)
    reading = threading.Event()
    release = threading.Event()
    inner = collector.refresh

    def slow_refresh(symbols: list[str]) -> MarketSnapshot:
        reading.set()
        assert release.wait(5)
        return inner(symbols)

    collector.refresh = slow_refresh  # type: ignore[method-assign]
    worker = threading.Thread(target=runtime.tick)
    worker.start()
    assert reading.wait(5)
    # A panel action while the cycle waits on Binance gets the lock at once.
    assert runtime.lock.acquire(timeout=1)
    runtime.lock.release()
    release.set()
    worker.join(5)
    assert not worker.is_alive()


def test_a_failed_market_read_still_counts_toward_the_halt(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch)

    def broken(symbols: list[str]) -> MarketSnapshot:
        raise TimeoutError("binance")

    collector.refresh = broken  # type: ignore[method-assign]
    runtime.store.set_state("running", True)
    for _ in range(3):
        assert runtime.tick()["status"] == "error"
    assert runtime.halted is True


def test_a_runtime_replaced_while_reading_binance_does_not_act(tmp_path, monkeypatch) -> None:
    runtime, collector, executor = _runtime(tmp_path, monkeypatch, entry_mode="auto")
    runtime.store.set_state("running", True)
    inner = collector.refresh

    def replaced_meanwhile(symbols: list[str]) -> MarketSnapshot:
        # The API rebinds (a mode switch) while this tick waits on Binance.
        with runtime.lock:
            runtime.retired = True
        return inner(symbols)

    collector.refresh = replaced_meanwhile  # type: ignore[method-assign]
    assert runtime.tick()["status"] == "skipped"
    assert executor.events == []


def test_a_ranking_that_finishes_after_the_panel_changed_the_pairs_writes_nothing(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, auto_select=True)
    runtime.config.save()

    def rank_while_the_panel_rebinds(_collector: Any, count: int, listed: Any) -> list[str]:
        # The operator picks a preset meanwhile: the API saves its pairs and retires this runtime.
        with runtime.lock:
            BotConfig.load().model_copy(update={"symbols": ["ETH/USDT"], "auto_select": False}).save()
            runtime.retired = True
        return ["SOL/USDT"]

    monkeypatch.setattr("bot.runtime.rank_promising", rank_while_the_panel_rebinds)
    monkeypatch.setattr("bot.runtime.venue_symbols", lambda _collector, mode: None)
    assert runtime.tick()["status"] == "skipped"
    assert BotConfig.load().symbols == ["ETH/USDT"]



def _failing(snapshot: MarketSnapshot, symbols: set[str]) -> MarketSnapshot:
    snapshot.failed = frozenset(symbols)
    return snapshot


def test_a_failed_pair_without_a_position_only_skips_that_pair(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT", "ETH/USDT"])
    monkeypatch.setattr("bot.runtime._setup", _setup)
    inner = collector.refresh
    collector.refresh = lambda symbols: _failing(inner(symbols), {"ETH/USDT"})  # type: ignore[method-assign]
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "ok" and runtime.failures == 0
    assert {row["symbol"] for row in runtime.store.opportunities()} == {"BTC/USDT"}


def test_a_failed_pair_with_a_position_fails_the_cycle(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT", "ETH/USDT"])
    _open(runtime, "ETH/USDT", "trend", qty=1, avg=100.0, stop=90.0)
    inner = collector.refresh
    collector.refresh = lambda symbols: _failing(inner(symbols), {"ETH/USDT"})  # type: ignore[method-assign]
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "error" and runtime.failures == 1


def test_manual_actions_of_a_replaced_runtime_are_refused(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch)
    runtime.retired = True
    assert runtime.execute_manual(_manual_opportunity())["results"][0]["decision"] == "bloqueado"
    assert runtime.close_position(1)["status"] == "blocked"
    assert executor.events == []


def test_a_setup_seen_every_cycle_is_recorded_once_until_it_changes(tmp_path, monkeypatch) -> None:
    runtime, _executor = _ready_cycle(tmp_path, monkeypatch)
    for _ in range(3):
        runtime.tick()
    rows = [row for row in runtime.store.decisions() if row.get("strategy") == "trend" and row.get("symbol") == "BTC/USDT"]
    assert len(rows) == 1
    # The setup disappears, then comes back: that is a new decision.
    monkeypatch.setattr("bot.runtime._setup", lambda *_args: None)
    runtime.tick()
    monkeypatch.setattr("bot.runtime._setup", _setup)
    runtime.tick()
    rows = [row for row in runtime.store.decisions() if row.get("strategy") == "trend" and row.get("symbol") == "BTC/USDT"]
    assert len(rows) == 2


def test_a_position_opened_during_the_collect_on_a_failed_pair_fails_the_cycle(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT", "ETH/USDT"])
    inner = collector.refresh
    collector.refresh = lambda symbols: _failing(inner(symbols), {"ETH/USDT"})  # type: ignore[method-assign]
    free_cash = runtime._free_cash
    reads: list[int] = []

    def manual_entry_lands() -> float | None:
        # The last read of the collect: a manual entry on ETH lands right after it, before the lock.
        if not reads:
            _open(runtime, "ETH/USDT", "trend", qty=1, avg=100.0, stop=90.0)
        reads.append(1)
        return free_cash()

    monkeypatch.setattr(runtime, "_free_cash", manual_entry_lands)
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "error" and runtime.failures == 1


def test_a_second_tick_while_one_runs_is_skipped(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch)
    runtime._ticking.acquire()
    try:
        assert runtime.tick()["status"] == "skipped"
    finally:
        runtime._ticking.release()
    assert runtime.tick()["status"] == "ok"


def test_a_pair_with_a_position_is_read_even_when_the_analysed_list_dropped_it(tmp_path, monkeypatch) -> None:
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT"])
    _open(runtime, "ETH/USDT", "trend", qty=1, avg=100.0, stop=90.0)
    asked: list[list[str]] = []
    collector.snapshot = _snapshot(["BTC/USDT", "ETH/USDT"])
    inner = collector.refresh
    collector.refresh = lambda symbols: (asked.append(list(symbols)), inner(symbols))[1]  # type: ignore[method-assign]
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "ok"
    assert asked == [["ETH/USDT", "BTC/USDT"]]


def test_a_position_opened_during_the_collect_on_an_unread_pair_fails_the_cycle(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT"])
    free_cash = runtime._free_cash
    reads: list[int] = []

    def manual_entry_lands() -> float | None:
        # A manual entry on a pair the panel still lists but this collect did not read.
        if not reads:
            _open(runtime, "SOL/USDT", "trend", qty=1, avg=100.0, stop=90.0)
        reads.append(1)
        return free_cash()

    monkeypatch.setattr(runtime, "_free_cash", manual_entry_lands)
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "error" and runtime.failures == 1


def test_a_position_change_during_the_collect_reads_the_cash_again(tmp_path, monkeypatch) -> None:
    runtime, _collector, _executor = _runtime(tmp_path, monkeypatch, ["BTC/USDT"])
    free_cash = runtime._free_cash
    reads: list[int] = []

    def manual_entry_lands() -> float | None:
        if not reads:
            _open(runtime, "BTC/USDT", "trend", qty=1, avg=100.0, stop=90.0)
        reads.append(1)
        return free_cash()

    monkeypatch.setattr(runtime, "_free_cash", manual_entry_lands)
    runtime.store.set_state("running", True)
    assert runtime.tick()["status"] == "ok"
    assert len(reads) == 2


def test_a_full_analysed_list_is_read_whole_next_to_a_held_pair_outside_it(tmp_path, monkeypatch) -> None:
    analysed = [f"C{index}/USDT" for index in range(20)]
    runtime, collector, _executor = _runtime(tmp_path, monkeypatch, analysed)
    _open(runtime, "ETH/USDT", "trend", qty=1, avg=100.0, stop=90.0)
    asked: list[list[str]] = []
    collector.snapshot = _snapshot([*analysed, "ETH/USDT"])
    inner = collector.refresh
    collector.refresh = lambda symbols: (asked.append(list(symbols)), inner(symbols))[1]  # type: ignore[method-assign]
    assert runtime.tick()["status"] == "ok"
    # Every scored pair is read in this cycle; none falls back on an older cycle's candles.
    assert asked == [["ETH/USDT", *analysed]]


def test_a_leftover_row_of_a_pair_no_longer_analysed_is_refused(tmp_path, monkeypatch) -> None:
    runtime, _collector, executor = _runtime(tmp_path, monkeypatch, ["ETH/USDT"])
    result = runtime.execute_manual(_manual_opportunity())
    assert result["results"][0]["decision"] == "bloqueado" and executor.events == []

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from bot.backtest.runner import VALIDATABLE, simulate
from bot.collector import SPOT, Collector
from bot.config import BotConfig, RiskLimits
from bot.evaluator import Opportunity, regime_factor
from bot.markets import TESTNET_SPOT, merge_symbols, rank_promising, venue_symbols
from bot.risk import RiskManager, allocate, open_risk
from bot.strategies import STRATEGIES
from bot.strategies.momentum import MIN_BARS, Momentum


def _frame(drift: float, bars: int = 220) -> pd.DataFrame:
    closes = [100 * (1 + drift) ** i * (1 + 0.004 * math.sin(i)) for i in range(bars)]
    index = pd.date_range("2026-01-01", periods=bars, freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value * 1.005 for value in closes],
            "low": [value * 0.995 for value in closes],
            "close": closes,
            "volume": [10.0] * bars,
        },
        index=index,
    )


# -- momentum strategy -------------------------------------------------------


def test_momentum_buys_a_steady_rise_with_an_atr_stop_and_bounded_confidence() -> None:
    setup = Momentum().evaluate("BTC/USDT", "4h", _frame(0.003))
    assert setup is not None
    assert setup.direction == 1 and setup.market == "spot" and setup.timeframe == "4h"
    assert 0 < setup.confidence <= 1
    assert setup.target_pct == pytest.approx(2 * setup.stop_pct)
    assert "momentum" in STRATEGIES and "momentum" in VALIDATABLE


def test_momentum_skips_a_fall_a_short_history_and_other_timeframes() -> None:
    strategy = Momentum()
    assert strategy.evaluate("BTC/USDT", "4h", _frame(-0.003)) is None
    assert strategy.evaluate("BTC/USDT", "4h", _frame(0.003, bars=MIN_BARS - 1)) is None
    assert strategy.evaluate("BTC/USDT", "1h", _frame(0.003)) is None


def test_momentum_exits_when_ema20_falls_under_ema50() -> None:
    rising = _frame(0.003)
    assert Momentum().exit_reason(rising, "buy") is None
    top = float(rising["close"].iloc[-1])
    closes = [*rising["close"], *(top * 0.99**i for i in range(1, 61))]
    falling = pd.DataFrame({"close": closes})
    assert Momentum().exit_reason(falling, "buy") is not None


def test_momentum_regime_factor_never_backs_a_falling_market() -> None:
    assert regime_factor("momentum", "trend_up", 1) == 1.0
    assert regime_factor("momentum", "range", 1) == 0.5
    assert regime_factor("momentum", "trend_down", 1) == 0.0
    assert regime_factor("momentum", "trend_up", -1) == 0.0


def test_the_backtest_replays_momentum_with_its_own_exit() -> None:
    frame = pd.concat([_frame(0.003), _frame(-0.004, bars=120)], ignore_index=True)
    frame.index = pd.date_range("2026-01-01", periods=len(frame), freq="4h", tz="UTC")
    trades = simulate("momentum", "BTC/USDT", frame, "4h", exit_policy="trailing")
    assert len(trades) >= 1


# -- sizing and config -------------------------------------------------------


def _opportunity(symbol: str, score: float, *, size: float = 100.0, risk_pct: float = 0.02) -> Opportunity:
    return Opportunity(
        symbol=symbol, strategy="trend", action="Comprar", score=score, net_return=0.04, risk_pct=risk_pct,
        status="pronta", reason="setup", timeframe="1h", direction=1, cost=0.002, gross_return=0.05, size_usd=size,
    )


def test_size_follows_the_risk_per_trade_on_the_capital_the_account_holds() -> None:
    limits = RiskLimits(risk_per_trade_pct=0.02, max_order_notional_pct=1.0)
    assert RiskManager(limits, 1000.0).size_for(_opportunity("BTC/USDT", 1, risk_pct=0.05)) == pytest.approx(400.0)
    assert RiskManager(limits, 1000.0, 100.0).size_for(_opportunity("BTC/USDT", 1, risk_pct=0.05)) == pytest.approx(40.0)
    # The order cap is 1000; an entry keeps 10% of it as headroom for its stop.
    assert RiskManager(limits, 1000.0).size_for(_opportunity("BTC/USDT", 1, risk_pct=0.01)) == pytest.approx(900.0)
    # The sizing capital never exceeds the configured one.
    assert RiskManager(limits, 1000.0, 5000.0).sizing_capital == 1000.0


def test_config_rejects_a_trade_risk_above_the_open_risk_and_fills_new_strategies() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(risk_per_trade_pct=0.02, max_open_risk_pct=0.01)
    with pytest.raises(ValidationError):
        RiskLimits(max_open_risk_pct=0.5)
    with pytest.raises(ValidationError):
        BotConfig(auto_select_count=21)
    old = BotConfig.model_validate({"strategies": {"trend": True}})
    assert old.strategies == {
        "trend": True,
        "mean_reversion": False,
        "momentum": False,
        "funding_carry": False,
        "intraday_momentum": False,
        "vwap_pullback": False,
    }


# -- allocation --------------------------------------------------------------


def _allocate(rows: list[Opportunity], *, cash: float | None, live: list[dict[str, Any]] | None = None,
              limits: RiskLimits | None = None, floor: float | None = 10.0) -> list[Opportunity]:
    return allocate(rows, cash=cash, live=live or [], capital=1000.0, limits=limits or RiskLimits(),
                    min_notional=lambda _symbol: floor)


def test_allocation_takes_the_best_score_first_and_one_position_per_pair() -> None:
    rows = [_opportunity("ETH/USDT", 1.0), _opportunity("BTC/USDT", 2.0), _opportunity("SOL/USDT", 1.5)]
    live = [{"symbol": "SOL/USDT", "status": "open", "qty": 1, "avg_price": 10, "stop_price": 10}]
    accepted = _allocate(rows, cash=10_000, live=live)
    assert [row.symbol for row in accepted] == ["BTC/USDT", "ETH/USDT"]
    assert rows[2].status == "bloqueada" and "já em carteira" in rows[2].reason


def test_allocation_shrinks_to_the_cash_then_blocks_below_the_minimum() -> None:
    rows = [_opportunity("BTC/USDT", 3.0), _opportunity("ETH/USDT", 2.0), _opportunity("SOL/USDT", 1.0)]
    accepted = _allocate(rows, cash=170.0, limits=RiskLimits(max_entries_per_cycle=5))
    budget = 170.0 * 0.95 / 1.001
    assert [row.symbol for row in accepted] == ["BTC/USDT", "ETH/USDT"]
    assert rows[1].size_usd == pytest.approx(budget - 100.0, abs=0.01)
    assert rows[2].status == "bloqueada" and "saldo insuficiente" in rows[2].reason
    tiny = [_opportunity("BTC/USDT", 1.0)]
    assert _allocate(tiny, cash=8.0) == [] and "saldo insuficiente" in tiny[0].reason
    unknown = [_opportunity("BTC/USDT", 1.0)]
    assert _allocate(unknown, cash=50.0, floor=None) == []


def test_allocation_keeps_the_open_risk_and_the_slots_within_the_limits() -> None:
    limits = RiskLimits(max_open_risk_pct=0.05, max_positions=5)
    # Each row risks 100 × 2% = 2 USDT; a pending entry counts as 1% of 1000 = 10 USDT.
    live = [
        {"symbol": "ADA/USDT", "status": "pending"},
        {"symbol": "XRP/USDT", "status": "open", "qty": 100, "avg_price": 1.0, "stop_price": 0.62},
    ]
    assert open_risk(live, pending_risk=10.0) == pytest.approx(48.0)
    rows = [_opportunity("BTC/USDT", 2.0), _opportunity("ETH/USDT", 1.0)]
    accepted = _allocate(rows, cash=10_000, live=live, limits=limits)
    assert [row.symbol for row in accepted] == ["BTC/USDT"]
    assert "risco da carteira" in rows[1].reason
    slots = [_opportunity("BTC/USDT", 2.0), _opportunity("ETH/USDT", 1.0)]
    full = [{"symbol": "ADA/USDT", "status": "open", "qty": 1, "avg_price": 1, "stop_price": 1}]
    assert len(_allocate(slots, cash=10_000, live=full, limits=RiskLimits(max_positions=2))) == 1
    assert "no máximo" in slots[1].reason


def test_a_row_below_the_minimum_expected_profit_is_refused() -> None:
    # 100 USDT at a 4% net return expects 4 USDT; the shrunk 40 USDT order expects 1.60.
    rows = [_opportunity("BTC/USDT", 2.0), _opportunity("ETH/USDT", 1.0)]
    accepted = allocate(rows, cash=147.5, live=[], capital=1000.0, limits=RiskLimits(),
                        min_notional=lambda _symbol: 5.0, min_profit_usd=2.0)
    assert [row.symbol for row in accepted] == ["BTC/USDT"]
    assert rows[1].status == "bloqueada" and "lucro esperado" in rows[1].reason


def test_an_unreadable_balance_blocks_every_entry() -> None:
    rows = [_opportunity("BTC/USDT", 1.0)]
    assert _allocate(rows, cash=None) == []
    assert rows[0].status == "bloqueada" and rows[0].size_usd == 0


# -- pair ranking ------------------------------------------------------------


def _daily(closes: list[float]) -> list[list[Any]]:
    return [[i, "0", "0", "0", str(value), "1"] for i, value in enumerate(closes)]


def _ranking_fetch(series: dict[str, list[float]], volumes: dict[str, float]):
    bases = [pair.split("/")[0] for pair in series] + ["USDC", "BTCUP"]

    def fetch(url: str):
        if url.endswith("/api/v3/exchangeInfo"):
            symbols = [{"status": "TRADING", "baseAsset": base, "quoteAsset": "USDT"} for base in bases]
            return {"symbols": symbols}, {}
        if url.endswith("/api/v3/ticker/24hr"):
            rows = [{"symbol": f"{base}USDT", "quoteVolume": str(volumes.get(f"{base}/USDT", 9e9))} for base in bases]
            return rows, {}
        if url.startswith(f"{SPOT}/api/v3/klines") and "interval=1d" in url:
            pair = url.split("symbol=")[1].split("&")[0].replace("USDT", "/USDT")
            return _daily(series[pair]), {}
        raise AssertionError(url)

    return fetch


def test_ranking_keeps_positive_risk_adjusted_momentum_best_first() -> None:
    steady = [100 * 1.01**i * (1 + 0.002 * math.sin(i)) for i in range(31)]
    jumpy = [100 * 1.012**i * (1 + 0.03 * math.sin(i)) for i in range(31)]
    falling = [100 * 0.99**i * (1 + 0.002 * math.sin(i)) for i in range(31)]
    series = {"SOL/USDT": steady, "DOGE/USDT": jumpy, "ADA/USDT": falling}
    collector = Collector(_ranking_fetch(series, {}))
    assert rank_promising(collector, 5) == ["SOL/USDT", "DOGE/USDT"]
    assert rank_promising(collector, 1) == ["SOL/USDT"]


def test_merge_keeps_live_pairs_first_and_the_symbol_cap() -> None:
    chosen = [f"P{index}/USDT" for index in range(25)]
    merged = merge_symbols(["ZZ/USDT", "P3/USDT"], chosen)
    assert merged[:2] == ["ZZ/USDT", "P3/USDT"] and len(merged) == 20 and merged.count("P3/USDT") == 1


def test_testnet_picks_only_pairs_the_spot_testnet_lists() -> None:
    steady = [100 * 1.01**i * (1 + 0.002 * math.sin(i)) for i in range(31)]
    series = {"SOL/USDT": steady, "DOGE/USDT": steady}
    inner = _ranking_fetch(series, {"SOL/USDT": 2e9, "DOGE/USDT": 1e9})
    testnet_reads: list[str] = []

    def fetch(url: str):
        if url.startswith(TESTNET_SPOT):
            testnet_reads.append(url)
            if url.endswith("/ticker/24hr"):
                return [{"symbol": "DOGEUSDT", "count": 25000}, {"symbol": "SOLUSDT", "count": 900}], {}
            listed = [{"status": "TRADING", "baseAsset": base, "quoteAsset": "USDT"} for base in ("DOGE", "SOL")]
            return {"symbols": listed}, {}
        return inner(url)

    collector = Collector(fetch)
    assert venue_symbols(collector, "production") is None and testnet_reads == []
    # SOL is listed on the testnet but trades too rarely there for fresh quotes.
    listed = venue_symbols(collector, "testnet")
    assert listed == frozenset({"DOGE/USDT"})
    assert venue_symbols(collector, "testnet") == listed and len(testnet_reads) == 2
    assert rank_promising(collector, 5, listed=listed) == ["DOGE/USDT"]
    assert rank_promising(collector, 5) == ["SOL/USDT", "DOGE/USDT"]



def test_one_cycle_opens_at_most_the_entry_cap() -> None:
    rows = [_opportunity(symbol, score) for symbol, score in (("BTC/USDT", 3.0), ("ETH/USDT", 2.0), ("SOL/USDT", 1.0))]
    accepted = _allocate(rows, cash=10_000, limits=RiskLimits(max_entries_per_cycle=2))
    assert [row.symbol for row in accepted] == ["BTC/USDT", "ETH/USDT"]
    assert rows[2].status == "bloqueada" and "por ciclo" in rows[2].reason


def _rated(win_rate: float | None, *, net: float = 0.06, risk: float = 0.04, test_only: bool = False) -> Opportunity:
    row = _opportunity("BTC/USDT", 1.0, risk_pct=risk)
    row.net_return = net
    row.win_rate = win_rate
    row.test_only = test_only
    return row


def test_kelly_lowers_the_risk_share_when_the_edge_is_thin() -> None:
    limits = RiskLimits(risk_per_trade_pct=0.02, max_order_notional_pct=1.0, kelly_fraction=0.25)
    manager = RiskManager(limits, 1000.0)
    # p=0.45, b=1.5: full Kelly 0.0833, a quarter 0.0208 -> the 2% trade risk binds.
    assert manager.size_for(_rated(0.45)) == pytest.approx(1000 * 0.02 / 0.04)
    # p=0.42, b=1.5: full Kelly 0.0333, a quarter 0.0083 -> risk 0.83% instead of 2%.
    assert manager.size_for(_rated(0.42)) == pytest.approx(1000 * 0.25 * (0.42 - 0.58 / 1.5) / 0.04)
    # No hit rate, a test row, or Kelly off: the stop-risk size stands.
    plain = 1000 * 0.02 / 0.04
    assert manager.size_for(_rated(None)) == pytest.approx(plain)
    assert manager.size_for(_rated(0.42, test_only=True)) == pytest.approx(plain)
    off = RiskManager(RiskLimits(max_order_notional_pct=1.0, kelly_fraction=0), 1000.0)
    # Kelly off: the default 1% risk share.
    assert off.size_for(_rated(0.42)) == pytest.approx(1000 * 0.01 / 0.04)


def test_a_row_with_no_estimated_edge_is_refused_but_a_test_row_is_not() -> None:
    limits = RiskLimits(kelly_fraction=0.25)
    manager = RiskManager(limits, 1000.0)
    common = dict(open_positions=[], exposure_usd=0.0, day_pnl=0.0, peak_equity=1000.0, equity=1000.0,
                  trades_today=0, returns_by_symbol={}, stale=False, signal_only=False)
    refused = manager.assess(_rated(0.30), **common)
    assert refused.allowed is False and "sem vantagem estimada" in refused.reason
    assert manager.assess(_rated(0.30, test_only=True), **common).allowed is True
    assert manager.assess(_rated(0.70), **common).allowed is True

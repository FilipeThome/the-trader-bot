"""Backtest gate. A strategy is executable only after it passes here.

The live strategies are replayed on closed bars with their default
parameters; nothing is fitted. Like the live spot bot, the replay only buys: a
sell setup is not a short. A position leaves by the configured exit policy:
``target`` (stop or target) or ``trailing`` (a stop that follows the highest
close at the entry's stop distance, no target), ``trailing_tp`` (the entry stop
until the activation gain, then a closer stop under the highest close;
``bot.strategies.trailing``), and always at the strategy's own exit (with the
EMA hysteresis of trend and momentum when ``hysteresis`` is on). The
operator's fixed rule (``bot.strategies.fixed``) replays apart, in
``simulate_fixed``. Stops rest at the broker, so they are checked against each
bar's low and fill at the stop, or at the open when the bar gaps through it;
the stop wins when one bar touches both. A target is sold by the cycle at the
last price, so the replay sells at the close of the bar that closes at or
above it, never on a wick. Each round trip pays the spot taker fee twice and
``LEG_SLIPPAGE`` on each leg. With the market filter on, no entry is taken
while BTC's last closed 4h bar is under its 40-day EMA.

The gate (``GATE_VERSION`` 2 in ``bot.strategies``) judges in-sample trades:
the last ``HOLDOUT_SHARE`` of the span, by exit time, is a hold-out that must
not lose, and the rest is cut into ``WF_WINDOWS`` windows of which
``WF_MIN_PROFITABLE`` must make money, with a walk-forward efficiency of at
least ``MIN_WFE``. The in-sample Sharpe must pass 1 (per trade annualised by
trades per year for swing strategies, daily for day strategies), the unit
drawdown stay under 20%, at least ``MIN_TRADES`` trades count, and the Sharpe
deflated by the number of trials since the last pass reach ``MIN_DSR``.
Monte Carlo drawdown in R is recorded, not gated: the live cycle compares it
with the current limits.
"""

from __future__ import annotations

import math
import multiprocessing
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from bot.backtest import stats
from bot.evaluator import SPOT_TAKER
from bot.indicators import btc_trend_ok_series, ema
from bot.strategies import STRATEGIES, validation_params
from bot.strategies.base import Setup
from bot.strategies.day import DAY, ENTRY_BAR, DayRules, in_quiet_hours
from bot.strategies.fixed import FixedExit, parse_fixed
from bot.strategies.intraday import IntradayMomentum, VwapPullback
from bot.strategies.mean_reversion import MeanReversion
from bot.strategies.momentum import Momentum
from bot.strategies.trailing import TRAILING_POLICIES, trailing_level
from bot.strategies.trend import TrendBreakout

#: Gate thresholds (README: Sharpe above 1 and drawdown below 20%).
MIN_SHARPE = 1.0
MAX_DRAWDOWN = 0.20
#: In-sample trades the gate needs, swing and day alike.
MIN_TRADES = 30
#: Walk-forward: equal in-sample windows, how many must make money, and the efficiency floor.
WF_WINDOWS = 4
WF_MIN_PROFITABLE = 3
MIN_WFE = 0.5
#: The last share of the span, by exit time, kept out of the gate's sample.
HOLDOUT_SHARE = 0.2
#: Deflated Sharpe floor: the probability that the Sharpe beats the best of the trials by luck.
MIN_DSR = 0.95
#: Bootstrap resamples of the Monte Carlo drawdown, with a fixed seed.
MC_RUNS = 1000
MC_SEED = 0
ROUND_TRIP_COST = 2 * SPOT_TAKER
_WARMUP_BARS = 60
#: Closed candles a strategy sees per step: the live collector reads 300 and
#: the cycle drops the one still forming (``runtime._closed``).
LIVE_WINDOW = 299
#: Worker processes of a validation, one symbol per task. The replay is pure
#: Python and pandas, so threads would share one interpreter lock.
MAX_WORKERS = 4
VALIDATABLE = ("trend", "mean_reversion", "momentum", "intraday_momentum", "vwap_pullback")
#: Slippage charged on each leg (entry, first target, exit), on top of the fees. Measured
#: per-pair slippage is not replayed: the validation must not depend on live history.
LEG_SLIPPAGE = 0.0003
DAY_SLIPPAGE = LEG_SLIPPAGE
#: The day gate: more trades make a per-trade Sharpe easier, so day strategies are judged
#: on daily returns, with a floor on the mean net return per trade.
DAY_MIN_TRADES = MIN_TRADES
DAY_MIN_MEAN_RETURN = 0.001
#: Days of history a validation replays (D11): about 2-3x the old budget, held to
#: about 10 minutes for 15 pairs.
DAY_VALIDATION_DAYS = 120
SWING_VALIDATION_DAYS = {"15m": 120, "1h": 240, "4h": 480}
#: The market filter's bars.
MARKET_BAR = pd.Timedelta(hours=4)


@dataclass
class BacktestResult:
    """One timeframe's validation.

    ``trades``, ``wins`` and ``net_return`` cover every replayed trade (the
    hit-rate estimate); ``sharpe``, ``max_drawdown``, ``dsr`` and
    ``gate_trades`` the in-sample trades the gate judged. The other metrics
    describe the whole sample. R is a trade's net return over its entry stop.
    """

    strategy_id: str
    sharpe: float
    max_drawdown: float
    trades: int
    net_return: float
    passed: bool
    wins: int = 0
    exit_policy: str = "target"
    #: Why the gate refused it, empty when it passed.
    reason: str = ""
    timeframe: str = ""
    #: Sharpe of daily returns, idle days as 0, over the whole span (what decay compares live with).
    daily_sharpe: float = 0.0
    dsr: float = 0.0
    trials: int = 1
    wfe: float = 0.0
    windows: list[dict[str, Any]] = field(default_factory=list)
    holdout: dict[str, Any] = field(default_factory=dict)
    mc_p95_dd_r: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    #: Share of the span spent in a position, averaged over the pairs.
    exposure: float = 0.0
    mae_r: float = 0.0
    mfe_r: float = 0.0
    #: Trades per year.
    turnover: float = 0.0
    avg_hold_hours: float = 0.0
    gate_trades: int = 0
    #: In-sample per-trade Sharpe, not annualised: the trial's entry in the deflated Sharpe.
    sharpe_trade: float = 0.0
    days: int = 0
    #: Replay time; it differs between identical runs, so it is left out of equality.
    elapsed_s: float = field(default=0.0, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Trade:
    """A closed long position. ``mae`` and ``mfe`` are the worst and best price
    excursions from the entry while it was held, as returns."""

    exit_time: pd.Timestamp
    net_return: float
    entry_time: pd.Timestamp | None = None
    stop_pct: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0
    bars: int = 0


def _strategy(strategy_id: str) -> TrendBreakout | MeanReversion | Momentum | IntradayMomentum | VwapPullback | None:
    if strategy_id == "trend":
        return TrendBreakout()
    if strategy_id == "mean_reversion":
        return MeanReversion()
    if strategy_id == "momentum":
        return Momentum()
    if strategy_id == "intraday_momentum":
        return IntradayMomentum()
    if strategy_id == "vwap_pullback":
        return VwapPullback()
    return None


def is_day(strategy_id: str) -> bool:
    return getattr(_strategy(strategy_id), "style", "swing") == "day"


def validation_days(strategy_id: str, timeframe: str) -> int:
    """Days of history one timeframe's validation replays."""
    if is_day(strategy_id):
        return DAY_VALIDATION_DAYS
    return SWING_VALIDATION_DAYS.get(timeframe, SWING_VALIDATION_DAYS["1h"])


def _bar_width(timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(timeframe.replace("m", "min"))


def _prices(frame: pd.DataFrame) -> tuple[list[float], list[float], list[float], list[float]]:
    """Open, low, high and close as Python floats: one lookup per bar, the same values as ``iloc``."""
    opens, lows, highs, closes = (frame[column].to_numpy(dtype=float).tolist() for column in ("open", "low", "high", "close"))
    return opens, lows, highs, closes


class _MarketGate:
    """The market filter replayed without look-ahead: a BTC 4h bar counts once it has closed."""

    def __init__(self, market: pd.DataFrame) -> None:
        self.closes_at = (market.index + MARKET_BAR).values
        self.ok = btc_trend_ok_series(market["close"].to_numpy(dtype=float), LIVE_WINDOW)

    def allows(self, moment: pd.Timestamp) -> bool:
        seen = int(np.searchsorted(self.closes_at, moment.to_datetime64(), side="right"))
        # Before the first closed bar there is no trend to judge, as with a short history live.
        return True if seen == 0 else bool(self.ok[seen - 1])


def _market_gate(market: pd.DataFrame | None) -> _MarketGate | None:
    return None if market is None or market.empty else _MarketGate(market)


def _trade(
    entered_at: pd.Timestamp, exit_time: pd.Timestamp, net: float, stop_pct: float, entry: float, worst: float, best: float, bars: int
) -> Trade:
    return Trade(exit_time, net, entered_at, stop_pct, worst / entry - 1, best / entry - 1, bars)


def simulate(
    strategy_id: str,
    symbol: str,
    frame: pd.DataFrame,
    timeframe: str,
    *,
    exit_policy: str = "target",
    hourly: pd.DataFrame | None = None,
    day: DayRules | None = None,
    hysteresis: bool = True,
    market: pd.DataFrame | None = None,
) -> list[Trade]:
    """Replay one symbol. Returns one trade per closed long position.

    A day strategy replays with ``simulate_day`` (its 1h frame and ``day`` rules)
    and the operator's fixed rule with ``simulate_fixed``; everything below is
    the swing replay. ``market`` is BTC's 4h bars when the market filter is on.
    """
    strategy = _strategy(strategy_id)
    if strategy is None:
        return []
    fixed = parse_fixed(exit_policy)
    if fixed is not None:
        return simulate_fixed(strategy, symbol, frame, timeframe, fixed, hourly=hourly, day=day or DAY, market=market)
    if isinstance(strategy, (IntradayMomentum, VwapPullback)):
        return simulate_day(strategy, symbol, frame, hourly, day or DAY, market=market)
    gate = _market_gate(market)
    bar = _bar_width(timeframe)
    opens, lows, highs, closes = _prices(frame)
    trades: list[Trade] = []
    position: Setup | None = None
    entry = peak = worst = best = 0.0
    entered = 0
    for end in range(_WARMUP_BARS, len(frame)):
        window = frame.iloc[max(0, end + 1 - LIVE_WINDOW) : end + 1]
        price = closes[end]
        if position is None:
            if gate is not None and not gate.allows(window.index[-1] + bar):
                continue
            setup = strategy.evaluate(symbol, timeframe, window)
            if setup is not None and setup.direction > 0:
                position = setup
                entry = peak = worst = best = price
                entered = end
            continue
        # Live stops rest at the broker and fill inside the bar, so the replay checks
        # them against the bar's low: a wick through the stop exits at the stop, or at
        # the open when the bar gaps past it. The cycle sells a target at the last
        # price, so the replay sells at the close of a bar that closes at or above it.
        opened, low, high = opens[end], lows[end], highs[end]
        stop = entry * (1 - position.stop_pct)
        trailed = trailing_level(exit_policy, entry, peak, position.stop_pct)
        if trailed is not None:
            stop = max(stop, trailed)
        target = entry * (1 + position.target_pct)
        if low <= stop:
            fill = min(opened, stop)
            worst, best = min(worst, fill), max(best, opened)
        else:
            worst, best = min(worst, low), max(best, high)
            if exit_policy not in TRAILING_POLICIES and price >= target:
                fill = price
            elif _strategy_exit(strategy, window, hysteresis):
                fill = price
            else:
                peak = max(peak, price)
                continue
        net = (fill - entry) / entry - ROUND_TRIP_COST - 2 * LEG_SLIPPAGE
        trades.append(_trade(frame.index[entered], window.index[-1], net, position.stop_pct, entry, worst, best, end - entered))
        position = None
    return trades


def simulate_day(
    strategy: IntradayMomentum | VwapPullback,
    symbol: str,
    frame: pd.DataFrame,
    hourly: pd.DataFrame | None,
    rules: DayRules,
    *,
    market: pd.DataFrame | None = None,
) -> list[Trade]:
    """Replay a day strategy on closed 15m bars, as the live cycle trades it.

    * The entry is the close of the bar whose setup appeared, outside the quiet
      hours (judged at that bar's close), with the 1h bars already closed then.
    * Each later bar: the stop is checked on the low first and wins when the
      same bar also reaches the first target. The first target sells
      ``tp1_fraction`` at the close of the bar that closes at or above it (the
      live cycle checks the last price and sells with an IOC). From the next
      bar the stop of the rest sits at the entry plus the buffer.
    * The rest leaves at the close of the bar where ``max_hold_minutes`` ends.

    The return weighs both legs and pays the taker fee twice and the slippage
    on each leg.
    """
    if hourly is None or hourly.empty:
        return []
    gate = _market_gate(market)
    closes = (hourly.index + pd.Timedelta(hours=1)).values
    hold_bars = -(-rules.max_hold_minutes // int(ENTRY_BAR.total_seconds() // 60))
    opens, lows, highs, bar_closes = _prices(frame)
    trades: list[Trade] = []
    position: Setup | None = None
    entry = stop = target = worst = best = 0.0
    entered = sold = 0
    first_leg: float | None = None
    for end in range(_WARMUP_BARS, len(frame)):
        window = frame.iloc[max(0, end + 1 - LIVE_WINDOW) : end + 1]
        decided_at = window.index[-1] + ENTRY_BAR
        close = bar_closes[end]
        if position is None:
            if in_quiet_hours(decided_at, rules):
                continue
            if gate is not None and not gate.allows(decided_at):
                continue
            seen = int(np.searchsorted(closes, decided_at.to_datetime64(), side="right"))
            setup = strategy.evaluate(symbol, "15m", window, hourly=hourly.iloc[max(0, seen - LIVE_WINDOW) : seen])
            if setup is not None and setup.direction > 0:
                position = setup
                entry = worst = best = close
                stop = entry * (1 - setup.stop_pct)
                target = entry * (1 + setup.target_pct)
                entered, sold, first_leg = end, 0, None
            continue
        opened = opens[end]
        low = lows[end]
        rest: float | None = None
        if low <= stop:
            rest = min(opened, stop)
            worst, best = min(worst, rest), max(best, opened)
        else:
            worst, best = min(worst, low), max(best, highs[end])
            if first_leg is None and close >= target:
                first_leg = close
                sold = end
        if rest is None and end - entered >= hold_bars:
            rest = close
        if first_leg is not None and sold == end and rest is None:
            # The break-even stop rests from the next bar.
            stop = entry * (1 + rules.breakeven_buffer)
            continue
        if rest is None:
            continue
        if first_leg is None:
            gross = (rest - entry) / entry
        else:
            share = rules.tp1_fraction
            gross = share * (first_leg - entry) / entry + (1 - share) * (rest - entry) / entry
        # Fees and slippage by notional: the whole position is bought once and sold once, in one or two parts.
        net = gross - ROUND_TRIP_COST - 2 * DAY_SLIPPAGE
        trades.append(_trade(frame.index[entered], window.index[-1], net, position.stop_pct, entry, worst, best, end - entered))
        position = None
    return trades


def simulate_fixed(
    strategy: TrendBreakout | MeanReversion | Momentum | IntradayMomentum | VwapPullback,
    symbol: str,
    frame: pd.DataFrame,
    timeframe: str,
    fixed: FixedExit,
    *,
    hourly: pd.DataFrame | None = None,
    day: DayRules = DAY,
    market: pd.DataFrame | None = None,
) -> list[Trade]:
    """Replay the operator's fixed take-profit and stop-loss.

    * The entry is the close of the bar whose setup appeared. A day strategy keeps
      its quiet hours and sees only the 1h bars closed by then, as in ``simulate_day``.
    * Each later bar: the stop rests at the broker, so it is checked on the low first
      and fills at the stop (or the open, on a gap through it). The target is not an
      order on the book: the cycle sells at the last price once it reaches the
      target. The replay only has closed bars, so it sells at the close of the bar
      that closes at or above it; live checks every minute and can sell on a touch
      the closed bar no longer shows, so the replay never counts a wick as a win.
    * No strategy exit, trailing or time stop. Both legs pay ``LEG_SLIPPAGE``.
    """
    day_style = isinstance(strategy, (IntradayMomentum, VwapPullback))
    if day_style and (hourly is None or hourly.empty):
        return []
    gate = _market_gate(market)
    bar = ENTRY_BAR if day_style else _bar_width(timeframe)
    closes = (hourly.index + pd.Timedelta(hours=1)).values if day_style and hourly is not None else None
    cost = ROUND_TRIP_COST + 2 * LEG_SLIPPAGE
    opens, lows, highs, bar_closes = _prices(frame)
    trades: list[Trade] = []
    holding = False
    entry = stop = target = worst = best = 0.0
    entered = 0
    for end in range(_WARMUP_BARS, len(frame)):
        window = frame.iloc[max(0, end + 1 - LIVE_WINDOW) : end + 1]
        close = bar_closes[end]
        if not holding:
            decided_at = window.index[-1] + bar
            if isinstance(strategy, (IntradayMomentum, VwapPullback)) and closes is not None and hourly is not None:
                if in_quiet_hours(decided_at, day):
                    continue
                if gate is not None and not gate.allows(decided_at):
                    continue
                seen = int(np.searchsorted(closes, decided_at.to_datetime64(), side="right"))
                setup = strategy.evaluate(symbol, "15m", window, hourly=hourly.iloc[max(0, seen - LIVE_WINDOW) : seen])
            else:
                if gate is not None and not gate.allows(decided_at):
                    continue
                setup = strategy.evaluate(symbol, timeframe, window)
            if setup is not None and setup.direction > 0:
                holding = True
                entry = worst = best = close
                stop = entry * (1 - fixed.stop_loss_pct)
                target = entry * (1 + fixed.take_profit_pct)
                entered = end
            continue
        opened = opens[end]
        low = lows[end]
        if low <= stop:
            fill = min(opened, stop)
            worst, best = min(worst, fill), max(best, opened)
        else:
            worst, best = min(worst, low), max(best, highs[end])
            if close >= target:
                fill = close
            else:
                continue
        net = (fill - entry) / entry - cost
        trades.append(_trade(frame.index[entered], window.index[-1], net, fixed.stop_loss_pct, entry, worst, best, end - entered))
        holding = False
    return trades


def _strategy_exit(strategy: Any, window: pd.DataFrame, hysteresis: bool = True) -> bool:
    """The same strategy exits the live runtime uses."""
    if isinstance(strategy, (TrendBreakout, Momentum)):
        return strategy.exit_reason(window, "buy", hysteresis=hysteresis) is not None
    if isinstance(strategy, MeanReversion):
        if len(window) < 20:
            return False
        return float(window["close"].iloc[-1]) >= float(ema(window["close"], 20).iloc[-1])
    return False


def _unit_drawdown(returns: np.ndarray) -> float:
    """Drawdown of compounded unit equity, one trade after another."""
    if len(returns) == 0:
        return 0.0
    equity = np.cumprod(1 + returns)
    return abs(float((equity / np.maximum.accumulate(equity) - 1).min()))


def _daily_sharpe(trades: Sequence[Trade], start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Sharpe of the net return per UTC day from ``start`` to ``end``; days without a trade count as 0."""
    days = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D")
    if not trades:
        return 0.0
    # Sorted by exit so callers may pass trades in any order (store rows come newest first).
    ordered = sorted(trades, key=lambda trade: pd.Timestamp(trade.exit_time))
    exits = pd.DatetimeIndex([trade.exit_time for trade in ordered]).normalize()
    daily = pd.Series([trade.net_return for trade in ordered], index=exits, dtype=float).groupby(level=0).sum()
    values = daily.reindex(days.union(daily.index), fill_value=0.0).to_numpy()
    if len(values) < 2:
        return 0.0
    sigma = float(values.std(ddof=1))
    return 0.0 if sigma == 0 else float(values.mean() / sigma * np.sqrt(365))


def evaluate(
    strategy_id: str,
    trades: list[Trade],
    years: float,
    *,
    exit_policy: str = "target",
    span: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    trials: int = 1,
    trial_sharpes: Sequence[float] = (),
    pairs: int = 1,
) -> BacktestResult:
    """Judge pooled trades against the gate.

    ``span`` is the replayed ``(start, end)``; without it the span is the
    ``years`` before the last exit. ``trials`` and ``trial_sharpes`` deflate
    the Sharpe (``bot.backtest.stats.deflated_sharpe``); ``pairs`` is the
    number of replayed symbols, for the exposure. A day strategy is judged by
    the day gate whatever rule it exits by: many short trades.
    """
    day_gate = exit_policy == "day" or is_day(strategy_id)
    wins = sum(1 for trade in trades if trade.net_return > 0)
    if not trades or years <= 0:
        return BacktestResult(strategy_id, 0.0, 0.0, len(trades), 0.0, False, wins, exit_policy, "nenhum trade", trials=trials)
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    if span is None:
        end = pd.Timestamp(ordered[-1].exit_time)
        start = end - pd.Timedelta(days=years * 365)
    else:
        start, end = pd.Timestamp(span[0]), pd.Timestamp(span[1])
    split = stats.walk_forward(ordered, start, end, windows=WF_WINDOWS, holdout=HOLDOUT_SHARE)
    sample: list[Trade] = split["in_sample"]
    held_from = pd.Timestamp(split["holdout"]["start"])
    sample_returns = np.array([trade.net_return for trade in sample], dtype=float)
    sharpe_trade = stats.per_trade_sharpe(sample_returns)
    if day_gate:
        sharpe = _daily_sharpe(sample, start, held_from)
    else:
        sample_years = years * (1 - HOLDOUT_SHARE)
        sharpe = sharpe_trade * math.sqrt(len(sample) / sample_years) if sample else 0.0
    drawdown = _unit_drawdown(sample_returns)
    profitable = sum(1 for window in split["windows"] if window["profitable"])
    wfe = stats.walk_forward_efficiency(split["windows"])
    dsr = stats.deflated_sharpe(sample_returns, trials, trial_sharpes)
    holdout = split["holdout"]
    label = "Sharpe diário" if day_gate else "Sharpe"
    checks = [
        (sharpe <= MIN_SHARPE, f"{label} {sharpe:.2f} não passa de {MIN_SHARPE:.0f}"),
        (drawdown >= MAX_DRAWDOWN, f"drawdown de {drawdown:.0%} não fica abaixo de {MAX_DRAWDOWN:.0%}"),
        (len(sample) < MIN_TRADES, f"{len(sample)} trades antes do hold-out, menos que {MIN_TRADES}"),
    ]
    if day_gate:
        mean = float(sample_returns.mean()) if len(sample_returns) else 0.0
        checks.append(
            (mean < DAY_MIN_MEAN_RETURN, f"retorno médio de {mean:.2%} por trade, abaixo de {DAY_MIN_MEAN_RETURN:.2%}")
        )
    checks += [
        (
            profitable < WF_MIN_PROFITABLE,
            f"walk-forward: {profitable} de {WF_WINDOWS} janelas lucram, menos que {WF_MIN_PROFITABLE}",
        ),
        (wfe < MIN_WFE, f"eficiência walk-forward de {wfe:.2f}, abaixo de {MIN_WFE}"),
        (not holdout["passed"], f"o hold-out (últimos {HOLDOUT_SHARE:.0%} do período) perde {holdout['net']:.1%}"),
        (dsr < MIN_DSR, f"Sharpe deflacionado de {dsr:.2f} em {trials} testes, abaixo de {MIN_DSR}"),
    ]
    reasons = [text for failed, text in checks if failed]

    returns = np.array([trade.net_return for trade in ordered], dtype=float)
    equity_end = float(np.prod(1 + returns))
    full_drawdown = _unit_drawdown(returns)
    cagr = equity_end ** (1 / years) - 1 if equity_end > 0 else -1.0
    if full_drawdown > 0:
        calmar = float(np.clip(cagr / full_drawdown, -stats.RATIO_CAP, stats.RATIO_CAP))
    else:
        calmar = stats.RATIO_CAP if cagr > 0 else 0.0
    risked = [trade for trade in ordered if trade.stop_pct > 0]
    r = np.array([trade.net_return / trade.stop_pct for trade in risked], dtype=float)
    held_hours = [
        (pd.Timestamp(trade.exit_time) - pd.Timestamp(trade.entry_time)).total_seconds() / 3600
        for trade in ordered
        if trade.entry_time is not None
    ]
    span_hours = (end - start).total_seconds() / 3600
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return BacktestResult(
        strategy_id,
        float(sharpe),
        drawdown,
        len(ordered),
        equity_end - 1,
        not reasons,
        wins,
        exit_policy,
        "; ".join(reasons),
        daily_sharpe=_daily_sharpe(ordered, start, end),
        dsr=float(dsr),
        trials=int(trials),
        wfe=wfe,
        windows=split["windows"],
        holdout=holdout,
        mc_p95_dd_r=stats.mc_drawdown_r(r, runs=MC_RUNS, seed=MC_SEED),
        profit_factor=stats.profit_factor(returns),
        expectancy_r=float(r.mean()) if len(r) else 0.0,
        sortino=stats.sortino(returns, len(returns) / years),
        calmar=calmar,
        avg_win=float(gains.mean()) if len(gains) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        exposure=sum(held_hours) / (span_hours * max(1, pairs)) if span_hours > 0 else 0.0,
        mae_r=float(np.mean([trade.mae / trade.stop_pct for trade in risked])) if risked else 0.0,
        mfe_r=float(np.mean([trade.mfe / trade.stop_pct for trade in risked])) if risked else 0.0,
        turnover=len(ordered) / years,
        avg_hold_hours=float(np.mean(held_hours)) if held_hours else 0.0,
        gate_trades=len(sample),
        sharpe_trade=sharpe_trade,
        days=int(round(years * 365)),
    )


def validate_strategy(
    strategy_id: str,
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    *,
    exit_policy: str = "target",
    hourly_frames: dict[str, pd.DataFrame] | None = None,
    hysteresis: bool = True,
    market_frame: pd.DataFrame | None = None,
    trials: int = 1,
    trial_sharpes: Sequence[float] = (),
    span: tuple[pd.Timestamp, pd.Timestamp] | None = None,
) -> BacktestResult:
    """Replay every symbol, in parallel processes, and judge the pooled trades against the gate.

    A day strategy replays with the day exits (``exit_policy`` "day") unless the
    operator's fixed rule replaces them, and needs ``hourly_frames``, the closed 1h
    bars of each symbol. ``market_frame`` (BTC's 4h bars) turns the market filter
    on. Without ``span`` the walk-forward runs from the first bar any symbol could
    trade (after the warm-up) to the last bar.
    """
    began = time.monotonic()
    if is_day(strategy_id) and parse_fixed(exit_policy) is None:
        exit_policy = "day"
    hourly = hourly_frames or {}
    jobs: list[_Job] = [
        (strategy_id, symbol, frame, timeframe, exit_policy, hourly.get(symbol), hysteresis, market_frame)
        for symbol, frame in frames.items()
        if not frame.empty
    ]
    trades: list[Trade] = []
    for replayed in _replay_all(jobs):
        trades.extend(replayed)
    if span is None and jobs:
        span = (
            min(job[2].index[min(_WARMUP_BARS, len(job[2]) - 1)] for job in jobs),
            max(job[2].index[-1] for job in jobs),
        )
    years = max(0.0, (span[1] - span[0]).total_seconds() / (365 * 86400)) if span is not None else 0.0
    result = evaluate(
        strategy_id, trades, years, exit_policy=exit_policy, span=span, trials=trials, trial_sharpes=trial_sharpes, pairs=len(jobs)
    )
    result.timeframe = timeframe
    result.elapsed_s = round(time.monotonic() - began, 3)
    return result


_Job = tuple[str, str, pd.DataFrame, str, str, "pd.DataFrame | None", bool, "pd.DataFrame | None"]


_POOL: ProcessPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _replay(job: _Job) -> list[Trade]:
    strategy_id, symbol, frame, timeframe, exit_policy, hourly, hysteresis, market = job
    return simulate(
        strategy_id, symbol, frame, timeframe, exit_policy=exit_policy, hourly=hourly, hysteresis=hysteresis, market=market
    )


def _replay_all(jobs: list[_Job]) -> list[list[Trade]]:
    """Each job's trades, in job order. One symbol runs here; more go to the process pool."""
    if len(jobs) < 2 or MAX_WORKERS < 2:
        return [_replay(job) for job in jobs]
    pool = _pool()
    try:
        return list(pool.map(_replay, jobs))
    except BrokenProcessPool:
        # A worker died (killed, out of memory): drop this pool, never a newer one, and finish here.
        shutdown_pool(pool)
        return [_replay(job) for job in jobs]


def _pool() -> ProcessPoolExecutor:
    """One pool per process, started on first use. ``spawn`` on every OS: forking the
    API process would copy its threads' locks in whatever state they hold."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=MAX_WORKERS, mp_context=multiprocessing.get_context("spawn"))
        return _POOL


def shutdown_pool(only: ProcessPoolExecutor | None = None) -> None:
    """Stop the worker processes. The API calls it on shutdown.

    ``only`` stops that pool if it is still the current one: a validation whose
    pool broke must not stop the newer pool another validation is using.
    """
    global _POOL
    with _POOL_LOCK:
        if only is not None and _POOL is not only:
            return
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


#: Per-timeframe keys of the validation record (``strategy_stats.params["timeframes"][tf]``).
_TIMEFRAME_FIELDS = (
    "passed", "reason", "wins", "trades", "gate_trades", "days", "sharpe", "sharpe_trade", "daily_sharpe", "dsr",
    "trials", "max_drawdown", "net_return", "mc_p95_dd_r", "wfe", "windows", "holdout", "profit_factor", "expectancy_r",
    "sortino", "calmar", "avg_win", "avg_loss", "exposure", "mae_r", "mfe_r", "turnover", "avg_hold_hours", "elapsed_s",
)


def timeframe_record(result: BacktestResult) -> dict[str, Any]:
    """One timeframe's entry in the validation record."""
    values = result.to_dict()
    return {key: values[key] for key in _TIMEFRAME_FIELDS}


def strategy_record(
    results: dict[str, BacktestResult],
    *,
    symbols: Sequence[str],
    exit_policy: str,
    market_filter: str,
    hysteresis: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``strategy_stats`` row of one validation: one result per operated timeframe.

    The strategy is executable (not signal-only) when any timeframe passed; the
    runtime then trades only setups of a passed timeframe. ``backtest_sharpe``
    is the best passed timeframe's daily Sharpe, the best of all when none
    passed. ``wins`` and ``trades`` pool every timeframe.
    """
    if not results:
        raise ValueError("a validation record needs at least one timeframe result")
    strategy_id = next(iter(results.values())).strategy_id
    passed = [result for result in results.values() if result.passed]
    best = max(passed or results.values(), key=lambda result: result.daily_sharpe)
    params = {
        **(extra or {}),
        **validation_params(
            strategy_id,
            exit_policy,
            timeframes={timeframe: timeframe_record(result) for timeframe, result in results.items()},
            symbols=symbols,
            market_filter=market_filter,
            hysteresis=hysteresis,
        ),
        "window": LIVE_WINDOW,
        "wins": sum(result.wins for result in results.values()),
        "trades": sum(result.trades for result in results.values()),
        "reason": "; ".join(
            f"{timeframe}: {result.reason}" for timeframe, result in results.items() if not result.passed and result.reason
        ),
    }
    return {
        "strategy_id": strategy_id,
        "backtest_sharpe": best.daily_sharpe,
        "backtest_drawdown": best.max_drawdown,
        "validated_at": datetime.now(timezone.utc).isoformat() if passed else None,
        "live_sharpe": None,
        "signal_only": not passed,
        "params": params,
    }


def promotion_record(result: BacktestResult, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """A one-timeframe validation record with the default market filter (off) and hysteresis (on).

    The timeframe is the result's, else ``params["timeframe"]``, else the
    strategy's first; the pair set is ``params["symbols"]``.
    """
    extra = dict(params or {})
    strategy = STRATEGIES.get(result.strategy_id)
    timeframe = result.timeframe or extra.get("timeframe") or (strategy.timeframes[0] if strategy is not None else "")
    return strategy_record(
        {timeframe: result},
        symbols=list(extra.get("symbols") or []),
        exit_policy=result.exit_policy,
        market_filter="off",
        hysteresis=True,
        extra=extra,
    )

"""Scoring of one cycle: every enabled strategy's setup on every analysed pair, classified,
checked against the validation and the risk limits, and sized; plus the hit-rate floor and
the late-signal rule automatic entries obey.

``_score`` writes nothing: it reads the ``CycleContext`` and hands back ``ScoreEffect`` items
the ``Runtime`` applies. Prices are read through ``market._last_price``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pandas as pd

from bot.advisor.service import AdviceKey, AdviceState, advice_key
from bot.collector import MarketSnapshot
from bot.config import AdvisorConfig, BotConfig, RiskLimits
from bot.cycle import market
from bot.cycle.context import CycleContext, ScoreEffect, profile_for
from bot.decay import pair_demoted, pair_slippage_blocked
from bot.evaluator import SPOT_TAKER, Opportunity, classify, estimate_win_rate
from bot.exchange import service
from bot.indicators import atr_pct_daily, btc_trend_ok
from bot.promotion import PILOT_SKIP_VALIDATION_STATE
from bot.risk import (
    RiskManager,
    breakeven_win_rate,
    mc_drawdown,
    mc_drawdown_ok,
    planned_notional,
    utc_day,
    vol_target_cap,
    vol_throttle,
)
from bot.store import Store
from bot.strategies import (
    DAY_STRATEGIES,
    STRATEGIES,
    FundingCarry,
    IntradayMomentum,
    Strategy,
    VwapPullback,
    exit_policy_for,
    live_thresholds,
    timeframe_passed,
    timeframe_result,
    validated_for,
)
from bot.strategies.base import Setup
from bot.strategies.day import DAY_WIN_RATE_MARGIN, in_quiet_hours
from bot.strategies.fixed import FIXED_WIN_RATE_MARGIN, is_fixed, parse_fixed

logger = logging.getLogger(__name__)

#: Quality filters the Spot Testnet test mode may waive for a manual buy.
TESTABLE_BLOCKERS = frozenset({"score", "cost", "reward_risk", "regime"})
#: A signal older than ``max_signal_age_seconds`` is taken automatically only while the price
#: is at most this many stop distances above the signal bar's close: further up, the entry
#: chases a move the replay never paid for.
MAX_LATE_DRIFT_R = 0.5
#: Refusal of a strategy with no current validation, or one demoted to signal only.
SIGNAL_ONLY = "estratégia em somente sinal: sem validação no histórico ou rebaixada"


@dataclass(frozen=True)
class _PairInputs:
    """What sizing reads of one pair in one cycle (``RiskManager.sizing``)."""

    venue_cap: Any
    atr_daily: float | None
    throttle: float


def _score(
    context: CycleContext, *, store: Store, config: BotConfig, decided: Mapping[tuple[str, str], tuple[str, str]]
) -> tuple[list[Opportunity], list[ScoreEffect]]:
    """Every enabled strategy's setup on every analysed pair: classified, checked and sized.

    Reads the cycle's context (risk level, account taker fee per pair, closed hourly
    returns with BTC/USDT, book, equity, the advisor's verdicts) and writes nothing: the
    equity point, the pair blocks, the refusals counted and the decisions come back as
    effects, each with the row's status and reason at score time, for ``_apply_effects``.
    ``decided`` is the last decision recorded per (symbol, strategy), read only.

    A row the advisor judged shows the verdict (``Opportunity.advice``). In ``filter`` a
    ``reduce`` verdict's factor goes into ``RiskManager.assess``, which applies it last and
    only downwards (``_advice_factor``).
    """
    state = context.state
    assert state is not None, "the cycle scores on its own risk level"
    snapshot, moment, limits = context.snapshot, context.moment, context.limits
    capital, sizing_capital = context.capital, context.sizing_capital
    risk = RiskManager(limits, capital, sizing_capital, risk_scale=state.risk_scale)
    returns = context.returns
    takers = context.takers
    profile = profile_for(config)
    ignored = store.ignored_keys(moment)
    positions = context.positions
    live = context.live
    # Exposure at the last price, as the gate values it.
    held_usd = context.held_usd
    exposure = sum(abs(value) for value in held_usd.values())
    exposure += sum(float(row.get("reserved_usd") or 0) for row in context.book if row["status"] == "reserved")
    day = utc_day(moment)
    effects = [
        ScoreEffect("equity", {"equity": context.equity, "ts": moment.isoformat(), "pnl": context.equity - capital})
    ]
    trades_today = store.trades_today(day)
    market_block = _market_block(snapshot, moment, config)
    macro_block = _macro_block(moment, config)
    saved = store.get_state("pair_blocked")
    blocks: dict[str, str] = dict(saved) if isinstance(saved, dict) else {}
    enabled = {key: value for key, value in STRATEGIES.items() if config.strategies.get(key, False)}
    stats_by_id = {strategy_id: store.strategy(strategy_id) for strategy_id in enabled}
    # Part of the validation's key: a pass under other thresholds is signal-only until re-validated.
    thresholds = live_thresholds(config.score_threshold, config.cost_multiple, config.min_reward_risk, config.exit_policy)
    unvalidated_ok = pilot_unvalidated(config, store)
    ranked: list[Opportunity] = []
    for symbol in config.symbols:
        if symbol in snapshot.failed:
            # Its candles are from an earlier cycle: no setup is read from them.
            continue
        inputs: _PairInputs | None = None
        slippage_block: str | None = None
        for strategy_id, strategy in enabled.items():
            stats = stats_by_id[strategy_id]
            params = stats["params"] if stats is not None else None
            # A validation run under other rules (exit, market filter, hysteresis, classify
            # thresholds) does not validate these.
            keyed = stats is not None and validated_for(
                params,
                strategy_id,
                config.exit_policy,
                market_filter=config.market_filter,
                hysteresis=config.exit_hysteresis,
                thresholds=thresholds,
            )
            passed = tuple(tf for tf in strategy.timeframes if keyed and timeframe_passed(params, tf))
            setup = _setup(strategy, symbol, snapshot, config.funding_annualized_threshold, moment, passed)
            if setup is None:
                # The setup is gone: the next one is a new decision.
                if (symbol, strategy_id) in decided:
                    effects.append(ScoreEffect("forget", {"key": (symbol, strategy_id)}))
                continue
            if inputs is None:
                inputs = _pair_inputs(symbol, snapshot, moment, limits, profile)
                slippage_block = _pair_block(
                    blocks, symbol, None, "slippage",
                    pair_slippage_blocked(store, symbol, limits.slippage_budget_pct, now=moment),
                    effects,
                )
            policy = exit_policy_for(strategy_id, config.exit_policy)
            fixed = parse_fixed(policy)
            if fixed is not None:
                setup = fixed.apply(setup)
            result = timeframe_result(params, setup.timeframe) if keyed else None
            signal_reason = _signal_only_reason(
                stats, keyed, result, setup.timeframe, symbol, strategy_id, limits, moment, blocks, effects,
                store=store, waived=unvalidated_ok,
            )
            # The thresholds classify judges with below: the filtered hit rate counts only when
            # the replay marked its trades with the same ones.
            min_reward_risk = 0.0 if fixed is not None else config.min_reward_risk
            win_rate, win_rate_n = estimate_win_rate(
                {"exit_policy": params.get("exit_policy"), **result} if params and result is not None else None,
                store.strategy_outcomes(strategy_id, policy),
                exit_policy=policy,
                min_sample=config.min_win_rate_sample,
                thresholds=(config.score_threshold, config.cost_multiple, min_reward_risk),
            )
            stale = snapshot.stale(symbol, market._price_interval(setup.timeframe), now=moment)
            draft = classify(
                setup,
                slippage=snapshot.slippage(symbol, planned_notional(limits, sizing_capital, setup.stop_pct)),
                blocked_reason=_entry_block(strategy_id, moment, macro_block, slippage_block, market_block),
                ignored=(symbol, strategy_id) in ignored,
                in_position=(symbol, strategy_id) in live,
                score_threshold=config.score_threshold,
                cost_multiple=config.cost_multiple,
                # The operator chose the payoff of a fixed rule; the hit-rate floor judges it.
                min_reward_risk=min_reward_risk,
                # Never cheaper than the backtest's fee, whatever discount the account has.
                taker=max(SPOT_TAKER, takers.get(symbol, SPOT_TAKER)),
            )
            draft.data_age_seconds = market._age(snapshot, symbol, setup.timeframe, moment)
            draft.signal_age_seconds = _signal_age(snapshot, symbol, setup.timeframe, moment)
            draft.entry_drift_r = _entry_drift_r(snapshot, symbol, setup.timeframe, moment, draft.risk_pct)
            # The executor re-applies the target distance on the average fill.
            draft.plan_target_pct = setup.target_pct if setup.target_pct > 0 else None
            draft.win_rate = win_rate
            draft.win_rate_n = win_rate_n
            if config.test_mode_active and setup.market == "spot" and setup.direction > 0:
                waivable = draft.status == "potencial" and draft.blocker in TESTABLE_BLOCKERS
                if waivable or (draft.status == "pronta" and signal_reason is not None):
                    draft.status = "pronta"
                    draft.test_only = True
                    draft.reason = f"[teste] {draft.reason}"
            if draft.status == "em_posicao":
                draft.action = "Manter; stop na corretora"
            refuse_signal = signal_reason is not None and draft.status == "pronta" and not draft.test_only
            vol_cap: float | None = None
            if draft.status == "pronta" and not refuse_signal:
                planned = risk.sizing(
                    draft, venue_cap_usd=inputs.venue_cap, atr_daily_pct=inputs.atr_daily, throttle=inputs.throttle
                )
                vol_cap = vol_target_cap(limits, sizing_capital, held_usd, symbol, planned.size_usd, returns)
            advice = _advice_entry(context, symbol, strategy_id, setup.timeframe, config.advisor)
            if advice is not None:
                draft.advice = advice.public()
            verdict = risk.assess(
                draft,
                open_positions=positions,
                exposure_usd=exposure,
                trades_today=trades_today,
                returns_by_symbol=returns,
                stale=stale,
                signal_only=refuse_signal,
                state=state,
                venue_cap_usd=inputs.venue_cap,
                atr_daily_pct=inputs.atr_daily,
                throttle=inputs.throttle,
                vol_target_cap_usd=vol_cap,
                advice_factor=_advice_factor(advice, config.advisor),
            )
            if draft.status == "pronta" and not verdict.allowed:
                draft.status = "bloqueada"
                draft.reason = signal_reason if refuse_signal and not stale and signal_reason else verdict.reason
                effects.append(ScoreEffect("violation", {"day": day}))
            if verdict.allowed:
                draft.size_usd = verdict.size_usd
            draft.size_limit = verdict.size_limit or None
            draft.size_caps = verdict.caps or None
            price = market._last_price(snapshot, symbol, setup.timeframe)
            if price and draft.direction > 0:
                draft.stop_price = price * (1 - draft.risk_pct)
                draft.target_price = price * (1 + max(setup.target_pct, 0))
            ranked.append(draft)
            if decided.get((symbol, strategy_id)) != (draft.status, draft.reason):
                record = {
                    "ts": moment.isoformat(),
                    "symbol": symbol,
                    "strategy": strategy_id,
                    "decision": draft.status,
                    "reason": draft.reason,
                }
                effects.append(ScoreEffect("decision", {"key": (symbol, strategy_id), "record": record}))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked, effects


def _signal_only_reason(
    stats: dict[str, Any] | None,
    keyed: bool,
    result: dict[str, Any] | None,
    timeframe: str,
    symbol: str,
    strategy_id: str,
    limits: RiskLimits,
    moment: datetime,
    blocks: dict[str, str],
    effects: list[ScoreEffect],
    *,
    store: Store,
    waived: bool = False,
) -> str | None:
    """Why a setup may only signal, or None when it may trade (6.5, 6.12).

    The validation must hold for the current rules, the setup's own timeframe must have
    passed, the pair must belong to the validated set, the Monte Carlo drawdown at the
    risk per trade (``mc_p95_dd_r × risk_per_trade_pct``) must be readable and stay under
    ``max_drawdown_pct``, and the strategy must not be demoted on this pair. A production
    pilot that ``waived`` the validation (``pilot_unvalidated``) skips every check but the
    demotion: its capital is bounded by the pilot cap.
    """
    if waived:
        return _pair_block(blocks, symbol, strategy_id, "expectancy", pair_demoted(store, strategy_id, symbol, now=moment), effects)
    if stats is None or bool(stats.get("signal_only")) or not keyed:
        return SIGNAL_ONLY
    if not (result and result.get("passed")):
        return f"somente sinal: o timeframe {timeframe} não passou na validação"
    if symbol not in ((stats.get("params") or {}).get("symbols") or ()):
        return f"somente sinal: {symbol} fora do conjunto de pares validado"
    mc = result.get("mc_p95_dd_r")
    drawdown = mc_drawdown(mc, limits)
    if drawdown is None:
        # A record without a readable Monte Carlo drawdown proves nothing about the limit.
        return "somente sinal — Monte Carlo: drawdown p95 ausente ou ilegível na validação; valide de novo"
    if not mc_drawdown_ok(mc, limits):
        return (
            f"somente sinal — Monte Carlo: drawdown p95 de {_pct(drawdown)} passa do limite "
            f"de {_pct(limits.max_drawdown_pct)}"
        )
    return _pair_block(blocks, symbol, strategy_id, "expectancy", pair_demoted(store, strategy_id, symbol, now=moment), effects)


def _pair_block(
    blocks: dict[str, str],
    symbol: str,
    strategy: str | None,
    kind: str,
    reason: str | None,
    effects: list[ScoreEffect],
) -> str | None:
    """Return ``reason``; when the pair's block changed, add a ``pair_blocked`` effect.

    ``blocks`` is this cycle's copy of ``bot_state.pair_blocked``; each change saves a
    copy of it whole, with its ledger event, when the effects are applied.
    """
    key = f"{symbol}|{strategy or ''}|{kind}"
    if blocks.get(key) != reason:
        if reason is None:
            blocks.pop(key, None)
        else:
            blocks[key] = reason
        event = {"symbol": symbol, "strategy": strategy, "reason": kind, "blocked": reason is not None, "detail": reason}
        effects.append(ScoreEffect("pair_blocked", {"blocks": dict(blocks), "event": event}))
    return reason


def _pair_inputs(symbol: str, snapshot: MarketSnapshot, moment: datetime, limits: RiskLimits, profile: str) -> _PairInputs:
    """The venue cap, the daily ATR and the 7-day volatility throttle of one pair, from closed 4h bars.

    The cap is read from the mandate and binance.json (no network); an unreadable one allows nothing.
    """
    four_hour = market._closed(snapshot, symbol, "4h", moment)
    atr = atr_pct_daily(four_hour) if four_hour is not None else None
    throttle = vol_throttle(four_hour["close"].to_numpy(), limits.vol_throttle_ratio) if four_hour is not None else 1.0
    try:
        cap: Any = service.order_notional_cap(symbol, profile)
    except Exception:  # noqa: BLE001 - an unreadable cap sizes the order to zero
        logger.exception("order cap read failed for %s", symbol)
        cap = Decimal(0)
    return _PairInputs(cap, atr, throttle)


def _market_block(snapshot: MarketSnapshot, moment: datetime, config: BotConfig) -> str | None:
    """The market filter (D13): with ``btc_ema40d``, no entry while BTC's last closed 4h close
    is under its 240-bar EMA, nor while BTC could not be read."""
    if config.market_filter == "off":
        return None
    frame = None if market.MARKET_SYMBOL in snapshot.failed else market._closed(snapshot, market.MARKET_SYMBOL, "4h", moment)
    if frame is None:
        return "filtro de mercado: BTC sem leitura neste ciclo"
    if not btc_trend_ok(frame["close"]):
        return "filtro de mercado: BTC abaixo da EMA de 40 dias"
    return None


def _macro_block(moment: datetime, config: BotConfig) -> str | None:
    """The operator's macro calendar (D21): day entries pause within ``macro_pause_minutes`` of an event.
    A convention of the operator: the backtest does not replay it."""
    minutes = config.macro_pause_minutes
    if minutes <= 0:
        return None
    window = timedelta(minutes=minutes)
    for raw in config.macro_events_utc:
        event = datetime.fromisoformat(raw)
        event = event if event.tzinfo else event.replace(tzinfo=timezone.utc)
        if abs(moment - event) <= window:
            return f"pausa de evento macro até {event + window:%H:%M} UTC"
    return None


def pilot_unvalidated(config: BotConfig, store: Store) -> bool:
    """Whether a production pilot operates without a stored validation (``PILOT_SKIP_VALIDATION_STATE``).

    Only the production store ever holds the key, and only the switch writes it; the mode
    guard keeps a store file copied between environment roots from waiving anything on the testnet.
    """
    return config.mode == "production" and bool(store.get_state(PILOT_SKIP_VALIDATION_STATE))


def _auto_eligible(item: Opportunity, test_auto: bool, config: BotConfig, *, unvalidated_ok: bool = False) -> bool:
    """Whether automatic trading may take a ready row: its hit rate (or the test mode, or a
    pilot without validation while it has no estimate), and a signal that is still worth its price (D21).

    A signal younger than ``max_signal_age_seconds`` is always taken. An older one is
    taken only while the price has not left it: at most ``MAX_LATE_DRIFT_R`` above the
    signal bar's close, and not a whole stop below it. A row stays ready on its closed
    bar for the bar's whole width (four hours on 4h), so a clock limit alone would let
    automatic trading enter only in the first minutes after each close and never after
    a restart; the risk of a late entry is chasing the price, which this measures. A
    late row the rule skips stays ready for a manual or armed entry and says why.
    """
    if not ((item.test_only and test_auto) or _clears_floor(item, config, unvalidated_ok=unvalidated_ok)):
        return False
    limit = config.limits.max_signal_age_seconds
    age = item.signal_age_seconds
    if limit <= 0 or age is None or age <= limit:
        return True
    drift = item.entry_drift_r
    if drift is None:
        why = "preço atual sem leitura"
    elif drift > MAX_LATE_DRIFT_R:
        why = f"preço {drift:.2f} R acima do sinal"
    elif drift <= -1.0:
        why = f"preço {abs(drift):.2f} R abaixo do sinal, além do stop"
    else:
        return True
    item.reason = f"{item.reason}; sinal com {age:.0f} s de atraso e {why}: sem entrada automática"
    return False


def _advice_entry(
    context: CycleContext, symbol: str, strategy_id: str, timeframe: str, settings: AdvisorConfig
) -> AdviceState | None:
    """The verdict cached for this setup's signal bar, or None (advisor off, or never asked)."""
    if settings.mode == "off" or not context.advice:
        return None
    bar = _signal_bar(context.snapshot, symbol, timeframe, context.moment)
    if bar is None:
        return None
    return context.advice.get(advice_key(context.advice_scope, symbol, strategy_id, timeframe, bar[0], settings.model))


def _advice_factor(entry: AdviceState | None, settings: AdvisorConfig) -> float:
    """The size factor scoring passes to ``RiskManager.assess``: a ``reduce`` verdict's in
    ``filter``, 1 otherwise. Only the factor goes in, never the verdict (S-A3)."""
    if settings.mode != "filter" or entry is None or entry.advice is None or entry.advice.verdict != "reduce":
        return 1.0
    return entry.advice.size_factor


def advice_request_key(
    item: Opportunity, snapshot: MarketSnapshot, moment: datetime, model: str, scope: str
) -> tuple[AdviceKey, float] | None:
    """The advisor's cache key of a ready row for the store ``scope`` (``Store.identity``) and its
    signal bar's close (the counterfactual's entry); None without a closed bar of the row's timeframe."""
    bar = _signal_bar(snapshot, item.symbol, item.timeframe, moment)
    if bar is None:
        return None
    return advice_key(scope, item.symbol, item.strategy, item.timeframe, bar[0], model), bar[1]


def _advice_allows(item: Opportunity, entry: AdviceState | None, moment: datetime, settings: AdvisorConfig) -> bool:
    """Whether the advisor lets automatic trading take a ready row (6.4). Only ``filter`` holds
    anything back; ``shadow`` never changes or delays an entry.

    A veto at ``min_veto_confidence`` or above keeps the row out and says why; it stays ready
    for a manual or armed entry, as the late-signal rule leaves it. A verdict still pending
    holds the row until ``wait_seconds`` after the request, counted across cycles, never
    waited for inside one; after that, or with no verdict at all (no key, budget spent, an
    API failure, a full queue, an abstention), the row goes on, unless ``require_verdict``.
    """
    if settings.mode != "filter":
        return True
    advice = entry.advice if entry is not None else None
    if entry is not None and entry.status == "pending":
        waited = max(0.0, (moment - entry.requested_at).total_seconds())
        if waited < settings.wait_seconds:
            item.reason = f"{item.reason}; aguardando a análise IA ({waited:.0f} de {settings.wait_seconds} s)"
            return False
    elif advice is not None and advice.verdict != "abstain":
        if advice.verdict == "veto" and advice.confidence >= settings.min_veto_confidence:
            why = advice.rationale or "sem justificativa"
            item.reason = f"{item.reason}; IA desaconselha: {why} (confiança {advice.confidence:.0%}); sem entrada automática"
            return False
        return True
    if settings.require_verdict:
        item.reason = f"{item.reason}; análise IA exigida e sem veredito: sem entrada automática"
        return False
    return True


def _clears_floor(item: Opportunity, config: BotConfig, *, unvalidated_ok: bool = False) -> bool:
    """Whether a real (non-test) row's estimated hit rate allows an automatic entry.

    Swing rows need ``min_win_rate``. Day rows need the break-even of their
    first target plus ``DAY_WIN_RATE_MARGIN``: a day trade wins less often by
    design (half at 1.5R, the rest at break-even), so the swing floor would
    never let one in. Rows under the operator's fixed take-profit and stop-loss
    need the break-even of that payoff plus ``FIXED_WIN_RATE_MARGIN``: a 3%
    target over a 1.5% stop pays at a hit rate well under the swing floor.
    A row without an estimate (no validation and fewer live closes than the
    sample) enters only under a pilot that waived the validation; once the live
    sample gives an estimate, the floor judges it like any other row.
    """
    if item.test_only:
        return False
    if item.win_rate is None:
        return unvalidated_ok
    policy = exit_policy_for(item.strategy, config.exit_policy)
    if policy == "day":
        margin = DAY_WIN_RATE_MARGIN
    elif is_fixed(policy):
        margin = FIXED_WIN_RATE_MARGIN
    else:
        return item.win_rate >= config.min_win_rate
    breakeven = breakeven_win_rate(item)
    return breakeven is not None and item.win_rate >= breakeven + margin


def _setup(
    strategy: Strategy,
    symbol: str,
    snapshot: MarketSnapshot,
    funding_threshold: float,
    moment: datetime,
    passed: tuple[str, ...] = (),
) -> Setup | None:
    """The strategy's setup on the pair's closed bars, or None.

    A swing strategy tries its timeframes in order, the ones in ``passed`` (validated)
    first, so a setup on a validated timeframe is preferred to one that may only signal.
    """
    if isinstance(strategy, (IntradayMomentum, VwapPullback)):
        # 15m entries filtered by the closed 1h bars (bot.strategies.day).
        entry = market._closed(snapshot, symbol, "15m", moment)
        if entry is None:
            return None
        day_setup: Setup | None = strategy.evaluate(symbol, "15m", entry, hourly=market._closed(snapshot, symbol, "1h", moment))
        return day_setup
    if isinstance(strategy, FundingCarry):
        frame = market._closed(snapshot, symbol, "1h", moment)
        if frame is None:
            return None
        found: Setup | None = strategy.evaluate(
            symbol,
            "8h",
            frame,
            funding_rates=snapshot.funding.get(symbol),
            threshold=funding_threshold,
        )
        return found
    ordered = [tf for tf in strategy.timeframes if tf in passed] + [tf for tf in strategy.timeframes if tf not in passed]
    for timeframe in ordered:
        frame = market._closed(snapshot, symbol, timeframe, moment)
        if frame is None:
            continue
        setup: Setup | None = strategy.evaluate(symbol, timeframe, frame)
        if setup is not None:
            return setup
    return None


def _entry_drift_r(
    snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime, risk_pct: float
) -> float | None:
    """The live price's distance from the signal bar's close, in stop distances (R).

    None when the closed bar, the live price or a positive stop distance is missing.
    """
    frame = market._closed(snapshot, symbol, timeframe, moment)
    last = market._last_price(snapshot, symbol, timeframe)
    if frame is None or frame.empty or last is None or not risk_pct > 0:
        return None
    signal = float(frame["close"].iloc[-1])
    if not (signal > 0 and math.isfinite(signal) and math.isfinite(last)):
        return None
    return (last / signal - 1) / risk_pct


def _signal_bar(snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime) -> tuple[str, float] | None:
    """When the signal's closed bar closed (ISO time) and its close; None without such a bar."""
    width = market._BAR.get(timeframe)
    frame = market._closed(snapshot, symbol, timeframe, moment) if width is not None else None
    if frame is None or width is None:
        return None
    close = float(frame["close"].iloc[-1])
    if not (close > 0 and math.isfinite(close)):
        return None
    return (pd.Timestamp(frame.index[-1]) + width).isoformat(), close


def _signal_age(snapshot: MarketSnapshot, symbol: str, timeframe: str, moment: datetime) -> float | None:
    """Seconds since the bar that produced the signal closed; None without a closed bar of that width."""
    width = market._BAR.get(timeframe)
    frame = market._closed(snapshot, symbol, timeframe, moment) if width is not None else None
    if frame is None or width is None:
        return None
    closed_at = pd.Timestamp(frame.index[-1]) + width
    return max(0.0, float((pd.Timestamp(moment) - closed_at).total_seconds()))


def _entry_block(
    strategy_id: str, moment: datetime, macro: str | None, slippage: str | None, market_filter: str | None
) -> str | None:
    """Why no entry of this strategy may open now, whatever its score: day trades skip the
    thin hours and the macro windows, a pair over its slippage budget takes no entry, and
    the market filter holds every entry."""
    if strategy_id in DAY_STRATEGIES:
        if in_quiet_hours(moment):
            return "hora sem liquidez: sem entrada de day trade entre 03:00 e 07:00 UTC"
        if macro:
            return macro
    return slippage or market_filter


def _pct(value: float) -> str:
    """A share as a percentage with at most one decimal: 0.05 → 5%, 0.075 → 7.5%."""
    return f"{value:.1%}".replace(".0%", "%")

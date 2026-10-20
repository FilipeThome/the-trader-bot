"""Position sizing and the bot's own limits. A failed check means no trade."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np

from bot.config import RiskLimits
from bot.evaluator import SPOT_TAKER, Opportunity

#: Share of the free quote balance new entries may spend; the rest absorbs fees and price moves.
CASH_BUFFER = 0.95
#: An entry uses at most this share of the per-order cap. The gate prices the stop
#: and its replacements at the bid, so a winner can rise about 11% before a new stop
#: would breach the cap; past that the executor keeps the old stop.
ORDER_HEADROOM = 0.90
#: Window of the loss guard: losing closes counted over the last six hours.
LOSS_GUARD_WINDOW = timedelta(hours=6)

#: The limits that can set an order's size, as the panel names them. ``SizeDecision.binding``,
#: ``RiskVerdict.size_limit`` and ``Opportunity.size_limit`` hold one of these keys.
SIZE_LIMITS: dict[str, str] = {
    "risk": "risco por trade",
    "kelly": "Kelly",
    "safety": "teto por ordem",
    "venue": "teto da corretora",
    "volatility": "volatilidade (ATR diário)",
    "exposure": "exposição total",
    "vol_target": "alvo de volatilidade",
    "cash": "caixa",
    "throttle": "volatilidade de 7 dias alta (metade)",
}

#: Risk levels of ``RiskManager.portfolio_state``, mildest first.
RISK_LEVELS = ("normal", "cautious", "reducing", "halt")
#: The cautious level (drawdown past ``dd_reduce_pct``) halves the risk share and opens one entry per cycle.
CAUTIOUS_RISK_SCALE = 0.5
CAUTIOUS_MAX_ENTRIES = 1
#: ``vol_throttle``: the size multiplier when the last seven days of 4h returns run hot.
THROTTLE_FACTOR = 0.5
THROTTLE_WINDOW_BARS = 42
#: Portfolio volatility from hourly returns: at least two days of them, an EWMA
#: covariance with a three-day half-life, annualised over the hours of a year.
MIN_VOL_RETURNS = 48
VOL_HALFLIFE_HOURS = 72
HOURS_PER_YEAR = 8760
#: The correlation cluster: BTC itself and every pair that moves with it.
CLUSTER_ANCHOR = "BTC/USDT"
CLUSTER_NAME = "BTC"
#: Slack of the drawdown comparisons, so a 10% drop reads as 10% after float rounding.
_EPS = 1e-12


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str = ""
    size_usd: float = 0.0
    pause_day: bool = False
    halt: bool = False
    #: The drawdown reached ``max_drawdown_pct``: exits only, no buy of any kind.
    reducing: bool = False
    #: The ``SIZE_LIMITS`` key that set ``size_usd``, and every cap in USD.
    size_limit: str = ""
    caps: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SizeDecision:
    """An order's size, the ``SIZE_LIMITS`` key that set it, and every cap considered, in USD."""

    size_usd: float
    binding: str
    caps: dict[str, float]


@dataclass(frozen=True)
class RiskState:
    """The account's drawdown level for this cycle (``RiskManager.portfolio_state``).

    ``level`` is one of ``RISK_LEVELS``. ``risk_scale`` multiplies the risk share of
    new entries and ``max_entries`` caps the entries of the cycle (None: the
    configured cap). ``pause_day`` is the daily loss, independent of the level.
    """

    level: str
    drawdown: float
    risk_scale: float
    max_entries: int | None
    reason: str
    pause_day: bool = False


def correlation(left: list[float], right: list[float]) -> float:
    n = min(len(left), len(right))
    if n < 5:
        return 0.0
    a = left[-n:]
    b = right[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    den_a = float(sum((x - mean_a) ** 2 for x in a)) ** 0.5
    den_b = float(sum((y - mean_b) ** 2 for y in b)) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    value = float(num / (den_a * den_b))
    # A NaN or infinite return makes the series unreadable: no correlation is claimed.
    return value if math.isfinite(value) else 0.0


def breakeven_win_rate(opportunity: Opportunity) -> float | None:
    """Hit rate at which a row's risk and target pay even: ``1 / (1 + b)`` with
    ``b = net_return / risk_pct``. ``kelly_risk`` is positive exactly above it. None
    when the row has no positive payoff or risk."""
    if opportunity.risk_pct <= 0 or opportunity.net_return <= 0:
        return None
    return opportunity.risk_pct / (opportunity.risk_pct + opportunity.net_return)


class RiskManager:
    """``capital`` is the configured capital: equity, daily loss and drawdown are measured on it.

    ``sizing_capital`` (at most ``capital``) is what the account can fund: order
    size and the exposure limit use it, so a smaller balance shrinks new
    orders without reading as a loss. ``risk_scale`` (within [0, 1]) multiplies the
    risk share of every order, as the cautious drawdown level does.
    """

    def __init__(
        self,
        limits: RiskLimits,
        capital_usd: float,
        sizing_capital: float | None = None,
        *,
        risk_scale: float = 1.0,
    ) -> None:
        if not math.isfinite(risk_scale):
            raise ValueError("risk_scale must be finite")
        self.limits = limits
        self.capital = capital_usd
        self.sizing_capital = capital_usd if sizing_capital is None else max(0.0, min(capital_usd, sizing_capital))
        self.risk_scale = min(1.0, max(0.0, risk_scale))

    def sizing(
        self,
        opportunity: Opportunity,
        *,
        venue_cap_usd: Any = None,
        atr_daily_pct: float | None = None,
        throttle: float = 1.0,
    ) -> SizeDecision:
        """The order size: the smallest of the caps below, then times ``throttle``.

        - ``risk`` or ``kelly``: ``C·share·risk_scale/risk_pct``, so the loss at the stop is
          the risk share. The share is ``risk_per_trade_pct``, lowered to ``kelly_fraction``
          of full Kelly when the row's hit rate is known (``kelly_risk``); the key says which.
        - ``safety``: ``C·max_order_notional_pct·ORDER_HEADROOM``.
        - ``venue``: ``venue_cap_usd·ORDER_HEADROOM`` (the mandate and testnet cap), when given.
        - ``volatility``: ``C·vol_cap_pct/atr_daily_pct`` when both are positive: one daily
          ATR of the order costs at most ``vol_cap_pct`` of the capital.

        ``C`` is the sizing capital. A throttle below 1 reports ``throttle`` as the binding
        limit. Without the optional inputs this is the stop-risk size under the order ceiling.
        """
        return self._decide(
            opportunity, self.risk_scale, venue_cap_usd=venue_cap_usd, atr_daily_pct=atr_daily_pct, throttle=throttle
        )

    def size_for(
        self,
        opportunity: Opportunity,
        *,
        venue_cap_usd: Any = None,
        atr_daily_pct: float | None = None,
        throttle: float = 1.0,
    ) -> float:
        """``sizing``'s size in USD."""
        decision = self.sizing(opportunity, venue_cap_usd=venue_cap_usd, atr_daily_pct=atr_daily_pct, throttle=throttle)
        return decision.size_usd

    def kelly_risk(self, opportunity: Opportunity) -> float | None:
        """Full-Kelly share of capital to lose at the stop: ``p − (1 − p) / b``.

        ``p`` is the strategy's estimated hit rate and ``b`` the payoff per unit
        risked, net return over stop distance. ``b`` is the target's payoff; with
        the trailing stop the target is never an exit, so this is an upper bound,
        and for a day trade it is the payoff of the first target (TP1) leg.
        ``None`` when it does not apply: Kelly off, a test row, or no hit rate.
        """
        if self.limits.kelly_fraction <= 0 or opportunity.test_only or opportunity.win_rate is None:
            return None
        if opportunity.risk_pct <= 0 or opportunity.net_return <= 0:
            return -1.0
        payoff = opportunity.net_return / opportunity.risk_pct
        p = opportunity.win_rate
        return p - (1 - p) / payoff

    def portfolio_state(
        self,
        *,
        day_change: float,
        peak_equity: float,
        equity: float,
        previous: RiskState | Mapping[str, Any] | str | None = None,
    ) -> RiskState:
        """The drawdown level of the account, and the daily loss.

        The drawdown is ``(peak − equity)/peak``. From the deepest step:
        ``halt_drawdown_pct`` halts; ``max_drawdown_pct`` is ``reducing`` (exits only);
        ``dd_reduce_pct`` (0 = off) is ``cautious``, with the risk share halved and one
        entry per cycle; below it, ``normal``. A ``previous`` reducing (or halted) level
        stays reducing until the drawdown falls below ``dd_reduce_pct`` (below
        ``max_drawdown_pct`` when that step is off), so it does not flicker at the line.
        ``previous`` is the last state, its stored dict, or its level.

        ``pause_day`` is a ``day_change`` (equity now minus the equity at the start of the
        UTC day) at or below ``−capital·max_daily_loss_pct``. An input that is not a
        finite number reads as ``reducing``: no buy until the equity is readable again.
        """
        limits = self.limits
        if not all(_finite(value) for value in (day_change, peak_equity, equity)):
            return RiskState("reducing", 0.0, 0.0, 0, "modo só reduzir: patrimônio ilegível; só saídas até a leitura voltar")
        drawdown = max(0.0, (peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0
        pause_day = day_change <= -self.capital * limits.max_daily_loss_pct
        release = limits.dd_reduce_pct if limits.dd_reduce_pct > 0 else limits.max_drawdown_pct
        was_reducing = _level_of(previous) in ("reducing", "halt")
        reached = drawdown + _EPS
        level: str
        entries: int | None
        if reached >= limits.halt_drawdown_pct:
            level, scale, entries = "halt", 0.0, 0
            reason = f"drawdown de {_pct(drawdown)} desde o pico: parada no limite de {_pct(limits.halt_drawdown_pct)}"
        elif reached >= limits.max_drawdown_pct:
            level, scale, entries = "reducing", 0.0, 0
            reason = (
                f"modo só reduzir: drawdown de {_pct(drawdown)} desde o pico "
                f"(limite de {_pct(limits.max_drawdown_pct)}); só saídas"
            )
        elif was_reducing and reached >= release:
            level, scale, entries = "reducing", 0.0, 0
            reason = f"modo só reduzir: drawdown de {_pct(drawdown)}, ainda acima de {_pct(release)}; só saídas"
        elif limits.dd_reduce_pct > 0 and reached >= limits.dd_reduce_pct:
            level, scale, entries = "cautious", CAUTIOUS_RISK_SCALE, CAUTIOUS_MAX_ENTRIES
            reason = (
                f"modo cauteloso: drawdown de {_pct(drawdown)} desde o pico; risco pela metade e "
                f"{CAUTIOUS_MAX_ENTRIES} entrada por ciclo"
            )
        else:
            level, scale, entries, reason = "normal", 1.0, None, ""
        if pause_day:
            reason = "; ".join(part for part in (reason, _daily_loss_reason(limits)) if part)
        return RiskState(level, drawdown, scale, entries, reason, pause_day=pause_day)

    def assess(
        self,
        opportunity: Opportunity,
        *,
        open_positions: list[dict[str, Any]],
        exposure_usd: float,
        trades_today: int,
        returns_by_symbol: dict[str, list[float]],
        stale: bool,
        signal_only: bool,
        state: RiskState,
        venue_cap_usd: Any = None,
        atr_daily_pct: float | None = None,
        throttle: float = 1.0,
        vol_target_cap_usd: float | None = None,
    ) -> RiskVerdict:
        """Whether a row may trade, and its size (``sizing`` within the exposure room).

        The account limits come from ``state`` alone (``portfolio_state``, read once per
        cycle): a halt or reducing level refuses, the daily loss pauses, and the state's
        ``risk_scale`` lowers the risk share. The exposure room is ``min(C·max_exposure_pct,
        vol_target_cap_usd) − exposure_usd``: an order larger than the room shrinks to it
        (``exposure`` or ``vol_target`` binds), and no room refuses.
        """
        if stale:
            return RiskVerdict(False, "OHLCV com mais de 2 intervalos de atraso")
        if signal_only:
            return RiskVerdict(False, "estratégia em somente sinal: sem validação no histórico ou rebaixada")
        blocked = self._state_verdict(state)
        if blocked is not None:
            return blocked
        scale = min(self.risk_scale, max(0.0, state.risk_scale))
        if trades_today >= self.limits.max_trades_per_day:
            return RiskVerdict(False, "limite de trades por dia")
        if len(open_positions) >= self.limits.max_positions and opportunity.status != "em_posicao":
            return RiskVerdict(False, "posições simultâneas no máximo")
        kelly = self.kelly_risk(opportunity)
        if kelly is not None and kelly <= 0:
            return RiskVerdict(
                False,
                f"sem vantagem estimada: {opportunity.win_rate:.0%} de acerto não paga o risco até o alvo (Kelly ≤ 0)",
            )
        room = self._exposure_room(exposure_usd, vol_target_cap_usd)
        tightest = min(room, key=room.__getitem__)
        if room[tightest] <= 0:
            caps = {name: round(value, 2) for name, value in room.items()}
            return RiskVerdict(False, self._room_reason(tightest, exposure_usd), size_limit=tightest, caps=caps)
        decision = self._decide(
            opportunity, scale, venue_cap_usd=venue_cap_usd, atr_daily_pct=atr_daily_pct, throttle=throttle, room=room
        )
        # Below a cent the order would be sent as zero.
        if round(decision.size_usd, 2) <= 0:
            return RiskVerdict(
                False,
                f"tamanho de ordem zerado pelo limite: {SIZE_LIMITS[decision.binding]}",
                size_limit=decision.binding,
                caps=decision.caps,
            )
        if self._too_correlated(opportunity.symbol, open_positions, returns_by_symbol):
            return RiskVerdict(
                False,
                f"já há {self.limits.max_correlated} posições correlacionadas acima de {self.limits.correlation_threshold:.2f}",
            )
        return RiskVerdict(True, size_usd=round(decision.size_usd, 2), size_limit=decision.binding, caps=decision.caps)

    def _decide(
        self,
        opportunity: Opportunity,
        risk_scale: float,
        *,
        venue_cap_usd: Any,
        atr_daily_pct: float | None,
        throttle: float,
        room: dict[str, float] | None = None,
    ) -> SizeDecision:
        capital = self.sizing_capital
        share, share_limit = self._risk_share(opportunity)
        stop = opportunity.risk_pct
        caps: dict[str, float] = {
            share_limit: capital * share * risk_scale / stop if _positive(stop) and share > 0 else 0.0,
            "safety": capital * self.limits.max_order_notional_pct * ORDER_HEADROOM,
        }
        venue = _cap_value(venue_cap_usd)
        if venue is not None:
            caps["venue"] = venue * ORDER_HEADROOM
        if self.limits.vol_cap_pct > 0 and atr_daily_pct is not None and _positive(atr_daily_pct):
            caps["volatility"] = capital * self.limits.vol_cap_pct / float(atr_daily_pct)
        caps.update(room or {})
        # Of equal caps the first one above is reported.
        binding = min(caps, key=caps.__getitem__)
        size = max(0.0, caps[binding])
        factor = _throttle_factor(throttle)
        if factor < 1 and size > 0:
            size *= factor
            caps["throttle"] = size
            binding = "throttle"
        return SizeDecision(size, binding, {name: round(value, 2) for name, value in caps.items()})

    def _risk_share(self, opportunity: Opportunity) -> tuple[float, str]:
        """The share of capital to lose at the stop, and its source: ``risk`` or ``kelly``."""
        share = self.limits.risk_per_trade_pct
        kelly = self.kelly_risk(opportunity)
        if kelly is None:
            return share, "risk"
        scaled = self.limits.kelly_fraction * kelly
        if not math.isfinite(scaled):
            # An unreadable hit rate estimates no edge.
            return 0.0, "kelly"
        if scaled < share:
            return max(0.0, scaled), "kelly"
        return share, "risk"

    def _exposure_room(self, exposure_usd: float, vol_target_cap_usd: float | None) -> dict[str, float]:
        """New exposure each ceiling allows; an unreadable exposure allows none."""
        exposure = float(exposure_usd) if _finite(exposure_usd) else math.inf
        room = {"exposure": self.sizing_capital * self.limits.max_exposure_pct - exposure}
        target = _cap_value(vol_target_cap_usd)
        if target is not None:
            room["vol_target"] = target - exposure
        return room

    def _room_reason(self, limit: str, exposure_usd: float) -> str:
        if limit == "vol_target":
            return (
                f"alvo de volatilidade da carteira de {_pct(self.limits.target_vol_annual)} ao ano atingido: "
                "sem nova exposição"
            )
        if self.sizing_capital <= 0 or not _finite(exposure_usd):
            return f"exposição total no limite de {self.limits.max_exposure_pct:.0%}"
        share = exposure_usd / self.sizing_capital
        return f"exposição total já em {share:.0%} do limite de {self.limits.max_exposure_pct:.0%}"

    def _state_verdict(self, state: RiskState) -> RiskVerdict | None:
        if state.level == "halt":
            return RiskVerdict(False, state.reason, halt=True, pause_day=state.pause_day)
        if state.level == "reducing":
            return RiskVerdict(False, state.reason, reducing=True, pause_day=state.pause_day)
        if state.pause_day:
            return RiskVerdict(False, _daily_loss_reason(self.limits), pause_day=True)
        return None

    def _too_correlated(
        self,
        symbol: str,
        open_positions: list[dict[str, Any]],
        returns_by_symbol: dict[str, list[float]],
    ) -> bool:
        own = returns_by_symbol.get(symbol) or []
        correlated = 0
        for position in open_positions:
            other = str(position.get("symbol") or "")
            if other == symbol:
                continue
            if abs(correlation(own, returns_by_symbol.get(other) or [])) > self.limits.correlation_threshold:
                correlated += 1
        return correlated >= self.limits.max_correlated


def mc_drawdown(p95_r: object, limits: RiskLimits) -> float | None:
    """The validation's Monte Carlo p95 drawdown at today's risk per trade, or None when unreadable.

    ``p95_r`` is ``mc_p95_dd_r`` from a timeframe result, in R. A missing, boolean or
    non-finite value proves nothing about the drawdown limit, so it reads as None.
    """
    if isinstance(p95_r, bool) or not isinstance(p95_r, (int, float)) or not math.isfinite(float(p95_r)):
        return None
    return float(p95_r) * limits.risk_per_trade_pct


def mc_drawdown_ok(p95_r: object, limits: RiskLimits) -> bool:
    """True only when the Monte Carlo p95 drawdown is readable and stays under ``max_drawdown_pct``.

    The one rule the cycle (signal-only rows) and the panel (``mc_ok``) both use.
    """
    drawdown = mc_drawdown(p95_r, limits)
    return drawdown is not None and drawdown < limits.max_drawdown_pct


def planned_notional(limits: RiskLimits, sizing_capital: float, risk_pct: float) -> float:
    """The order a row with this stop distance plans before its per-row caps: the stop-risk
    size under the order ceiling. Without a usable stop distance, the ceiling itself."""
    capital = max(0.0, float(sizing_capital)) if _finite(sizing_capital) else 0.0
    ceiling = capital * limits.max_order_notional_pct * ORDER_HEADROOM
    if not _positive(risk_pct):
        return ceiling
    return min(ceiling, capital * limits.risk_per_trade_pct / float(risk_pct))


def vol_throttle(closes_4h: Any, ratio: float) -> float:
    """``THROTTLE_FACTOR`` when the volatility of the last seven days of closed 4h returns
    exceeds ``ratio`` times that of every closed 4h return given, else 1.

    1 when the throttle is off (``ratio`` 0), with fewer than two weeks of returns, or a
    flat history. Closes that are not positive finite numbers are skipped.
    """
    if not _positive(ratio):
        return 1.0
    closes = np.asarray(closes_4h, dtype=float).ravel()
    closes = closes[np.isfinite(closes) & (closes > 0)]
    returns = closes[1:] / closes[:-1] - 1
    if len(returns) < 2 * THROTTLE_WINDOW_BARS:
        return 1.0
    long_run = float(np.std(returns, ddof=1))
    recent = float(np.std(returns[-THROTTLE_WINDOW_BARS:], ddof=1))
    if not (math.isfinite(long_run) and long_run > 0 and math.isfinite(recent)):
        return 1.0
    return THROTTLE_FACTOR if recent > ratio * long_run else 1.0


def portfolio_vol(
    weights_usd: Mapping[str, float],
    returns_by_symbol: Mapping[str, Sequence[float]],
    *,
    periods_per_year: int = HOURS_PER_YEAR,
    halflife: float = VOL_HALFLIFE_HOURS,
) -> float | None:
    """Annualised standard deviation, in USD, of a book of positions worth ``weights_usd``.

    The covariance is an EWMA (zero mean, as RiskMetrics) of the hourly returns the
    symbols share, aligned on their last values. 0 for an empty book; None when a
    symbol has no returns or fewer than ``MIN_VOL_RETURNS`` aligned finite ones remain.
    """
    book = _book(weights_usd)
    if not book:
        return 0.0
    symbols = list(book)
    covariance = _covariance(symbols, returns_by_symbol, periods_per_year=periods_per_year, halflife=halflife)
    if covariance is None:
        return None
    weights = np.array([book[symbol] for symbol in symbols])
    return math.sqrt(max(0.0, float(weights @ covariance @ weights)))


def vol_target_cap(
    limits: RiskLimits,
    sizing_capital: float,
    held_usd: Mapping[str, float],
    symbol: str,
    size_usd: float,
    returns_by_symbol: Mapping[str, Sequence[float]],
) -> float | None:
    """The total exposure, in USD, at which a new order in ``symbol`` brings the book's
    annualised volatility (``portfolio_vol``) to ``target_vol_annual`` of the sizing capital.

    It is the held exposure plus the largest order ``x ≥ 0`` with ``σ(held + x) ≤
    target·C``, solved exactly on the covariance: ``σ²(x)`` is a parabola, and the room
    is its larger root. It only limits new entries; nothing held is sold. A book already
    above the target gets room only from a hedge, a pair whose covariance with the book
    is negative: such an order lowers ``σ`` first, and the room runs up to the size at
    which ``σ`` climbs back to the target, so any order within it leaves the book below
    its current ``σ``. Without a hedge, or when no size brings ``σ`` down to the target,
    a book above the target gets no room. None when it does not bind: the target is off,
    the history is short (``portfolio_vol``), the pair carries no volatility, or the
    planned ``size_usd`` keeps the book within the target.
    """
    if not _positive(limits.target_vol_annual) or not _positive(sizing_capital):
        return None
    book = _book(held_usd)
    symbols = [symbol, *(other for other in book if other != symbol)]
    covariance = _covariance(symbols, returns_by_symbol, periods_per_year=HOURS_PER_YEAR, halflife=VOL_HALFLIFE_HOURS)
    if covariance is None:
        return None
    held = np.array([book.get(name, 0.0) for name in symbols])
    # σ²(x) = a·x² + 2·b·x + c for an order of x USD in the pair.
    a = float(covariance[0, 0])
    b = float(covariance[0] @ held)
    c = float(held @ covariance @ held)
    limit = (limits.target_vol_annual * float(sizing_capital)) ** 2
    if not a > 0:
        return None
    planned = float(size_usd) if _positive(size_usd) else 0.0
    if a * planned * planned + 2 * b * planned + c <= limit:
        return None
    discriminant = b * b - a * (c - limit)
    room = max(0.0, (-b + math.sqrt(discriminant)) / a) if discriminant >= 0 else 0.0
    return sum(book.values()) + room


def open_risk(positions: list[dict[str, Any]], *, pending_risk: float) -> float:
    """Loss to the stop of every live position.

    An open position risks ``qty × (average − stop)``, zero once a trailing stop
    passed its average. A pending entry has no fill yet and counts as one full
    trade's risk. A ``reserved`` row is what a DCA or grid instance may still
    lose beyond its current position, counted from its creation.
    """
    total = 0.0
    for row in positions:
        if row.get("status") == "reserved":
            total += float(row.get("reserved_risk") or 0)
            continue
        if row.get("status") == "pending":
            total += pending_risk
            continue
        stop = row.get("stop_price")
        if stop is None:
            total += pending_risk
            continue
        total += max(0.0, float(row.get("qty") or 0) * (float(row.get("avg_price") or 0) - float(stop)))
    return total


def allocate(
    rows: list[Opportunity],
    *,
    cash: float | None,
    live: list[dict[str, Any]],
    capital: float,
    limits: RiskLimits,
    min_notional: Callable[[str], float | None],
    min_profit_usd: float = 0.0,
    returns_by_symbol: Mapping[str, Sequence[float]] | None = None,
    max_entries: int | None = None,
) -> list[Opportunity]:
    """Share the free balance among ready rows, best score first.

    A row is accepted when its pair holds no live position in any strategy,
    a position slot is free, the open risk stays within ``max_open_risk_pct``
    of ``capital`` and its size fits the cash left. When the cash covers only
    part of the size, the order shrinks to the cash if that is still above the
    pair's minimum notional (``size_limit`` becomes ``cash``); a size below a known
    minimum notional is refused. A row whose expected profit at the target, net of
    costs and at the size it gets, is below ``min_profit_usd`` is refused too.
    A refused row becomes ``bloqueada`` with the reason. Sizes only shrink.
    ``max_entries`` lowers the entries per cycle (the cautious drawdown level).

    With ``returns_by_symbol`` (BTC/USDT included), the pairs held and the rows
    accepted in this cycle count together: a row is refused when ``max_correlated``
    of them move with it above ``correlation_threshold``, and the BTC cluster (BTC and
    every pair with |ρ| to it above ``cluster_corr``, reported in ``row.cluster``) may
    risk at most half of ``max_open_risk_pct`` to the stops. Without it neither applies.
    """
    accepted: list[Opportunity] = []
    ordered = sorted(rows, key=lambda item: item.score, reverse=True)
    if cash is None:
        for row in ordered:
            _refuse(row, "saldo em USDT indisponível — nenhuma entrada até a leitura voltar")
        return accepted
    budget = max(0.0, cash) * CASH_BUFFER / (1 + SPOT_TAKER)
    held = {str(row["symbol"]) for row in live}
    # One position per pair; a DCA or grid instance reserves its pair even with no position.
    slots = limits.max_positions - len(held)
    pending_risk = capital * limits.risk_per_trade_pct
    risk = open_risk(live, pending_risk=pending_risk)
    risk_cap = capital * limits.max_open_risk_pct
    per_cycle = limits.max_entries_per_cycle
    if max_entries is not None:
        per_cycle = max(0, min(per_cycle, max_entries))
    in_cluster = _cluster_of(returns_by_symbol, limits.cluster_corr) if returns_by_symbol is not None else None
    cluster_cap = risk_cap / 2
    cluster_risk = 0.0
    if in_cluster is not None:
        cluster_risk = open_risk([row for row in live if in_cluster(str(row["symbol"]))], pending_risk=pending_risk)
        for row in ordered:
            row.cluster = CLUSTER_NAME if in_cluster(row.symbol) else None
    floors: dict[str, float | None] = {}

    def floor_of(symbol: str) -> float | None:
        if symbol not in floors:
            floors[symbol] = min_notional(symbol)
        return floors[symbol]

    for row in ordered:
        if len(accepted) >= per_cycle:
            _refuse(row, _per_cycle_reason(per_cycle, lowered=per_cycle < limits.max_entries_per_cycle))
            continue
        if row.symbol in held:
            _refuse(row, "ativo já em carteira: uma posição por par")
            continue
        if slots <= 0:
            _refuse(row, "posições simultâneas no máximo")
            continue
        if returns_by_symbol is not None and _correlated(row.symbol, held, returns_by_symbol, limits) >= limits.max_correlated:
            _refuse(
                row,
                f"já há {limits.max_correlated} posições correlacionadas acima de {limits.correlation_threshold:.2f}, "
                "contando as entradas deste ciclo",
            )
            continue
        size = row.size_usd
        shrunk = False
        if budget < size:
            floor = floor_of(row.symbol) if budget > 0 else None
            if floor is None or budget < floor:
                _refuse(row, f"saldo insuficiente: {budget:.2f} USDT livres para novas entradas")
                continue
            size, shrunk = budget, True
        else:
            floor = floor_of(row.symbol)
            if floor is not None and size < floor:
                _refuse(row, f"ordem de US$ {size:.2f} abaixo do mínimo de US$ {floor:.2f} do par")
                continue
        if risk + size * row.risk_pct > risk_cap:
            _refuse(row, f"risco da carteira no limite de {limits.max_open_risk_pct:.0%} do capital até os stops")
            continue
        member = in_cluster is not None and in_cluster(row.symbol)
        if member and cluster_risk + size * row.risk_pct > cluster_cap:
            _refuse(
                row,
                f"risco do cluster {CLUSTER_NAME} no limite de {_pct(limits.max_open_risk_pct / 2)} do capital até os stops",
            )
            continue
        expected = size * row.net_return
        if min_profit_usd > 0 and expected < min_profit_usd:
            _refuse(row, f"lucro esperado no alvo de US$ {expected:.2f}, abaixo do mínimo de US$ {min_profit_usd:.2f}")
            continue
        row.size_usd = round(size, 2)
        if shrunk:
            row.size_limit = "cash"
            row.size_caps = {**(row.size_caps or {}), "cash": round(size, 2)}
        budget -= size
        risk += size * row.risk_pct
        if member:
            cluster_risk += size * row.risk_pct
        slots -= 1
        held.add(row.symbol)
        accepted.append(row)
    return accepted


def protect(
    rows: list[Opportunity],
    losses: list[dict[str, Any]],
    limits: RiskLimits,
    now: datetime,
) -> None:
    """Refuse ready rows after recent losses, as Freqtrade's StoplossGuard and CooldownPeriod.

    ``losses`` are losing closes (``symbol``, ``closed_at``). With at least
    ``loss_guard_trades`` of them in the last six hours, every entry waits; the
    pause ends by itself as they age out. A pair that closed at a loss within
    ``pair_cooldown_minutes`` gets no new entry.
    """
    closes = [(str(row["symbol"]), _at(row["closed_at"])) for row in losses]
    recent = [moment for _symbol, moment in closes if moment >= now - LOSS_GUARD_WINDOW]
    guard = limits.loss_guard_trades > 0 and len(recent) >= limits.loss_guard_trades
    cooldown = timedelta(minutes=limits.pair_cooldown_minutes)
    for row in rows:
        if row.status != "pronta":
            continue
        if guard:
            until = sorted(recent)[-limits.loss_guard_trades] + LOSS_GUARD_WINDOW
            _refuse(row, f"{len(recent)} perdas nas últimas 6 h: entradas pausadas até {until:%H:%M} UTC")
            continue
        if cooldown:
            last = max((moment for symbol, moment in closes if symbol == row.symbol), default=None)
            if last is not None and now < last + cooldown:
                _refuse(row, f"par em pausa após uma perda, até {last + cooldown:%H:%M} UTC")


def _cluster_of(returns_by_symbol: Mapping[str, Sequence[float]], threshold: float) -> Callable[[str], bool]:
    """Membership of the BTC cluster, memoised for one allocation."""
    anchor = list(returns_by_symbol.get(CLUSTER_ANCHOR) or [])
    known: dict[str, bool] = {}

    def member(symbol: str) -> bool:
        if symbol not in known:
            own = list(returns_by_symbol.get(symbol) or [])
            known[symbol] = symbol.split("/", 1)[0] == CLUSTER_NAME or abs(correlation(own, anchor)) > threshold
        return known[symbol]

    return member


def _correlated(symbol: str, held: set[str], returns_by_symbol: Mapping[str, Sequence[float]], limits: RiskLimits) -> int:
    """How many held pairs move with ``symbol`` above ``correlation_threshold``."""
    own = list(returns_by_symbol.get(symbol) or [])
    return sum(
        1
        for other in held
        if other != symbol
        and abs(correlation(own, list(returns_by_symbol.get(other) or []))) > limits.correlation_threshold
    )


def _per_cycle_reason(cap: int, *, lowered: bool) -> str:
    if cap <= 0:
        return "nenhuma entrada neste ciclo pelo nível de risco da carteira"
    noun = "entrada" if cap == 1 else "entradas"
    mode = " no modo cauteloso" if lowered else ""
    return f"limite de {cap} {noun} por ciclo{mode}; volta no próximo"


def _covariance(
    symbols: Sequence[str],
    returns_by_symbol: Mapping[str, Sequence[float]],
    *,
    periods_per_year: float,
    halflife: float,
) -> np.ndarray | None:
    """Annualised EWMA covariance of the symbols' last aligned hourly returns, or None."""
    if not (periods_per_year > 0 and halflife > 0):
        raise ValueError("periods_per_year and halflife must be positive")
    series: list[np.ndarray] = []
    for symbol in symbols:
        values = returns_by_symbol.get(symbol)
        if values is None:
            return None
        try:
            series.append(np.asarray(values, dtype=float).ravel())
        except (TypeError, ValueError):
            return None
    length = min((len(values) for values in series), default=0)
    if length < MIN_VOL_RETURNS:
        return None
    matrix = np.column_stack([values[-length:] for values in series])
    # An hour any series cannot read is dropped for all, so the rows stay aligned.
    matrix = matrix[np.isfinite(matrix).all(axis=1)]
    if len(matrix) < MIN_VOL_RETURNS:
        return None
    weights = 0.5 ** (np.arange(len(matrix) - 1, -1, -1, dtype=float) / halflife)
    weights /= weights.sum()
    covariance: np.ndarray = (matrix * weights[:, None]).T @ matrix * periods_per_year
    return covariance


def _book(weights_usd: Mapping[str, float]) -> dict[str, float]:
    """The finite, non-zero positions of a book."""
    book: dict[str, float] = {}
    for symbol, value in weights_usd.items():
        if _finite(value) and float(value) != 0:
            book[str(symbol)] = book.get(str(symbol), 0.0) + float(value)
    return book


def _cap_value(value: Any) -> float | None:
    """A USD cap as a float: None or +inf is no cap; NaN, a negative or unreadable value allows nothing."""
    if value is None:
        return None
    try:
        cap = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(cap) or cap < 0:
        return 0.0
    return None if math.isinf(cap) else cap


def _throttle_factor(throttle: float) -> float:
    """A throttle only shrinks: within [0, 1], and 1 when unreadable."""
    if not _finite(throttle):
        return 1.0
    return min(1.0, max(0.0, float(throttle)))


def _level_of(previous: RiskState | Mapping[str, Any] | str | None) -> str | None:
    if previous is None:
        return None
    if isinstance(previous, RiskState):
        return previous.level
    if isinstance(previous, Mapping):
        level = previous.get("level")
        return str(level) if level is not None else None
    return str(previous)


def _daily_loss_reason(limits: RiskLimits) -> str:
    return f"perda diária de {limits.max_daily_loss_pct:.0%} — pausa até o próximo dia UTC"


def _pct(value: float) -> str:
    """A share as a percentage with at most one decimal: 0.05 → 5%, 0.075 → 7.5%."""
    return f"{value:.1%}".replace(".0%", "%")


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _at(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _refuse(row: Opportunity, reason: str) -> None:
    row.status = "bloqueada"
    row.blocker = "blocked"
    row.reason = reason
    row.size_usd = 0.0


def utc_day(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

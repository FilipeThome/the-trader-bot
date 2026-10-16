"""Position sizing and the bot's own limits. A failed check means no trade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

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


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str = ""
    size_usd: float = 0.0
    pause_day: bool = False
    halt: bool = False


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
    return float(num / (den_a * den_b))


class RiskManager:
    """``capital`` is the configured capital: equity, daily loss and drawdown are measured on it.

    ``sizing_capital`` (at most ``capital``) is what the account can fund: order
    size and the exposure limit use it, so a smaller balance shrinks new
    orders without reading as a loss.
    """

    def __init__(self, limits: RiskLimits, capital_usd: float, sizing_capital: float | None = None) -> None:
        self.limits = limits
        self.capital = capital_usd
        self.sizing_capital = capital_usd if sizing_capital is None else max(0.0, min(capital_usd, sizing_capital))

    def size_for(self, opportunity: Opportunity) -> float:
        """Size by the stop distance so the loss at the stop is the risk share, capped at the order ceiling less headroom.

        The risk share is ``risk_per_trade_pct``, lowered to ``kelly_fraction`` of
        full Kelly when the row's hit rate is known (``kelly_risk``).
        """
        ceiling = self.sizing_capital * self.limits.max_order_notional_pct * ORDER_HEADROOM
        if opportunity.risk_pct <= 0:
            return 0.0
        share = self.limits.risk_per_trade_pct
        kelly = self.kelly_risk(opportunity)
        if kelly is not None:
            share = min(share, self.limits.kelly_fraction * kelly)
        if share <= 0:
            return 0.0
        by_stop = (self.sizing_capital * share) / opportunity.risk_pct
        return max(0.0, min(ceiling, by_stop))

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

    def assess(
        self,
        opportunity: Opportunity,
        *,
        open_positions: list[dict[str, Any]],
        exposure_usd: float,
        day_pnl: float,
        peak_equity: float,
        equity: float,
        trades_today: int,
        returns_by_symbol: dict[str, list[float]],
        stale: bool,
        signal_only: bool,
    ) -> RiskVerdict:
        if stale:
            return RiskVerdict(False, "OHLCV com mais de 2 intervalos de atraso")
        if signal_only:
            return RiskVerdict(False, "estratégia em somente sinal: sem validação no histórico ou rebaixada")
        if day_pnl <= -self.capital * self.limits.max_daily_loss_pct:
            return RiskVerdict(
                False,
                f"perda diária de {self.limits.max_daily_loss_pct:.0%} — pausa até o próximo dia UTC",
                pause_day=True,
            )
        if peak_equity > 0 and equity <= peak_equity * (1 - self.limits.max_drawdown_pct):
            return RiskVerdict(False, f"drawdown de {self.limits.max_drawdown_pct:.0%} desde o pico", halt=True)
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
        size = self.size_for(opportunity)
        if size <= 0:
            return RiskVerdict(False, "tamanho de ordem zerado pelo teto de nocional")
        if exposure_usd + size > self.sizing_capital * self.limits.max_exposure_pct:
            return RiskVerdict(
                False,
                f"exposição total já em {exposure_usd / self.sizing_capital:.0%} do limite de {self.limits.max_exposure_pct:.0%}",
            )
        if self._too_correlated(opportunity.symbol, open_positions, returns_by_symbol):
            return RiskVerdict(
                False,
                f"já há {self.limits.max_correlated} posições correlacionadas acima de {self.limits.correlation_threshold:.2f}",
            )
        return RiskVerdict(True, size_usd=round(size, 2))

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


def open_risk(positions: list[dict[str, Any]], *, pending_risk: float) -> float:
    """Loss to the stop of every live position.

    An open position risks ``qty × (average − stop)``, zero once a trailing stop
    passed its average. A pending entry has no fill yet and counts as one full
    trade's risk.
    """
    total = 0.0
    for row in positions:
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
) -> list[Opportunity]:
    """Share the free balance among ready rows, best score first.

    A row is accepted when its pair holds no live position in any strategy,
    a position slot is free, the open risk stays within ``max_open_risk_pct``
    of ``capital`` and its size fits the cash left. When the cash covers only
    part of the size, the order shrinks to the cash if that is still above the
    pair's minimum notional. A row whose expected profit at the target, net of
    costs and at the size it gets, is below ``min_profit_usd`` is refused too.
    A refused row becomes ``bloqueada`` with the reason. Sizes only shrink.
    ``min_notional`` is called only to shrink.
    """
    accepted: list[Opportunity] = []
    ordered = sorted(rows, key=lambda item: item.score, reverse=True)
    if cash is None:
        for row in ordered:
            _refuse(row, "saldo em USDT indisponível — nenhuma entrada até a leitura voltar")
        return accepted
    budget = max(0.0, cash) * CASH_BUFFER / (1 + SPOT_TAKER)
    held = {str(row["symbol"]) for row in live}
    slots = limits.max_positions - len(live)
    risk = open_risk(live, pending_risk=capital * limits.risk_per_trade_pct)
    risk_cap = capital * limits.max_open_risk_pct
    for row in ordered:
        if len(accepted) >= limits.max_entries_per_cycle:
            _refuse(row, f"limite de {limits.max_entries_per_cycle} entradas por ciclo; volta no próximo")
            continue
        if row.symbol in held:
            _refuse(row, "ativo já em carteira: uma posição por par")
            continue
        if slots <= 0:
            _refuse(row, "posições simultâneas no máximo")
            continue
        size = row.size_usd
        if budget < size:
            floor = min_notional(row.symbol) if budget > 0 else None
            if floor is None or budget < floor:
                _refuse(row, f"saldo insuficiente: {budget:.2f} USDT livres para novas entradas")
                continue
            size = budget
        if risk + size * row.risk_pct > risk_cap:
            _refuse(row, f"risco da carteira no limite de {limits.max_open_risk_pct:.0%} do capital até os stops")
            continue
        expected = size * row.net_return
        if min_profit_usd > 0 and expected < min_profit_usd:
            _refuse(row, f"lucro esperado no alvo de US$ {expected:.2f}, abaixo do mínimo de US$ {min_profit_usd:.2f}")
            continue
        row.size_usd = round(size, 2)
        budget -= size
        risk += size * row.risk_pct
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

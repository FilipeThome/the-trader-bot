"""Position sizing and the bot's own limits. A failed check means no trade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import RiskLimits
from bot.evaluator import Opportunity


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
    den_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
    den_b = sum((y - mean_b) ** 2 for y in b) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


class RiskManager:
    def __init__(self, limits: RiskLimits, capital_usd: float) -> None:
        self.limits = limits
        self.capital = capital_usd

    def size_for(self, opportunity: Opportunity) -> float:
        """Risk the stop distance so the loss at the stop is about 1% of capital, capped at the order ceiling."""
        ceiling = self.capital * self.limits.max_order_notional_pct
        if opportunity.risk_pct <= 0:
            return 0.0
        by_stop = (self.capital * 0.01) / opportunity.risk_pct
        return max(0.0, min(ceiling, by_stop))

    def assess(
        self,
        opportunity: Opportunity,
        *,
        open_positions: list[dict],
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
            return RiskVerdict(False, "estratégia rebaixada para somente sinal")
        if day_pnl <= -self.capital * self.limits.max_daily_loss_pct:
            return RiskVerdict(False, "perda diária de 3% — pausa até o próximo dia UTC", pause_day=True)
        if peak_equity > 0 and equity <= peak_equity * (1 - self.limits.max_drawdown_pct):
            return RiskVerdict(False, "drawdown de 10% desde o pico", halt=True)
        if trades_today >= self.limits.max_trades_per_day:
            return RiskVerdict(False, "limite de trades por dia")
        if len(open_positions) >= self.limits.max_positions and opportunity.status != "em_posicao":
            return RiskVerdict(False, "posições simultâneas no máximo")
        size = self.size_for(opportunity)
        if size <= 0:
            return RiskVerdict(False, "tamanho de ordem zerado pelo teto de nocional")
        if exposure_usd + size > self.capital * self.limits.max_exposure_pct:
            return RiskVerdict(
                False,
                f"exposição total já em {exposure_usd / self.capital:.0%} do limite de {self.limits.max_exposure_pct:.0%}",
            )
        if self._too_correlated(opportunity.symbol, open_positions, returns_by_symbol):
            return RiskVerdict(False, "já há 2 posições correlacionadas acima de 0,8")
        if opportunity.direction < 0 and opportunity.strategy != "funding_carry":
            leverage = size / self.capital if self.capital else 99
            if leverage > self.limits.max_leverage:
                return RiskVerdict(False, "alavancagem acima do mandato")
        return RiskVerdict(True, size_usd=round(size, 2))

    def _too_correlated(
        self,
        symbol: str,
        open_positions: list[dict],
        returns_by_symbol: dict[str, list[float]],
    ) -> bool:
        own = returns_by_symbol.get(symbol) or []
        correlated = 0
        for position in open_positions:
            other = position.get("symbol")
            if other == symbol:
                continue
            if abs(correlation(own, returns_by_symbol.get(other) or [])) > self.limits.correlation_threshold:
                correlated += 1
        return correlated >= self.limits.max_correlated


def utc_day(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

"""Funding carry: long spot and short USD-M when funding stays rich.

Futures writes are disabled in v1, so the setup is scored and shown, and the
evaluator marks it as not executable. The spot leg is never sent alone.
"""

from __future__ import annotations

import pandas as pd

from bot.strategies.base import Setup


class FundingCarry:
    id = "funding_carry"
    timeframes = ("8h",)
    markets = ("spot", "usdm")
    style = "swing"
    history_days = 180

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        frame: pd.DataFrame,
        *,
        funding_rates: list[float] | None = None,
        threshold: float = 0.10,
    ) -> Setup | None:
        rates = list(funding_rates or [])
        if len(rates) < 3:
            return None
        last = rates[-3:]
        if not all(rate >= threshold for rate in last):
            return None
        # The rates are annualised: their mean over 365 is the carry of one day, not of one 8h period.
        daily_return = sum(last) / 3 / 365
        return Setup(
            strategy_id=self.id,
            symbol=symbol,
            timeframe="8h",
            action="Comprar spot + vender futuro",
            direction=1.0,
            expected_return=max(daily_return, 0.001),
            sigma=0.01,
            stop_pct=0.002,
            target_pct=max(daily_return, 0.001),
            reason=(
                "Funding anualizado acima do limiar há 3 períodos; "
                f"retorno estimado de {daily_return:.3%} ao dia"
            ),
            regime="funding",
            market="usdm",
            confidence=1.0,
            extra={"funding_rates": last, "futures_enabled": False, "return_horizon": "1d"},
        )

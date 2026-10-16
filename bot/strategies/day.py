"""Rules shared by the day-trade strategies, the live exits and the backtest.

A day trade enters on a closed 15m bar only while the closed 1h bars trend
up, and only outside the quiet hours. Half the position is sold at ``tp1_r``
times the risk; the stop of the other half then moves to the entry plus a
buffer for the fees, and whatever is left leaves at ``max_hold_minutes``.
The values are constants, not operator settings: a validation is valid only
for the rules it replayed, and they are recorded with it (``DAY.params``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from bot.indicators import ema

#: The entry bar of every day strategy.
ENTRY_TIMEFRAME = "15m"
ENTRY_BAR = timedelta(minutes=15)
#: Closed 1h bars needed for the EMA 50 trend filter.
MIN_HOURLY_BARS = 60


@dataclass(frozen=True)
class DayRules:
    #: Stop distance: at least this share of the price, so the round trip
    #: (about 0.25%) costs at most a quarter of the risk.
    min_stop_pct: float = 0.01
    stop_atr: float = 1.5
    #: The first target, in multiples of the risk; ``tp1_fraction`` of the position is sold there.
    tp1_r: float = 1.5
    tp1_fraction: float = 0.5
    #: After the first target, the stop of the rest sits this far above the entry.
    breakeven_buffer: float = 0.002
    max_hold_minutes: int = 360
    #: UTC hours [start, end) without new entries: the thinnest books of the day.
    quiet_start_hour: int = 3
    quiet_end_hour: int = 7

    @property
    def params(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def max_hold(self) -> timedelta:
        return timedelta(minutes=self.max_hold_minutes)


DAY = DayRules()


def in_quiet_hours(decision_time: datetime | pd.Timestamp, rules: DayRules = DAY) -> bool:
    """True when ``decision_time`` (the close of the entry bar) falls in the quiet hours."""
    return rules.quiet_start_hour <= decision_time.hour < rules.quiet_end_hour


def hourly_trend(hourly: pd.DataFrame | None) -> float | None:
    """How far EMA 20 sits above EMA 50 on the closed 1h bars, or None when the 1h does not trend up.

    Up means EMA 20 above EMA 50 and the last close above EMA 50.
    """
    if hourly is None or len(hourly) < MIN_HOURLY_BARS:
        return None
    close = hourly["close"]
    fast = float(ema(close, 20).iloc[-1])
    slow = float(ema(close, 50).iloc[-1])
    if slow <= 0 or fast <= slow or float(close.iloc[-1]) <= slow:
        return None
    return fast / slow - 1


def closed_hourly(hourly: pd.DataFrame, decided_at: pd.Timestamp, window: int) -> pd.DataFrame:
    """The 1h bars already closed when a 15m bar closes at ``decided_at``: no look-ahead."""
    closed = hourly[hourly.index + pd.Timedelta(hours=1) <= decided_at]
    return closed.iloc[-window:]

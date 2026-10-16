"""Process-wide bot handles. Tests replace the collector and the store root."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from bot.collector import Collector
from bot.config import BotConfig
from bot.runtime import Runtime
from bot.store import Store


class BotContext:
    def __init__(self) -> None:
        self.config: BotConfig | None = None
        self.runtime: Runtime | None = None
        self.collector: Collector | None = None
        self.token: str | None = None
        #: (profile id, read at, response) of the last balance read.
        self.balance_cache: tuple[str, datetime, dict[str, Any]] | None = None
        #: One lock for the whole process: the tick, manual orders, halt and mode switch.
        self.lock = threading.RLock()
        #: Background validation jobs by (mode, strategy). In memory: a restart forgets them.
        self.validations: dict[tuple[str, str], dict[str, Any]] = {}
        self.validation_lock = threading.Lock()
        #: Trade-chart candles by (mode, symbol, interval, start ms): (read at, candles).
        self.chart_cache: dict[tuple[str, str, str, int], tuple[float, list[dict[str, Any]]]] = {}
        self.chart_lock = threading.Lock()

    def store_for(self, config: BotConfig) -> Store:
        from bot.runtime import mode_root

        return Store(mode_root(config) / "bot.sqlite")

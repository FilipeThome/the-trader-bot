"""Process-wide bot handles. Tests replace the collector and the store root."""

from __future__ import annotations

from bot.collector import Collector
from bot.config import BotConfig
from bot.runtime import Runtime
from bot.store import Store


class BotContext:
    def __init__(self) -> None:
        self.config: BotConfig | None = None
        self.runtime: Runtime | None = None
        self.collector: Collector | None = None

    def store_for(self, config: BotConfig) -> Store:
        from bot.runtime import mode_root

        return Store(mode_root(config) / "bot.sqlite")

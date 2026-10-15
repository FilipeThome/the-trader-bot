"""In-process fan-out for server-sent events."""

from __future__ import annotations

import asyncio
import json
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []
        self.latest: dict[str, Any] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.latest = event
        payload = json.dumps(event, default=str)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                continue

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

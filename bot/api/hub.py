"""In-process fan-out for server-sent events.

``publish`` runs on the tick thread while each subscriber's queue belongs to
the event loop that serves it. asyncio queues are not thread-safe, so the put
is handed to that loop with ``call_soon_threadsafe``, which also wakes it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []
        self.latest: dict[str, Any] | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.latest = event
        payload = json.dumps(event, default=str)
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, payload)
            except RuntimeError:
                # The loop is closed: that subscriber is gone.
                self.unsubscribe(queue)

    def subscribe(self) -> asyncio.Queue[str]:
        """Called from the event loop that will read the queue."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item[1] is not queue]


def _offer(queue: asyncio.Queue[str], payload: str) -> None:
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        # A slow reader misses events; the next one carries the full state.
        pass

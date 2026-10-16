from __future__ import annotations

import asyncio
import threading

from bot.api.hub import EventHub


def test_an_event_published_from_another_thread_wakes_the_reader() -> None:
    hub = EventHub()

    async def scenario() -> str:
        queue = hub.subscribe()
        threading.Thread(target=hub.publish, args=({"type": "tick", "n": 1},)).start()
        return await asyncio.wait_for(queue.get(), timeout=2)

    assert '"n": 1' in asyncio.run(scenario())


def test_a_subscriber_whose_loop_closed_is_dropped() -> None:
    hub = EventHub()

    async def subscribe() -> None:
        hub.subscribe()

    asyncio.run(subscribe())
    hub.publish({"type": "tick"})
    assert hub._subscribers == []

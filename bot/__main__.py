"""Run the API and a tick loop. ``python -m bot``."""

from __future__ import annotations

import threading
import time

import uvicorn

from bot.api.app import _runtime, app
from bot.config import BotConfig


def _loop() -> None:
    while True:
        try:
            runtime = _runtime()
            if runtime.running:
                runtime.tick()
        except Exception:
            # The tick already fail-closes. Keep the API up.
            pass
        time.sleep(max(5, BotConfig.load().tick_seconds))


def main() -> None:
    threading.Thread(target=_loop, name="bot-tick", daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()

"""Run the API and a tick loop. ``python -m bot``."""

from __future__ import annotations

import logging
import os
import threading
import time

import uvicorn

from bot.api.app import _runtime, app
from bot.config import BotConfig
from bot.runtime import MAX_FAILURES

logger = logging.getLogger("bot")
_DEFAULT_INTERVAL = 60


def run_once(failures: int) -> tuple[int, int]:
    """Run one loop step. Returns the failure streak and the next sleep in seconds.

    The cycle counts and halts on its own failures. This step covers what
    happens outside it (reading ``bot.json``, building the runtime) and halts
    after ``MAX_FAILURES`` failures in a row.
    """
    interval = _DEFAULT_INTERVAL
    try:
        config = BotConfig.load()
        interval = max(5, config.tick_seconds)
        runtime = _runtime()
        if runtime.running:
            runtime.tick()
        return 0, interval
    except Exception:
        failures += 1
        logger.exception("tick loop failed (%s in a row)", failures)
        if failures >= MAX_FAILURES:
            try:
                _runtime().halt(f"{MAX_FAILURES} falhas seguidas no laço do processo")
            except Exception:
                logger.exception("halt after loop failures failed")
        return failures, interval


def _loop() -> None:
    failures = 0
    while True:
        failures, interval = run_once(failures)
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=_loop, name="bot-tick", daemon=True).start()
    # 127.0.0.1 outside a container. The Host-header guard (bot.api.auth) still applies on 0.0.0.0.
    uvicorn.run(app, host=os.environ.get("BOT_API_HOST", "127.0.0.1"), port=8000)


if __name__ == "__main__":
    main()

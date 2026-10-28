"""Run the API and a tick loop. ``python -m bot``."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import uvicorn

from bot.api.app import _runtime, app
from bot.api.validation import revalidate_stale
from bot.config import BotConfig
from bot.logging_setup import UVICORN_LOGGERS, install_redaction
from bot.runtime import MAX_FAILURES, Runtime

logger = logging.getLogger("bot")
_DEFAULT_INTERVAL = 60
#: uvicorn's exit code for a server that did not start (``uvicorn.main.STARTUP_FAILURE``, not exported).
_STARTUP_FAILURE = 3


def run_once(failures: int) -> tuple[int, int]:
    """Run one loop step. Returns the failure streak and the next sleep in seconds.

    The cycle counts and halts on its own failures. This step covers what
    happens outside it (reading ``bot.json``, building the runtime) and halts
    after ``MAX_FAILURES`` failures in a row. After the cycle, stale
    validations are re-queued (``_revalidate``), outside that count.
    """
    interval = _DEFAULT_INTERVAL
    try:
        config = BotConfig.load()
        interval = max(5, config.tick_seconds)
        runtime = _runtime()
        if runtime.running:
            runtime.tick()
        _revalidate(runtime)
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


def _revalidate(runtime: Runtime) -> None:
    """Queue the re-validation of stale strategies; a failure is a warning, never a loop failure."""
    try:
        revalidate_stale(runtime)
    except Exception:  # noqa: BLE001 - trading goes on; the next step tries again
        logger.warning("stale validation check failed", exc_info=True)


def _loop() -> None:
    failures = 0
    while True:
        failures, interval = run_once(failures)
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_redaction()
    # 127.0.0.1 outside a container. The Host-header guard (bot.api.auth) still applies on 0.0.0.0.
    # uvicorn installs its own handlers when its Config is built, so the redaction wraps them
    # after that and before the server (and the tick loop) can log anything.
    config = uvicorn.Config(app, host=os.environ.get("BOT_API_HOST", "127.0.0.1"), port=8000)
    install_redaction(UVICORN_LOGGERS)
    threading.Thread(target=_loop, name="bot-tick", daemon=True).start()
    server = uvicorn.Server(config)
    server.run()
    if not server.started:
        # As uvicorn.run does: a failed startup exits non-zero, so a supervisor restarts the bot.
        sys.exit(_STARTUP_FAILURE)


if __name__ == "__main__":
    main()

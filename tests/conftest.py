"""Keep ported modules on the environment root unless a test pins a mode."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from bot.exchange.execution.journal import reset_journal_cache
from bot.exchange.sdk import reset_market_cache
from bot.exchange.weight import reset_weight
from bot.paths import pin_mode_root, reset_mode_root


@pytest.fixture(autouse=True)
def _serial_backtest(monkeypatch) -> None:
    """Validations replay in this process; the pool has its own test."""
    monkeypatch.setattr("bot.backtest.runner.MAX_WORKERS", 1)


@pytest.fixture(autouse=True)
def _clear_mode_pin() -> Iterator[None]:
    reset_weight()
    reset_journal_cache()
    reset_market_cache()
    token = pin_mode_root(None)
    yield
    reset_mode_root(token)
    reset_weight()
    reset_journal_cache()


@pytest.fixture
def api_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """The API as the Vite proxy calls it: own Host, install token, panel header."""
    monkeypatch.setenv("BOT_RUNTIME_ROOT", str(tmp_path))
    from bot.api.app import app, ctx
    from bot.api.auth import load_or_create_token

    ctx.runtime = None
    ctx.config = None
    ctx.collector = None
    ctx.balance_cache = None
    token = load_or_create_token()
    headers = {"X-Bot-Token": token, "X-Bot-Client": "panel"}
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=headers) as client:
        yield client
    ctx.runtime = None
    ctx.config = None
    ctx.collector = None

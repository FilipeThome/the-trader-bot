"""Keep ported modules on the environment root unless a test pins a mode."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from fake_venue import venue  # noqa: F401 - the scripted Binance fixture for executor tests
from fastapi.testclient import TestClient

from bot.exchange.execution.journal import reset_journal_cache
from bot.exchange.service import reset_fee_cache
from bot.exchange.sdk import reset_market_cache
from bot.exchange.weight import reset_weight
from bot.paths import pin_mode_root, reset_mode_root


@pytest.fixture(autouse=True)
def _serial_backtest(monkeypatch) -> None:
    """Validations replay in this process; the pool has its own test."""
    monkeypatch.setattr("bot.backtest.runner.MAX_WORKERS", 1)


@pytest.fixture(autouse=True)
def _no_advice_klines(monkeypatch) -> None:
    """The advisor's resolver reads Binance klines for rows outside the snapshot; never in tests."""
    monkeypatch.setattr("bot.advisor.calibration.fetch_closed", lambda *_args: None)


@pytest.fixture(autouse=True)
def _clear_mode_pin() -> Iterator[None]:
    reset_weight()
    reset_journal_cache()
    reset_market_cache()
    # The account-fee cache is process-wide: a runtime tick warms it.
    reset_fee_cache()
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
    _reset_advisor(ctx)
    with ctx.validation_lock:
        ctx.validations.clear()

    class _NoHistory:
        def fetch(self, *_args, **_kwargs):
            raise RuntimeError("no Binance history in tests")

    # A config change starts validations by itself; tests that want one stub the loader.
    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _market: _NoHistory())
    token = load_or_create_token()
    headers = {"X-Bot-Token": token, "X-Bot-Client": "panel"}
    with TestClient(app, base_url="http://127.0.0.1:8000", headers=headers) as client:
        yield client
    ctx.runtime = None
    ctx.config = None
    ctx.collector = None
    _reset_advisor(ctx)


def _reset_advisor(ctx) -> None:
    """The advisor is process-wide like the collector: each API test starts with none."""
    if ctx.advisor is not None:
        ctx.advisor.close()
    ctx.advisor = None

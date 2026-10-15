"""Keep ported modules on the environment root unless a test pins a mode."""

from __future__ import annotations

import pytest

from bot.exchange.weight import reset_weight
from bot.paths import pin_mode_root, reset_mode_root


@pytest.fixture(autouse=True)
def _clear_mode_pin():
    reset_weight()
    token = pin_mode_root(None)
    yield
    reset_mode_root(token)
    reset_weight()

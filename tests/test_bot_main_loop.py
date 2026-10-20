"""The process loop step: the cycle, then the stale-validation check outside the failure count."""

from __future__ import annotations

import logging

import pytest

import bot.__main__ as entry
from bot.config import BotConfig


class _Runtime:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.calls: list[str] = []

    def tick(self) -> dict[str, str]:
        self.calls.append("tick")
        return {"status": "ok"}

    def halt(self, reason: str) -> None:
        self.calls.append(f"halt: {reason}")


@pytest.fixture
def loop(monkeypatch) -> list[object]:
    """Stub the config read; returns the runtimes ``revalidate_stale`` was called with."""
    monkeypatch.setattr(entry.BotConfig, "load", staticmethod(lambda: BotConfig()))
    seen: list[object] = []
    monkeypatch.setattr(entry, "revalidate_stale", lambda runtime: seen.append(runtime) or [])
    return seen


@pytest.mark.parametrize("running", [True, False])
def test_each_step_checks_stale_validations_after_the_tick(monkeypatch, loop, running: bool) -> None:
    runtime = _Runtime(running)
    monkeypatch.setattr(entry, "revalidate_stale", lambda given: runtime.calls.append("revalidate") or loop.append(given) or [])
    monkeypatch.setattr(entry, "_runtime", lambda: runtime)
    failures, interval = entry.run_once(0)
    assert (failures, interval) == (0, max(5, BotConfig().tick_seconds))
    # A stopped bot still re-validates: the backtest sends no order.
    assert runtime.calls == (["tick", "revalidate"] if running else ["revalidate"])
    assert loop == [runtime]


def test_a_failed_revalidation_is_a_warning_not_a_loop_failure(monkeypatch, loop, caplog) -> None:
    runtime = _Runtime(True)

    def broken(_runtime: object) -> list[str]:
        raise RuntimeError("store locked")

    monkeypatch.setattr(entry, "revalidate_stale", broken)
    monkeypatch.setattr(entry, "_runtime", lambda: runtime)
    failures = 0
    with caplog.at_level(logging.WARNING, logger="bot"):
        for _ in range(entry.MAX_FAILURES):
            failures, _interval = entry.run_once(failures)
    assert failures == 0
    assert runtime.calls == ["tick"] * entry.MAX_FAILURES
    assert any("stale validation check failed" in record.getMessage() for record in caplog.records)


def test_a_failed_tick_still_counts_and_skips_the_revalidation(monkeypatch, loop) -> None:
    runtime = _Runtime(True)

    def tick() -> dict[str, str]:
        raise ValueError("bot.json")

    runtime.tick = tick  # type: ignore[method-assign]
    monkeypatch.setattr(entry, "_runtime", lambda: runtime)
    failures = 0
    for _ in range(entry.MAX_FAILURES):
        failures, _interval = entry.run_once(failures)
    assert failures == entry.MAX_FAILURES
    assert runtime.calls == [f"halt: {entry.MAX_FAILURES} falhas seguidas no laço do processo"]
    assert loop == []

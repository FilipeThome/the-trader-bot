from __future__ import annotations

import pytest

from bot.config import BotConfig
from bot.promotion import production_blockers
from bot.store import Store

GATES = {"g1": True, "g2": True, "g3": True, "g4": True}
CHECKS = {"confirmation": "PRODUCAO", "bot_stopped": True, "open_orders": 0, "mandate_ok": True, "keys_ok": True}


def _config(allocated: float, cap: float) -> BotConfig:
    # Still on testnet when the switch is judged: the pilot must use the production capital.
    return BotConfig(allocated_capital_usd=allocated, pilot_max_production_capital_usd=cap, gates=GATES)


def test_a_small_production_capital_skips_only_the_testnet_history(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    assert production_blockers(_config(150, 25), store, **CHECKS) == ([], 25)


def test_a_pilot_above_its_cap_needs_the_30_days(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    reasons, pilot = production_blockers(_config(300, 25), store, **CHECKS)
    assert pilot is None
    assert "são necessários 30 dias de testnet" in reasons and any("piloto" in reason for reason in reasons)


def test_without_a_pilot_cap_the_history_is_required(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    reasons, pilot = production_blockers(_config(150, 0), store, **CHECKS)
    assert pilot is None and reasons == ["são necessários 30 dias de testnet"]


def test_a_pilot_still_needs_every_other_check(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    store.set_state("limit_violations", 1)
    reasons, _pilot = production_blockers(
        _config(150, 25), store, **{**CHECKS, "confirmation": "sim", "keys_ok": False}
    )
    assert "há violações de limite no testnet" in reasons
    assert any("PRODUCAO" in reason for reason in reasons) and any("chaves" in reason for reason in reasons)


def test_30_good_days_make_a_full_promotion_even_with_a_pilot_cap(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    for day in range(1, 31):
        store.record_day(f"2026-09-{day:02d}", net_pnl=1.0, gross_pnl=1.0, fees=0.0)
    assert production_blockers(_config(150, 25), store, **CHECKS) == ([], None)


@pytest.fixture
def pilot_ready(api_client, monkeypatch):
    from bot.api import app as module

    monkeypatch.setattr(module.service, "check_status", lambda _profile: {"ok": True})
    monkeypatch.setattr(module, "mandate_is_restricted", lambda: True)
    config = BotConfig.load().model_copy(
        update={"allocated_capital_usd": 150.0, "pilot_max_production_capital_usd": 25.0, "gates": GATES}
    )
    config.save()
    with module.ctx.lock:
        module._bind(BotConfig.load())
    return api_client


def _rebind_from_file(**fields) -> None:
    from bot.api import app as module

    config = BotConfig.load().model_copy(update=fields)
    config.save()
    with module.ctx.lock:
        module._bind(BotConfig.load())


def test_a_pilot_switch_records_its_cap_and_writes_a_small_mandate(pilot_ready) -> None:
    from bot.live.mandate.store import load_mandate

    response = pilot_ready.put("/bot/config", json={"mode": "production", "confirmation": "PRODUCAO"})
    assert response.status_code == 200, response.text
    status = pilot_ready.get("/bot/status").json()
    assert status["mode"] == "production" and status["pilot_cap_usd"] == 25.0
    mandate = load_mandate("binance")
    assert mandate is not None and mandate.hard_caps.account_funding_usd == pytest.approx(15.0)


def test_a_pilot_never_raises_its_capital(pilot_ready) -> None:
    assert pilot_ready.put("/bot/config", json={"mode": "production", "confirmation": "PRODUCAO"}).status_code == 200
    raised = pilot_ready.put("/bot/config", json={"allocated_capital_usd": 300, "reconfirm_mandate": True})
    assert raised.status_code == 409 and "piloto" in raised.json()["detail"]
    assert pilot_ready.get("/bot/status").json()["allocated_capital_usd"] == 150.0
    # A hand edit of bot.json (cap 0, more capital) cannot loosen the recorded cap.
    _rebind_from_file(allocated_capital_usd=300.0, pilot_max_production_capital_usd=0.0)
    assert pilot_ready.put("/bot/config", json={"reconfirm_mandate": True}).status_code == 409
    assert pilot_ready.post("/bot/presets/day").status_code == 409
    from bot.live.mandate.store import load_mandate

    mandate = load_mandate("binance")
    assert mandate is not None and mandate.hard_caps.account_funding_usd == pytest.approx(15.0)


def test_a_restart_with_a_pilot_capital_above_its_cap_keeps_the_cycle_stopped(pilot_ready) -> None:
    from bot.api.app import ctx

    assert pilot_ready.put("/bot/config", json={"mode": "production", "confirmation": "PRODUCAO"}).status_code == 200
    assert pilot_ready.post("/bot/start").status_code == 200
    _rebind_from_file(allocated_capital_usd=300.0)
    assert ctx.runtime is not None and not ctx.runtime.running

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pytest

from bot.backtest.stats import mc_drawdown_r
from bot.config import BotConfig
from bot.live.audit import LiveActionEvent, write_live_action
from bot.paths import pin_mode_root
from bot.promotion import audit_breaches, audit_ledger_for, production_blockers
from bot.store import Store

GATES = {"g1": True, "g2": True, "g3": True, "g4": True}
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
CHECKS = {
    "confirmation": "PRODUCAO",
    "bot_stopped": True,
    "open_orders": 0,
    "mandate_ok": True,
    "keys_ok": True,
    "now": NOW,
}
NEED_TRADES = "são necessários 30 trades fechados com R registrado nos últimos 30 dias"


def _config(allocated: float, cap: float) -> BotConfig:
    # Still on testnet when the switch is judged: the pilot must use the production capital.
    return BotConfig(allocated_capital_usd=allocated, pilot_max_production_capital_usd=cap, gates=GATES)


def test_skip_gates_waives_only_the_gates_and_only_for_a_pilot(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    config = _config(150, 0)
    config.gates = {"g1": False, "g2": True, "g3": False, "g4": True}
    gates = ["gate G1 não aprovado", "gate G3 não aprovado"]
    # Without the waiver the gates stay with the pilot's cap.
    reasons, pilot = production_blockers(config, store, pilot_cap=25, **CHECKS)
    assert reasons == gates and pilot == 25
    # The waiver needs bot.json's consent: without it the gates stay and the refusal says why.
    reasons, pilot = production_blockers(config, store, pilot_cap=25, skip_gates=True, **CHECKS)
    assert reasons == ["dispensar as liberações G1 a G4 exige pilot_gates_waivable no bot.json", *gates] and pilot == 25
    config.pilot_gates_waivable = True
    # With it, the pilot goes through; the stopped bot is still required.
    assert production_blockers(config, store, pilot_cap=25, skip_gates=True, **CHECKS) == ([], 25)
    reasons, _ = production_blockers(config, store, pilot_cap=25, skip_gates=True, **{**CHECKS, "bot_stopped": False})
    assert reasons == ["bot precisa estar parado"]
    # Over the cap it is no pilot, so the gates come back with the history.
    reasons, pilot = production_blockers(config, store, pilot_cap=10, skip_gates=True, **CHECKS)
    assert pilot is None and all(gate in reasons for gate in gates)
    # A full promotion never waives them.
    _good_days(store)
    _trades(store, [1.0] * 40)
    reasons, pilot = production_blockers(config, store, pilot_cap=25, skip_gates=True, **CHECKS)
    assert reasons == gates and pilot is None


def _good_days(store: Store) -> None:
    for day in range(1, 31):
        store.record_day(f"2026-09-{day:02d}", net_pnl=1.0, gross_pnl=1.0, fees=0.0)


def _trades(store: Store, r: Sequence[float | None], *, days_ago: float = 1.0) -> None:
    for index, value in enumerate(r):
        position_id = store.open_pending("BTC/USDT", "trend")
        assert position_id is not None
        store.update_position(position_id, status="open", qty=1.0, avg_price=100.0, r_multiple=value)
        store.close_position(position_id, exit_price=101.0, realized_pnl=1.0, fees=0.0)
        closed_at = NOW - timedelta(days=days_ago, minutes=index)
        with store._conn:
            store._conn.execute("UPDATE positions SET closed_at=? WHERE id=?", (closed_at.isoformat(), position_id))


def _audit(root, kind: str, ts: datetime, gate: dict | None = None) -> None:
    """One record through the real ledger writer, into ``root``'s ledger."""
    pin_mode_root(root)  # the autouse fixture resets the pin
    write_live_action(
        LiveActionEvent(
            kind=kind,  # type: ignore[arg-type]
            session_id="testnet",
            outcome="blocked",
            server="binance-live-trade-bot",
            gate_decision=gate,
            ts=ts.isoformat(timespec="milliseconds"),
        ),
        chain=False,
    )


def _breach(root, ts: datetime, kind: str = "quantitative") -> None:
    gate = {"allowed": False, "decision": "pause", "limit": "max_order_notional_usd", "kind": kind,
            "limit_value": 100.0, "attempted_value": 140.0}
    _audit(root, "breach", ts, gate)


def test_a_small_production_capital_skips_only_the_testnet_history(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    assert production_blockers(_config(150, 25), store, **CHECKS) == ([], 25)


def test_the_cap_sent_with_the_switch_replaces_the_one_in_bot_json(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    # bot.json has no pilot; the panel's cap makes the switch a pilot at that cap.
    assert production_blockers(_config(150, 0), store, pilot_cap=25, **CHECKS) == ([], 25)
    # The panel's cap also replaces a larger one in bot.json: too small, the history is required.
    reasons, pilot = production_blockers(_config(150, 50), store, pilot_cap=10, **CHECKS)
    assert pilot is None
    assert "são necessários 30 dias de testnet" in reasons
    assert "piloto: capital de produção de US$ 15.00 acima do limite de US$ 10.00" in reasons


def test_a_pilot_above_its_cap_needs_the_30_days(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    reasons, pilot = production_blockers(_config(300, 25), store, **CHECKS)
    assert pilot is None
    assert "são necessários 30 dias de testnet" in reasons and any("piloto" in reason for reason in reasons)


def test_without_a_pilot_cap_the_history_is_required(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    reasons, pilot = production_blockers(_config(150, 0), store, **CHECKS)
    assert pilot is None and reasons == ["são necessários 30 dias de testnet", f"{NEED_TRADES} (há 0)"]


def test_a_pilot_still_needs_every_other_check(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _breach(tmp_path, NOW - timedelta(days=2))
    reasons, _pilot = production_blockers(
        _config(150, 25), store, **{**CHECKS, "confirmation": "sim", "keys_ok": False}
    )
    assert "há violações de limite no testnet: 1 nos últimos 30 dias" in reasons
    assert any("PRODUCAO" in reason for reason in reasons) and any("chaves" in reason for reason in reasons)


def test_30_good_days_make_a_full_promotion_even_with_a_pilot_cap(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _good_days(store)
    _trades(store, [0.5] * 30)
    assert production_blockers(_config(150, 25), store, **CHECKS) == ([], None)


def test_promotion_needs_30_closed_trades(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _good_days(store)
    _trades(store, [0.5] * 28)
    _trades(store, [None])  # closed before R was recorded
    _trades(store, [0.5], days_ago=31)  # older than the 30 days
    reasons, pilot = production_blockers(_config(150, 0), store, **CHECKS)
    assert pilot is None and reasons == [f"{NEED_TRADES} (há 28)"]

    _trades(store, [0.5, 0.5])
    assert production_blockers(_config(150, 0), store, **CHECKS) == ([], None)


def test_promotion_needs_positive_r_expectancy(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _good_days(store)
    _trades(store, [1.0, -1.5] * 15)
    reasons, pilot = production_blockers(_config(150, 0), store, **CHECKS)
    assert pilot is None
    assert "expectativa em R dos últimos 30 dias não é positiva (-0.25 R)" in reasons


def test_promotion_needs_mc_p95_within_the_drawdown_limit(tmp_path) -> None:
    # Same +0.25 R expectancy; only the spread of the outcomes differs.
    wide = [3.0, -2.5] * 15
    narrow = [2.0, -1.0] * 15
    config = _config(150, 0)
    limits = config.limits
    assert mc_drawdown_r(wide) * limits.risk_per_trade_pct >= limits.max_drawdown_pct
    assert mc_drawdown_r(narrow) * limits.risk_per_trade_pct < limits.max_drawdown_pct

    store = Store(tmp_path / "wide.sqlite")
    _good_days(store)
    _trades(store, wide)
    reasons, _pilot = production_blockers(config, store, **CHECKS)
    assert len(reasons) == 1 and reasons[0].startswith("drawdown p95 do Monte Carlo")

    store = Store(tmp_path / "narrow.sqlite")
    _good_days(store)
    _trades(store, narrow)
    assert production_blockers(config, store, **CHECKS) == ([], None)


def test_pilot_skips_history_checks_but_not_breaches(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _trades(store, [-1.0] * 5)
    assert production_blockers(_config(150, 25), store, **CHECKS) == ([], 25)

    _breach(tmp_path, NOW - timedelta(days=29))
    assert production_blockers(_config(150, 25), store, **CHECKS) == (
        ["há violações de limite no testnet: 1 nos últimos 30 dias"],
        25,
    )


def test_breaches_in_the_testnet_audit_block_promotion(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _good_days(store)
    _trades(store, [0.5] * 30)
    ledger = audit_ledger_for(store)
    assert ledger == tmp_path / "live" / "audit.jsonl"

    _breach(tmp_path, NOW - timedelta(days=31))  # older than the 30 days
    _breach(tmp_path, NOW - timedelta(days=3), kind="universe")  # structural refusal, not a limit
    _audit(tmp_path, "order_placed", NOW - timedelta(days=3), {"allowed": True})
    _audit(tmp_path, "order_rejected", NOW - timedelta(days=3), {"allowed": False})
    assert audit_breaches(NOW - timedelta(days=30), path=ledger) == 0
    assert production_blockers(_config(150, 25), store, **CHECKS) == ([], None)

    _breach(tmp_path, NOW - timedelta(days=10))
    _audit(tmp_path, "breach", NOW - timedelta(days=5), {"allowed": False})  # no kind: counted
    assert audit_breaches(NOW - timedelta(days=30), path=ledger) == 2
    assert production_blockers(_config(150, 25), store, **CHECKS) == (
        ["há violações de limite no testnet: 2 nos últimos 30 dias"],
        None,
    )
    # Read only: the ledger is never rewritten.
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 6


def test_an_unreadable_testnet_audit_blocks_promotion(tmp_path) -> None:
    store = Store(tmp_path / "bot.sqlite")
    _good_days(store)
    _trades(store, [0.5] * 30)
    ledger = audit_ledger_for(store)
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"kind": "breach", "ts": "2026-10-1', encoding="utf-8")
    with pytest.raises(ValueError):
        audit_breaches(NOW - timedelta(days=30), path=ledger)
    assert production_blockers(_config(150, 25), store, **CHECKS) == (["auditoria do testnet ilegível"], None)


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

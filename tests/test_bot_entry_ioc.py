"""Strategy entries as LIMIT IOC buys, the execution records of a position, and the REDUCING choke point
(review 6.4, 6.6, 6.7, 6.14; D4, D5)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bot.exchange import service
from bot.executor import ENTRY_LIMIT_GAP, Executor
from bot.store import Store
from test_bot_executor import PROFILE, _ready


def _executor(tmp_path, **kwargs: Any) -> Executor:
    return Executor(Store(tmp_path / "bot.sqlite"), PROFILE, **kwargs)


def _buys(venue) -> list[dict[str, Any]]:
    return [call for call in venue.calls if call["side"] == "buy"]


def test_entry_is_a_limit_ioc_buy_at_ask_plus_gap_sized_in_base(tmp_path, venue, monkeypatch) -> None:
    venue.ask = Decimal("100.37")
    executor = _executor(tmp_path)
    seen: list[dict[str, Any]] = []
    send = venue.submit_order

    def spy(symbol: str, profile_id: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("side") == "buy":
            seen.append(executor.store.live_positions()[0])
        return send(symbol, profile_id, **kwargs)

    monkeypatch.setattr("bot.exchange.service.submit_order", spy)
    decision = executor.enter(_ready(size_usd=100))
    assert decision["decision"] == "operar"
    (entry,) = _buys(venue)
    # 100.37 x 1.002 = 100.57074, floored to the tick; 100 / 100.57, floored to the lot.
    assert entry["order_type"] == "limit" and entry["time_in_force"] == "ioc"
    assert entry["limit_price"] == "100.57" and entry["quantity"] == "0.9943"
    assert Decimal(entry["quantity"]) * Decimal(entry["limit_price"]) <= Decimal("100")
    assert "notional" not in entry and entry["held"] == [] and entry["session_id"] == "bot"
    # The client id and the quote it was priced on are on the row before the send.
    (pending,) = seen
    assert pending["status"] == "pending"
    assert pending["entry_client_order_id"] == service.client_order_id(PROFILE, entry["idempotency_key"])
    assert pending["entry_ref_price"] == pytest.approx(100.37)
    assert ENTRY_LIMIT_GAP == Decimal("0.002")


def test_a_partial_ioc_entry_opens_with_the_filled_qty(tmp_path, venue) -> None:
    venue.responses["buy:limit"] = [{"status": "ok", "filled": "0.5", "cost": "50", "fees": []}]
    executor = _executor(tmp_path)
    decision = executor.enter(_ready(size_usd=100))
    assert decision["decision"] == "operar" and "executada em parte: 0.5 de 0.998" in decision["reason"]
    position = executor.store.open_positions()[0]
    # The 0.1% sell margin on 0.5, floored to the lot; the stop covers what was bought.
    assert position["qty"] == pytest.approx(0.4995)
    stop = venue.calls[-1]
    assert stop["order_type"] == "stop_loss_limit" and stop["quantity"] == "0.4995"


def test_an_unfilled_ioc_entry_voids_the_row(tmp_path, venue) -> None:
    venue.responses["buy:limit"] = [{"status": "ok", "filled": "0", "cost": "0", "fees": [], "order_status": "expired"}]
    executor = _executor(tmp_path)
    decision = executor.enter(_ready())
    assert decision["decision"] == "bloqueado" and "sem execução" in decision["reason"]
    assert executor.store.live_positions() == []
    assert [call["order_type"] for call in venue.calls] == ["limit"]
    # The slot is free again, under a new order key.
    assert executor.enter(_ready())["decision"] == "operar"
    first, second = _buys(venue)
    assert first["idempotency_key"] != second["idempotency_key"]


@pytest.mark.parametrize(("filled", "opened"), [("0.5", True), ("0", False)])
def test_an_unknown_ioc_entry_resolves_in_the_sync(tmp_path, venue, filled: str, opened: bool) -> None:
    venue.responses["buy:limit"] = [{"status": "unknown", "error": "timeout"}]
    executor = _executor(tmp_path)
    assert executor.enter(_ready())["decision"] == "pendente"
    pending = executor.store.live_positions()[0]
    venue.orders[pending["entry_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "expired", "filled": filled, "cost": str(Decimal(filled) * 100), "order_id": "o9"},
    }
    executor.sync_positions()
    row = executor.store.position(pending["id"])
    assert row is not None
    if opened:
        assert row["status"] == "open" and row["qty"] == pytest.approx(0.4995) and row["stop_order_id"]
    else:
        assert row["status"] == "void" and venue.calls[-1]["order_type"] == "limit"


@pytest.mark.parametrize(
    ("strategy", "policy", "recorded"),
    [
        ("trend", "trailing", "trailing"),
        ("trend", "target", "target"),
        ("trend", "trailing_tp", "trailing_tp"),
        ("intraday_momentum", "trailing", "day"),
    ],
)
def test_stop_and_target_are_recomputed_on_the_average_fill_for_trailing_target_and_day(
    tmp_path, venue, strategy: str, policy: str, recorded: str
) -> None:
    executor = _executor(tmp_path, exit_policy=policy)
    # Scored on a stale price: its stop and target are far from where the order fills.
    opportunity = _ready(strategy=strategy, risk_pct=0.02, plan_target_pct=0.04, stop_price=50.0, target_price=60.0)
    assert executor.enter(opportunity)["decision"] == "operar"
    position = executor.store.open_positions()[0]
    avg = position["avg_price"]
    assert position["exit_policy"] == recorded
    assert position["stop_price"] == pytest.approx(avg * 0.98)
    assert position["target_price"] == pytest.approx(avg * 1.04)
    assert position["trail_pct"] == pytest.approx(0.02)
    assert position["initial_stop"] == pytest.approx(position["stop_price"])
    assert position["initial_risk_usd"] == pytest.approx(position["qty"] * (avg - position["stop_price"]))
    assert position["plan_stop_pct"] == 0.02 and position["plan_target_pct"] == 0.04


def test_a_plan_without_a_target_distance_keeps_the_scored_target(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready(risk_pct=0.02, plan_target_pct=None, target_price=104.0))
    position = executor.store.open_positions()[0]
    assert position["stop_price"] == pytest.approx(position["avg_price"] * 0.98)
    assert position["target_price"] == 104.0 and position["plan_target_pct"] is None


def test_instance_rows_keep_their_planned_stop(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"
    position = executor.store.open_positions()[0]
    # Instance buys stay MARKET, sized in quote.
    entry = venue.calls[0]
    assert entry["order_type"] == "market" and entry["notional"] == "20.00"
    assert position["stop_price"] == 90.0 and position["plan_stop_pct"] is None
    assert position["initial_stop"] == 90.0 and position["entry_slippage"] is None


def test_entry_exit_slippage_and_r_multiple_are_recorded(tmp_path, venue) -> None:
    # The quote says 100; the IOC fills at 100.2, the top of its limit.
    venue.responses["buy:limit"] = [{"status": "ok", "filled": "0.998", "cost": "99.9996", "fees": []}]
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    assert position["entry_ref_price"] == 100.0
    assert position["entry_slippage"] == pytest.approx(0.002)
    assert position["mae_price"] == position["mfe_price"] == position["avg_price"]
    venue.bid = Decimal("105")
    # The exit was decided at 105.5; the IOC sells at 104.47 (0.5% under the bid, floored).
    assert executor.exit_position(position["id"], "alvo atingido", exit_ref=Decimal("105.5"))["status"] == "ok"
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert closed["exit_slippage"] == pytest.approx(1 - 104.47 / 105.5)
    assert closed["realized_pnl"] > 0
    assert closed["r_multiple"] == pytest.approx(closed["realized_pnl"] / closed["initial_risk_usd"])


def test_an_exit_without_a_reference_measures_slippage_from_the_first_bid(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    venue.bid = Decimal("105")
    executor.exit_position(position["id"], "fechamento manual")
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["exit_slippage"] == pytest.approx(1 - 104.47 / 105)


def test_a_stop_fill_records_slippage_against_the_stop_and_a_negative_r(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    stop = position["stop_price"]
    qty = Decimal(str(position["qty"]))
    venue.orders[position["stop_client_order_id"]] = {
        "status": "ok",
        "order": {"status": "closed", "filled": str(qty), "cost": str(qty * Decimal("97.9")), "order_id": "stop2"},
    }
    executor.sync_positions()
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert closed["exit_slippage"] == pytest.approx(1 - 97.9 / stop)
    # Just over one R lost: the stop slipped and both legs paid fees.
    assert -1.2 < closed["r_multiple"] < -1.0


def test_entry_without_ask_is_refused(tmp_path, venue, monkeypatch) -> None:
    monkeypatch.setattr(
        "bot.exchange.service.get_quote", lambda *_a: {"status": "ok", "quote": {"bid": "100", "last": "100"}}
    )
    executor = _executor(tmp_path)
    decision = executor.enter(_ready())
    assert decision["decision"] == "bloqueado" and decision["reason"] == "cotação sem ask"
    assert venue.calls == [] and executor.store.live_positions() == []


def test_an_entry_below_the_exchange_minimum_is_refused_before_the_row(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    decision = executor.enter(_ready(size_usd=4.9))
    assert decision["decision"] == "bloqueado" and "mínimo" in decision["reason"]
    assert venue.calls == [] and executor.store.live_positions() == []


def test_position_records_timeframe_plan_and_hysteresis(tmp_path, venue) -> None:
    executor = _executor(tmp_path, exit_hysteresis=False)
    executor.enter(_ready(timeframe="4h"))
    position = executor.store.open_positions()[0]
    assert position["timeframe"] == "4h" and position["exit_hysteresis"] == 0 and position["plan_stop_pct"] == 0.02
    other = _executor(tmp_path / "on")
    other.enter(_ready())
    assert other.store.open_positions()[0]["exit_hysteresis"] == 1


def test_executor_refuses_buys_in_reducing(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"
    held = executor.store.open_positions()[0]
    # The stored reason is the canonical refusal (risk.buy_block), the same words the cycle shows.
    reason = "modo só reduzir: drawdown de 10% desde o pico (limite de 10%); só saídas"
    executor.store.set_state(
        "risk_state", {"level": "reducing", "since": "2026-10-20T00:00:00+00:00", "drawdown": 0.1, "reason": reason}
    )
    sent = len(venue.calls)
    decision = executor.enter(_ready(symbol="ETH/USDT"))
    assert decision["decision"] == "bloqueado" and decision["reason"] == reason
    manual = executor.enter(_ready(symbol="ETH/USDT"), manual=True)
    assert manual["decision"] == "bloqueado" and manual["reason"] == reason
    assert executor.open_instance(2, "dca", "SOL/USDT", Decimal("20"), Decimal("90")) == {"status": "blocked", "error": reason}
    assert executor.scale_in(held["id"], Decimal("10"), "grid-buy-1", Decimal("90"), reserve=2) == reason
    assert len(venue.calls) == sent and len(executor.store.live_positions()) == 1
    # Stops and exits go on.
    assert executor.exit_position(held["id"], "zerar")["status"] == "ok"
    # A cautious level buys as usual.
    executor.store.set_state("risk_state", {"level": "cautious", "drawdown": 0.06})
    assert executor.enter(_ready(symbol="ETH/USDT"))["decision"] == "operar"


def test_mark_tracks_mae_and_mfe_without_orders(tmp_path, venue) -> None:
    executor = _executor(tmp_path)
    executor.enter(_ready())
    position = executor.store.open_positions()[0]
    avg = position["avg_price"]
    sent = len(venue.calls)
    executor.mark(position["id"], Decimal("99"))
    executor.mark(position["id"], Decimal("104"))
    executor.mark(position["id"], Decimal("100.5"))
    marked = executor.store.position(position["id"])
    assert marked is not None and marked["mae_price"] == 99.0 and marked["mfe_price"] == 104.0
    assert marked["avg_price"] == avg and marked["stop_price"] == position["stop_price"]
    assert len(venue.calls) == sent and venue.cancelled == []
    executor.exit_position(position["id"], "fim")
    executor.mark(position["id"], Decimal("90"))
    assert executor.store.position(position["id"])["mae_price"] == 99.0  # type: ignore[index]

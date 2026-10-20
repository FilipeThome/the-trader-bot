"""A stop-limit the price gapped through: cancelled and replaced by bounded IOC sales (review 6.2, D15)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bot.evaluator import Opportunity
from bot.executor import Executor, UnprotectedPosition
from bot.store import Store

PROFILE = "binance-paper-trade"
#: The resting stop of ``_entered``: client id, exchange id, trigger and limit (98 x 0.995, floored).
STOP_CLIENT_ID = "c2"
STOP_ORDER_ID = "stop2"
STOP = 98.0
STOP_LIMIT = "97.51"
NO_FILL = {"status": "ok", "filled": "0", "cost": "0", "fees": []}


def _ready() -> Opportunity:
    return Opportunity(
        symbol="BTC/USDT",
        strategy="trend",
        action="Comprar",
        score=0.82,
        net_return=0.02,
        risk_pct=0.02,
        status="pronta",
        reason="rompimento",
        timeframe="1h",
        direction=1,
        cost=0.002,
        gross_return=0.04,
        size_usd=100.1,
        stop_price=STOP,
        target_price=104.0,
    )


def _executor(tmp_path, events: list[tuple[str, dict[str, Any]]]) -> Executor:
    return Executor(
        Store(tmp_path / "bot.sqlite"), PROFILE, ledger=lambda kind, payload: events.append((kind, payload))
    )


def _entered(executor: Executor, venue) -> dict[str, Any]:
    """An open position of 0.998 BTC with its stop resting at 98 / 97.51.

    The entry IOC buys exactly 1 BTC at an ask of 99.9 (100.1 USD at a 100.09 limit), so
    the average with the 0.001 BTC fee is 100 and the 2% stop of the plan sits at 98.
    """
    venue.ask = Decimal("99.9")
    executor.enter(_ready())
    venue.ask = None
    position = executor.store.open_positions()[0]
    assert position["avg_price"] == pytest.approx(100.0) and position["stop_price"] == pytest.approx(STOP)
    assert position["stop_client_order_id"] == STOP_CLIENT_ID and position["stop_order_id"] == STOP_ORDER_ID
    return position


def _stuck(venue, *, filled: str = "0", cost: str = "0", state: str = "open") -> None:
    """The stop rests on the book (triggered or not); a cancel turns it into a cancelled order."""
    order = {"status": state, "filled": filled, "cost": cost, "order_id": STOP_ORDER_ID, "price": STOP_LIMIT}
    venue.orders[STOP_CLIENT_ID] = {"status": "ok", "order": dict(order)}

    def cancel(_order_id: str) -> None:
        venue.orders[STOP_CLIENT_ID] = {"status": "ok", "order": {**order, "status": "canceled"}}

    venue.on_cancel = cancel


def _sells(venue) -> list[dict[str, Any]]:
    return [call for call in venue.calls if call["order_type"] == "limit" and call["side"] == "sell"]


def _kinds(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [kind for kind, _ in events]


def test_a_triggered_stop_left_below_its_limit_is_cancelled_and_sold_next_sync(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    executor.sync_positions()
    assert venue.cancelled == [STOP_ORDER_ID]
    (sell,) = _sells(venue)
    assert venue.events.index(f"cancel:{STOP_ORDER_ID}") < venue.events.index("submit:limit")
    assert sell["side"] == "sell" and sell["time_in_force"] == "ioc"
    # 0.5% below the bid, floored to the tick; the whole position.
    assert sell["quantity"] == "0.998" and sell["limit_price"] == "94.52"
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert closed["exit_price"] == pytest.approx(94.52)
    assert closed["realized_pnl"] < 0
    assert _kinds(events) == ["stop_gapped", "stop_gapped_exit"]
    gapped, done = events[0][1], events[1][1]
    assert gapped == {
        "position_id": position["id"],
        "symbol": "BTC/USDT",
        "stop": "98",
        "limit": STOP_LIMIT,
        "bid": "95",
        "order_state": "open",
    }
    assert done["position_id"] == position["id"] and done["attempts"] == 1
    assert done["exit_avg"] == pytest.approx(94.52)
    assert done["slippage_vs_stop"] == pytest.approx(94.52 / STOP - 1)


def test_a_partially_filled_stuck_stop_books_the_part_and_sells_the_rest(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue, filled="0.4", cost="39.004")
    venue.bid = Decimal("95")
    executor.sync_positions()
    (sell,) = _sells(venue)
    # The 0.4 the stop sold is booked on the cancel; only what is still held is offered.
    assert sell["quantity"] == "0.598" and sell["limit_price"] == "94.52"
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    proceeds = Decimal("39.004") + Decimal("0.598") * Decimal("94.52")
    avg = Decimal(str(position["avg_price"]))
    # Fees: 0.1% of the stop part (taker estimate) and the IOC fee the venue reported in USDT.
    fees = Decimal("39.004") * Decimal("0.001") + Decimal("0.598") * Decimal("94.52") * Decimal("0.001")
    expected_pnl = proceeds - avg * Decimal("0.998") - fees
    assert closed["realized_pnl"] == pytest.approx(float(expected_pnl))
    assert executor.store.pnl_days()[0]["net_pnl"] == pytest.approx(closed["realized_pnl"])
    done = events[-1]
    assert done[0] == "stop_gapped_exit"
    assert done[1]["exit_avg"] == pytest.approx(float(proceeds / Decimal("0.998")))


@pytest.mark.parametrize("bid", ["97.6", "97.51"])
def test_an_open_stop_with_the_bid_above_its_limit_is_left_alone(tmp_path, venue, bid) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    sent = len(venue.calls)
    # Below the trigger, but not below the limit: the stop-limit can still fill.
    venue.bid = Decimal(bid)
    executor.sync_positions()
    assert venue.cancelled == [] and len(venue.calls) == sent
    assert events == []
    still = executor.store.position(position["id"])
    assert still is not None and still["status"] == "open" and still["stop_order_id"] == STOP_ORDER_ID


def test_a_failed_first_ioc_retries_2pct_below_then_halts_when_still_open(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    venue.responses["sell:limit"] = [dict(NO_FILL), dict(NO_FILL)]
    with pytest.raises(UnprotectedPosition, match="gap"):
        executor.sync_positions()
    first, second = _sells(venue)
    assert first["limit_price"] == "94.52" and second["limit_price"] == "93.1"
    assert first["idempotency_key"] != second["idempotency_key"]
    # No market fallback, and no stop re-placed under a price already below it.
    assert [call["order_type"] for call in venue.calls] == ["limit", "stop_loss_limit", "limit", "limit"]
    left = executor.store.position(position["id"])
    assert left is not None and left["status"] == "open" and left["qty"] == pytest.approx(0.998)
    assert not left["stop_order_id"] and not left["stop_client_order_id"]
    assert _kinds(events) == ["stop_gapped"]


def test_a_partial_first_ioc_sells_the_rest_2pct_below_the_bid(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    venue.responses["sell:limit"] = [{"status": "ok", "filled": "0.5", "cost": "47.26", "fees": []}]
    executor.sync_positions()
    first, second = _sells(venue)
    assert first["quantity"] == "0.998" and second["quantity"] == "0.498"
    assert second["limit_price"] == "93.1"
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    done = events[-1][1]
    assert done["attempts"] == 2
    proceeds = Decimal("47.26") + Decimal("0.498") * Decimal("93.1")
    assert done["exit_avg"] == pytest.approx(float(proceeds / Decimal("0.998")))


def test_a_write_blocker_leaves_the_stuck_stop_on_the_book(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    sent = len(venue.calls)
    venue.bid = Decimal("95")
    venue.blocker = "halt ativo"
    executor.sync_positions()
    assert venue.cancelled == [] and len(venue.calls) == sent
    still = executor.store.position(position["id"])
    assert still is not None and still["status"] == "open" and still["stop_order_id"] == STOP_ORDER_ID
    assert _kinds(events) == ["stop_gapped"]
    # Once the gate allows writes again, the next sync replaces the stop.
    venue.blocker = None
    executor.sync_positions()
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert _kinds(events) == ["stop_gapped", "stop_gapped", "stop_gapped_exit"]


def test_an_instance_position_with_a_stuck_stop_is_closed_the_same_way(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    assert executor.open_instance(1, "grid", "BTC/USDT", Decimal("20"), Decimal("90"))["status"] == "ok"
    position = executor.store.open_positions()[0]
    assert position["instance_id"] == 1 and position["stop_order_id"] == STOP_ORDER_ID
    venue.orders[STOP_CLIENT_ID] = {"status": "ok", "order": {"status": "open", "filled": "0", "order_id": STOP_ORDER_ID}}
    venue.on_cancel = lambda _id: venue.orders.update(
        {STOP_CLIENT_ID: {"status": "ok", "order": {"status": "canceled", "filled": "0", "order_id": STOP_ORDER_ID}}}
    )
    venue.bid = Decimal("85")
    executor.sync_positions()
    (sell,) = _sells(venue)
    assert sell["quantity"] == "0.1996" and sell["limit_price"] == "84.57"
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert _kinds(events) == ["stop_gapped", "stop_gapped_exit"]
    assert events[0][1]["limit"] == "89.55"


def test_a_stop_that_fills_before_the_cancel_closes_the_row_from_its_own_fill(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")

    def filled_first(_order_id: str) -> dict[str, Any]:
        # Read as resting, filled while the cancel was on its way: Binance refuses the cancel.
        venue.orders[STOP_CLIENT_ID] = {
            "status": "ok",
            "order": {"status": "closed", "filled": "0.998", "cost": "97.31498", "order_id": STOP_ORDER_ID},
        }
        return {"status": "error", "error": "Unknown order sent."}

    venue.on_cancel = filled_first
    executor.sync_positions()
    assert _sells(venue) == []
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert closed["exit_price"] == pytest.approx(97.51)
    done = events[-1]
    assert done[0] == "stop_gapped_exit" and done[1]["exit_avg"] == pytest.approx(97.51)


def test_a_stuck_stop_that_cannot_be_cancelled_halts_without_selling(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    venue.on_cancel = lambda _id: {"status": "unknown", "error": "cancel outcome is unknown"}
    # The stop still rests after a failed cancel: no 2% IOC follows, the next sync retries (review F3)...
    for _ in range(2):
        executor.sync_positions()
    assert venue.cancelled == [STOP_ORDER_ID, STOP_ORDER_ID]
    assert [payload["retries"] for kind, payload in events if kind == "stop_gapped_retry"] == [1, 2]
    # ...and the third sync in a row that cannot take it off halts.
    with pytest.raises(UnprotectedPosition, match="3 ciclos"):
        executor.sync_positions()
    # The base is still locked by the stop: no sale is sent over it.
    assert _sells(venue) == []
    assert venue.cancelled == [STOP_ORDER_ID] * 3
    still = executor.store.position(position["id"])
    assert still is not None and still["status"] == "open" and still["stop_order_id"] == STOP_ORDER_ID


def test_an_ioc_of_unknown_outcome_halts_without_a_second_sale(tmp_path, venue) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(tmp_path, events)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    venue.responses["sell:limit"] = [{"status": "unknown", "error": "timeout"}]
    with pytest.raises(UnprotectedPosition, match="timeout"):
        executor.sync_positions()
    assert len(_sells(venue)) == 1
    left = executor.store.position(position["id"])
    assert left is not None and left["status"] == "open"
    # The attempt did not move on: a retry replays the same key instead of selling again.
    assert left["exit_attempts"] == 0


def test_a_failing_default_ledger_never_stops_the_exit(tmp_path, venue, monkeypatch) -> None:
    written: list[dict[str, Any]] = []

    def broken(payload: dict[str, Any]) -> None:
        written.append(dict(payload))
        raise OSError("disk full")

    monkeypatch.setattr("bot.executor.append_ledger", broken)
    executor = Executor(Store(tmp_path / "bot.sqlite"), PROFILE)
    position = _entered(executor, venue)
    _stuck(venue)
    venue.bid = Decimal("95")
    executor.sync_positions()
    closed = executor.store.position(position["id"])
    assert closed is not None and closed["status"] == "closed"
    assert [row["kind"] for row in written] == ["stop_gapped", "stop_gapped_exit"]
    assert written[0]["position_id"] == position["id"]

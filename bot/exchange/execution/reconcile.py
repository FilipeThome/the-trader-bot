# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Recover unresolved Binance spot writes and reconcile the journal with the exchange.

Only exchange evidence changes a journal state. A pending or unknown
submission becomes ``ORDER_SUBMITTED`` when Binance has the order. Elapsed time
alone never releases a write that may still be in flight:

* ``SUBMISSION_PENDING`` means the submitting call has not returned (it may be
  stalled, or its process crashed). Reconciliation never marks it absent; an
  operator does, with ``confirm_submission_absent`` after confirming that the
  submitting process is gone.
* ``SUBMISSION_UNKNOWN`` means the call returned without a definite answer, so
  the request was signed before that record was written. It becomes
  ``ORDER_ABSENT`` only when the record is older than ``ACCEPTANCE_WINDOW``
  (well beyond ``sdk.RECV_WINDOW_MS``, after which Binance refuses the signed
  request), a status query reports the order missing, and a successful
  open-orders read does not list it.

A failed read changes nothing. Recently absent orders are re-checked; if
Binance shows one after all, an ``EXECUTION_CONFLICT`` blocks new orders until
an operator acknowledges it. This module never places or cancels an order; it
reads the exchange and appends to the journal. The operator functions here are
not exposed as agent tools, MCP tools or API routes.

The journal lock is held only by each append, never across an exchange read.
``append_event`` re-checks the transition under the lock, so a write that
raced a concurrent submission is counted as ``raced`` and left alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from bot.exchange import sdk
from bot.exchange.execution.journal import (
    InvalidTransition,
    _latest_states,
    _records,
    append_event,
    has_unresolved,
    new_event,
)
from bot.exchange.execution.lifecycle import BLOCKING_EVENTS
from bot.exchange.types import TradingProfile

#: Binance refuses a signed request older than ``recvWindow``
#: (``sdk.RECV_WINDOW_MS``, 10 s). A minute of grace covers clock skew and slow
#: matching. Measured from the record written after the request returned.
ACCEPTANCE_WINDOW = timedelta(seconds=70)
#: Most recent submitted orders whose existence is re-checked per run.
SUBMITTED_SAMPLE = 50
#: Most recent absent orders re-checked for a late appearance per run.
ABSENT_SAMPLE = 50

_CARRIED_FIELDS = (
    "profile_id",
    "environment",
    "market_type",
    "session_id",
    "intent_id",
    "client_order_id",
    "symbol",
    "side",
    "order_type",
    "quantity",
    "notional",
    "limit_price",
    "request_fingerprint",
    "order_id",
)


def reconcile_journal(profile: TradingProfile, config: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Resolve this profile's unresolved writes, then compare the journal with Binance."""
    current = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "resolved": [],
        "still_unresolved": [],
        "mismatches": [],
        "untracked_open_orders": [],
        "raced": [],
        "read_errors": [],
    }
    try:
        latest = _profile_latest_rows(profile)
    except Exception as exc:  # noqa: BLE001 - a broken journal is reported, not repaired
        return {"status": "error", "error": "journal_unavailable", "detail": type(exc).__name__}

    for key, row in latest.items():
        event = str(row.get("event") or "")
        if event not in BLOCKING_EVENTS:
            continue
        if key.startswith("order:"):
            _resolve_order(key, row, config, current, summary)
        elif key.startswith("cancel:"):
            _resolve_cancel(key, row, config, summary, current)
        elif key.startswith("conflict:"):
            summary["still_unresolved"].append(
                {"state_key": key, "event": row.get("event"), "resolution": "operator_acknowledgement_required"}
            )

    latest = _profile_latest_rows(profile)
    states = {key: str(row.get("event") or "") for key, row in latest.items()}
    absent = [key for key, event in states.items() if key.startswith("order:") and event == "ORDER_ABSENT"]
    for key in absent[-ABSENT_SAMPLE:]:
        row = latest[key]
        lookup = _read(
            sdk.get_order,
            config,
            symbol=str(row.get("symbol") or ""),
            client_order_id=str(row.get("client_order_id") or ""),
        )
        if lookup.get("status") == "ok":
            _conflict(row, "absent_order_found", summary, order=lookup.get("order"))
        elif lookup.get("status") != "not_found":
            summary["read_errors"].append({"state_key": key, "error": lookup.get("error", "read_failed")})
    submitted = [key for key, event in states.items() if key.startswith("order:") and event == "ORDER_SUBMITTED"]
    for key in submitted[-SUBMITTED_SAMPLE:]:
        row = latest[key]
        lookup = _read(
            sdk.get_order,
            config,
            symbol=str(row.get("symbol") or ""),
            client_order_id=str(row.get("client_order_id") or ""),
        )
        if lookup.get("status") == "not_found":
            _mismatch(row, "submitted_order_not_found", summary)
        elif lookup.get("status") != "ok":
            summary["read_errors"].append({"state_key": key, "error": lookup.get("error", "read_failed")})

    symbols = sorted({str(row.get("symbol")) for row in latest.values() if row.get("symbol")})
    for symbol in symbols:
        orders = _read(sdk.get_open_orders, config, symbol=symbol)
        if orders.get("status") != "ok":
            summary["read_errors"].append({"symbol": symbol, "error": orders.get("error", "read_failed")})
            continue
        for order in orders.get("open_orders") or []:
            client_order_id = str(order.get("client_order_id") or "")
            key = f"order:{client_order_id}"
            if client_order_id and key in latest:
                if states.get(key) == "ORDER_ABSENT":
                    _conflict(latest[key], "absent_order_open", summary, order=order)
                elif states.get(key) != "ORDER_SUBMITTED":
                    _mismatch(latest[key], "open_order_without_submission", summary, order=order)
            else:
                summary["untracked_open_orders"].append(
                    {"symbol": symbol, "order_id": order.get("order_id"), "client_order_id": client_order_id}
                )

    try:
        remaining = has_unresolved()
    except Exception:  # noqa: BLE001
        remaining = True
    return {
        "status": "ok",
        "profile_id": profile.id,
        **summary,
        "unresolved_remaining": remaining,
        "profile_unresolved_remaining": bool(summary["still_unresolved"]),
    }


def _profile_latest_rows(profile: TradingProfile) -> dict[str, dict[str, Any]]:
    rows = [row for row in _records() if row.get("profile_id") == profile.id]
    keys = _latest_states(rows)
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("state_key") or "")
        if key in keys:
            latest[key] = row
    return latest


def _resolve_order(
    key: str,
    row: Mapping[str, Any],
    config: Any,
    now: datetime,
    summary: dict[str, Any],
) -> None:
    lookup = _read(
        sdk.get_order,
        config,
        symbol=str(row.get("symbol") or ""),
        client_order_id=str(row.get("client_order_id") or ""),
    )
    status = lookup.get("status")
    if status == "ok" and isinstance(lookup.get("order"), Mapping):
        order = lookup["order"]
        _append(
            key,
            "ORDER_SUBMITTED",
            row,
            summary,
            exchange_status="ok",
            exchange_order_id=str(order.get("order_id") or ""),
            exchange_order_status=str(order.get("status") or ""),
            resolved_by="reconcile",
        )
        return
    entry = {"state_key": key, "event": row.get("event"), "status_query": status or "error"}
    if status == "not_found" and row.get("event") == "SUBMISSION_PENDING":
        # The submitting call never returned; it may still be in flight.
        entry["resolution"] = "operator_confirmation_required"
    elif status == "not_found" and _age(row, now) > ACCEPTANCE_WINDOW and _not_open(row, config):
        _append(key, "ORDER_ABSENT", row, summary, exchange_status="not_found", resolved_by="reconcile")
        return
    summary["still_unresolved"].append(entry)


def _not_open(row: Mapping[str, Any], config: Any) -> bool:
    """A second, independent read: the open-orders list succeeds without the order."""
    orders = _read(sdk.get_open_orders, config, symbol=str(row.get("symbol") or ""))
    if orders.get("status") != "ok" or not isinstance(orders.get("open_orders"), list):
        return False
    client_order_id = str(row.get("client_order_id") or "")
    return not any(
        isinstance(order, Mapping) and str(order.get("client_order_id") or "") == client_order_id
        for order in orders["open_orders"]
    )


def _resolve_cancel(
    key: str,
    row: Mapping[str, Any],
    config: Any,
    summary: dict[str, Any],
    now: datetime,
) -> None:
    lookup = _read(sdk.get_order, config, symbol=str(row.get("symbol") or ""), order_id=str(row.get("order_id") or ""))
    status = lookup.get("status")
    if status == "ok" and isinstance(lookup.get("order"), Mapping):
        order_status = str(lookup["order"].get("status") or "").lower()
        cancelled = order_status in {"canceled", "cancelled"}
        if not cancelled and row.get("event") == "CANCEL_REQUESTED" and _age(row, now) <= ACCEPTANCE_WINDOW:
            # The cancel may still be in flight; do not call it rejected yet.
            summary["still_unresolved"].append(
                {"state_key": key, "event": row.get("event"), "status_query": "ok", "order_status": order_status}
            )
            return
        event = "CANCEL_ACKNOWLEDGED" if cancelled else "CANCEL_REJECTED"
        _append(key, event, row, summary, exchange_order_status=order_status, resolved_by="reconcile")
        return
    if status == "not_found":
        # Binance can archive a canceled order with no fills, so this label is
        # auditable rather than certain; it unblocks without inventing a fill.
        _append(key, "CANCEL_REJECTED", row, summary, exchange_status="not_found", resolved_by="not_found")
        return
    summary["still_unresolved"].append({"state_key": key, "event": row.get("event"), "status_query": status or "error"})


def _append(
    key: str,
    event: str,
    row: Mapping[str, Any],
    summary: dict[str, Any],
    **fields: Any,
) -> None:
    carried = {name: row[name] for name in _CARRIED_FIELDS if row.get(name) is not None}
    try:
        append_event(new_event(event, key, **carried, **fields))
    except InvalidTransition:
        summary["raced"].append({"state_key": key, "event": event})
        return
    except Exception as exc:  # noqa: BLE001 - an unwritten resolution leaves the key unresolved
        summary["still_unresolved"].append({"state_key": key, "event": row.get("event"), "error": type(exc).__name__})
        return
    summary["resolved"].append({"state_key": key, "event": event, "resolved_by": fields.get("resolved_by")})


def _mismatch(
    row: Mapping[str, Any],
    reason: str,
    summary: dict[str, Any],
    *,
    order: Mapping[str, Any] | None = None,
) -> None:
    client_order_id = str(row.get("client_order_id") or "")
    key = f"recon:{client_order_id}"
    entry: dict[str, Any] = {"state_key": key, "client_order_id": client_order_id, "reason": reason}
    if order is not None:
        entry["exchange_order_id"] = order.get("order_id")
        entry["exchange_order_status"] = order.get("status")
    carried = {name: row[name] for name in _CARRIED_FIELDS if row.get(name) is not None}
    try:
        append_event(new_event("RECONCILIATION_MISMATCH", key, **carried, reason=reason))
    except InvalidTransition:
        entry["already_recorded"] = True
    except Exception as exc:  # noqa: BLE001 - the mismatch is still reported
        entry["journal_error"] = type(exc).__name__
    summary["mismatches"].append(entry)


def _conflict(
    row: Mapping[str, Any],
    reason: str,
    summary: dict[str, Any],
    *,
    order: Any = None,
) -> None:
    """Exchange evidence contradicts a settled key. This blocks new orders."""
    client_order_id = str(row.get("client_order_id") or "")
    key = f"conflict:{client_order_id}"
    entry: dict[str, Any] = {"state_key": key, "client_order_id": client_order_id, "reason": reason, "blocking": True}
    if isinstance(order, Mapping):
        entry["exchange_order_id"] = order.get("order_id")
        entry["exchange_order_status"] = order.get("status")
    carried = {name: row[name] for name in _CARRIED_FIELDS if row.get(name) is not None}
    try:
        append_event(new_event("EXECUTION_CONFLICT", key, **carried, conflict_reason=reason))
    except InvalidTransition:
        entry["already_recorded"] = True
    except Exception as exc:  # noqa: BLE001 - the conflict is still reported
        entry["journal_error"] = type(exc).__name__
    summary["mismatches"].append(entry)


def confirm_submission_absent(
    profile: TradingProfile,
    config: Any,
    state_key: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Operator only: mark a stuck ``SUBMISSION_PENDING`` order absent.

    For a submission whose process is known to be gone (crashed, killed).
    Never called by the agent: no tool, MCP tool or API route exposes it. It
    still requires exchange evidence: the record is older than the acceptance
    window, a status query reports the order missing, and the open-orders list
    does not hold it. The operator's ``reason`` is journaled.
    """
    text = str(reason or "").strip()
    if not text:
        return {"status": "error", "error": "an operator reason is required"}
    current = now or datetime.now(timezone.utc)
    try:
        row = _profile_latest_rows(profile).get(state_key)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": "journal_unavailable", "detail": type(exc).__name__}
    if row is None or row.get("event") != "SUBMISSION_PENDING":
        return {"status": "error", "error": "state key is not a pending submission of this profile"}
    if _age(row, current) <= ACCEPTANCE_WINDOW:
        return {"status": "error", "error": "submission is younger than the acceptance window"}
    lookup = _read(
        sdk.get_order,
        config,
        symbol=str(row.get("symbol") or ""),
        client_order_id=str(row.get("client_order_id") or ""),
    )
    if lookup.get("status") != "not_found" or not _not_open(row, config):
        return {"status": "error", "error": "the exchange did not confirm the order is absent", "status_query": lookup.get("status")}
    summary: dict[str, Any] = {"resolved": [], "still_unresolved": [], "raced": []}
    _append(state_key, "ORDER_ABSENT", row, summary, exchange_status="not_found", resolved_by="operator", operator_reason=text)
    return {"status": "ok" if summary["resolved"] else "error", **summary}


def acknowledge_conflict(state_key: str, *, reason: str) -> dict[str, Any]:
    """Operator only: record that an ``EXECUTION_CONFLICT`` was investigated.

    Never called by the agent. Unblocks new orders only for this conflict.
    """
    text = str(reason or "").strip()
    if not text:
        return {"status": "error", "error": "an operator reason is required"}
    if not str(state_key).startswith("conflict:"):
        return {"status": "error", "error": "state key is not a conflict"}
    try:
        append_event(new_event("CONFLICT_ACKNOWLEDGED", state_key, operator_reason=text, ack_id=str(uuid.uuid4())))
    except InvalidTransition:
        return {"status": "error", "error": "state key holds no open conflict"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": "journal_unavailable", "detail": type(exc).__name__}
    return {"status": "ok", "state_key": state_key}


def _age(row: Mapping[str, Any], now: datetime) -> timedelta:
    try:
        recorded = datetime.fromisoformat(str(row.get("recorded_at") or ""))
    except ValueError:
        return timedelta(0)
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return now - recorded


def _read(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a failed read is never evidence
        return {"status": "error", "error": f"read_failed ({type(exc).__name__})"}
    return result if isinstance(result, dict) else {"status": "error", "error": "read_failed"}

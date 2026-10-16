# First-party code of this repository. It was written in a local, unpublished
# Vibe-Trading checkout and is in no Vibe-Trading commit. See ORIGIN.md.
"""Authorize one Binance spot order, journal it, then submit it once.

Paper orders are journaled and then sent. Live orders call ``execute_live_order``;
the in-flight journal line is written inside the wrapper that the gate calls
on ALLOW, so a mandate denial does not look like an order that might exist.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import urlparse

from bot.exchange import sdk
from bot.exchange.sdk import authorize_exchange_write
from bot.exchange.shaping import normalize_symbol
from bot.exchange.execution.controls import standing_blockers
from bot.exchange.execution.journal import (
    DailyLimitReached,
    InvalidTransition,
    PersistError,
    UnresolvedExecution,
    _latest_states,
    _records,
    append_event,
    begin_submission,
    daily_operation_count,
    has_unresolved,
    new_event,
)
from bot.exchange.execution.lifecycle import (
    BLOCKING_EVENTS,
    ORDER_REUSABLE_EVENTS,
    ORDER_TERMINAL_EVENTS,
)
from bot.exchange.execution.preflight import (
    _as_decimal,
    evaluate_account,
    quote_is_fresh,
    reference_price,
)
from bot.exchange.execution.rules import (
    SizeError,
    SizeOutOfRange,
    float_not_above,
    parse_market,
    parse_size,
    size_order,
)
from bot.exchange.types import TradingProfile
from bot.live.sdk_order_gate import gate_allowed

_PAPER_PROFILE = "binance-paper-trade"
_LIVE_PROFILE = "binance-live-trade"
_BOT_LIVE_PROFILE = "binance-live-trade-bot"
_PROTECTED_TYPES = frozenset({"stop_loss_limit", "take_profit_limit"})
_LIVE_HOST = "api.binance.com"
_BLOCKED_HOSTS = frozenset({_LIVE_HOST, "fapi.binance.com"})
_TIME_IN_FORCE = frozenset({"day", "gtc", "ioc", "fok"})
_DEFINITE_REJECT = "error"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CANCEL_TERMINAL_EVENTS = frozenset({"CANCEL_ACKNOWLEDGED", "CANCEL_REJECTED"})


def place_spot_order(
    profile: TradingProfile,
    config: Any,
    *,
    symbol: str,
    side: str,
    quantity: Any = None,
    notional: Any = None,
    order_type: str = "market",
    limit_price: Any = None,
    stop_price: Any = None,
    time_in_force: str = "day",
    session_id: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Place one spot order, or deny it before the exchange is contacted.

    Idempotency lifecycle. With ``idempotency_key`` the client order id is
    derived from the profile and the key, and the key's journal state decides
    what a repeated request does:

    * no state, ``PREFLIGHT_DENIED`` or ``SUBMISSION_ABORTED``: the exchange
      never received this client order id (the request was refused before
      submission, or the adapter proved it returned before sending), so the
      request is evaluated fresh under the same key;
    * ``ORDER_SUBMITTED``, ``ORDER_REJECTED`` or ``ORDER_ABSENT``: the
      recorded outcome is replayed and nothing is sent;
    * ``SUBMISSION_PENDING`` or ``SUBMISSION_UNKNOWN``: the request may have
      reached the exchange; it is refused (``unresolved_execution``) until
      reconciliation, and it is never re-sent automatically;
    * ``PREFLIGHT_ALLOWED``: an interrupted attempt resumes its checks.

    A request whose fingerprint differs from the key's recorded request is
    refused (``idempotency_conflict``). Denials before any order key exists
    are journaled under ``deny:<uuid>``. Without a key every call is a new
    intent.

    Every standing control (halt, audit-only, cap, daily limit, credentials,
    journal) is checked again in the final step before submission, and the
    halt flag once more inside the adapter just before the exchange write.
    """
    identity = _identity_error(profile, config)
    if identity:
        return identity
    if profile.id == _BOT_LIVE_PROFILE and not gate_allowed("binance"):
        # Production writes exist only inside the mandate gate's ALLOW, never on a caller's word.
        return _deny(("mandate_gate_required",))
    audit = _audit_only_denial(profile)
    if audit:
        return audit
    structural = _structural_error(
        side=side,
        order_type=order_type,
        quantity=quantity,
        notional=notional,
        limit_price=limit_price,
        stop_price=stop_price,
        time_in_force=time_in_force,
    )
    if structural:
        return structural

    canonical = normalize_symbol(symbol)
    side_clean = str(side).strip().lower()
    type_clean = str(order_type).strip().lower()
    tif_clean = str(time_in_force or "day").strip().lower()
    fingerprint = _request_fingerprint(
        canonical, side_clean, type_clean, tif_clean, quantity, notional, limit_price, stop_price
    )
    resume_preflight = False
    if idempotency_key is not None:
        key = str(idempotency_key).strip()
        if not _IDEMPOTENCY_KEY.match(key):
            return _deny(("idempotency_key_invalid",))
        intent_id = key
        client_order_id = client_order_id_for(profile.id, key)
        try:
            rows = _records()
        except Exception as exc:  # noqa: BLE001 - a broken journal blocks the write
            return _deny(("journal_unavailable",), detail=type(exc).__name__)
        recorded = _latest_row(rows, f"order:{client_order_id}")
        if recorded is not None:
            if recorded.get("request_fingerprint") != fingerprint:
                return _deny(("idempotency_conflict",))
            latest = str(recorded.get("event") or "")
            if latest in ORDER_TERMINAL_EVENTS:
                return _replay(recorded)
            if latest in BLOCKING_EVENTS:
                return _deny(("unresolved_execution",))
            if latest == "PREFLIGHT_ALLOWED":
                resume_preflight = True
            elif latest not in ORDER_REUSABLE_EVENTS:
                return _deny(("idempotency_conflict",))
    else:
        intent_id = str(uuid.uuid4())
        client_order_id = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:32]

    try:
        from bot.live.halt import halt_flag_set

        if halt_flag_set("binance"):
            return _deny(("halted",))
    except Exception as exc:  # noqa: BLE001 - an unreadable halt flag blocks the write
        return _deny(("halted",), detail=type(exc).__name__)
    try:
        if has_unresolved():
            return _deny(("unresolved_execution",))
    except PersistError:
        return _deny(("journal_unavailable",))
    except Exception as exc:  # noqa: BLE001 - a broken journal blocks the write
        return _deny(("journal_unavailable",), detail=type(exc).__name__)
    daily_limit: int | None = None
    if profile.id == _PAPER_PROFILE:
        daily_limit = sdk.testnet_daily_operation_limit()
        if daily_limit is None:
            return _deny(
                ("daily_limit_invalid",),
                detail="testnet_max_daily_operations in binance.json must be a non-negative integer",
            )
        try:
            if daily_operation_count(profile.id) >= daily_limit:
                return _deny(("daily_limit_reached",))
        except Exception as exc:  # noqa: BLE001 - an uncountable journal blocks the write
            return _deny(("journal_unavailable",), detail=type(exc).__name__)

    if "/" not in canonical:
        return _deny(("symbol_unresolved",))
    try:
        parsed_quantity = None if quantity is None else parse_size(quantity)
        parsed_notional = None if notional is None else parse_size(notional)
        parsed_price = None if limit_price is None else parse_size(limit_price)
    except SizeOutOfRange:
        return _deny(("size_out_of_range",))
    except SizeError:
        return _deny(("size_not_positive",))

    market_payload = _guarded(sdk.get_spot_market, canonical, config=config)
    snapshot = _guarded(sdk.get_account_snapshot, config)
    orders = _guarded(sdk.get_open_orders, config)
    quote = _guarded(sdk.get_quote, canonical, config=config)
    if any(item.get("status") == "error" and item.get("error") == "read_failed" for item in (
        market_payload, snapshot, orders, quote,
    )):
        return _deny(("read_failed",))

    market = market_payload.get("market") if isinstance(market_payload, dict) else None
    if str(market_payload.get("status", "")).lower() != "ok" or not isinstance(market, dict):
        return _deny(("symbol_unresolved",))
    if str(market.get("symbol") or market_payload.get("symbol") or "") not in {"", canonical}:
        if str(market.get("symbol") or "") != canonical:
            return _deny(("symbol_unresolved",))
    rules, rule_reasons = parse_market(market)
    if rules is None:
        return _journal_denial(profile, canonical, side_clean, type_clean, rule_reasons, session_id)
    if profile.id == _PAPER_PROFILE:
        cap = sdk.paper_order_notional_limit(canonical.split("/", 1)[1])
        if cap is not None and rules.min_notional is not None and cap < rules.min_notional:
            # No order can be both at or above minNotional and within the cap.
            return _journal_denial(
                profile, canonical, side_clean, type_clean, ("notional_limit_infeasible",), session_id
            )
    sized, size_reasons = size_order(
        order_type=type_clean,
        quantity=parsed_quantity,
        notional=parsed_notional,
        limit_price=parsed_price,
        rules=rules,
        reference_price=reference_price(quote, side_clean),
    )
    if sized is None:
        return _journal_denial(profile, canonical, side_clean, type_clean, size_reasons, session_id)

    if profile.id == _PAPER_PROFILE:
        cap_reasons = _paper_notional_reasons(
            canonical, sized, order_type=type_clean, side=side_clean, rules=rules, quote=quote
        )
        if cap_reasons:
            denied = _journal_denial(
                profile, canonical, side_clean, type_clean, cap_reasons, session_id,
                client_order_id=client_order_id, intent_id=intent_id,
            )
            if "notional_limit_unconfigured" in cap_reasons:
                denied["detail"] = (
                    "set testnet_max_order_notional in binance.json, keyed by quote asset, "
                    "for example {\"USDT\": \"100\"}"
                )
            return denied

    account_reasons = evaluate_account(
        sized=sized,
        rules=rules,
        side=side_clean,
        snapshot=snapshot,
        quote=quote,
        open_orders=orders,
        canonical_symbol=canonical,
        client_order_id=client_order_id,
        now=datetime.now(timezone.utc),
    )
    if account_reasons:
        return _journal_denial(
            profile, canonical, side_clean, type_clean, account_reasons, session_id,
            client_order_id=client_order_id, intent_id=intent_id,
        )

    state_key = f"order:{client_order_id}"
    common = _event_fields(
        profile,
        session_id=session_id,
        intent_id=intent_id,
        client_order_id=client_order_id,
        symbol=canonical,
        side=side_clean,
        order_type=type_clean,
        quantity=sized.quantity_text(),
        notional=sized.notional_text(),
        limit_price=sized.limit_price_text(),
        request_fingerprint=fingerprint,
    )
    if not resume_preflight:
        try:
            append_event(new_event("PREFLIGHT_ALLOWED", state_key, **common))
        except InvalidTransition as exc:
            if idempotency_key is not None:
                return _deny(("idempotency_conflict",))
            return _deny(("persist_failed",), detail=type(exc).__name__)
        except (PersistError, Exception) as exc:  # noqa: BLE001
            return _deny(("persist_failed",), detail=type(exc).__name__)

    place_kwargs: dict[str, Any] = {
        "symbol": canonical,
        "side": side_clean,
        "order_type": type_clean,
        "time_in_force": tif_clean,
        "client_order_id": client_order_id,
    }
    if sized.quantity_text() is not None:
        place_kwargs["quantity"] = sized.quantity_text()
    if sized.notional_text() is not None:
        place_kwargs["notional"] = sized.notional_text()
    if sized.limit_price_text() is not None:
        place_kwargs["limit_price"] = sized.limit_price_text()
    if stop_price is not None:
        place_kwargs["stop_price"] = stop_price

    if profile.id in (_PAPER_PROFILE, _BOT_LIVE_PROFILE):
        # Final critical section: every standing control again, then the cap
        # against a freshly read quote (a limit sell's cap uses the live bid).
        late = _final_reasons(profile, config, canonical, sized, type_clean, side_clean, rules)
        if late:
            _deny_preflight(state_key, common, late)
            return _deny(late)
        try:
            begin_submission(
                new_event("SUBMISSION_PENDING", state_key, **common),
                daily_limit=None if daily_limit is None else (profile.id, int(daily_limit)),
            )
        except DailyLimitReached:
            _deny_preflight(state_key, common, ("daily_limit_reached",))
            return _deny(("daily_limit_reached",))
        except UnresolvedExecution:
            return _deny(("unresolved_execution",))
        except InvalidTransition as exc:
            if idempotency_key is not None:
                return _deny(("idempotency_conflict",))
            return _deny(("persist_failed",), detail=type(exc).__name__)
        except (PersistError, Exception) as exc:  # noqa: BLE001
            _abort_pending(state_key, common)
            return _deny(("persist_failed",), detail=type(exc).__name__)
        return _submit(config, state_key, common, place_kwargs)

    return _place_live(
        config,
        place_kwargs=place_kwargs,
        state_key=state_key,
        common=common,
        sized_quantity=sized.quantity,
        sized_notional=sized.notional,
        sized_price=sized.limit_price,
        session_id=session_id,
    )


def client_order_id_for(profile_id: str, idempotency_key: str) -> str:
    """The Binance client order id a keyed request is sent with. Callers may persist it first."""
    return hashlib.sha256(f"{profile_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:32]


def cancel_spot_order(
    profile: TradingProfile,
    config: Any,
    order_id: str,
    *,
    symbol: str | None,
    session_id: str = "",
) -> dict[str, Any]:
    """Cancel one spot order. A halt does not block this risk-reducing write.

    Spot Testnet audit-only mode does block it: it allows no writes at all.

    Cancel lifecycle (``cancel:<profile>:<symbol>:<order id>``): ``CANCEL_REQUESTED`` is
    journaled before the request and blocks new orders until it resolves. A
    definite exchange answer gives ``CANCEL_ACKNOWLEDGED`` (final) or
    ``CANCEL_REJECTED``; a return before the request was sent gives
    ``CANCEL_ABORTED``; both of the latter may be retried by calling this
    again. An unknown outcome (``CANCEL_UNKNOWN``) or a crash after
    ``CANCEL_REQUESTED`` stays blocking until ``reconcile_journal`` reads the
    order. Cancels are never retried automatically, and success is reported
    only on the exchange's answer.
    """
    identity = _identity_error(profile, config)
    if identity:
        return identity
    audit = _audit_only_denial(profile, cancel=True)
    if audit:
        return audit
    order_id_clean = str(order_id or "").strip()
    if not order_id_clean or not str(symbol or "").strip():
        return _deny(("cancel_identity_required",))
    canonical = normalize_symbol(str(symbol))
    if "/" not in canonical:
        return _deny(("symbol_unresolved",))
    state_key = _cancel_state_key(profile, canonical, order_id_clean)
    fields = _event_fields(
        profile,
        session_id=session_id,
        intent_id="",
        client_order_id="",
        symbol=canonical,
        side="",
        order_type="cancel",
        quantity=None,
        notional=None,
        limit_price=None,
        order_id=order_id_clean,
    )
    try:
        append_event(new_event("CANCEL_REQUESTED", state_key, **fields))
    except InvalidTransition:
        latest = _latest_event(state_key)
        if latest == "CANCEL_ACKNOWLEDGED":
            return _deny(("cancel_already_acknowledged",))
        if latest in BLOCKING_EVENTS:
            return _deny(("unresolved_execution",))
        return _deny(("persist_failed",), detail="InvalidTransition")
    except (PersistError, Exception) as exc:  # noqa: BLE001
        return _deny(("persist_failed",), detail=type(exc).__name__)
    try:
        with authorize_exchange_write():
            result = sdk.cancel_order(config, order_id_clean, symbol=canonical)
    except Exception as exc:  # noqa: BLE001 - an adapter raise may follow a send
        result = {"status": "unknown", "error": f"cancel outcome is unknown ({type(exc).__name__})"}
    status = str(result.get("status", "")).lower() if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("request_sent") is False and status != "ok":
        event = "CANCEL_ABORTED"
    elif status == "ok":
        event = "CANCEL_ACKNOWLEDGED"
    elif status == "unknown":
        event = "CANCEL_UNKNOWN"
    else:
        event = "CANCEL_REJECTED"
    try:
        append_event(new_event(event, state_key, **fields, exchange_status=status or "error"))
    except InvalidTransition:
        settled = _latest_event(state_key)
        if settled in _CANCEL_TERMINAL_EVENTS and isinstance(result, dict):
            return {**result, "journal_state": settled}
        if isinstance(result, dict):
            return {**result, "status": "unknown", "error": "cancel outcome was not journaled"}
        return {"status": "unknown", "error": "cancel outcome was not journaled"}
    except (PersistError, Exception):
        if isinstance(result, dict):
            return {**result, "status": "unknown", "error": "cancel outcome was not journaled"}
        return {"status": "unknown", "error": "cancel outcome was not journaled"}
    return result if isinstance(result, dict) else {"status": "unknown", "error": "cancel outcome is unknown"}


def _place_live(
    config: Any,
    *,
    place_kwargs: dict[str, Any],
    state_key: str,
    common: dict[str, Any],
    sized_quantity: Any,
    sized_notional: Any,
    sized_price: Any,
    session_id: str,
) -> dict[str, Any]:
    from bot.live.enforcement import OrderIntent
    from bot.live.mandate.model import AssetClass, InstrumentType
    from bot.live.sdk_order_gate import execute_live_order

    intent = OrderIntent(
        symbol=str(place_kwargs["symbol"]).upper(),
        side=str(place_kwargs["side"]),
        notional_usd=None if sized_notional is None else float_not_above(sized_notional),
        quantity=None if sized_quantity is None else float_not_above(sized_quantity),
        instrument_type=InstrumentType.CRYPTO,
        asset_class=AssetClass.CRYPTO,
        limit_price=None if sized_price is None else float_not_above(sized_price),
    )

    def place_order(config: Any = None, **kwargs: Any) -> dict[str, Any]:
        # Called by the live gate on ALLOW, inside its lock. Standing controls
        # are checked again; the mandate itself was just checked by the gate.
        late = [reason for reason in standing_blockers(_live_profile(), config) if reason != "mandate_required"]
        if late:
            _deny_preflight(state_key, common, tuple(late))
            return _deny(tuple(late))
        try:
            begin_submission(new_event("SUBMISSION_PENDING", state_key, **common))
        except UnresolvedExecution:
            return _deny(("unresolved_execution",))
        except InvalidTransition as exc:
            return _deny(("persist_failed",), detail=type(exc).__name__)
        except (PersistError, Exception) as exc:  # noqa: BLE001
            _abort_pending(state_key, common)
            return _deny(("persist_failed",), detail=type(exc).__name__)
        return _submit(config, state_key, common, kwargs)

    class _Module:
        def get_quote(self, symbol: str, *, config: Any = None, **_: Any) -> dict[str, Any]:
            return sdk.get_quote(symbol, config=config)

        def get_positions(self, config: Any = None) -> dict[str, Any]:
            return sdk.get_positions(config)

        def get_account_snapshot(self, config: Any = None) -> dict[str, Any]:
            return sdk.get_account_snapshot(config)

        def place_order(self, config: Any = None, **kwargs: Any) -> dict[str, Any]:
            return place_order(config, **kwargs)

    result = execute_live_order(
        broker="binance",
        connector_module=_Module(),
        config=config,
        intent=intent,
        place_kwargs=place_kwargs,
        session_id=session_id,
    )
    return result if isinstance(result, dict) else {"status": "unknown", "error": "live gate returned no result"}


def _live_profile() -> TradingProfile:
    from bot.exchange.profiles import profile_by_id

    return profile_by_id(_LIVE_PROFILE)


def _final_reasons(
    profile: TradingProfile,
    config: Any,
    symbol: str,
    sized: Any,
    order_type: str,
    side: str,
    rules: Any,
) -> tuple[str, ...]:
    """Standing controls plus the cap, re-read right before submission."""
    reasons = list(standing_blockers(profile, config, quote_asset=symbol.split("/", 1)[1]))
    if profile.id == _PAPER_PROFILE:
        # Every priced sell (limit, stop-limit, take-profit-limit) caps on the live bid.
        priced_sell = side == "sell" and order_type in {"limit", *_PROTECTED_TYPES}
        quote = _guarded(sdk.get_quote, symbol, config=config) if priced_sell else {}
        reasons.extend(
            _paper_notional_reasons(symbol, sized, order_type=order_type, side=side, rules=rules, quote=quote)
        )
    return tuple(dict.fromkeys(reasons))


def _submit(config: Any, state_key: str, common: dict[str, Any], place_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Hand one journaled order to the adapter and journal its outcome."""
    try:
        with authorize_exchange_write():
            result = sdk.place_order(config, **place_kwargs)
    except Exception as exc:  # noqa: BLE001 - an adapter raise may follow a send
        result = {"status": "unknown", "error": f"exchange outcome is unknown ({type(exc).__name__})"}
    return _journal_submission(state_key, common, result, config=config)


def _abort_pending(state_key: str, common: dict[str, Any]) -> None:
    """Close a pending record whose order was never handed to the adapter.

    Best effort: when the journal cannot record it, the pending record keeps
    blocking new orders until reconciled, which is the safe direction.
    """
    try:
        append_event(new_event("SUBMISSION_ABORTED", state_key, **common, abort_reason="journal_error_before_send"))
    except Exception:  # noqa: BLE001
        pass


def _journal_submission(
    state_key: str,
    common: dict[str, Any],
    result: Any,
    *,
    config: Any = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        result = {"status": "unknown", "error": "exchange outcome is unknown"}
    status = str(result.get("status", "")).lower()
    if result.get("request_sent") is False and status != "ok":
        # The adapter returned before calling the exchange: nothing was sent.
        event = "SUBMISSION_ABORTED"
    elif status == "ok":
        event = "ORDER_SUBMITTED"
    elif status == "unknown":
        event = "SUBMISSION_UNKNOWN"
    else:
        event = "ORDER_REJECTED"
    extra: dict[str, Any] = {"exchange_status": status or _DEFINITE_REJECT}
    if event == "ORDER_SUBMITTED" and result.get("order_id"):
        extra["exchange_order_id"] = str(result["order_id"])
    if event == "SUBMISSION_ABORTED":
        extra["abort_reason"] = str(result.get("error") or "not_sent")[:200]
    try:
        append_event(new_event(event, state_key, **common, **extra))
    except InvalidTransition:
        settled = _latest_event(state_key)
        if settled == "ORDER_ABSENT" and status in {"ok", "unknown"}:
            return _record_conflict(state_key, common, result, "late_outcome_after_absent")
        if settled in ORDER_TERMINAL_EVENTS:
            return {**result, "journal_state": settled, "client_order_id": common.get("client_order_id", "")}
        return _unjournaled(result, common)
    except (PersistError, Exception):  # noqa: BLE001
        return _unjournaled(result, common)
    if event == "SUBMISSION_UNKNOWN":
        return _resolve_unknown_submission(state_key, common, result, config)
    if event == "SUBMISSION_ABORTED":
        return {**result, "journal_state": event, "client_order_id": common.get("client_order_id", "")}
    if "client_order_id" not in result:
        return {**result, "client_order_id": common.get("client_order_id", "")}
    return dict(result)


def _record_conflict(
    state_key: str,
    common: dict[str, Any],
    result: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """A key settled as absent produced an order after all. Block new orders."""
    client_order_id = str(common.get("client_order_id") or "")
    try:
        append_event(
            new_event(
                "EXECUTION_CONFLICT",
                f"conflict:{client_order_id}",
                **common,
                conflict_reason=reason,
                order_state_key=state_key,
                exchange_order_id=str(result.get("order_id") or ""),
            )
        )
    except Exception:  # noqa: BLE001 - the conflict is still reported to the caller
        pass
    return {
        **result,
        "status": "unknown",
        "error": "order outcome conflicts with the journal (it was marked absent); new orders are blocked",
        "journal_state": "EXECUTION_CONFLICT",
        "client_order_id": client_order_id,
    }


def _unjournaled(result: dict[str, Any], common: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "status": "unknown",
        "error": "submission outcome was not journaled",
        "client_order_id": common.get("client_order_id", ""),
    }


def _resolve_unknown_submission(
    state_key: str,
    common: dict[str, Any],
    result: dict[str, Any],
    config: Any,
) -> dict[str, Any]:
    """Query the order once after an unknown outcome.

    Only a found order resolves the key. ``not_found`` right after a timeout
    may mean Binance has not recorded the order yet, so the key stays
    ``SUBMISSION_UNKNOWN`` until ``reconcile_journal`` sees it absent after the
    acceptance window.
    """
    client_order_id = str(common.get("client_order_id") or "")
    unresolved = {**result, "client_order_id": client_order_id}
    if config is None or not client_order_id:
        return unresolved
    lookup = _guarded(
        sdk.get_order, config, symbol=str(common.get("symbol") or ""), client_order_id=client_order_id
    )
    order = lookup.get("order") if lookup.get("status") == "ok" else None
    if not isinstance(order, dict):
        return {**unresolved, "status_query": lookup.get("status", "error")}
    try:
        append_event(
            new_event(
                "ORDER_SUBMITTED",
                state_key,
                **common,
                exchange_status="ok",
                exchange_order_id=str(order.get("order_id") or ""),
                exchange_order_status=str(order.get("status") or ""),
                resolved_by="status_query",
            )
        )
    except (PersistError, InvalidTransition, Exception):  # noqa: BLE001
        return {**unresolved, "status_query": "ok", "order": order}
    return {
        "status": "ok",
        "order_id": str(order.get("order_id") or ""),
        "client_order_id": client_order_id,
        "symbol": order.get("symbol"),
        "order_status": str(order.get("status") or ""),
        "resolved_by": "status_query",
    }


def _latest_row(rows: list[dict[str, Any]], state_key: str) -> dict[str, Any] | None:
    found = None
    for row in rows:
        if str(row.get("state_key") or "") == state_key:
            found = row
    return found


def _cancel_state_key(profile: TradingProfile, symbol: str, order_id: str) -> str:
    """Return the journal key for cancelling ``order_id`` on ``symbol``.

    Binance numbers orders per symbol, so the key names the profile (and with
    it the environment), the symbol and the order id: cancelling order 123 on
    BTC/USDT and order 123 on ETH/USDT are different operations. A record
    written before the symbol was part of the key (``cancel:<order id>``) is
    reused when it belongs to this profile and symbol, so a repeated cancel
    still replays its recorded outcome after an upgrade.
    """
    key = f"cancel:{profile.id}:{symbol}:{order_id}"
    legacy = f"cancel:{order_id}"
    try:
        rows = _records()
    except Exception:  # noqa: BLE001 - append_event reports an unreadable journal
        return key
    if any(str(row.get("state_key") or "") == key for row in rows):
        return key
    for row in reversed(rows):
        if str(row.get("state_key") or "") == legacy:
            if row.get("profile_id") == profile.id and row.get("symbol") == symbol:
                return legacy
            break
    return key


def _latest_event(state_key: str) -> str | None:
    try:
        return _latest_states(_records()).get(state_key)
    except Exception:  # noqa: BLE001 - an unreadable journal settles nothing
        return None


def _replay(recorded: Mapping[str, Any]) -> dict[str, Any]:
    event = str(recorded.get("event") or "")
    body: dict[str, Any] = {
        "idempotent_replay": True,
        "journal_state": event,
        "client_order_id": recorded.get("client_order_id", ""),
        "symbol": recorded.get("symbol"),
        "side": recorded.get("side"),
    }
    if event == "ORDER_SUBMITTED":
        return {**body, "status": "ok", "order_id": recorded.get("exchange_order_id", "")}
    if event == "ORDER_ABSENT":
        return {**body, "status": "error", "error": "the exchange has no record of this order"}
    return {**body, "status": "error", "error": "the exchange rejected this order"}


def _request_fingerprint(
    symbol: str,
    side: str,
    order_type: str,
    time_in_force: str,
    quantity: Any,
    notional: Any,
    limit_price: Any,
    stop_price: Any = None,
) -> str:
    """Hash the caller's request so a reused idempotency key must repeat it exactly."""

    def norm(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return format(Decimal(str(value).strip()).normalize(), "f")
        except (ArithmeticError, ValueError):
            return str(value)

    body = {
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "time_in_force": time_in_force if order_type in {"limit", *_PROTECTED_TYPES} else None,
        "quantity": norm(quantity),
        "notional": norm(notional),
        "limit_price": norm(limit_price),
    }
    if order_type in _PROTECTED_TYPES:
        body["stop_price"] = norm(stop_price)
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _audit_only_denial(profile: TradingProfile, *, cancel: bool = False) -> dict[str, Any] | None:
    if profile.id != _PAPER_PROFILE:
        return None
    on, source = sdk.testnet_audit_only()
    if not on:
        return None
    detail = f"Spot Testnet audit-only mode is on (source: {source}); set testnet_audit_only to false in binance.json to allow writes"
    if cancel:
        detail += "; resting orders stay open until audit-only is turned off"
    return _deny(("audit_only",), detail=detail)


def _deny_preflight(state_key: str, common: dict[str, Any], reasons: tuple[str, ...]) -> None:
    """Close an allowed preflight that will not be submitted. Best effort."""
    try:
        append_event(new_event("PREFLIGHT_DENIED", state_key, **common, reasons=list(reasons)))
    except Exception:  # noqa: BLE001 - nothing was sent; the deny stands either way
        pass


def _paper_notional_reasons(
    symbol: str,
    sized: Any,
    *,
    order_type: str,
    side: str,
    rules: Any,
    quote: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Check the Spot Testnet per-order notional cap, independent of exchange filters.

    The cap is in the symbol's exchange quote asset (USDT for BTC/USDT) and is
    compared with the most the order can trade, a conservative local estimate
    (exchange filters are checked separately by ``size_order``):

    * market: must be notional-sized; Binance trades at most ``quoteOrderQty``,
      floored to the price tick;
    * limit buy: quantity x limit price (it can only fill at or below it);
    * limit sell: quantity x max(limit price, best bid). A sell priced below
      the bid fills at the bid, so the limit price alone understates it. The
      bid must come from a fresh exchange-timestamped quote (``QUOTE_MAX_AGE``);
      a missing, stale, malformed or non-positive bid denies the order.

    A cap below the exchange minimum notional can never be met (infeasible).
    """
    limit = sdk.paper_order_notional_limit(symbol.split("/", 1)[1])
    if limit is None:
        return ("notional_limit_unconfigured",)
    minimum = getattr(rules, "min_notional", None)
    if minimum is not None and limit < minimum:
        return ("notional_limit_infeasible",)
    if order_type == "market" and sized.notional is None:
        return ("notional_required_for_market",)
    if sized.notional is not None:
        order_notional = sized.notional
    else:
        if sized.limit_price is None or sized.quantity is None:
            return ("notional_unknown",)
        price = sized.limit_price
        if side == "sell":
            bid, reason = _fresh_bid(quote or {}, symbol, now)
            if bid is None:
                return (reason,)
            price = max(price, bid)
        order_notional = sized.quantity * price
    if order_notional > limit:
        return ("notional_limit_exceeded",)
    return ()


def _fresh_bid(quote: Mapping[str, Any], symbol: str, now: datetime | None) -> tuple[Decimal | None, str]:
    """Best bid from a fresh quote for this symbol, read on the high side."""
    if str(quote.get("status", "")).lower() != "ok" or quote.get("symbol") not in (None, "", symbol):
        return None, "quote_unavailable"
    body = quote.get("quote") if isinstance(quote.get("quote"), Mapping) else None
    if body is None:
        return None, "quote_unavailable"
    fresh, _ = quote_is_fresh(quote, now=now or datetime.now(timezone.utc))
    if not fresh:
        return None, "quote_stale"
    bid = _as_decimal(body.get("bid"), high=True)
    if bid is None:
        return None, "quote_unavailable"
    return bid, ""


def _journal_denial(
    profile: TradingProfile,
    symbol: str,
    side: str,
    order_type: str,
    reasons: tuple[str, ...],
    session_id: str,
    *,
    client_order_id: str = "",
    intent_id: str = "",
) -> dict[str, Any]:
    # Every denial is its own record, so a keyed retry is evaluated fresh.
    state_key = f"deny:{uuid.uuid4()}"
    try:
        append_event(
            new_event(
                "PREFLIGHT_DENIED",
                state_key,
                **_event_fields(
                    profile,
                    session_id=session_id,
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=None,
                    notional=None,
                    limit_price=None,
                    reasons=list(reasons),
                ),
            )
        )
    except (PersistError, InvalidTransition, Exception):
        return _deny(("journal_unavailable",))
    return _deny(reasons)


def _event_fields(profile: TradingProfile, **fields: Any) -> dict[str, Any]:
    body = {
        "profile_id": profile.id,
        "environment": profile.environment,
        "market_type": "spot",
    }
    body.update(fields)
    return {key: value for key, value in body.items() if value is not None}


def _identity_error(profile: TradingProfile, config: Any) -> dict[str, Any] | None:
    if profile.id == _BOT_LIVE_PROFILE:
        if getattr(profile, "environment", None) != "live" or bool(getattr(profile, "readonly", True)):
            return _deny(("profile_not_tradable",))
        if getattr(config, "profile", None) != "live-trade-bot" or getattr(config, "market_type", None) != "spot":
            return _deny(("profile_not_tradable",))
        host = _https_host(str(getattr(config, "host", "") or ""))
        if host != _LIVE_HOST:
            return _deny(("host_rejected",))
        return None
    if profile.id == _LIVE_PROFILE or getattr(profile, "environment", None) != "paper":
        # Binance Production is read-only, whatever the profile table says,
        # except the gated bot profile handled above.
        return _deny(("production_writes_disabled",))
    if profile.id == _PAPER_PROFILE:
        expected_profile = "paper"
    else:
        return _deny(("profile_not_tradable",))
    if getattr(config, "profile", None) != expected_profile or getattr(config, "market_type", None) != "spot":
        return _deny(("profile_not_tradable",))
    host = _https_host(str(getattr(config, "host", "") or ""))
    if profile.id == _LIVE_PROFILE:
        if host != _LIVE_HOST:
            return _deny(("host_rejected",))
        return None
    expected = _https_host(sdk.DEFAULT_TESTNET_HOST)
    if host is None or expected is None or host != expected or host in _BLOCKED_HOSTS:
        return _deny(("host_rejected",))
    return None


def _https_host(url: str) -> str | None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    if parsed.port not in (None, 443):
        return None
    hostname = (parsed.hostname or "").lower()
    return hostname or None


def _structural_error(
    *,
    side: str,
    order_type: str,
    quantity: Any,
    notional: Any,
    limit_price: Any,
    time_in_force: str,
    stop_price: Any = None,
) -> dict[str, Any] | None:
    if str(side or "").strip().lower() not in {"buy", "sell"}:
        return _deny(("invalid_side",))
    kind = str(order_type or "").strip().lower()
    if kind not in {"market", "limit", *_PROTECTED_TYPES}:
        return _deny(("invalid_order_type",))
    if (quantity is None) == (notional is None):
        return _deny(("size_conflict",) if quantity is not None else ("size_missing",))
    priced = kind in {"limit", *_PROTECTED_TYPES}
    if priced and notional is not None:
        return _deny(("notional_on_limit",))
    if priced and limit_price is None:
        return _deny(("limit_price_required",))
    if kind in _PROTECTED_TYPES and stop_price is None:
        return _deny(("stop_price_required",))
    if priced and str(time_in_force or "").strip().lower() not in _TIME_IN_FORCE:
        return _deny(("time_in_force_rejected",))
    return None


def _guarded(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a failed read is not an order
        return {"status": "error", "error": "read_failed", "detail": type(exc).__name__}
    if not isinstance(result, dict):
        return {"status": "error", "error": "read_failed"}
    return result


def _deny(reasons: tuple[str, ...], **extra: Any) -> dict[str, Any]:
    safe = {key: value for key, value in extra.items() if key == "detail"}
    return {
        "status": "error",
        "decision": "DENY",
        "reasons": list(reasons),
        "error": "order blocked: " + ", ".join(reasons),
        **safe,
    }

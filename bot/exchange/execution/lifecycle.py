# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Allowed Binance spot order-journal transitions.

A client order id or cancel key has one latest event. Terminal events are not
reopened, and an in-flight or unknown submission is not a new submission.

A pending or unknown write is resolved only by exchange evidence: the order was
found (``ORDER_SUBMITTED``), or Binance reported it absent after its acceptance
window (``ORDER_ABSENT``). A cancel of unknown outcome resolves to
``CANCEL_ACKNOWLEDGED`` or ``CANCEL_REJECTED`` the same way.

Order key lifecycle (``order:<client order id>``)::

    PREFLIGHT_ALLOWED -> PREFLIGHT_DENIED            denied before submission
                      -> SUBMISSION_PENDING          may be handed to the exchange
    SUBMISSION_PENDING -> SUBMISSION_ABORTED         adapter proved nothing was sent
                       -> ORDER_SUBMITTED | ORDER_REJECTED | SUBMISSION_UNKNOWN
                       -> ORDER_ABSENT               operator confirmation only
    SUBMISSION_UNKNOWN -> ORDER_SUBMITTED | ORDER_ABSENT (reconcile evidence)

``PREFLIGHT_DENIED`` and ``SUBMISSION_ABORTED`` prove the exchange never saw
the client order id, so the same idempotency key may start again
(``PREFLIGHT_ALLOWED``). Every other outcome is final for its key.

Cancel key lifecycle (``cancel:<profile id>:<symbol>:<order id>``; journals
written before the symbol was part of the key use ``cancel:<order id>``):
``CANCEL_REQUESTED`` is written
before the request and blocks new orders until it resolves. It becomes
``CANCEL_ACKNOWLEDGED`` (final), ``CANCEL_REJECTED`` or ``CANCEL_ABORTED``
(nothing sent), from which a new ``CANCEL_REQUESTED`` may retry, or
``CANCEL_UNKNOWN`` (blocking until reconciled).

``EXECUTION_CONFLICT`` (``conflict:<client order id>``) records exchange
evidence that contradicts a settled key, such as an order found after it was
marked absent. It blocks new orders until an operator records
``CONFLICT_ACKNOWLEDGED``. ``CHECKPOINT_ADOPTED`` (``admin:<uuid>``) records
an operator re-anchoring the journal checkpoint.
"""

from __future__ import annotations

#: Events that mean a write might have reached the exchange and is not resolved.
BLOCKING_EVENTS = frozenset(
    {
        "SUBMISSION_PENDING",
        "SUBMISSION_UNKNOWN",
        "CANCEL_REQUESTED",
        "CANCEL_UNKNOWN",
        "EXECUTION_CONFLICT",
    }
)

_NEXT: dict[str | None, frozenset[str]] = {
    None: frozenset(
        {
            "PREFLIGHT_DENIED",
            "PREFLIGHT_ALLOWED",
            "SUBMISSION_PENDING",
            "CANCEL_REQUESTED",
            "RECONCILIATION_MISMATCH",
            "EXECUTION_CONFLICT",
            "CHECKPOINT_ADOPTED",
        }
    ),
    "PREFLIGHT_ALLOWED": frozenset({"SUBMISSION_PENDING", "PREFLIGHT_DENIED"}),
    "PREFLIGHT_DENIED": frozenset({"PREFLIGHT_ALLOWED"}),
    "SUBMISSION_PENDING": frozenset(
        {
            "ORDER_SUBMITTED",
            "ORDER_REJECTED",
            "SUBMISSION_UNKNOWN",
            "ORDER_ABSENT",
            "SUBMISSION_ABORTED",
        }
    ),
    "SUBMISSION_ABORTED": frozenset({"PREFLIGHT_ALLOWED"}),
    "ORDER_SUBMITTED": frozenset(),
    "ORDER_REJECTED": frozenset(),
    "ORDER_ABSENT": frozenset(),
    "SUBMISSION_UNKNOWN": frozenset({"ORDER_SUBMITTED", "ORDER_ABSENT"}),
    "CANCEL_REQUESTED": frozenset(
        {"CANCEL_ACKNOWLEDGED", "CANCEL_REJECTED", "CANCEL_UNKNOWN", "CANCEL_ABORTED"}
    ),
    "CANCEL_ACKNOWLEDGED": frozenset(),
    "CANCEL_REJECTED": frozenset({"CANCEL_REQUESTED"}),
    "CANCEL_ABORTED": frozenset({"CANCEL_REQUESTED"}),
    "CANCEL_UNKNOWN": frozenset({"CANCEL_ACKNOWLEDGED", "CANCEL_REJECTED"}),
    "RECONCILIATION_MISMATCH": frozenset(),
    "EXECUTION_CONFLICT": frozenset({"CONFLICT_ACKNOWLEDGED"}),
    "CONFLICT_ACKNOWLEDGED": frozenset(),
    "CHECKPOINT_ADOPTED": frozenset(),
}

#: Latest events after which the same idempotency key may start a new attempt,
#: because the exchange never received that client order id.
ORDER_REUSABLE_EVENTS = frozenset({"PREFLIGHT_DENIED", "SUBMISSION_ABORTED"})


#: Latest events after which an order key needs no further resolution.
ORDER_TERMINAL_EVENTS = frozenset({"ORDER_SUBMITTED", "ORDER_REJECTED", "ORDER_ABSENT"})


def transition_allowed(previous: str | None, event: str) -> bool:
    """Return whether ``event`` may follow ``previous`` for the same key."""
    return event in _NEXT.get(previous, frozenset())

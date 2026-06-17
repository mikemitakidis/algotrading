"""M20.A paper-order lifecycle — states + pure transition validation.

A fill must happen before a close: PENDING_SIMULATION can never go straight to
CLOSED. Terminal states never transition out. No live/broker semantics.
"""
from __future__ import annotations

from enum import Enum


class PaperOrderStatus(str, Enum):
    PENDING_SIMULATION = "PENDING_SIMULATION"
    REJECTED_BY_PAPER_RISK = "REJECTED_BY_PAPER_RISK"
    ROUTED_TO_PAPER = "ROUTED_TO_PAPER"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


# Allowed forward transitions. Terminal states map to an empty frozenset.
_ALLOWED: dict = {
    PaperOrderStatus.PENDING_SIMULATION: frozenset({
        PaperOrderStatus.ROUTED_TO_PAPER,
        PaperOrderStatus.REJECTED_BY_PAPER_RISK,
        PaperOrderStatus.CANCELED,
        PaperOrderStatus.EXPIRED,
    }),
    PaperOrderStatus.ROUTED_TO_PAPER: frozenset({
        PaperOrderStatus.PARTIAL_FILL,
        PaperOrderStatus.FILLED,
        PaperOrderStatus.CANCELED,
        PaperOrderStatus.EXPIRED,
    }),
    PaperOrderStatus.PARTIAL_FILL: frozenset({
        PaperOrderStatus.PARTIAL_FILL,
        PaperOrderStatus.FILLED,
        PaperOrderStatus.CANCELED,
        PaperOrderStatus.CLOSED,
    }),
    PaperOrderStatus.FILLED: frozenset({
        PaperOrderStatus.CLOSED,
    }),
    # terminal
    PaperOrderStatus.REJECTED_BY_PAPER_RISK: frozenset(),
    PaperOrderStatus.CANCELED: frozenset(),
    PaperOrderStatus.EXPIRED: frozenset(),
    PaperOrderStatus.CLOSED: frozenset(),
}

TERMINAL_STATES = frozenset(
    s for s, nxt in _ALLOWED.items() if not nxt)


class InvalidPaperTransition(Exception):
    """Raised when an illegal paper-order lifecycle transition is attempted."""


def _coerce(status) -> PaperOrderStatus:
    if isinstance(status, PaperOrderStatus):
        return status
    if isinstance(status, str):
        try:
            return PaperOrderStatus(status)
        except ValueError:
            raise InvalidPaperTransition(f"unknown status: {status!r}")
    raise InvalidPaperTransition(f"unknown status: {status!r}")


def is_valid_transition(from_status, to_status) -> bool:
    """Pure predicate: True iff from_status -> to_status is allowed."""
    f = _coerce(from_status)
    t = _coerce(to_status)
    return t in _ALLOWED[f]


def validate_transition(from_status, to_status) -> PaperOrderStatus:
    """Return the (coerced) to_status if the transition is valid, else raise
    InvalidPaperTransition."""
    f = _coerce(from_status)
    t = _coerce(to_status)
    if t not in _ALLOWED[f]:
        raise InvalidPaperTransition(
            f"illegal paper transition: {f.value} -> {t.value}")
    return t

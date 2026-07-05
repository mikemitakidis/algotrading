#!/usr/bin/env python3
"""M21.1extra-D2 — loop state machine + cycle records.

The dry-run path decides what a real cycle WOULD do, given injected gate
decisions and candidates, and NEVER places an order (its only success terminal
is DRY_RUN_WOULD_SUBMIT). The supervised path (run_supervised_cycle) reuses the
same states plus the supervised-only lifecycle states (ORDER_SUBMITTED,
POSITION_OBSERVED, FLATTEN_CONFIRMED, PERSISTED), which a dry-run record is
forbidden from carrying (see DryRunCycleRecord.__post_init__ and
DRY_RUN_ALLOWED_STATES).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


class LoopState(str, Enum):
    IDLE = "IDLE"
    SCAN_STARTED = "SCAN_STARTED"
    NO_CANDIDATE = "NO_CANDIDATE"
    CANDIDATE_SELECTED = "CANDIDATE_SELECTED"
    D0_BLOCKED = "D0_BLOCKED"
    D1_BLOCKED = "D1_BLOCKED"
    PRECHECK_FAILED = "PRECHECK_FAILED"
    DRY_RUN_WOULD_SUBMIT = "DRY_RUN_WOULD_SUBMIT"
    # Supervised (real paper lifecycle) states — reached only AFTER both gates
    # are affirmative, and only via run_supervised_cycle (never dry-run).
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    POSITION_OBSERVED = "POSITION_OBSERVED"
    FLATTEN_CONFIRMED = "FLATTEN_CONFIRMED"
    PERSISTED = "PERSISTED"
    FAILED_CLOSED = "FAILED_CLOSED"


# Terminal states a cycle can finish in.
TERMINAL_STATES = frozenset({
    LoopState.NO_CANDIDATE,
    LoopState.D0_BLOCKED,
    LoopState.D1_BLOCKED,
    LoopState.PRECHECK_FAILED,
    LoopState.DRY_RUN_WOULD_SUBMIT,
    LoopState.PERSISTED,
    LoopState.FAILED_CLOSED,
})

# States a DRY-RUN record may legitimately hold. Supervised-only states
# (ORDER_SUBMITTED / POSITION_OBSERVED / FLATTEN_CONFIRMED / PERSISTED) are
# excluded — a dry-run places no orders and persists nothing, so a dry-run
# record carrying one of those states would violate the dry-run contract.
DRY_RUN_ALLOWED_STATES = frozenset({
    LoopState.IDLE,
    LoopState.SCAN_STARTED,
    LoopState.NO_CANDIDATE,
    LoopState.CANDIDATE_SELECTED,
    LoopState.D0_BLOCKED,
    LoopState.D1_BLOCKED,
    LoopState.PRECHECK_FAILED,
    LoopState.DRY_RUN_WOULD_SUBMIT,
    LoopState.FAILED_CLOSED,
})

# Supervised-only states, forbidden in a dry-run record.
SUPERVISED_ONLY_STATES = frozenset({
    LoopState.ORDER_SUBMITTED,
    LoopState.POSITION_OBSERVED,
    LoopState.FLATTEN_CONFIRMED,
    LoopState.PERSISTED,
})

# The ONLY state in which a real cycle would have submitted.
_WOULD_SUBMIT_STATE = LoopState.DRY_RUN_WOULD_SUBMIT


@dataclass(frozen=True)
class DryRunCycleRecord:
    """Immutable in-memory result of one dry-run cycle. D2.0 returns this; it
    does NOT persist it. `would_submit` is True iff state is
    DRY_RUN_WOULD_SUBMIT (enforced in __post_init__)."""
    cycle_id: str
    started_at_utc: str
    finished_at_utc: str
    state: str
    would_submit: bool
    d0_checked: bool
    d1_checked: bool
    mode: str = "dry_run"
    terminal: bool = True
    symbol: Optional[str] = None
    candidate_score: Optional[float] = None
    d0_allowed: Optional[bool] = None
    d0_reason: Optional[str] = None
    d1_may_submit: Optional[bool] = None
    d1_reason: Optional[str] = None
    market_calendar_id: str = "US_EQ"
    submit_blocked_reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # explicit runtime checks (never assert — must survive python -O).
        valid_states = {s.value for s in LoopState}
        if self.state not in valid_states:
            raise ValueError("unknown loop state: %r" % (self.state,))
        # a DRY-RUN record must never carry a supervised-only state.
        if self.state in {s.value for s in SUPERVISED_ONLY_STATES}:
            raise ValueError(
                "dry-run record cannot hold supervised state %r" % self.state)
        if self.state not in {s.value for s in DRY_RUN_ALLOWED_STATES}:
            raise ValueError(
                "state %r is not a valid dry-run state" % self.state)
        if self.mode != "dry_run":
            raise ValueError("D2.0 record mode must be 'dry_run'")
        # the core invariant: would_submit True <=> DRY_RUN_WOULD_SUBMIT.
        is_would = (self.state == _WOULD_SUBMIT_STATE.value)
        if self.would_submit is not is_would:
            raise ValueError(
                "would_submit must be True iff state==DRY_RUN_WOULD_SUBMIT "
                "(state=%r, would_submit=%r)" % (self.state, self.would_submit))
        # a submit must never coincide with a blocking reason or errors.
        if self.would_submit and self.submit_blocked_reason is not None:
            raise ValueError("would_submit=True cannot carry a blocked reason")
        if self.would_submit and self.errors:
            raise ValueError("would_submit=True cannot carry errors")

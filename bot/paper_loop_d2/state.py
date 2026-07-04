#!/usr/bin/env python3
"""M21.1extra-D2.0 — loop state machine + dry-run cycle record.

D2.0 is PURE dry-run orchestration. It decides what a real cycle WOULD do,
given injected gate decisions and candidates, and NEVER places an order, opens
a broker/gateway connection, touches D0/D1 modules, hits the network, or writes
a DB. The only "success" terminal is DRY_RUN_WOULD_SUBMIT (a real cycle would
submit here) — there is deliberately no ORDER_SUBMITTED state in D2.0.
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
    FAILED_CLOSED = "FAILED_CLOSED"


# Terminal states a cycle can finish in.
TERMINAL_STATES = frozenset({
    LoopState.NO_CANDIDATE,
    LoopState.D0_BLOCKED,
    LoopState.D1_BLOCKED,
    LoopState.PRECHECK_FAILED,
    LoopState.DRY_RUN_WOULD_SUBMIT,
    LoopState.FAILED_CLOSED,
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

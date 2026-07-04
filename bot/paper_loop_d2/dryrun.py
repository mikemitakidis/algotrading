#!/usr/bin/env python3
"""M21.1extra-D2.0 — dry-run cycle orchestration (state machine).

run_dry_cycle(...) walks: IDLE -> SCAN_STARTED -> (NO_CANDIDATE | CANDIDATE_
SELECTED) -> gates -> (D0_BLOCKED | D1_BLOCKED | PRECHECK_FAILED |
DRY_RUN_WOULD_SUBMIT), with FAILED_CLOSED as the fail-closed backstop for any
exception or ambiguity (incl. a malformed candidate).

Everything external is INJECTED — clock, cycle-id factory, candidate provider,
D0 gate, D1 gate, threshold. D2.0 imports NO broker, NO D0/D1 modules, NO
network, NO DB. Gate callables mirror the real shapes so D2.1 can drop in the
real adapters unchanged:
    d0_gate()  -> (allowed: bool, reason: str)     # runtime_policy: not-skip == allowed
    d1_gate()  -> (may_submit: bool, reason: str)  # MarketClockDecision shape
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Callable, Sequence, Tuple, List, Any

from bot.paper_loop_d2.state import LoopState, DryRunCycleRecord

# Injected callable types (documentation only).
ClockFn = Callable[[], datetime]
CycleIdFn = Callable[[], str]
CandidateProvider = Callable[[], Sequence[Any]]
D0Gate = Callable[[], Tuple[bool, str]]
D1Gate = Callable[[], Tuple[bool, str]]

_MARKET_CALENDAR_ID = "US_EQ"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_cycle_id() -> str:
    # NON-deterministic by design: a fresh unique id per cycle (see tightening 1).
    return "d2cyc-%s-%s" % (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"), uuid.uuid4().hex)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        # never assume naive is UTC; stamp explicitly as UTC to stay honest.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class MalformedCandidate(ValueError):
    """A candidate did not match the (symbol: str, score: number) contract."""


class _DependencyError(RuntimeError):
    """An injected dependency (clock/cycle_id) produced unusable output."""


def _validate_candidate(item: Any) -> Tuple[str, float]:
    """Return (symbol, score) or raise MalformedCandidate. Strict: the provider
    contract is (str, number); anything else is ambiguous -> fail closed."""
    if not isinstance(item, (tuple, list)) or len(item) != 2:
        raise MalformedCandidate("candidate not a (symbol, score) pair: %r"
                                 % (item,))
    symbol, score = item
    if not isinstance(symbol, str) or not symbol:
        raise MalformedCandidate("candidate symbol invalid: %r" % (symbol,))
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise MalformedCandidate("candidate score not a number: %r" % (score,))
    if score != score:  # NaN
        raise MalformedCandidate("candidate score is NaN")
    return symbol, float(score)


def _select_candidate(items: Sequence[Any], threshold: float
                      ) -> Optional[Tuple[str, float]]:
    """Deterministic selection. Validates ALL items (a malformed one raises ->
    fail closed). Among items with score >= threshold, pick the highest score,
    breaking ties by lexicographic symbol. None if nothing qualifies."""
    validated: List[Tuple[str, float]] = [_validate_candidate(it) for it in items]
    qualifying = [(sym, sc) for sym, sc in validated if sc >= threshold]
    if not qualifying:
        return None
    # deterministic: max score, then lexicographic symbol.
    qualifying.sort(key=lambda p: (-p[1], p[0]))
    return qualifying[0]


def run_dry_cycle(
    *,
    d0_gate: D0Gate,
    d1_gate: D1Gate,
    candidate_provider: CandidateProvider,
    threshold: float,
    clock: Optional[ClockFn] = None,
    cycle_id_factory: Optional[CycleIdFn] = None,
) -> DryRunCycleRecord:
    """Run one dry-run cycle and return a DryRunCycleRecord. Never places an
    order, never touches a broker/D0/D1/network/DB. Fail-closed throughout —
    including a bad injected clock or cycle_id factory (those yield a
    FAILED_CLOSED record rather than raising)."""
    clock = clock or _default_clock
    cycle_id_factory = cycle_id_factory or _default_cycle_id

    # accumulators
    symbol: Optional[str] = None
    score: Optional[float] = None
    d0_checked = d1_checked = False
    d0_allowed: Optional[bool] = None
    d0_reason: Optional[str] = None
    d1_may: Optional[bool] = None
    d1_reason: Optional[str] = None
    errors: List[str] = []

    def _now() -> datetime:
        """Call the injected clock and REQUIRE a timezone-aware datetime.
        Raises on bad output; callers convert that into FAILED_CLOSED."""
        val = clock()
        if not isinstance(val, datetime):
            raise _DependencyError("clock did not return a datetime")
        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
            raise _DependencyError("clock returned a naive datetime")
        return val.astimezone(timezone.utc)

    def _safe_now_iso() -> str:
        """A timestamp that always works, ONLY for building a failure record
        when the injected clock is unusable. Uses real UTC now."""
        try:
            return _iso(_now())
        except Exception:
            return _iso(datetime.now(timezone.utc))

    # ── set up clock + cycle_id under fail-closed handling ────────────────
    try:
        started = _now()
    except Exception as exc:
        errors.append("clock_setup: %s" % type(exc).__name__)
        return DryRunCycleRecord(
            cycle_id="d2cyc-clock-error", started_at_utc=_safe_now_iso(),
            finished_at_utc=_safe_now_iso(),
            state=LoopState.FAILED_CLOSED.value, would_submit=False,
            d0_checked=False, d1_checked=False,
            market_calendar_id=_MARKET_CALENDAR_ID,
            submit_blocked_reason="clock_dependency_error", errors=errors)

    try:
        cycle_id = cycle_id_factory()
        if not isinstance(cycle_id, str) or not cycle_id:
            raise _DependencyError("cycle_id_factory returned empty/non-string")
    except Exception as exc:
        errors.append("cycle_id_setup: %s" % type(exc).__name__)
        return DryRunCycleRecord(
            cycle_id="d2cyc-id-error", started_at_utc=_iso(started),
            finished_at_utc=_safe_now_iso(),
            state=LoopState.FAILED_CLOSED.value, would_submit=False,
            d0_checked=False, d1_checked=False,
            market_calendar_id=_MARKET_CALENDAR_ID,
            submit_blocked_reason="cycle_id_dependency_error", errors=errors)

    def _finish(state: LoopState, *, would_submit: bool = False,
                blocked: Optional[str] = None) -> DryRunCycleRecord:
        return DryRunCycleRecord(
            cycle_id=cycle_id, started_at_utc=_iso(started),
            finished_at_utc=_safe_now_iso(), state=state.value,
            would_submit=would_submit, d0_checked=d0_checked,
            d1_checked=d1_checked, symbol=symbol, candidate_score=score,
            d0_allowed=d0_allowed, d0_reason=d0_reason, d1_may_submit=d1_may,
            d1_reason=d1_reason, market_calendar_id=_MARKET_CALENDAR_ID,
            submit_blocked_reason=blocked, errors=list(errors))

    try:
        # SCAN_STARTED -> candidate selection
        try:
            items = candidate_provider()
        except Exception as exc:
            errors.append("candidate_provider: %s" % type(exc).__name__)
            return _finish(LoopState.FAILED_CLOSED,
                           blocked="candidate_provider_error")
        try:
            picked = _select_candidate(items, threshold)
        except MalformedCandidate as exc:
            errors.append("malformed_candidate: %s" % str(exc)[:80])
            return _finish(LoopState.FAILED_CLOSED,
                           blocked="malformed_candidate")

        if picked is None:
            return _finish(LoopState.NO_CANDIDATE, blocked="no_candidate")
        symbol, score = picked

        # CANDIDATE_SELECTED -> D0 gate
        d0_checked = True
        try:
            d0_allowed, d0_reason = d0_gate()
        except Exception as exc:
            errors.append("d0_gate: %s" % type(exc).__name__)
            return _finish(LoopState.FAILED_CLOSED, blocked="d0_gate_error")
        if d0_allowed is not True:
            return _finish(LoopState.D0_BLOCKED,
                           blocked=d0_reason or "d0_blocked")

        # D1 gate
        d1_checked = True
        try:
            d1_may, d1_reason = d1_gate()
        except Exception as exc:
            errors.append("d1_gate: %s" % type(exc).__name__)
            return _finish(LoopState.FAILED_CLOSED, blocked="d1_gate_error")
        if d1_may is not True:
            return _finish(LoopState.D1_BLOCKED,
                           blocked=d1_reason or "d1_blocked")

        # both gates affirmative + valid candidate -> WOULD submit (dry run).
        return _finish(LoopState.DRY_RUN_WOULD_SUBMIT, would_submit=True)

    except Exception as exc:  # absolute fail-closed backstop
        errors.append("unexpected: %s" % type(exc).__name__)
        return _finish(LoopState.FAILED_CLOSED, blocked="internal_error")

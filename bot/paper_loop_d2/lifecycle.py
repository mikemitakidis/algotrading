#!/usr/bin/env python3
"""M21.1extra-D2 — supervised paper lifecycle orchestration.

run_supervised_cycle(...) runs ONE real paper lifecycle, but only after the D0
authorization gate and the D1 market-clock gate are BOTH affirmative. It reuses
the existing, proven primitives and creates NO new broker path:

  * B2b tools.paper_loop.m21_1extra_b2b_single_submit.run_lifecycle(...)
    is called EXACTLY ONCE. B2b already performs its own paper-mode / account /
    port / size-cap checks and its own internal flatten via
    broker.flatten_paper_position(symbol, confirm=True). D2 does NOT call a
    separate flatten on the happy path (no double-flatten).
  * C tools.paper_loop.m21_1extra_c_outcome_persistence.persist_lifecycle(...)
    is called ONCE to persist the (enriched) B2b result.

D2 builds no OrderIntent, imports no broker adapter, never writes
execution_intents. This module is dry-run's sibling: run_dry_cycle is untouched.

Fail-closed: any gate block -> no B2b call. Any B2b refusal / non-confirmation /
exception -> FAILED_CLOSED (or PRECHECK_FAILED), persisted truthfully. If B2b
raises AFTER the broker may have been touched, a narrow emergency flatten is
attempted via the injected broker's flatten_paper_position; if flatness cannot
be proven, the cycle fails closed LOUDLY and does not claim flatness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Callable, Sequence, Tuple, Any, Dict, List

from bot.paper_loop_d2.state import LoopState
from bot.paper_loop_d2.dryrun import _select_candidate, MalformedCandidate

# Injected callable types.
ClockFn = Callable[[], datetime]
CycleIdFn = Callable[[], str]
CandidateProvider = Callable[[], Sequence[Any]]
D0Gate = Callable[[], Tuple[bool, str]]
D1Gate = Callable[[], Tuple[bool, str]]
# B2b run_lifecycle(...) and C persist_lifecycle(...) are injected so tests use
# a mock broker and a temp DB; production passes the real functions.
B2bRunLifecycle = Callable[..., Dict[str, Any]]
CPersist = Callable[..., Dict[str, Any]]

_MARKET_CALENDAR_ID = "US_EQ"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class SupervisedCycleResult:
    """Plain result holder for a supervised cycle (not persisted itself; the
    B2b record is what C persists). Mirrors the dry-run record's fields plus
    lifecycle outcome fields."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def run_supervised_cycle(
    *,
    d0_gate: D0Gate,
    d1_gate: D1Gate,
    candidate_provider: CandidateProvider,
    threshold: float,
    entry_price: float,
    stop_loss: float,
    target_price: float,
    b2b_run_lifecycle: B2bRunLifecycle,
    c_persist_lifecycle: CPersist,
    broker: Any,
    db_path: Optional[str] = None,
    kill_switch_active: Optional[bool] = None,
    clock: Optional[ClockFn] = None,
    cycle_id_factory: Optional[CycleIdFn] = None,
) -> SupervisedCycleResult:
    """Run ONE supervised paper lifecycle. Paper only; gated by D0 AND D1."""
    clock = clock or _utcnow
    cycle_id_factory = cycle_id_factory or (
        lambda: "d2sup-" + _iso(_utcnow()))

    errors: List[str] = []
    symbol: Optional[str] = None
    score: Optional[float] = None
    d0_checked = d1_checked = False
    d0_allowed: Optional[bool] = None
    d0_reason: Optional[str] = None
    d1_may: Optional[bool] = None
    d1_reason: Optional[str] = None
    b2b_result: Optional[Dict[str, Any]] = None
    persist_result: Optional[Dict[str, Any]] = None

    # fail-closed clock/id setup (same discipline as dry-run).
    try:
        started = clock()
        if not isinstance(started, datetime) or started.tzinfo is None \
                or started.tzinfo.utcoffset(started) is None:
            raise ValueError("clock must return an aware datetime")
        started = started.astimezone(timezone.utc)
    except Exception as exc:
        return SupervisedCycleResult(
            cycle_id="d2sup-clock-error", mode="supervised",
            state=LoopState.FAILED_CLOSED.value, terminal=True,
            would_submit=False, submit_blocked_reason="clock_dependency_error",
            errors=["clock_setup:%s" % type(exc).__name__],
            started_at_utc=_iso(_utcnow()), finished_at_utc=_iso(_utcnow()),
            symbol=None, candidate_score=None, d0_checked=False,
            d1_checked=False, d0_allowed=None, d0_reason=None,
            d1_may_submit=None, d1_reason=None,
            market_calendar_id=_MARKET_CALENDAR_ID, b2b_result=None,
            persist_result=None)

    try:
        cycle_id = cycle_id_factory()
        if not isinstance(cycle_id, str) or not cycle_id:
            raise ValueError("cycle_id must be a non-empty string")
    except Exception as exc:
        return SupervisedCycleResult(
            cycle_id="d2sup-id-error", mode="supervised",
            state=LoopState.FAILED_CLOSED.value, terminal=True,
            would_submit=False,
            submit_blocked_reason="cycle_id_dependency_error",
            errors=["cycle_id_setup:%s" % type(exc).__name__],
            started_at_utc=_iso(started), finished_at_utc=_iso(_utcnow()),
            symbol=None, candidate_score=None, d0_checked=False,
            d1_checked=False, d0_allowed=None, d0_reason=None,
            d1_may_submit=None, d1_reason=None,
            market_calendar_id=_MARKET_CALENDAR_ID, b2b_result=None,
            persist_result=None)

    def _finish(state: LoopState, *, blocked: Optional[str] = None,
                would_submit: bool = False) -> SupervisedCycleResult:
        return SupervisedCycleResult(
            cycle_id=cycle_id, mode="supervised", state=state.value,
            terminal=True, would_submit=would_submit,
            submit_blocked_reason=blocked, errors=list(errors),
            started_at_utc=_iso(started), finished_at_utc=_iso(_utcnow()),
            symbol=symbol, candidate_score=score, d0_checked=d0_checked,
            d1_checked=d1_checked, d0_allowed=d0_allowed, d0_reason=d0_reason,
            d1_may_submit=d1_may, d1_reason=d1_reason,
            market_calendar_id=_MARKET_CALENDAR_ID, b2b_result=b2b_result,
            persist_result=persist_result)

    try:
        # ── candidate selection (same deterministic, fail-closed selector) ──
        try:
            items = candidate_provider()
        except Exception as exc:
            errors.append("candidate_provider:%s" % type(exc).__name__)
            return _finish(LoopState.FAILED_CLOSED,
                           blocked="candidate_provider_error")
        try:
            picked = _select_candidate(items, threshold)
        except MalformedCandidate:
            errors.append("malformed_candidate")
            return _finish(LoopState.FAILED_CLOSED,
                           blocked="malformed_candidate")
        if picked is None:
            return _finish(LoopState.NO_CANDIDATE, blocked="no_candidate")
        symbol, score = picked

        # ── D0 gate ──
        d0_checked = True
        try:
            d0_allowed, d0_reason = d0_gate()
        except Exception as exc:
            errors.append("d0_gate:%s" % type(exc).__name__)
            return _finish(LoopState.FAILED_CLOSED, blocked="d0_gate_error")
        if d0_allowed is not True:
            return _finish(LoopState.D0_BLOCKED,
                           blocked=d0_reason or "d0_blocked")

        # ── D1 gate ──
        d1_checked = True
        try:
            d1_may, d1_reason = d1_gate()
        except Exception as exc:
            errors.append("d1_gate:%s" % type(exc).__name__)
            return _finish(LoopState.FAILED_CLOSED, blocked="d1_gate_error")
        if d1_may is not True:
            return _finish(LoopState.D1_BLOCKED,
                           blocked=d1_reason or "d1_blocked")

        # ── core price validation BEFORE any broker action ──
        # The scheduler will call run_supervised_cycle directly, so this must
        # not rely on the proof runner's validation. Fail closed with NO B2b
        # call and NO persist (a pre-submit config refusal is not a lifecycle).
        if not _prices_valid(entry_price, stop_loss, target_price):
            errors.append("invalid_prices")
            return _finish(LoopState.PRECHECK_FAILED,
                           blocked="invalid_prices")

        # ── both gates affirmative + prices valid: call B2b EXACTLY ONCE ──
        try:
            b2b_result = b2b_run_lifecycle(
                symbol, confirm=True, entry_price=entry_price,
                stop_loss=stop_loss, target_price=target_price,
                kill_switch_active=kill_switch_active, broker=broker)
        except Exception as exc:
            # B2b raised. A broker MAY have been touched. Attempt a narrow
            # emergency flatten via the SAME injected broker (no new wrapper,
            # no B2flat duplication). If we cannot prove flatness, fail closed
            # loudly and do NOT claim flatness.
            errors.append("b2b_raised:%s" % type(exc).__name__)
            flat_ok = _emergency_flatten(broker, symbol, errors)
            if not flat_ok:
                errors.append("EMERGENCY_FLATTEN_UNCONFIRMED")
            # persist the failure truthfully (best effort).
            persist_result = _try_persist(
                c_persist_lifecycle, _enrich(
                    {"symbol": symbol, "lifecycle_confirmed": False,
                     "b2b_raised": type(exc).__name__,
                     "emergency_flatten_confirmed": flat_ok},
                    d0_allowed, d0_reason, d1_may, d1_reason),
                db_path, errors)
            return _finish(LoopState.FAILED_CLOSED, blocked="b2b_exception")

        if not isinstance(b2b_result, dict):
            errors.append("b2b_result_not_dict")
            return _finish(LoopState.FAILED_CLOSED, blocked="b2b_bad_result")

        # enrich the B2b result with D0/D1 provenance for C.
        enriched = _enrich(b2b_result, d0_allowed, d0_reason, d1_may, d1_reason)

        # classify B2b outcome.
        entry_originated = enriched.get("entry_order_originated") is True
        lifecycle_confirmed = enriched.get("lifecycle_confirmed") is True

        if not entry_originated:
            # B2b refused before originating an entry (precheck / paper-mode /
            # size cap / pre-existing position). Persist truthfully.
            persist_result = _try_persist(c_persist_lifecycle, enriched,
                                          db_path, errors)
            return _finish(LoopState.PRECHECK_FAILED,
                           blocked="b2b_precheck_no_entry")

        # an entry WAS originated -> persist the record regardless of outcome.
        persist_result = _try_persist(c_persist_lifecycle, enriched, db_path,
                                      errors)
        if persist_result is None:
            return _finish(LoopState.FAILED_CLOSED, blocked="persist_failed")

        if not lifecycle_confirmed:
            # entry happened but the full lifecycle (observe+flatten) did not
            # confirm. Fail closed; persisted truthfully above.
            return _finish(LoopState.FAILED_CLOSED,
                           blocked="lifecycle_not_confirmed")

        # happy path: B2b confirmed (it flattened internally), persisted once.
        return _finish(LoopState.PERSISTED)

    except Exception as exc:                       # absolute backstop
        errors.append("unexpected:%s" % type(exc).__name__)
        return _finish(LoopState.FAILED_CLOSED, blocked="internal_error")


# ── helpers ─────────────────────────────────────────────────────────────────
def _prices_valid(entry_price: Any, stop_loss: Any, target_price: Any) -> bool:
    """Core price sanity: all positive and stop_loss < entry_price <
    target_price. Fail closed (False) on any non-numeric / NaN / bad order."""
    try:
        e = float(entry_price)
        s = float(stop_loss)
        t = float(target_price)
    except (TypeError, ValueError):
        return False
    if e != e or s != s or t != t:   # NaN
        return False
    if not (e > 0 and s > 0 and t > 0):
        return False
    return s < e < t


def _enrich(b2b: Dict[str, Any], d0_allowed, d0_reason, d1_may,
            d1_reason) -> Dict[str, Any]:
    """Attach D0/D1 provenance + market-clock fields to a B2b result dict.
    market_clock_checked/reason are honored by the (updated) C persister; D0
    reason rides in raw_json (C stores the whole dict as raw_json)."""
    out = dict(b2b)
    out["market_calendar_id"] = _MARKET_CALENDAR_ID
    out["market_clock_checked"] = True
    out["market_clock_reason"] = d1_reason or "market_open"
    out["d0_allowed"] = d0_allowed
    out["d0_reason"] = d0_reason
    out["d1_may_submit"] = d1_may
    out["d1_reason"] = d1_reason
    return out


def _try_persist(c_persist_lifecycle: CPersist, enriched: Dict[str, Any],
                 db_path: Optional[str], errors: List[str]
                 ) -> Optional[Dict[str, Any]]:
    try:
        return c_persist_lifecycle(enriched, db_path=db_path)
    except Exception as exc:
        errors.append("persist_failed:%s" % type(exc).__name__)
        return None


def _emergency_flatten(broker: Any, symbol: Optional[str],
                       errors: List[str]) -> bool:
    """Narrow emergency flatten via the injected broker's own
    flatten_paper_position. Returns True only if flatness is affirmatively
    confirmed. No new wrapper, no B2flat duplication.

    IMPORTANT: when broker is None, B2b builds its OWN internal broker that D2
    cannot reach, so D2 cannot emergency-flatten it. In that case we record an
    explicit, loud marker and return False — a human must verify flatness. We
    never imply a guaranteed flatten when broker is None."""
    if symbol is None:
        errors.append("no_flatten_capability")
        return False
    if broker is None:
        # the real proof runner passes broker=None; B2b's internal broker is
        # unreachable from here. Be explicit and loud; do NOT claim flatness.
        errors.append("emergency_flatten_unavailable_b2b_internal_broker")
        return False
    flatten = getattr(broker, "flatten_paper_position", None)
    if not callable(flatten):
        errors.append("no_flatten_capability")
        return False
    try:
        res = flatten(symbol, confirm=True)
    except Exception as exc:
        errors.append("emergency_flatten_raised:%s" % type(exc).__name__)
        return False
    # accept only an explicit affirmative confirmation.
    if isinstance(res, dict):
        return res.get("flatten_confirmed") is True or res.get("flat") is True
    return res is True

"""M20.B paper routing decision.

Pure, in-memory: take an M19 ScoredSignalCandidate and produce an M20.A
PaperRoutingDecision with paper_routing_eligible = True/False plus deterministic
reason codes. NO order creation, sizing, fills, positions, PnL, storage, or live
trading. NO new M19 fields invented; gate/reason context is read only from the
real M19 fields.

Eligibility rule (all must hold):
    side == LONG
    decision_bucket in {ELIGIBLE, HIGH_CONVICTION}
    hard_gate_passed == True
    blocked_reasons == []
    execution_eligible == False   (True raises PaperContractViolation at ingest)

All failing reasons are recorded, not just the first. Universe metadata, when
supplied, is advisory only and never hard-blocks (scan_ready=False does not
block in M20.B; quality gating is deferred to M20.UC).
"""
from __future__ import annotations

from typing import List, Optional

from bot.signal_scoring.schema import (
    ScoredSignalCandidate, DecisionBucket, SignalSide,
)
from bot.paper.schema import (
    PaperRoutingDecision, PaperSide, assert_m19_candidate_contract,
)
from bot.universe.schema import SymbolRecord

_ROUTABLE_BUCKETS = (DecisionBucket.ELIGIBLE, DecisionBucket.HIGH_CONVICTION)

_BUCKET_REJECT_REASON = {
    DecisionBucket.WATCH: "watch_below_routing_threshold",
    DecisionBucket.REJECT: "reject_below_routing_threshold",
    DecisionBucket.MANUAL_REVIEW: "manual_review_not_auto_routed",
    DecisionBucket.BLOCKED: "blocked_not_paper_routed",
}


def _map_side(side) -> Optional[PaperSide]:
    """Map an M19 SignalSide to a PaperSide. Returns None if unmappable."""
    if isinstance(side, SignalSide):
        if side == SignalSide.LONG:
            return PaperSide.LONG
        if side == SignalSide.SHORT:
            return PaperSide.SHORT
    return None


def _has_required_shape(candidate: ScoredSignalCandidate) -> bool:
    """Defensive shape check on the fields routing reads. Returns False (safe
    reject) rather than crashing on a malformed candidate."""
    try:
        if not isinstance(candidate.symbol, str) or not candidate.symbol:
            return False
        if not isinstance(candidate.side, SignalSide):
            return False
        if not isinstance(candidate.decision_bucket, DecisionBucket):
            return False
        if not isinstance(candidate.hard_gate_passed, bool):
            return False
        if not isinstance(candidate.blocked_reasons, list):
            return False
        if not isinstance(candidate.candidate_id, str) or \
                not candidate.candidate_id:
            return False
    except AttributeError:
        return False
    return True


def _universe_advisory(universe_record: Optional[SymbolRecord]) -> List[str]:
    """Advisory-only universe context as reason codes. Never hard-blocks."""
    if universe_record is None:
        return ["universe_record_not_found"]
    notes = ["universe_record_found"]
    notes.append("universe_active_true" if universe_record.active
                 else "universe_active_false")
    notes.append("universe_scan_ready_true" if universe_record.scan_ready
                 else "universe_scan_ready_false")
    notes.append(
        f"universe_data_quality_{universe_record.data_quality_status.value}")
    return notes


def decide_paper_routing(
    candidate: ScoredSignalCandidate,
    *,
    evaluated_at_utc: str,
    universe_record: Optional[SymbolRecord] = None,
) -> PaperRoutingDecision:
    """Produce a PaperRoutingDecision for an M19 candidate. Pure and in-memory.

    Raises PaperContractViolation only if candidate.execution_eligible is True
    (M19 guarantees it is always False; True = corrupt/tampered input). All
    other rejections are recorded as paper_routing_eligible=False with reasons.
    """
    # Ingestion contract: execution_eligible == True is a hard violation.
    assert_m19_candidate_contract(candidate)

    reason_codes: List[str] = []
    warnings: List[str] = []

    # ── invalid shape -> safe reject (no crash) ──
    if not _has_required_shape(candidate):
        reason_codes.append("invalid_candidate_shape")
        reason_codes.extend(_universe_advisory(universe_record))
        return _build_decision(
            candidate, evaluated_at_utc, paper_routing_eligible=False,
            reason_codes=reason_codes, warnings=warnings, shape_ok=False)

    # ── eligibility checks (record ALL failing reasons) ──
    paper_side = _map_side(candidate.side)
    if candidate.side != SignalSide.LONG:
        reason_codes.append("short_not_paper_routed"
                            if candidate.side == SignalSide.SHORT
                            else "non_long_not_paper_routed")

    if candidate.decision_bucket not in _ROUTABLE_BUCKETS:
        reason_codes.append(_BUCKET_REJECT_REASON.get(
            candidate.decision_bucket, "bucket_not_routable"))

    if candidate.hard_gate_passed is not True:
        reason_codes.append("hard_gate_not_passed")

    if candidate.blocked_reasons:
        reason_codes.append("blocked_reasons_present")
        # surface the specific M19 gate reasons (stale data, risk authority,
        # liquidity, PIT, thinness, etc.) for transparency — read from real
        # M19 fields, never invented.
        for r in candidate.blocked_reasons:
            warnings.append(f"m19_blocked_reason:{r}")

    if candidate.hard_gate_failures:
        for r in candidate.hard_gate_failures:
            warnings.append(f"m19_hard_gate_failure:{r}")

    paper_routing_eligible = (
        candidate.side == SignalSide.LONG
        and candidate.decision_bucket in _ROUTABLE_BUCKETS
        and candidate.hard_gate_passed is True
        and not candidate.blocked_reasons
        # execution_eligible == False guaranteed by the ingestion guard above
    )
    if paper_routing_eligible:
        reason_codes.append("paper_routing_eligible")

    # advisory universe context (never affects eligibility)
    reason_codes.extend(_universe_advisory(universe_record))

    return _build_decision(
        candidate, evaluated_at_utc,
        paper_routing_eligible=paper_routing_eligible,
        reason_codes=reason_codes, warnings=warnings, shape_ok=True,
        paper_side=paper_side)


def _build_decision(candidate, evaluated_at_utc, *, paper_routing_eligible,
                    reason_codes, warnings, shape_ok, paper_side=None
                    ) -> PaperRoutingDecision:
    # calibration status recorded honestly (M20 records, does not hard-block)
    ml = candidate.ml_context if shape_ok and isinstance(
        candidate.ml_context, dict) else {}
    calib = bool(ml.get("predict_time_calibration_applied", False))

    prov = candidate.provenance if shape_ok and isinstance(
        candidate.provenance, dict) else {}
    input_digest = prov.get("input_digest") if isinstance(prov, dict) else None

    # PaperSide is required by the schema; for unmappable/invalid shapes default
    # to LONG only as a structural placeholder on an explicitly-ineligible
    # decision (paper_routing_eligible is False in those cases).
    side = paper_side or PaperSide.LONG
    bucket = candidate.decision_bucket.value if shape_ok else "INVALID"
    conf = candidate.confidence_bucket.value if shape_ok else "INVALID"
    cid = candidate.candidate_id if shape_ok else (
        getattr(candidate, "candidate_id", "") or "INVALID")
    symbol = candidate.symbol if shape_ok else (
        getattr(candidate, "symbol", "") or "INVALID")

    return PaperRoutingDecision(
        m19_candidate_id=cid,
        symbol=symbol,
        side=side,
        decision_bucket=bucket,
        confidence_bucket=conf,
        paper_routing_eligible=paper_routing_eligible,
        evaluated_at_utc=evaluated_at_utc,
        m19_input_digest=input_digest,
        calibration_applied=calib,
        reason_codes=sorted(reason_codes),
        warnings=sorted(warnings),
    )

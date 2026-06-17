"""M19.G audit helpers — PURE projections of a ScoredSignalCandidate.

Never opens/writes/creates files. No wall-clock, no hostname, no runtime
environment, no random IDs. Deterministic: identical input -> identical output.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from bot.signal_scoring.schema import ScoredSignalCandidate, DecisionBucket, \
    ConfidenceBucket

# Exact allowed audit-record fields (approved set; no more, no less).
_AUDIT_FIELDS = (
    "schema_version", "candidate_id", "symbol", "side", "profile",
    "decision_bucket", "confidence_bucket", "execution_eligible",
    "final_score", "hard_gate_passed", "block_reasons", "reason_codes",
    "warnings", "config_hash", "input_digest",
)


def build_scoring_audit_record(
    candidate: ScoredSignalCandidate,
    *,
    config_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure: a deterministic decision-summary projection of `candidate`. Returns
    a dict; writes nothing. config_hash override falls back to the candidate's
    provenance config_hash when not supplied."""
    prov = candidate.provenance if isinstance(candidate.provenance, dict) else {}
    cfg_hash = config_hash if config_hash is not None \
        else prov.get("config_hash")
    return {
        "schema_version": candidate.schema_version,
        "candidate_id": candidate.candidate_id,
        "symbol": candidate.symbol,
        "side": candidate.side.value,
        "profile": candidate.profile.value,
        "decision_bucket": candidate.decision_bucket.value,
        "confidence_bucket": candidate.confidence_bucket.value,
        "execution_eligible": candidate.execution_eligible,
        "final_score": candidate.final_score,
        "hard_gate_passed": candidate.hard_gate_passed,
        "block_reasons": sorted(candidate.blocked_reasons),
        "reason_codes": sorted(candidate.reason_codes),
        "warnings": sorted(candidate.warnings),
        "config_hash": cfg_hash,
        "input_digest": prov.get("input_digest"),
    }


def build_scoring_audit_summary(
    candidates: Sequence[ScoredSignalCandidate],
) -> Dict[str, Any]:
    """Pure: aggregate counts with fixed deterministic ordering. No I/O."""
    by_decision = {b.value: 0 for b in DecisionBucket}
    by_confidence = {b.value: 0 for b in ConfidenceBucket}
    eligible = 0
    for c in candidates:
        by_decision[c.decision_bucket.value] += 1
        by_confidence[c.confidence_bucket.value] += 1
        if c.execution_eligible:
            eligible += 1
    return {
        "total": len(candidates),
        "by_decision_bucket": by_decision,
        "by_confidence_bucket": by_confidence,
        "execution_eligible_count": eligible,
    }

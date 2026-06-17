"""M19.E composite score & bucket assembly.

Combines the existing pure layers into a ScoredSignalCandidate:
  gates (M19.B) + components (M19.C) + penalties/multipliers (M19.D).

Public entry:
  score_candidate(candidate_input, config) -> ScoredSignalCandidate
    -> evaluate_hard_gates, score_all_components, evaluate_penalties,
       evaluate_multipliers, then assemble_score(...)

Pure inner (used by matrix tests, no fetch/write/external calls):
  assemble_score(gate_result, components, penalty_result, multiplier_result,
                 candidate_input, config) -> ScoredSignalCandidate

Rules (per approved M19.E plan):
  * Does NOT reimplement gate/component/penalty/multiplier logic; consumes
    their outputs.
  * Composite formula (config-driven, no magic numbers):
        support     = renormalised weighted blend of the 10 non-ML components
        base        = ml_anchor_weight*ml_score + support_weight*support
        pre_penalty = base * effective_multiplier
        final_raw   = pre_penalty - total_penalty_points
        final_score = clamp(final_raw, 0, 100)
  * Hard BLOCK overrides: decision_bucket=BLOCKED, final_score=0,
    execution_eligible=False; sub-results still embedded for explainability.
  * MANUAL_REVIEW gate caps the bucket at MANUAL_REVIEW and forbids
    execution_eligible.
  * SHORT is never execution_eligible and never ELIGIBLE/HIGH_CONVICTION.
  * No fetch, no write, no broker/live/dashboard/main, no persistence.
"""
from __future__ import annotations

from typing import Dict

from bot.signal_scoring import provenance
from bot.signal_scoring.config import SignalScoringConfig
from bot.signal_scoring.components import (
    COMPONENT_NAMES, score_all_components,
)
from bot.signal_scoring.gates import evaluate_hard_gates
from bot.signal_scoring.penalties import (
    evaluate_penalties, evaluate_multipliers,
)
from bot.signal_scoring.schema import (
    SCHEMA_VERSION_OUTPUT, SignalCandidateInput, SignalSide, ScoringProfile,
    ScoredSignalCandidate, DecisionBucket, ConfidenceBucket,
    ComponentScore, PenaltyResult, MultiplierResult, GateResult,
)

_ML = "ml"


def _clamp_0_100(x: float) -> float:
    # Round to 6 dp before clamping so accumulated float noise (e.g.
    # 57.99999999999998) does not flip a value across a bucket boundary.
    return max(0.0, min(100.0, round(float(x), 6)))


def _weighted_support(components: Dict[str, ComponentScore],
                      config: SignalScoringConfig) -> float:
    """Renormalised weighted blend over the 10 NON-ML components (ML influence
    comes only through the anchor; ML's weight is excluded so it is not
    double-counted)."""
    weights = config.weights
    non_ml = [c for c in COMPONENT_NAMES if c != _ML]
    mass = sum(weights[c] for c in non_ml)
    if mass <= 0:
        return 0.0
    # Divide once at the end (Σ score*weight / mass) rather than per-term
    # (Σ score*(weight/mass)) to avoid accumulated float rounding at bucket
    # boundaries.
    total = 0.0
    for c in non_ml:
        score = components[c].score if c in components else 0.0
        total += score * weights[c]
    return total / mass


def _decision_bucket_from_score(final_score: float,
                                config: SignalScoringConfig) -> DecisionBucket:
    """Score-only bucket (before gate/MR/short caps). Thresholds from config."""
    t = config.thresholds
    if final_score < t["watch_min"]:
        return DecisionBucket.REJECT
    if final_score < t["manual_review_low"]:
        return DecisionBucket.WATCH
    if final_score < t["eligible_min"]:
        return DecisionBucket.MANUAL_REVIEW
    if final_score < t["high_conviction_min"]:
        return DecisionBucket.ELIGIBLE
    return DecisionBucket.HIGH_CONVICTION


def _confidence_from_score(final_score: float,
                           config: SignalScoringConfig) -> ConfidenceBucket:
    """Confidence derived from final_score, reusing the score thresholds."""
    t = config.thresholds
    if final_score < t["watch_min"]:
        return ConfidenceBucket.LOW
    if final_score < t["eligible_min"]:
        return ConfidenceBucket.MEDIUM
    if final_score < t["high_conviction_min"]:
        return ConfidenceBucket.MEDIUM_HIGH
    return ConfidenceBucket.HIGH


def assemble_score(gate_result: GateResult,
                   components: Dict[str, ComponentScore],
                   penalty_result: PenaltyResult,
                   multiplier_result: MultiplierResult,
                   candidate_input: SignalCandidateInput,
                   config: SignalScoringConfig) -> ScoredSignalCandidate:
    """Pure assembly. No fetch/write/external calls."""
    profile = config.profile
    side = candidate_input.side

    # ── sub-result projections (always embedded for explainability) ──
    component_scores = {name: components[name].score
                        for name in COMPONENT_NAMES if name in components}
    multipliers_map = {i.name: i.factor for i in multiplier_result.items}
    multipliers_map["product"] = multiplier_result.product
    multipliers_map["effective_multiplier"] = \
        multiplier_result.effective_multiplier
    penalties_map = {
        "total_points": penalty_result.total_points,
        "raw_total_points": penalty_result.raw_total_points,
        "items": {i.name: i.points for i in penalty_result.items},
    }

    # ── aggregate reason codes + warnings (deduped, sorted union) ──
    reason_codes = set()
    warnings = set()
    for c in components.values():
        reason_codes.update(c.reason_codes)
        warnings.update(c.warnings)
    reason_codes.update(penalty_result.reason_codes)
    reason_codes.update(multiplier_result.reason_codes)
    warnings.update(penalty_result.warnings)
    warnings.update(multiplier_result.warnings)

    gate_blocked = (gate_result.decision_bucket == DecisionBucket.BLOCKED)
    gate_manual_review = (
        not gate_blocked
        and gate_result.decision_bucket == DecisionBucket.MANUAL_REVIEW)

    # ── composite math (computed even when blocked, for transparency) ──
    ml_score = components[_ML].score if _ML in components else 0.0
    support = _weighted_support(components, config)
    base = (config.ml_anchor_weight * ml_score
            + config.support_weight * support)
    pre_penalty = base * multiplier_result.effective_multiplier
    final_raw = pre_penalty - penalty_result.total_points
    computed_score = _clamp_0_100(final_raw)

    # would-be bucket from the computed score (before caps)
    would_be = _decision_bucket_from_score(computed_score, config)

    # ── apply overrides / caps ──
    if gate_blocked:
        final_score = 0.0
        decision_bucket = DecisionBucket.BLOCKED
        execution_eligible = False
        reason_codes.update(gate_result.block_reasons)
        reason_codes.add("hard_gate_block")
    else:
        final_score = computed_score
        decision_bucket = would_be
        if gate_manual_review:
            reason_codes.update(gate_result.manual_review_reasons)
            reason_codes.add("manual_review_gate_cap")
            if would_be == DecisionBucket.HIGH_CONVICTION:
                reason_codes.add(
                    "would_be_high_conviction_capped_to_manual_review")
            elif would_be == DecisionBucket.ELIGIBLE:
                reason_codes.add(
                    "would_be_eligible_capped_to_manual_review")
            # cap: never above MANUAL_REVIEW
            if would_be in (DecisionBucket.ELIGIBLE,
                            DecisionBucket.HIGH_CONVICTION,
                            DecisionBucket.MANUAL_REVIEW):
                decision_bucket = DecisionBucket.MANUAL_REVIEW
            # WATCH/REJECT stay as-is (they are below MANUAL_REVIEW)
        # SHORT structural caps (applies regardless of gate pass)
        if side == SignalSide.SHORT:
            if would_be == DecisionBucket.HIGH_CONVICTION:
                reason_codes.add("would_be_high_conviction_capped_for_short")
            elif would_be == DecisionBucket.ELIGIBLE:
                reason_codes.add("would_be_eligible_capped_for_short")
            if decision_bucket in (DecisionBucket.ELIGIBLE,
                                   DecisionBucket.HIGH_CONVICTION):
                decision_bucket = DecisionBucket.MANUAL_REVIEW
            reason_codes.add("short_side_not_executable")

        # ── execution eligibility (default False) ──
        execution_eligible = (
            gate_result.passed is True
            and not gate_manual_review
            and side == SignalSide.LONG
            and decision_bucket in (DecisionBucket.ELIGIBLE,
                                    DecisionBucket.HIGH_CONVICTION))

    confidence_bucket = _confidence_from_score(final_score, config)

    # ── provenance (deterministic; identity-only) ──
    input_dict = candidate_input.to_dict()
    config_dict = config.to_dict()
    in_dig = provenance.input_digest(input_dict)
    cfg_hash = provenance.config_hash(config_dict)
    cid = provenance.candidate_id(
        schema_version=SCHEMA_VERSION_OUTPUT,
        symbol=candidate_input.symbol,
        side=side.value,
        signal_timestamp_utc=candidate_input.signal_timestamp_utc,
        input_digest_hex=in_dig,
        config_hash_hex=cfg_hash,
    )
    prov = {
        "candidate_id": cid,
        "input_digest": in_dig,
        "config_hash": cfg_hash,
        "schema_version": SCHEMA_VERSION_OUTPUT,
        "pre_clamp_final_raw": final_raw,
        "base_score": base,
        "pre_penalty_score": pre_penalty,
        "would_be_bucket": would_be.value,
    }

    return ScoredSignalCandidate(
        symbol=candidate_input.symbol,
        side=side,
        signal_timestamp_utc=candidate_input.signal_timestamp_utc,
        candidate_id=cid,
        profile=profile,
        decision_bucket=decision_bucket,
        execution_eligible=execution_eligible,
        final_score=final_score,
        final_score_100=final_score,
        confidence_bucket=confidence_bucket,
        hard_gate_passed=bool(gate_result.passed),
        hard_gate_failures=list(gate_result.block_reasons),
        blocked_reasons=list(gate_result.block_reasons),
        warnings=sorted(warnings),
        reason_codes=sorted(reason_codes),
        component_scores=component_scores,
        multipliers=multipliers_map,
        penalties=penalties_map,
        ml_context={},
        risk_preview={},
        provenance=prov,
    )


def score_candidate(candidate_input: SignalCandidateInput,
                    config: SignalScoringConfig) -> ScoredSignalCandidate:
    """Public entry point: run all layers and assemble. Pure (no I/O)."""
    gate_result = evaluate_hard_gates(candidate_input, config)
    components = score_all_components(candidate_input, config)
    penalty_result = evaluate_penalties(candidate_input, config)
    multiplier_result = evaluate_multipliers(candidate_input, config)
    return assemble_score(gate_result, components, penalty_result,
                          multiplier_result, candidate_input, config)

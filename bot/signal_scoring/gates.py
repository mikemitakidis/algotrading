"""M19.B gates — hard-gate engine (gates only; no scoring/penalties/adapters).

evaluate_hard_gates(candidate_input, config) -> GateResult

Pure, deterministic, fetch-free, write-free. Reads context via frozen keys.py
constants. Fail-safe: missing gate-critical keys -> `missing_context_key`
BLOCK; un-coercible values -> `invalid_context_value` BLOCK; never raises on
bad context. Gates run in explicit GATE_ORDER (not alphabetic). BLOCK takes
precedence over MANUAL_REVIEW. No now()/wall-clock/RNG.
"""
from __future__ import annotations

from typing import List, Tuple

from bot.signal_scoring import keys as K
from bot.signal_scoring.config import SignalScoringConfig
from bot.signal_scoring.schema import (
    SignalCandidateInput, SignalSide, ScoringProfile,
    GateResult, GateFailure, GateOutcome, DecisionBucket, PenaltySeverity,
)

# Explicit, deterministic evaluation order (NOT alphabetic). missing_context_key
# runs first so dependent gates only evaluate when their keys are present.
GATE_ORDER: Tuple[str, ...] = (
    "missing_context_key",
    "schema_match",
    "valid_timestamp",
    "risk_preview_available",
    "risk_authority",
    "min_available_timeframes",
    "non_stale_data",
    "adjusted_price_pit_risk",
    "model_readiness",
    "production_thinness_blocked",
    "min_liquidity",
    "calibration_present",
    "short_side",
)


def _block(name, code, detail, severity=PenaltySeverity.BLOCKING):
    return GateFailure(gate_name=name, outcome=GateOutcome.BLOCK,
                       reason_code=code, detail=detail, severity=severity)


def _review(name, code, detail):
    return GateFailure(gate_name=name, outcome=GateOutcome.MANUAL_REVIEW,
                       reason_code=code, detail=detail,
                       severity=PenaltySeverity.MAJOR)


def _missing_keys(ci: SignalCandidateInput) -> List[str]:
    """Return 'block.key' identifiers for any required gate key absent from
    its context block."""
    missing = []
    block_attr = {
        "ml_context": ci.ml_context,
        "data_quality_context": ci.data_quality_context,
        "advisory_context": ci.advisory_context,
        "timeframe_context": ci.timeframe_context,
        "risk_preview": ci.risk_preview,
        "liquidity_context": ci.liquidity_context,
    }
    for block_name, required in K.GATE_REQUIRED_KEYS.items():
        block = block_attr.get(block_name, {})
        if not isinstance(block, dict):
            # Fail-safe: a non-dict block means EVERY required key for that
            # block is unreadable. Expand into all `block.key` identifiers so
            # have() returns False for each and dependent gates never try to
            # subscript a non-dict (no raw KeyError/TypeError). Also record an
            # explicit marker for the detail message.
            missing.append(f"{block_name}:<not-a-dict>")
            for key in required:
                missing.append(f"{block_name}.{key}")
            continue
        for key in required:
            if key not in block:
                missing.append(f"{block_name}.{key}")
    return missing


def evaluate_hard_gates(candidate_input: SignalCandidateInput,
                        config: SignalScoringConfig) -> GateResult:
    """Run all hard gates in GATE_ORDER. Returns a GateResult listing every
    failure (not just the first). Deterministic and fail-safe."""
    profile = config.profile
    failures: List[GateFailure] = []
    evaluated: List[str] = []

    ci = candidate_input
    ml = ci.ml_context if isinstance(ci.ml_context, dict) else {}
    dq = ci.data_quality_context if isinstance(ci.data_quality_context, dict) else {}
    adv = ci.advisory_context if isinstance(ci.advisory_context, dict) else {}
    tf = ci.timeframe_context if isinstance(ci.timeframe_context, dict) else {}
    rp = ci.risk_preview if isinstance(ci.risk_preview, dict) else {}
    lq = ci.liquidity_context if isinstance(ci.liquidity_context, dict) else {}

    # Precompute missing keys once; gates that depend on absent keys skip their
    # own value-checks (the missing_context_key gate already blocks).
    missing = _missing_keys(ci)
    missing_set = set(missing)

    def have(block_name, key):
        return f"{block_name}.{key}" not in missing_set

    for gate in GATE_ORDER:
        evaluated.append(gate)
        try:
            if gate == "missing_context_key":
                if missing:
                    failures.append(_block(
                        gate, "missing_context_key",
                        f"missing gate-critical keys: {sorted(missing)}"))

            elif gate == "schema_match":
                if have("data_quality_context", K.DQ_SCHEMA_MATCH):
                    if K.as_bool(dq[K.DQ_SCHEMA_MATCH]) is not True:
                        failures.append(_block(
                            gate, "schema_mismatch",
                            "data_quality.schema_match is not True"))

            elif gate == "valid_timestamp":
                # Contract already enforces UTC at construction; re-affirm.
                from bot.signal_scoring.schema import _validate_timestamp
                _validate_timestamp(ci.signal_timestamp_utc)

            elif gate == "risk_preview_available":
                if have("risk_preview", K.RISK_PREVIEW_AVAILABLE):
                    if K.as_bool(rp[K.RISK_PREVIEW_AVAILABLE]) is not True:
                        failures.append(_block(
                            gate, "risk_preview_unavailable",
                            "risk_preview_available is not True"))

            elif gate == "risk_authority":
                if have("risk_preview", K.RISK_AUTHORITY_STATUS):
                    status = K.as_enum_str(
                        rp[K.RISK_AUTHORITY_STATUS],
                        K.ALLOWED_RISK_AUTHORITY_STATUS)
                    if status == "blocked":
                        failures.append(_block(
                            gate, "risk_authority_blocked",
                            "risk_authority_status == 'blocked'"))

            elif gate == "min_available_timeframes":
                if have("timeframe_context", K.TF_AVAILABLE):
                    count = K.timeframe_count(tf[K.TF_AVAILABLE])
                    threshold = int(config.scanner["min_available_timeframes"])
                    if count < threshold:
                        failures.append(_block(
                            gate, "insufficient_available_timeframes",
                            f"available_timeframes {count} < {threshold}"))

            elif gate == "non_stale_data":
                if have("data_quality_context", K.DQ_STALE_DATA_FLAG):
                    if K.as_bool(dq[K.DQ_STALE_DATA_FLAG]) is True:
                        failures.append(_block(
                            gate, "stale_data", "stale_data_flag is True"))
                if have("data_quality_context", K.DQ_DATA_FRESHNESS_MINUTES):
                    age = K.as_number(dq[K.DQ_DATA_FRESHNESS_MINUTES])
                    max_age = K.as_number(
                        config.data_quality["stale_data_max_age_minutes"])
                    if age > max_age:
                        failures.append(_block(
                            gate, "stale_data",
                            f"data_freshness {age} > max {max_age} min"))

            elif gate == "adjusted_price_pit_risk":
                tripped = False
                if have("advisory_context", K.ADV_ADJUSTED_PRICE_PIT_RISK):
                    if K.as_bool(adv[K.ADV_ADJUSTED_PRICE_PIT_RISK]) is True:
                        tripped = True
                if (have("ml_context", K.ML_PRICE_ADJUSTMENT_MODE)
                        and have("ml_context", K.ML_ALLOW_ADJUSTED_FOR_ML)):
                    mode = K.as_enum_str(
                        ml[K.ML_PRICE_ADJUSTMENT_MODE],
                        K.ALLOWED_PRICE_ADJUSTMENT_MODE)
                    allow = K.as_bool(ml[K.ML_ALLOW_ADJUSTED_FOR_ML])
                    if mode == "adjusted" and allow is not True:
                        tripped = True
                if tripped:
                    failures.append(_block(
                        gate, "adjusted_price_pit_risk",
                        "adjusted prices without allow flag (PIT leakage risk)"))

            elif gate == "model_readiness":
                if have("ml_context", K.ML_MODEL_READINESS_PASSED):
                    if K.as_bool(ml[K.ML_MODEL_READINESS_PASSED]) is not True:
                        failures.append(_block(
                            gate, "model_readiness_failed",
                            "model_readiness_passed is not True"))

            elif gate == "production_thinness_blocked":
                if have("ml_context", K.ML_PRODUCTION_THINNESS_STATUS):
                    status = K.as_enum_str(
                        ml[K.ML_PRODUCTION_THINNESS_STATUS],
                        K.ALLOWED_PRODUCTION_THINNESS_STATUS)
                    if status == "blocked":
                        failures.append(_block(
                            gate, "production_thinness_blocked",
                            "production_thinness_status == 'blocked'"))

            elif gate == "min_liquidity":
                if have("liquidity_context", K.LIQ_AVG_DOLLAR_VOLUME_20D):
                    adv20 = K.as_number(lq[K.LIQ_AVG_DOLLAR_VOLUME_20D])
                    min_adv = K.as_number(
                        config.liquidity["min_avg_dollar_volume_20d"])
                    if adv20 < min_adv:
                        failures.append(_block(
                            gate, "below_min_liquidity",
                            f"avg_dollar_volume_20d {adv20} < {min_adv}"))
                if have("liquidity_context", K.LIQ_PRICE):
                    price = K.as_number(lq[K.LIQ_PRICE])
                    min_price = K.as_number(config.liquidity["min_price"])
                    if price < min_price:
                        failures.append(_block(
                            gate, "below_min_liquidity",
                            f"price {price} < min_price {min_price}"))

            elif gate == "calibration_present":
                if (have("ml_context", K.ML_CALIBRATION_APPLIED)
                        and have("ml_context", K.ML_PRED_CALIBRATED)):
                    applied = K.as_bool(ml[K.ML_CALIBRATION_APPLIED])
                    calibrated = ml[K.ML_PRED_CALIBRATED]
                    missing_cal = (applied is not True) or (calibrated is None)
                    if missing_cal:
                        if profile == ScoringProfile.STRICT:
                            failures.append(_block(
                                gate, "calibration_unavailable",
                                "calibrated probability unavailable (strict)"))
                        else:
                            failures.append(_review(
                                gate, "calibration_unavailable",
                                "calibrated probability unavailable (research)"))

            elif gate == "short_side":
                if ci.side == SignalSide.SHORT:
                    if profile == ScoringProfile.STRICT:
                        failures.append(_block(
                            gate, "short_side_blocked",
                            "SHORT blocked in strict profile"))
                    else:
                        failures.append(_review(
                            gate, "short_side_manual_review",
                            "SHORT -> manual review in research profile"))

        except K.InvalidContextValue as e:
            failures.append(_block(
                gate, "invalid_context_value",
                f"{gate}: {e}"))

    block_reasons = [f.reason_code for f in failures
                     if f.outcome == GateOutcome.BLOCK]
    review_reasons = [f.reason_code for f in failures
                      if f.outcome == GateOutcome.MANUAL_REVIEW]

    if block_reasons:
        bucket = DecisionBucket.BLOCKED
        passed = False
    elif review_reasons:
        bucket = DecisionBucket.MANUAL_REVIEW
        passed = False
    else:
        bucket = None
        passed = True

    return GateResult(
        profile=profile,
        passed=passed,
        decision_bucket=bucket,
        failures=failures,
        block_reasons=block_reasons,
        manual_review_reasons=review_reasons,
        evaluated_gates=evaluated,
    )

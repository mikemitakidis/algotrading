"""M19.D penalties & multipliers — friction layer (no composite/score/bucket).

evaluate_penalties(candidate, config)   -> PenaltyResult
evaluate_multipliers(candidate, config) -> MultiplierResult

Pure, deterministic, profile-neutral, raw-context + config only. Does NOT:
consume ComponentScore, call gates/components, compute any final/composite
score, assign buckets, assemble ScoredSignalCandidate, fetch, or write.

Missing/invalid policy (consistent across the layer):
  missing trigger input -> penalty NOT applied / neutral multiplier 1.00 +
    missing_soft_input warning
  invalid trigger input  -> adverse penalty applied / adverse multiplier factor
    + invalid_soft_input warning
  non-dict context block -> invalid
  None                   -> missing (canonical unavailable sentinel)
  bool where numeric expected, or negative count/volume/ATR -> invalid
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from bot.signal_scoring import keys as K
from bot.signal_scoring.config import SignalScoringConfig
from bot.signal_scoring.schema import (
    SignalCandidateInput, SignalSide, ScoringProfile,
    PenaltyItem, PenaltyResult, MultiplierItem, MultiplierResult,
    PenaltySeverity,
)

PENALTY_NAMES = K.PENALTY_NAMES
MULTIPLIER_NAMES = K.MULTIPLIER_NAMES

_OK = "ok"
_MISSING = "missing"
_INVALID = "invalid"
_INVALID_BLOCK = object()

_W_MISSING = "missing_soft_input"
_W_INVALID = "invalid_soft_input"


def _block(ci: SignalCandidateInput, block_name: str):
    b = getattr(ci, block_name, {})
    return b if isinstance(b, dict) else _INVALID_BLOCK


def _get(block, key: str, coerce: Callable) -> Tuple[Any, str]:
    """(value, status) in {ok, missing, invalid}. Never raises. Non-dict block
    -> invalid for every key; explicit None -> missing."""
    if block is _INVALID_BLOCK:
        return None, _INVALID
    if key not in block:
        return None, _MISSING
    if block[key] is None:
        return None, _MISSING
    try:
        return coerce(block[key]), _OK
    except K.InvalidContextValue:
        return None, _INVALID
    except (ValueError, TypeError):
        return None, _INVALID


# ───────────────────────────── penalties ─────────────────────────────
def evaluate_penalties(candidate_input: SignalCandidateInput,
                       config: SignalScoringConfig) -> PenaltyResult:
    profile = config.profile
    pen = config.penalties
    items: List[PenaltyItem] = []
    warnings: List[str] = []

    ml = _block(candidate_input, "ml_context")
    sc = _block(candidate_input, "scanner_context")
    rp = _block(candidate_input, "risk_preview")

    def warn(status):
        if status == _MISSING and _W_MISSING not in warnings:
            warnings.append(_W_MISSING)
        elif status == _INVALID and _W_INVALID not in warnings:
            warnings.append(_W_INVALID)

    # uncalibrated_ml_probability
    applied, st = _get(ml, K.ML_CALIBRATION_APPLIED, K.as_bool)
    warn(st)
    if st == _OK and applied is not True:
        items.append(PenaltyItem(
            name="uncalibrated_ml_probability",
            points=pen["uncalibrated_ml_probability"],
            reason_code="uncalibrated_ml_probability",
            severity=PenaltySeverity.MAJOR,
            detail="calibration_applied is False",
            inputs_used={"calibration_applied": applied}))
    elif st == _INVALID:
        items.append(PenaltyItem(
            name="uncalibrated_ml_probability",
            points=pen["uncalibrated_ml_probability"],
            reason_code="uncalibrated_ml_probability_invalid",
            severity=PenaltySeverity.MAJOR,
            detail="calibration_applied invalid",
            inputs_used={"calibration_applied": None}))
    # missing -> no penalty (gate handles truly-missing calibration)

    # each_feature_extrapolation
    extrap, st = _get(ml, K.ML_FEATURE_EXTRAPOLATION_COUNT, K.as_nonneg_number)
    warn(st)
    cap = pen["extrapolation_cap"]
    if st == _OK and extrap and extrap > 0:
        pts = min(pen["each_feature_extrapolation"] * extrap, cap)
        items.append(PenaltyItem(
            name="each_feature_extrapolation", points=pts,
            reason_code="feature_extrapolation",
            severity=PenaltySeverity.WARNING,
            detail=f"{int(extrap)} extrapolated feature(s)",
            inputs_used={"feature_extrapolation_count": extrap}))
    elif st == _INVALID:
        items.append(PenaltyItem(
            name="each_feature_extrapolation", points=cap,
            reason_code="feature_extrapolation_invalid",
            severity=PenaltySeverity.WARNING,
            detail="feature_extrapolation_count invalid -> full cap",
            inputs_used={"feature_extrapolation_count": None}))

    # production_thinness_warning
    thin, st = _get(ml, K.ML_PRODUCTION_THINNESS_STATUS,
                    lambda v: K.as_enum_str(v, K.ALLOWED_PRODUCTION_THINNESS_STATUS))
    warn(st)
    if st == _OK and thin == "warned":
        items.append(PenaltyItem(
            name="production_thinness_warning",
            points=pen["production_thinness_warning"],
            reason_code="production_thinness_warning",
            severity=PenaltySeverity.WARNING,
            detail="production_thinness_status == 'warned'",
            inputs_used={"production_thinness_status": thin}))
    elif st == _INVALID:
        items.append(PenaltyItem(
            name="production_thinness_warning",
            points=pen["production_thinness_warning"],
            reason_code="production_thinness_warning_invalid",
            severity=PenaltySeverity.WARNING,
            detail="production_thinness_status invalid",
            inputs_used={"production_thinness_status": None}))
    # "ok"/"blocked" -> no penalty here (blocked is a gate concept)

    # scanner: valid_count + available_timeframes drive two penalties
    valid, st_v = _get(sc, K.SCAN_VALID_COUNT, K.as_nonneg_number)
    avail, st_a = _get(sc, K.SCAN_AVAILABLE_TIMEFRAMES, K.timeframe_count)
    warn(st_v)
    warn(st_a)
    min_valid = config.scanner["min_valid_timeframes"]

    # weak_scanner_confluence (reads valid_count)
    if st_v == _OK and valid < min_valid:
        items.append(PenaltyItem(
            name="weak_scanner_confluence",
            points=pen["weak_scanner_confluence"],
            reason_code="weak_scanner_confluence",
            severity=PenaltySeverity.WARNING,
            detail=f"valid_count {int(valid)} < min {min_valid}",
            inputs_used={"valid_count": valid}))
    elif st_v == _INVALID:
        items.append(PenaltyItem(
            name="weak_scanner_confluence",
            points=pen["weak_scanner_confluence"],
            reason_code="weak_scanner_confluence_invalid",
            severity=PenaltySeverity.WARNING,
            detail="valid_count invalid -> adverse",
            inputs_used={"valid_count": None}))

    # missing_noncritical_timeframe (reads valid_count + available_timeframes)
    if st_v == _OK and st_a == _OK:
        # valid_count > available_timeframes is contradictory -> adverse/invalid
        if valid > avail:
            if _W_INVALID not in warnings:
                warnings.append(_W_INVALID)
            items.append(PenaltyItem(
                name="missing_noncritical_timeframe",
                points=pen["missing_noncritical_timeframe"],
                reason_code="missing_noncritical_timeframe_invalid",
                severity=PenaltySeverity.WARNING,
                detail=f"valid {int(valid)} > available {int(avail)}",
                inputs_used={"valid_count": valid,
                             "available_timeframes": avail}))
        else:
            missing_tf = int(avail) - int(valid)
            if missing_tf > 0:
                items.append(PenaltyItem(
                    name="missing_noncritical_timeframe",
                    points=pen["missing_noncritical_timeframe"] * missing_tf,
                    reason_code="missing_noncritical_timeframe",
                    severity=PenaltySeverity.WARNING,
                    detail=f"{missing_tf} missing timeframe(s)",
                    inputs_used={"valid_count": valid,
                                 "available_timeframes": avail}))
    elif st_v == _INVALID or st_a == _INVALID:
        items.append(PenaltyItem(
            name="missing_noncritical_timeframe",
            points=pen["missing_noncritical_timeframe"],
            reason_code="missing_noncritical_timeframe_invalid",
            severity=PenaltySeverity.WARNING,
            detail="scanner count invalid -> adverse",
            inputs_used={"valid_count": valid if st_v == _OK else None,
                         "available_timeframes": avail if st_a == _OK else None}))

    # poor_reward_risk
    rr, st = _get(rp, K.RISK_REWARD_RISK_RATIO, K.as_number)
    warn(st)
    min_rr = config.risk["min_reward_risk_ratio"]
    if st == _OK and rr < min_rr:
        items.append(PenaltyItem(
            name="poor_reward_risk", points=pen["poor_reward_risk"],
            reason_code="poor_reward_risk", severity=PenaltySeverity.MAJOR,
            detail=f"reward_risk_ratio {rr} < min {min_rr}",
            inputs_used={"reward_risk_ratio": rr}))
    elif st == _INVALID:
        items.append(PenaltyItem(
            name="poor_reward_risk", points=pen["poor_reward_risk"],
            reason_code="poor_reward_risk_invalid",
            severity=PenaltySeverity.MAJOR,
            detail="reward_risk_ratio invalid -> adverse",
            inputs_used={"reward_risk_ratio": None}))

    raw_total = sum(i.points for i in items)
    total = min(raw_total, pen["max_total_penalty_points"])
    reason_codes = sorted(i.reason_code for i in items)
    return PenaltyResult(
        profile=profile,
        items=list(items),
        total_points=total,
        raw_total_points=raw_total,
        reason_codes=reason_codes,
        warnings=sorted(warnings),
    )


# ──────────────────────────── multipliers ────────────────────────────
def evaluate_multipliers(candidate_input: SignalCandidateInput,
                         config: SignalScoringConfig) -> MultiplierResult:
    profile = config.profile
    mult = config.multipliers
    vcfg = config.volatility
    lcfg = config.liquidity
    items: List[MultiplierItem] = []
    warnings: List[str] = []

    rc = _block(candidate_input, "regime_context")
    vc = _block(candidate_input, "volatility_context")
    lq = _block(candidate_input, "liquidity_context")
    adv = _block(candidate_input, "advisory_context")
    side = candidate_input.side

    def warn(status):
        if status == _MISSING and _W_MISSING not in warnings:
            warnings.append(_W_MISSING)
        elif status == _INVALID and _W_INVALID not in warnings:
            warnings.append(_W_INVALID)

    # regime (side-aware)
    label, st = _get(rc, K.REGIME_LABEL, K.as_str)
    warn(st)
    if st == _OK:
        lab = label.lower()
        fav_long = lab in ("bull", "uptrend", "above_sma", "risk_on")
        countertrend_lab = lab in ("bear", "downtrend", "below_sma", "risk_off")
        if lab in ("unknown", "neutral", "mixed"):
            factor, rcode = mult["regime_unknown"], "regime_unknown"
        elif side == SignalSide.LONG:
            if fav_long:
                factor, rcode = mult["regime_aligned"], "regime_aligned"
            elif countertrend_lab:
                factor, rcode = mult["regime_countertrend"], "regime_countertrend"
            else:
                factor, rcode = mult["regime_unknown"], "regime_unknown"
        else:  # SHORT
            if countertrend_lab:
                factor, rcode = mult["regime_aligned"], "regime_aligned"
            elif fav_long:
                factor, rcode = mult["regime_countertrend"], "regime_countertrend"
            else:
                factor, rcode = mult["regime_unknown"], "regime_unknown"
        items.append(MultiplierItem(name="regime", factor=factor,
                     reason_code=rcode, detail=f"regime_label={label}",
                     inputs_used={"regime_label": label}))
    elif st == _INVALID:
        items.append(MultiplierItem(
            name="regime", factor=mult["regime_countertrend"],
            reason_code="regime_invalid",
            detail="regime_label invalid -> conservative",
            inputs_used={"regime_label": None}))
    # missing -> neutral (no item), warning already recorded

    # volatility
    atr, st = _get(vc, K.VOL_ATR_PCT, K.as_nonneg_number)
    warn(st)
    if st == _OK:
        if vcfg["atr_pct_ideal_min"] <= atr <= vcfg["atr_pct_ideal_max"]:
            factor, rcode = mult["volatility_normal"], "volatility_normal"
        elif atr > vcfg["atr_pct_max"]:
            # above max is normally a gate; called independently -> adverse
            factor, rcode = mult["volatility_elevated"], "volatility_above_max"
        elif atr > vcfg["atr_pct_ideal_max"]:
            factor, rcode = mult["volatility_elevated"], "volatility_elevated"
        else:
            # below ideal band (low) -> treat as normal multiplier (soft)
            factor, rcode = mult["volatility_normal"], "volatility_normal"
        items.append(MultiplierItem(name="volatility", factor=factor,
                     reason_code=rcode, detail=f"atr_pct={atr}",
                     inputs_used={"atr_pct": atr}))
    elif st == _INVALID:
        items.append(MultiplierItem(
            name="volatility", factor=mult["volatility_elevated"],
            reason_code="volatility_invalid",
            detail="atr_pct invalid -> conservative",
            inputs_used={"atr_pct": None}))

    # liquidity
    adv20, st = _get(lq, K.LIQ_AVG_DOLLAR_VOLUME_20D, K.as_nonneg_number)
    warn(st)
    if st == _OK:
        if adv20 >= lcfg["ideal_avg_dollar_volume_20d"]:
            factor, rcode = 1.0, "liquidity_ideal"
        elif adv20 >= lcfg["min_avg_dollar_volume_20d"]:
            factor, rcode = mult["liquidity_thin_but_allowed"], \
                "liquidity_thin_but_allowed"
        else:
            # below min is normally a gate; called independently -> adverse
            factor, rcode = mult["liquidity_thin_but_allowed"], \
                "liquidity_below_min"
        items.append(MultiplierItem(name="liquidity", factor=factor,
                     reason_code=rcode, detail=f"avg_dollar_volume_20d={adv20}",
                     inputs_used={"avg_dollar_volume_20d": adv20}))
    elif st == _INVALID:
        items.append(MultiplierItem(
            name="liquidity", factor=mult["liquidity_thin_but_allowed"],
            reason_code="liquidity_invalid",
            detail="avg_dollar_volume_20d invalid -> conservative",
            inputs_used={"avg_dollar_volume_20d": None}))

    # fourh_alignment
    fourh, st = _get(adv, K.ADV_FOURH_ALIGNMENT, K.as_str)
    warn(st)
    if st == _OK:
        if fourh == "utc_fixed":
            items.append(MultiplierItem(
                name="fourh_alignment",
                factor=mult["fourh_utc_fixed_reliance"],
                reason_code="fourh_utc_fixed_reliance",
                detail="4H bucket is UTC-fixed",
                inputs_used={"fourh_bucket_alignment": fourh}))
        # other valid string -> neutral 1.00 (no item)
    elif st == _INVALID:
        items.append(MultiplierItem(
            name="fourh_alignment", factor=mult["fourh_utc_fixed_reliance"],
            reason_code="fourh_alignment_invalid",
            detail="fourh_bucket_alignment invalid -> conservative",
            inputs_used={"fourh_bucket_alignment": None}))
    # missing -> neutral 1.00 (no item) + missing warning

    product = 1.0
    for i in items:
        product *= i.factor
    effective = max(product, mult["multiplier_floor"])
    reason_codes = sorted(i.reason_code for i in items)
    return MultiplierResult(
        profile=profile,
        items=list(items),
        product=product,
        effective_multiplier=effective,
        reason_codes=reason_codes,
        warnings=sorted(warnings),
    )

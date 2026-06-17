"""M19.C components — pure, deterministic per-component scorers.

Each scorer takes (candidate_input, config) and returns a ComponentScore
{score:0-100, reason_codes, warnings, inputs_used, blocked_reasons}.

Rules (per approved M19.C plan):
  * profile-NEUTRAL: identical sub-scores in strict/research.
  * pure & deterministic: no now()/RNG/fetch/file IO.
  * NO gate dependency: does not import/call evaluate_hard_gates/GateResult.
  * NO composite/penalty/multiplier/bucket logic; NO ScoredSignalCandidate.
  * soft-input fallback: missing -> neutral 50 + warning; invalid -> low 25 +
    warning. ML special-cases calibrated/raw/both-unavailable.
  * gate-critical failures are handled separately by M19.B gates.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from bot.signal_scoring import keys as K
from bot.signal_scoring.config import SignalScoringConfig
from bot.signal_scoring.schema import (
    SignalCandidateInput, SignalSide, ComponentScore, make_component_score,
)

COMPONENT_NAMES = K.COMPONENT_NAMES

NEUTRAL_FALLBACK = 50.0
INVALID_FALLBACK = 25.0

_OK = "ok"
_MISSING = "missing"
_INVALID = "invalid"


_INVALID_BLOCK = object()  # sentinel: the context block itself was not a dict


def _block(ci: SignalCandidateInput, block_name: str):
    """Return the context dict, or the _INVALID_BLOCK sentinel if the block is
    present-but-not-a-dict / unusable. A non-dict block is INVALID (not
    missing): components fall back to the conservative-low path."""
    b = getattr(ci, block_name, {})
    if isinstance(b, dict):
        return b
    return _INVALID_BLOCK


def _get(block, key: str, coerce: Callable) -> Tuple[Any, str]:
    """Return (value, status). status in {ok, missing, invalid}. Never raises.
    If the whole block is the _INVALID_BLOCK sentinel, every key reads as
    invalid (a non-dict block is corrupt, not merely absent)."""
    if block is _INVALID_BLOCK:
        return None, _INVALID
    if key not in block:
        return None, _MISSING
    try:
        return coerce(block[key]), _OK
    except K.InvalidContextValue:
        return None, _INVALID
    except (ValueError, TypeError):
        return None, _INVALID


def _fallback_for(statuses) -> Tuple[float, list, list]:
    """Given a set of statuses, return (score, reason_codes, warnings) for the
    fallback path: any invalid -> 25; else (all missing) -> 50."""
    if _INVALID in statuses:
        return INVALID_FALLBACK, ["fallback_invalid_input"], ["invalid_soft_input"]
    return NEUTRAL_FALLBACK, ["fallback_missing_input"], ["missing_soft_input"]


def _mk(name, score, reasons=None, warnings=None, used=None, blocked=None):
    return make_component_score(
        name, score, allowed_components=COMPONENT_NAMES,
        reason_codes=reasons, warnings=warnings,
        inputs_used=used, blocked_reasons=blocked)


# ─────────────────────────── ml ───────────────────────────
def score_ml(ci: SignalCandidateInput, config: SignalScoringConfig
             ) -> ComponentScore:
    ml = _block(ci, "ml_context")
    applied, st_app = _get(ml, K.ML_CALIBRATION_APPLIED, K.as_bool)
    cal, st_cal = _get(ml, K.ML_PRED_CALIBRATED, K.as_probability)
    raw, st_raw = _get(ml, K.ML_PRED_RAW, K.as_probability)
    reasons, warnings, used, blocked = [], [], {}, []

    use_prob = None
    if applied is True and st_cal == _OK and cal is not None:
        use_prob = cal
        used["prediction_calibrated"] = cal
        reasons.append("ml_calibrated_probability_used")
    elif st_raw == _OK and raw is not None:
        use_prob = raw
        used["prediction_raw"] = raw
        warnings.append("raw_probability_used")
        reasons.append("ml_raw_probability_used")
    else:
        # both unavailable/invalid -> conservative low (not neutral)
        used["calibration_applied"] = applied
        return _mk("ml", INVALID_FALLBACK,
                   reasons=["ml_probability_unavailable"],
                   warnings=["ml_probability_unavailable"],
                   used=used, blocked=["ml_probability_unavailable"])

    hc = config.ml["high_conviction_probability"]
    mn = config.ml["min_calibrated_probability_for_eligible"]
    if use_prob >= hc:
        reasons.append("ml_high_conviction_band")
    elif use_prob >= mn:
        reasons.append("ml_eligible_band")
    else:
        reasons.append("ml_weak_band")
    return _mk("ml", use_prob * 100.0, reasons=reasons, warnings=warnings,
               used=used, blocked=blocked)


# ───────────────────────── scanner ─────────────────────────
def score_scanner(ci: SignalCandidateInput, config: SignalScoringConfig
                  ) -> ComponentScore:
    sc = _block(ci, "scanner_context")
    valid, st_v = _get(sc, K.SCAN_VALID_COUNT, K.as_number)
    avail, st_a = _get(sc, K.SCAN_AVAILABLE_TIMEFRAMES, K.timeframe_count)
    if st_v != _OK or st_a != _OK:
        score, reasons, warnings = _fallback_for({st_v, st_a})
        return _mk("scanner", score, reasons=reasons, warnings=warnings,
                   used={"valid_count": valid, "available_timeframes": avail},
                   blocked=["scanner_inputs_unusable"])
    valid = int(valid)
    per = config.scanner.get("score_per_confirmed_timeframe", 20) \
        if "score_per_confirmed_timeframe" in config.scanner else 20
    # config.scanner in M19.A holds counts only; use plan defaults for scoring.
    per_tf = 20.0
    bonus = 10.0 if (avail and valid >= avail) else 0.0
    missing_tf = max(0, (avail or 0) - valid)
    score = per_tf * valid + bonus - 10.0 * missing_tf
    reasons = [f"scanner_{valid}_of_{avail}_timeframes"]
    return _mk("scanner", score, reasons=reasons,
               used={"valid_count": valid, "available_timeframes": avail})


# ─────────────── shared technical helpers (pure) ───────────────
def _trend_subscore(ema20, ema50, side) -> float:
    if ema20 is None or ema50 is None:
        return NEUTRAL_FALLBACK
    bullish = ema20 > ema50
    if side == SignalSide.LONG:
        return 80.0 if bullish else 30.0
    return 80.0 if not bullish else 30.0  # SHORT favors ema20<ema50


def _momentum_subscore(rsi, macd_hist, side) -> float:
    s = NEUTRAL_FALLBACK
    if rsi is not None:
        if side == SignalSide.LONG:
            s = 75.0 if 50 <= rsi <= 70 else (40.0 if rsi > 70 else 35.0)
        else:
            s = 75.0 if 30 <= rsi <= 50 else (40.0 if rsi < 30 else 35.0)
    if macd_hist is not None:
        pos = macd_hist > 0
        favor = pos if side == SignalSide.LONG else (not pos)
        s = min(100.0, s + (10.0 if favor else -10.0))
    return max(0.0, s)


def _volume_subscore(volume_ratio) -> float:
    if volume_ratio is None:
        return NEUTRAL_FALLBACK
    if volume_ratio >= 1.5:
        return 85.0
    if volume_ratio >= 1.0:
        return 65.0
    return 40.0


def _volatility_band_subscore(atr_pct, config) -> float:
    if atr_pct is None:
        return NEUTRAL_FALLBACK
    v = config.volatility
    if v["atr_pct_ideal_min"] <= atr_pct <= v["atr_pct_ideal_max"]:
        return 90.0
    if atr_pct < v["atr_pct_min"]:
        return 30.0
    if atr_pct > v["atr_pct_max"]:
        return 25.0
    return 60.0  # outside ideal but within allowed range


# ─────────────────── technical_confluence ───────────────────
def score_technical_confluence(ci: SignalCandidateInput,
                               config: SignalScoringConfig) -> ComponentScore:
    tc = _block(ci, "technical_context")
    side = ci.side
    ema20, s1 = _get(tc, K.TECH_EMA20, K.as_number)
    ema50, s2 = _get(tc, K.TECH_EMA50, K.as_number)
    rsi, s3 = _get(tc, K.TECH_RSI, K.as_number)
    macd, s4 = _get(tc, K.TECH_MACD_HIST, K.as_number)
    volr, s5 = _get(tc, K.TECH_VOLUME_RATIO, K.as_number)
    atr, s6 = _get(tc, K.TECH_ATR_PCT, K.as_number)
    statuses = {s1, s2, s3, s4, s5, s6}
    # Non-dict block (or every key invalid) -> conservative-low fallback.
    if tc is _INVALID_BLOCK or statuses == {_INVALID}:
        return _mk("technical_confluence", INVALID_FALLBACK,
                   reasons=["fallback_invalid_input"],
                   warnings=["invalid_soft_input"],
                   used={"technical_context": "invalid"})
    warnings = []
    if _INVALID in statuses:
        warnings.append("invalid_soft_input")
    if _MISSING in statuses:
        warnings.append("missing_soft_input")
    w = config.technical
    trend = _trend_subscore(ema20, ema50, side)
    mom = _momentum_subscore(rsi, macd, side)
    vol = _volume_subscore(volr)
    vola = _volatility_band_subscore(atr, config)
    sr = NEUTRAL_FALLBACK  # support_resistance handled softly in M19.C
    score = (trend * w["trend_weight"] + mom * w["momentum_weight"]
             + vol * w["volume_weight"] + vola * w["volatility_weight"]
             + sr * w["support_resistance_weight"])
    return _mk("technical_confluence", score,
               reasons=["technical_confluence_blend"], warnings=warnings,
               used={"ema20": ema20, "ema50": ema50, "rsi": rsi,
                     "macd_hist": macd, "volume_ratio": volr, "atr_pct": atr})


# ───────────────────────── trend ─────────────────────────
def score_trend(ci: SignalCandidateInput, config: SignalScoringConfig
                ) -> ComponentScore:
    tc = _block(ci, "technical_context")
    ema20, s1 = _get(tc, K.TECH_EMA20, K.as_number)
    ema50, s2 = _get(tc, K.TECH_EMA50, K.as_number)
    if s1 != _OK or s2 != _OK:
        score, reasons, warnings = _fallback_for({s1, s2})
        return _mk("trend", score, reasons=reasons, warnings=warnings,
                   used={"ema20": ema20, "ema50": ema50})
    score = _trend_subscore(ema20, ema50, ci.side)
    reason = "trend_aligned" if score >= 60 else "trend_against"
    return _mk("trend", score, reasons=[reason],
               used={"ema20": ema20, "ema50": ema50})


# ──────────────────────── momentum ────────────────────────
def score_momentum(ci: SignalCandidateInput, config: SignalScoringConfig
                   ) -> ComponentScore:
    tc = _block(ci, "technical_context")
    rsi, s1 = _get(tc, K.TECH_RSI, K.as_number)
    macd, s2 = _get(tc, K.TECH_MACD_HIST, K.as_number)
    if s1 == _INVALID or s2 == _INVALID:
        return _mk("momentum", INVALID_FALLBACK,
                   reasons=["fallback_invalid_input"],
                   warnings=["invalid_soft_input"],
                   used={"rsi": rsi, "macd_hist": macd})
    if s1 == _MISSING and s2 == _MISSING:
        return _mk("momentum", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"],
                   used={"rsi": rsi, "macd_hist": macd})
    score = _momentum_subscore(rsi, macd, ci.side)
    return _mk("momentum", score, reasons=["momentum_evaluated"],
               used={"rsi": rsi, "macd_hist": macd})


# ──────────────────── volume_liquidity ────────────────────
def score_volume_liquidity(ci: SignalCandidateInput,
                           config: SignalScoringConfig) -> ComponentScore:
    lq = _block(ci, "liquidity_context")
    adv20, s1 = _get(lq, K.LIQ_AVG_DOLLAR_VOLUME_20D, K.as_number)
    volr, s2 = _get(_block(ci, "technical_context"),
                    K.TECH_VOLUME_RATIO, K.as_number)
    if s1 == _INVALID:
        return _mk("volume_liquidity", INVALID_FALLBACK,
                   reasons=["fallback_invalid_input"],
                   warnings=["invalid_soft_input"],
                   used={"avg_dollar_volume_20d": adv20})
    if s1 == _MISSING:
        return _mk("volume_liquidity", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"],
                   used={"avg_dollar_volume_20d": adv20})
    ideal = config.liquidity["ideal_avg_dollar_volume_20d"]
    mn = config.liquidity["min_avg_dollar_volume_20d"]
    if adv20 >= ideal:
        score = 95.0; reason = "liquidity_ideal"
    elif adv20 >= mn:
        score = 70.0; reason = "liquidity_thin_but_allowed"
    else:
        score = 35.0; reason = "liquidity_below_min_soft"
    # partial soft key (volume_ratio) status must surface as a warning
    warnings = []
    if s2 == _INVALID:
        warnings.append("invalid_soft_input")
    elif s2 == _MISSING:
        warnings.append("missing_soft_input")
    vr_used = volr if s2 == _OK else None
    return _mk("volume_liquidity", score, reasons=[reason], warnings=warnings,
               used={"avg_dollar_volume_20d": adv20, "volume_ratio": vr_used})


# ──────────────────────── volatility ────────────────────────
def score_volatility(ci: SignalCandidateInput, config: SignalScoringConfig
                     ) -> ComponentScore:
    vc = _block(ci, "volatility_context")
    atr, st = _get(vc, K.VOL_ATR_PCT, K.as_number)
    if st == _INVALID:
        return _mk("volatility", INVALID_FALLBACK,
                   reasons=["fallback_invalid_input"],
                   warnings=["invalid_soft_input"], used={"atr_pct": atr})
    if st == _MISSING:
        return _mk("volatility", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"], used={"atr_pct": atr})
    score = _volatility_band_subscore(atr, config)
    if score >= 80:
        reason = "volatility_ideal"
    elif score <= 30:
        reason = "volatility_extreme"
    else:
        reason = "volatility_acceptable"
    return _mk("volatility", score, reasons=[reason], used={"atr_pct": atr})


# ─────────────────────── market_regime ───────────────────────
def score_market_regime(ci: SignalCandidateInput, config: SignalScoringConfig
                        ) -> ComponentScore:
    rc = _block(ci, "regime_context")
    label, st = _get(rc, K.REGIME_LABEL, K.as_str)
    if st == _INVALID:
        return _mk("market_regime", INVALID_FALLBACK,
                   reasons=["fallback_invalid_input"],
                   warnings=["invalid_soft_input"],
                   used={"regime_label": None})
    if st == _MISSING:
        return _mk("market_regime", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"], used={"regime_label": None})
    lab = label.lower()
    side = ci.side
    favorable_long = lab in ("bull", "uptrend", "above_sma", "risk_on")
    countertrend = lab in ("bear", "downtrend", "below_sma", "risk_off")
    if lab in ("unknown", "neutral", "mixed"):
        score, reason = NEUTRAL_FALLBACK, "regime_unknown"
    elif side == SignalSide.LONG:
        score, reason = (85.0, "regime_aligned") if favorable_long else (
            (30.0, "regime_countertrend") if countertrend
            else (NEUTRAL_FALLBACK, "regime_unknown"))
    else:  # SHORT
        score, reason = (85.0, "regime_aligned") if countertrend else (
            (30.0, "regime_countertrend") if favorable_long
            else (NEUTRAL_FALLBACK, "regime_unknown"))
    return _mk("market_regime", score, reasons=[reason],
               used={"regime_label": label})


# ─────────────────────── risk_adjusted ───────────────────────
def score_risk_adjusted(ci: SignalCandidateInput, config: SignalScoringConfig
                        ) -> ComponentScore:
    rp = _block(ci, "risk_preview")
    rr, st = _get(rp, K.RISK_REWARD_RISK_RATIO, K.as_number)
    if st == _INVALID:
        return _mk("risk_adjusted", INVALID_FALLBACK,
                   reasons=["fallback_invalid_input"],
                   warnings=["invalid_soft_input"], used={"reward_risk_ratio": rr})
    if st == _MISSING:
        return _mk("risk_adjusted", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"], used={"reward_risk_ratio": rr})
    mn = config.risk["min_reward_risk_ratio"]
    ideal = config.risk["ideal_reward_risk_ratio"]
    if rr >= ideal:
        score, reason = 90.0, "reward_risk_ideal"
    elif rr >= mn:
        score, reason = 65.0, "reward_risk_ok"
    else:
        score, reason = 30.0, "reward_risk_below_min"
    return _mk("risk_adjusted", score, reasons=[reason],
               used={"reward_risk_ratio": rr})


# ─────────────────────── data_quality ───────────────────────
def score_data_quality(ci: SignalCandidateInput, config: SignalScoringConfig
                       ) -> ComponentScore:
    dq = _block(ci, "data_quality_context")
    miss, s1 = _get(dq, K.DQ_MISSING_FEATURE_COUNT, K.as_number)
    schema, s2 = _get(dq, K.DQ_SCHEMA_MATCH, K.as_bool)
    stale, s3 = _get(dq, K.DQ_STALE_DATA_FLAG, K.as_bool)
    fresh, s4 = _get(dq, K.DQ_DATA_FRESHNESS_MINUTES, K.as_number)
    statuses = {s1, s2, s3, s4}
    if statuses == {_MISSING}:
        return _mk("data_quality", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"])
    score = 100.0
    reasons, warnings = [], []
    if _INVALID in statuses:
        warnings.append("invalid_soft_input")
        score = min(score, INVALID_FALLBACK)
    if _MISSING in statuses:
        warnings.append("missing_soft_input")
    if s1 == _OK and miss and miss > 0:
        score -= min(40.0, 10.0 * miss); reasons.append("missing_features")
    if s2 == _OK and schema is not True:
        score -= 30.0; reasons.append("schema_not_matched_soft")
    if s3 == _OK and stale is True:
        score -= 30.0; reasons.append("stale_flag_soft")
    if s4 == _OK and fresh is not None:
        maxage = config.data_quality["stale_data_max_age_minutes"]
        if fresh > maxage:
            score -= 20.0; reasons.append("freshness_exceeded_soft")
    if not reasons:
        reasons.append("data_quality_clean")
    return _mk("data_quality", max(0.0, score), reasons=reasons,
               warnings=warnings,
               used={"missing_feature_count": miss, "schema_match": schema,
                     "stale_data_flag": stale, "data_freshness_minutes": fresh})


# ─────────────────── calibration_uncertainty ───────────────────
def score_calibration_uncertainty(ci: SignalCandidateInput,
                                  config: SignalScoringConfig) -> ComponentScore:
    ml = _block(ci, "ml_context")
    applied, s1 = _get(ml, K.ML_CALIBRATION_APPLIED, K.as_bool)
    extrap, s2 = _get(ml, K.ML_FEATURE_EXTRAPOLATION_COUNT, K.as_number)
    thin, s3 = _get(ml, K.ML_PRODUCTION_THINNESS_STATUS,
                    lambda v: K.as_enum_str(v, K.ALLOWED_PRODUCTION_THINNESS_STATUS))
    if {s1, s2, s3} == {_MISSING}:
        return _mk("calibration_uncertainty", NEUTRAL_FALLBACK,
                   reasons=["fallback_missing_input"],
                   warnings=["missing_soft_input"])
    score = 100.0
    reasons, warnings = [], []
    if _INVALID in {s1, s2, s3}:
        warnings.append("invalid_soft_input")
        score = min(score, INVALID_FALLBACK)
    if _MISSING in {s1, s2, s3}:
        warnings.append("missing_soft_input")
    if s1 == _OK and applied is not True:
        score -= 40.0; reasons.append("calibration_not_applied")
        warnings.append("raw_probability_used")
    if s2 == _OK and extrap and extrap > 0:
        score -= min(40.0, 8.0 * extrap); reasons.append("feature_extrapolation")
    if s3 == _OK and thin == "warned":
        score -= 15.0; reasons.append("production_thinness_warned")
    if not reasons:
        reasons.append("calibration_confidence_clean")
    return _mk("calibration_uncertainty", max(0.0, score), reasons=reasons,
               warnings=warnings,
               used={"calibration_applied": applied,
                     "feature_extrapolation_count": extrap,
                     "production_thinness_status": thin})


# Registry: name -> scorer (deterministic order via COMPONENT_NAMES).
COMPONENT_SCORERS = {
    "ml": score_ml,
    "scanner": score_scanner,
    "technical_confluence": score_technical_confluence,
    "trend": score_trend,
    "momentum": score_momentum,
    "volume_liquidity": score_volume_liquidity,
    "volatility": score_volatility,
    "market_regime": score_market_regime,
    "risk_adjusted": score_risk_adjusted,
    "data_quality": score_data_quality,
    "calibration_uncertainty": score_calibration_uncertainty,
}


def score_component(name: str, ci: SignalCandidateInput,
                    config: SignalScoringConfig) -> ComponentScore:
    """Run a single named component scorer."""
    if name not in COMPONENT_SCORERS:
        raise ValueError(f"unknown component name: {name!r}")
    return COMPONENT_SCORERS[name](ci, config)


def score_all_components(ci: SignalCandidateInput,
                         config: SignalScoringConfig) -> Dict[str, ComponentScore]:
    """Run every component in deterministic COMPONENT_NAMES order. Does NOT
    aggregate, weight, penalise, or assign any decision bucket."""
    return {name: COMPONENT_SCORERS[name](ci, config)
            for name in COMPONENT_NAMES}

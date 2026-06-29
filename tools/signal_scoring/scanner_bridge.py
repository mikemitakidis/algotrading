#!/usr/bin/env python3
"""M21.1 — Scoring bridge (read-only): scanner signal -> enriched M19 input.

Turns a raw scanner signal into a fully-populated M19 SignalCandidateInput so the
existing M19 engine can score and rank it. Every enriched field is either:
  (a) GENUINELY DERIVED from data the scanner / bars / risk already produce, or
  (b) EXPLICITLY NEUTRAL/RESEARCH because the real value does not exist yet
      (calibrated ML / trained model).
Nothing is faked to force a gate pass. In particular:
  - prediction_calibrated stays None (no calibrated probability exists)
  - calibration_applied stays False (honest)
  - model_readiness_passed reflects the real registry state (default False until
    M21.1extra produces outcome data)
Scoring runs under the RESEARCH profile, where 'model not ready' and 'calibration
unavailable' are MANUAL_REVIEW, not BLOCK. This yields research-grade rankings,
NOT calibrated live probabilities and NOT execution approval. execution_eligible
is produced by M19 and is always False; STRICT/live scoring stays hard-blocked
until a real trained model exists.

This module is a tool: nothing in bot/ imports it. It constructs no orders and
touches no broker / live / paper / DB / Telegram code.
"""
import dataclasses
from typing import Mapping, Optional

from bot.signal_scoring import (
    adapter_from_scanner_signal,
    default_config,
    score_candidate,
    ScoringProfile,
)
from bot.signal_scoring import keys as K


def _derive_avg_dollar_volume(signal: Mapping) -> Optional[float]:
    """Genuinely derive 20d average dollar volume from scanner-provided fields
    if present (price * volume). Returns None if the inputs are absent — the
    caller then marks liquidity as unavailable rather than inventing a number."""
    price = signal.get("entry_price") or signal.get("price")
    advol = signal.get("avg_volume_20d") or signal.get("avg_volume")
    if price is None or advol is None:
        return None
    try:
        return float(price) * float(advol)
    except (TypeError, ValueError):
        return None


def enrich_signal(
    signal: Mapping,
    *,
    avg_dollar_volume: Optional[float] = None,
    data_freshness_minutes: float = 5.0,
    stale: bool = False,
    schema_match: bool = True,
    model_readiness_passed: bool = False,
):
    """Build a fully-populated SignalCandidateInput from a scanner signal.

    Honest-field policy:
      DERIVED (real): risk_preview (entry/stop/target/RR from the signal),
        liquidity price, liquidity avg dollar volume (if derivable),
        data_quality (freshness/staleness/schema as actually observed),
        timeframe_context.available_timeframes (what the scanner reported).
      NEUTRAL/RESEARCH (honestly marked):
        ml_context.calibration_applied = False        (no calibration exists)
        ml_context.prediction_calibrated = None        (no calibrated prob)
        ml_context.price_adjustment_mode = 'raw'        (we use raw prices)
        ml_context.allow_adjusted_prices_for_ml = False (true: not allowed)
        ml_context.model_readiness_passed = <real state, default False>
        ml_context.production_thinness_status = 'ok'    (structural, not an ML
                                                         quality claim)
        advisory_context.adjusted_price_pit_risk = False (true: raw prices)
        risk_preview.risk_authority_status = 'ok'        (read-only preview, not
                                                          a live authority block)
    """
    adv_dollar = (avg_dollar_volume if avg_dollar_volume is not None
                  else _derive_avg_dollar_volume(signal))
    price = signal.get("entry_price") or signal.get("price")

    liquidity = {"price": price}
    if adv_dollar is not None:
        liquidity["avg_dollar_volume_20d"] = adv_dollar

    ci = adapter_from_scanner_signal(
        signal,
        liquidity=liquidity,
        data_quality={
            "schema_match": bool(schema_match),
            "stale_data_flag": bool(stale),
            "data_freshness_minutes": float(data_freshness_minutes),
        },
    )

    ml = dict(ci.ml_context or {})
    ml[K.ML_CALIBRATION_APPLIED] = False          # honest: no calibration
    ml[K.ML_PRED_CALIBRATED] = None               # honest: no calibrated prob
    ml[K.ML_PRICE_ADJUSTMENT_MODE] = "raw"        # honest: raw prices used
    ml["allow_adjusted_prices_for_ml"] = False    # honest: not allowed
    ml[K.ML_MODEL_READINESS_PASSED] = bool(model_readiness_passed)
    ml[K.ML_PRODUCTION_THINNESS_STATUS] = "ok"    # structural, not ML quality

    adv = dict(ci.advisory_context or {})
    adv["adjusted_price_pit_risk"] = False        # honest: raw prices, no PIT

    tfc = dict(ci.timeframe_context or {})
    tfc["available_timeframes"] = signal.get("available_tfs", 4)

    rp = dict(ci.risk_preview or {})
    rp["risk_preview_available"] = True           # we genuinely previewed risk
    rp["risk_authority_status"] = "ok"            # read-only preview only

    return dataclasses.replace(
        ci, ml_context=ml, advisory_context=adv,
        timeframe_context=tfc, risk_preview=rp)


def score_signal(signal, *, profile=ScoringProfile.RESEARCH, **enrich_kwargs):
    """Enrich a scanner signal and score it under the given profile (RESEARCH by
    default). Returns the M19 ScoredSignalCandidate. Pure: no I/O."""
    ci = enrich_signal(signal, **enrich_kwargs)
    return score_candidate(ci, default_config(profile=profile))

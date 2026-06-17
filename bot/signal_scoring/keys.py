"""M19.B keys — frozen string constants for gate-readable context keys.

Hard gates read context values via these constants ONLY (no untyped magic
strings). GATE_REQUIRED_KEYS declares, per input block, the keys that hard
gates depend on so the engine can fail-safe with `missing_context_key` instead
of raising KeyError. Non-gate keys (technical/momentum/volatility internals,
etc.) are intentionally NOT frozen here — later phases own those.
"""
from __future__ import annotations

# ── ml_context ──
ML_MODEL_ID                   = "model_id"
ML_CALIBRATION_APPLIED        = "calibration_applied"
ML_PRED_CALIBRATED            = "prediction_calibrated"
ML_PRED_RAW                   = "prediction_raw"
ML_PRICE_ADJUSTMENT_MODE      = "price_adjustment_mode"
ML_ALLOW_ADJUSTED_FOR_ML      = "allow_adjusted_prices_for_ml"
ML_MODEL_READINESS_PASSED     = "model_readiness_passed"
ML_PRODUCTION_THINNESS_STATUS = "production_thinness_status"

# ── data_quality_context ──
DQ_SCHEMA_MATCH               = "schema_match"
DQ_STALE_DATA_FLAG            = "stale_data_flag"
DQ_DATA_FRESHNESS_MINUTES     = "data_freshness_minutes"
DQ_MISSING_FEATURE_COUNT      = "missing_feature_count"

# ── advisory_context ──
ADV_ADJUSTED_PRICE_PIT_RISK   = "adjusted_price_pit_risk"
ADV_SHORT_SIDE_VALIDATED      = "scanner_replica_short_side_validated"
ADV_FOURH_ALIGNMENT           = "fourh_bucket_alignment"  # advisory only (not a gate)

# ── timeframe_context ──
TF_AVAILABLE                  = "available_timeframes"
TF_VALID                      = "valid_timeframes"

# ── risk_preview ──
RISK_PREVIEW_AVAILABLE        = "risk_preview_available"
RISK_AUTHORITY_STATUS         = "risk_authority_status"

# ── liquidity_context ──
LIQ_AVG_DOLLAR_VOLUME_20D     = "avg_dollar_volume_20d"
LIQ_PRICE                     = "price"


# Per-block required keys that the M19.B hard gates read. If any are absent,
# the engine emits a `missing_context_key` BLOCK (fail-safe).
GATE_REQUIRED_KEYS = {
    "ml_context": (
        ML_CALIBRATION_APPLIED,
        ML_PRED_CALIBRATED,
        ML_PRICE_ADJUSTMENT_MODE,
        ML_ALLOW_ADJUSTED_FOR_ML,
        ML_MODEL_READINESS_PASSED,
        ML_PRODUCTION_THINNESS_STATUS,
    ),
    "data_quality_context": (
        DQ_SCHEMA_MATCH,
        DQ_STALE_DATA_FLAG,
        DQ_DATA_FRESHNESS_MINUTES,
    ),
    "advisory_context": (
        ADV_ADJUSTED_PRICE_PIT_RISK,
    ),
    "timeframe_context": (
        TF_AVAILABLE,
    ),
    "risk_preview": (
        RISK_PREVIEW_AVAILABLE,
        RISK_AUTHORITY_STATUS,
    ),
    "liquidity_context": (
        LIQ_AVG_DOLLAR_VOLUME_20D,
        LIQ_PRICE,
    ),
}


# ── safe coercion helpers (fail-safe; raise _InvalidContextValue on bad data) ──
class InvalidContextValue(ValueError):
    """Raised internally when a gate-critical context value cannot be coerced
    to its expected type. The engine converts this into an
    `invalid_context_value` BLOCK rather than letting it propagate."""


def as_bool(value) -> bool:
    """Strict bool coercion: only real bools accepted. Strings like 'yes' are
    rejected (fail-safe) to avoid silently treating ambiguous data as True."""
    if isinstance(value, bool):
        return value
    raise InvalidContextValue(f"expected bool, got {value!r}")


def as_number(value) -> float:
    """Numeric coercion: real int/float only (bools rejected). Numeric strings
    are rejected to keep the safety surface unambiguous."""
    if isinstance(value, bool):
        raise InvalidContextValue(f"expected number, got bool {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    raise InvalidContextValue(f"expected number, got {value!r}")


def timeframe_count(value) -> int:
    """available_timeframes may be an int count or a list of TF names.
    Returns a safe count; raises InvalidContextValue on anything else."""
    if isinstance(value, bool):
        raise InvalidContextValue(f"expected count/list, got bool {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    raise InvalidContextValue(f"expected int or list, got {value!r}")

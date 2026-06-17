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


# ─────────────── M19.C component-readable (soft) keys ───────────────
# Soft scoring inputs. Unlike gate keys, absence/invalidity must NOT block;
# component scorers fall back conservatively (see components.py). Reuse the
# gate-frozen constants above where the same field is used (no duplicate
# string literals).

# technical_context
TECH_EMA20                    = "ema20"
TECH_EMA50                    = "ema50"
TECH_RSI                      = "rsi"
TECH_MACD_HIST                = "macd_hist"
TECH_MACD_SIGNAL              = "macd_signal"
TECH_VWAP_DEV                 = "vwap_dev"
TECH_ATR_PCT                  = "atr_pct"
TECH_BB_POS                   = "bb_pos"
TECH_VOLUME_RATIO             = "volume_ratio"
TECH_SUPPORT_RESISTANCE_DIST  = "support_resistance_distance"

# volatility_context  (ATR% reuses the same field name as technical atr_pct)
VOL_ATR_PCT                   = "atr_pct"
VOL_BAND                      = "volatility_band"

# liquidity_context (soft) — volume/price reuse LIQ_* gate keys above
LIQ_SPREAD_PCT                = "spread_pct"

# regime_context
REGIME_LABEL                  = "regime_label"
REGIME_BENCHMARK_TREND        = "benchmark_trend"
REGIME_SOURCE                 = "regime_source"

# scanner_context
SCAN_VALID_COUNT              = "valid_count"
SCAN_REQUIRED_COUNT           = "required_count"
SCAN_VALID_TIMEFRAMES         = "valid_timeframes"
SCAN_AVAILABLE_TIMEFRAMES     = "available_timeframes"

# risk_preview (soft)
RISK_REWARD_RISK_RATIO        = "reward_risk_ratio"
RISK_ESTIMATED_STOP           = "estimated_stop"
RISK_ESTIMATED_TARGET         = "estimated_target"
RISK_POSITION_SIZE_PREVIEW    = "position_size_preview"

# ml_context (soft) — reuse ML_PRED_*/ML_CALIBRATION_APPLIED/ML_PRODUCTION_*
ML_FEATURE_EXTRAPOLATION_COUNT = "feature_extrapolation_count"

# Canonical, deterministic component order (used in tests + exports).
COMPONENT_NAMES = (
    "ml",
    "scanner",
    "technical_confluence",
    "trend",
    "momentum",
    "volume_liquidity",
    "volatility",
    "market_regime",
    "risk_adjusted",
    "data_quality",
    "calibration_uncertainty",
)

# Soft keys each component reads (advisory; never gate-blocking). Each entry is
# a tuple of (block_name, (keys...)) pairs so a component may declare reads from
# more than one context block. This map MUST match what each scorer actually
# reads (verified by test_component_readable_keys_match_reads).
COMPONENT_READABLE_KEYS = {
    "ml": (
        ("ml_context", (ML_CALIBRATION_APPLIED, ML_PRED_CALIBRATED,
                        ML_PRED_RAW)),
    ),
    "scanner": (
        ("scanner_context", (SCAN_VALID_COUNT, SCAN_AVAILABLE_TIMEFRAMES)),
    ),
    "technical_confluence": (
        ("technical_context", (TECH_EMA20, TECH_EMA50, TECH_RSI,
                               TECH_MACD_HIST, TECH_VOLUME_RATIO,
                               TECH_ATR_PCT)),
    ),
    "trend": (
        ("technical_context", (TECH_EMA20, TECH_EMA50)),
    ),
    "momentum": (
        ("technical_context", (TECH_RSI, TECH_MACD_HIST)),
    ),
    "volume_liquidity": (
        ("liquidity_context", (LIQ_AVG_DOLLAR_VOLUME_20D, LIQ_PRICE,
                               LIQ_SPREAD_PCT)),
        ("technical_context", (TECH_VOLUME_RATIO,)),
    ),
    "volatility": (
        ("volatility_context", (VOL_ATR_PCT,)),
    ),
    "market_regime": (
        ("regime_context", (REGIME_LABEL,)),
    ),
    "risk_adjusted": (
        ("risk_preview", (RISK_REWARD_RISK_RATIO,)),
    ),
    "data_quality": (
        ("data_quality_context", (DQ_MISSING_FEATURE_COUNT, DQ_SCHEMA_MATCH,
                                  DQ_STALE_DATA_FLAG, DQ_DATA_FRESHNESS_MINUTES)),
    ),
    "calibration_uncertainty": (
        ("ml_context", (ML_CALIBRATION_APPLIED, ML_FEATURE_EXTRAPOLATION_COUNT,
                        ML_PRODUCTION_THINNESS_STATUS)),
    ),
}


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


# Allowed value sets for gate-critical string fields. Unknown values must
# fail-safe (invalid_context_value BLOCK), never pass silently.
ALLOWED_PRICE_ADJUSTMENT_MODE = ("raw", "adjusted")
ALLOWED_PRODUCTION_THINNESS_STATUS = ("ok", "warned", "blocked")
ALLOWED_RISK_AUTHORITY_STATUS = ("ok", "blocked")


def as_enum_str(value, allowed) -> str:
    """Coerce to one of the allowed string values; raise InvalidContextValue
    on a non-string or an unknown string (fail-safe)."""
    if not isinstance(value, str):
        raise InvalidContextValue(f"expected str, got {value!r}")
    if value not in allowed:
        raise InvalidContextValue(
            f"value {value!r} not in allowed {tuple(allowed)}")
    return value


def as_probability(value) -> float:
    """Coerce a calibrated/raw probability: real int/float only (bools
    rejected), within [0.0, 1.0]. Raises InvalidContextValue otherwise
    (fail-safe)."""
    if isinstance(value, bool):
        raise InvalidContextValue(f"expected probability, got bool {value!r}")
    if not isinstance(value, (int, float)):
        raise InvalidContextValue(f"expected numeric probability, got {value!r}")
    fv = float(value)
    if not (0.0 <= fv <= 1.0):
        raise InvalidContextValue(f"probability out of range [0,1]: {fv}")
    return fv


def as_str(value) -> str:
    """Coerce to a non-empty string; raise InvalidContextValue otherwise."""
    if not isinstance(value, str) or not value:
        raise InvalidContextValue(f"expected non-empty str, got {value!r}")
    return value


def as_nonneg_number(value) -> float:
    """Coerce to a non-negative real number (bools rejected; negatives are
    invalid since counts/volumes/ATR cannot be negative)."""
    n = as_number(value)  # rejects bool / non-numeric
    if n < 0:
        raise InvalidContextValue(f"expected non-negative number, got {n}")
    return n


# ─────────────── M19.D penalty / multiplier names + readable keys ───────────────
PENALTY_NAMES = (
    "uncalibrated_ml_probability",
    "each_feature_extrapolation",
    "production_thinness_warning",
    "missing_noncritical_timeframe",
    "weak_scanner_confluence",
    "poor_reward_risk",
)

MULTIPLIER_NAMES = (
    "regime",
    "volatility",
    "liquidity",
    "fourh_alignment",
)

# Soft keys each penalty trigger reads (raw context + config only; advisory).
# Must match actual reads (anti-drift tested).
PENALTY_READABLE_KEYS = {
    "uncalibrated_ml_probability": (
        ("ml_context", (ML_CALIBRATION_APPLIED,)),
    ),
    "each_feature_extrapolation": (
        ("ml_context", (ML_FEATURE_EXTRAPOLATION_COUNT,)),
    ),
    "production_thinness_warning": (
        ("ml_context", (ML_PRODUCTION_THINNESS_STATUS,)),
    ),
    "missing_noncritical_timeframe": (
        ("scanner_context", (SCAN_VALID_COUNT, SCAN_AVAILABLE_TIMEFRAMES)),
    ),
    "weak_scanner_confluence": (
        ("scanner_context", (SCAN_VALID_COUNT,)),
    ),
    "poor_reward_risk": (
        ("risk_preview", (RISK_REWARD_RISK_RATIO,)),
    ),
}

# Soft keys each multiplier trigger reads.
MULTIPLIER_READABLE_KEYS = {
    "regime": (
        ("regime_context", (REGIME_LABEL,)),
    ),
    "volatility": (
        ("volatility_context", (VOL_ATR_PCT,)),
    ),
    "liquidity": (
        ("liquidity_context", (LIQ_AVG_DOLLAR_VOLUME_20D,)),
    ),
    "fourh_alignment": (
        ("advisory_context", (ADV_FOURH_ALIGNMENT,)),
    ),
}

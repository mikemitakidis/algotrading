"""M19.F adapters — pure upstream -> SignalCandidateInput converters.

Converts real upstream structures into a scoring-ready SignalCandidateInput:
  * scanner signal dict            (bot/scanner.py)
  * flywheel candidate_snapshots   (bot/flywheel.py CANDIDATE_SCHEMA)
  * ML PredictionResult            (bot/ml/registry/predictions.py)
  * readiness advisory dict        (bot/ml/readiness.py)

Principles (per approved M19.F plan):
  * PURE: no fetch, no write, no DB, no broker/live/dashboard/main.
  * Required identity fields (symbol, side/direction, timestamp) missing/invalid
    -> ValueError. Optional scoring fields: omit if missing; pass present-but-
    malformed values THROUGH so the downstream invalid policy fires. Never
    fabricate clean defaults.
  * merge_* return a NEW SignalCandidateInput; never mutate the original.
  * Adapters do not interpret PIT/4H/calibration metadata — they pass it
    through verbatim. Raw ML probability is NEVER copied into calibrated.
  * INPUT ONLY. No scoring here (score_candidate already exists in M19.E).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from bot.signal_scoring import keys as K
from bot.signal_scoring.schema import SignalCandidateInput, SignalSide


# ─────────────────────────── helpers ───────────────────────────
_LONG = {"long", "buy", "l", "b"}
_SHORT = {"short", "sell", "s"}


def _require_symbol(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"adapter: symbol required (non-empty str), got {raw!r}")
    return raw


def _map_side(raw: Any) -> SignalSide:
    if isinstance(raw, SignalSide):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"adapter: side/direction required, got {raw!r}")
    v = raw.strip().lower()
    if v in _LONG:
        return SignalSide.LONG
    if v in _SHORT:
        return SignalSide.SHORT
    raise ValueError(f"adapter: unmappable side/direction {raw!r}")


def _normalize_timestamp(raw: Any) -> str:
    """Normalize to a tz-aware UTC ISO string. Reject naive / non-UTC / junk."""
    if isinstance(raw, datetime):
        if raw.tzinfo is None or raw.utcoffset() is None:
            raise ValueError("adapter: naive datetime rejected (no UTC assumed)")
        return raw.astimezone(timezone.utc).isoformat()
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"adapter: invalid timestamp {raw!r}")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("adapter: naive timestamp rejected (no UTC assumed)")
        if parsed.utcoffset().total_seconds() != 0:
            raise ValueError("adapter: non-UTC timestamp offset rejected")
        return parsed.astimezone(timezone.utc).isoformat()
    raise ValueError(f"adapter: timestamp required, got {raw!r}")


def _put(block: dict, key: str, value: Any) -> None:
    """Set key only if value is present (not None). Present-but-malformed values
    are passed through unchanged so downstream invalid handling can fire."""
    if value is not None:
        block[key] = value


def _is_pos_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _derive_reward_risk(side: SignalSide, entry: Any, stop: Any,
                        target: Any) -> Optional[float]:
    """LONG rr=(target-entry)/(entry-stop); SHORT rr=(entry-target)/(stop-entry).
    Returns None when not computable (missing/invalid/denominator<=0). Never
    raises, never fabricates."""
    nums = (entry, stop, target)
    if any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in nums):
        return None
    if side == SignalSide.LONG:
        num, den = (target - entry), (entry - stop)
    else:
        num, den = (entry - target), (stop - entry)
    if den <= 0 or num < 0:
        return None
    rr = num / den
    if rr != rr or rr in (float("inf"), float("-inf")):  # NaN/inf guard
        return None
    return rr


def _split_tfs_passing(raw: Any) -> Optional[list]:
    """flywheel tfs_passing is a '+'-joined string. Returns a list or None."""
    if isinstance(raw, str) and raw.strip():
        return [t for t in raw.split("+") if t]
    return None


def _scanner_tf_list(signal: Mapping[str, Any]) -> Optional[list]:
    """Scanner dict carries tf_15m/tf_1h/tf_4h/tf_1d truthy flags. Returns the
    list of passed TF labels, or None if no flag keys are present at all."""
    flags = (("15m", "tf_15m"), ("1H", "tf_1h"), ("4H", "tf_4h"),
             ("1D", "tf_1d"))
    if not any(fk in signal for _, fk in flags):
        return None
    passed = []
    for label, fk in flags:
        v = signal.get(fk)
        if isinstance(v, bool):
            if v:
                passed.append(label)
        elif isinstance(v, (int, float)) and v:
            passed.append(label)
    return passed


# ─────────────────────── scanner signal adapter ───────────────────────
def adapter_from_scanner_signal(
    signal: Mapping[str, Any],
    *,
    side_override: Optional[str] = None,
    liquidity: Optional[Mapping[str, Any]] = None,
    regime: Optional[Mapping[str, Any]] = None,
    data_quality: Optional[Mapping[str, Any]] = None,
) -> SignalCandidateInput:
    """Convert a scanner signal dict into a SignalCandidateInput (no scoring)."""
    sig = dict(signal)
    symbol = _require_symbol(sig.get("symbol"))
    side = _map_side(side_override if side_override is not None
                     else sig.get("direction"))
    ts = _normalize_timestamp(sig.get("timestamp"))

    entry = sig.get("entry_price")
    stop = sig.get("stop_loss")
    target = sig.get("target_price")
    atr = sig.get("atr")

    technical: dict = {}
    _put(technical, K.TECH_RSI, sig.get("rsi"))
    _put(technical, K.TECH_MACD_HIST, sig.get("macd_hist"))
    _put(technical, K.TECH_BB_POS, sig.get("bb_pos"))
    _put(technical, K.TECH_VWAP_DEV, sig.get("vwap_dev"))
    _put(technical, K.TECH_VOLUME_RATIO, sig.get("vol_ratio"))

    volatility: dict = {}
    # atr_pct = atr/entry only when both are valid positive numbers; otherwise
    # pass the malformed atr through so downstream invalid handling can fire.
    if atr is not None:
        if _is_pos_number(atr) and _is_pos_number(entry):
            volatility[K.VOL_ATR_PCT] = atr / entry
        elif not _is_pos_number(atr):
            volatility[K.VOL_ATR_PCT] = atr  # malformed -> pass through
        # atr valid but entry missing/invalid -> omit (cannot derive cleanly)

    liquidity_ctx: dict = {}
    _put(liquidity_ctx, K.LIQ_PRICE, entry)
    if liquidity is not None:
        for k, v in dict(liquidity).items():
            _put(liquidity_ctx, k, v)

    valid_count = sig.get("valid_count")
    available_tfs = sig.get("available_tfs")
    tf_list = _scanner_tf_list(sig)

    scanner_ctx: dict = {}
    _put(scanner_ctx, K.SCAN_VALID_COUNT, valid_count)
    _put(scanner_ctx, K.SCAN_AVAILABLE_TIMEFRAMES, available_tfs)
    if tf_list is not None:
        scanner_ctx[K.SCAN_VALID_TIMEFRAMES] = tf_list

    timeframe_ctx: dict = {}
    _put(timeframe_ctx, K.TF_AVAILABLE, available_tfs)
    if tf_list is not None:
        timeframe_ctx[K.TF_VALID] = tf_list
    elif valid_count is not None:
        timeframe_ctx[K.TF_VALID] = valid_count

    risk_preview: dict = {}
    _put(risk_preview, K.RISK_ESTIMATED_STOP, stop)
    _put(risk_preview, K.RISK_ESTIMATED_TARGET, target)
    rr = _derive_reward_risk(side, entry, stop, target)
    if rr is not None:
        risk_preview[K.RISK_REWARD_RISK_RATIO] = rr
        risk_preview[K.RISK_PREVIEW_AVAILABLE] = True
    elif any(v is not None for v in (entry, stop, target)):
        # required-ish fields present but rr not computable -> mark unavailable
        risk_preview[K.RISK_PREVIEW_AVAILABLE] = False

    regime_ctx = dict(regime) if regime is not None else {}
    data_quality_ctx = dict(data_quality) if data_quality is not None else {}

    return SignalCandidateInput(
        symbol=symbol, side=side, signal_timestamp_utc=ts,
        timeframe_context=timeframe_ctx,
        scanner_context=scanner_ctx,
        technical_context=technical,
        volatility_context=volatility,
        liquidity_context=liquidity_ctx,
        risk_preview=risk_preview,
        regime_context=regime_ctx,
        data_quality_context=data_quality_ctx,
        ml_context={}, advisory_context={},
    )


# ─────────────────── candidate_snapshot adapter ───────────────────
def adapter_from_candidate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    liquidity: Optional[Mapping[str, Any]] = None,
    regime: Optional[Mapping[str, Any]] = None,
    data_quality: Optional[Mapping[str, Any]] = None,
) -> SignalCandidateInput:
    """Convert a flywheel candidate_snapshots row (as dict) into a
    SignalCandidateInput (no scoring)."""
    snap = dict(snapshot)
    symbol = _require_symbol(snap.get("symbol"))
    side = _map_side(snap.get("direction"))
    ts = _normalize_timestamp(snap.get("timestamp"))

    technical: dict = {}
    _put(technical, K.TECH_RSI, snap.get("rsi"))
    _put(technical, K.TECH_MACD_HIST, snap.get("macd_hist"))
    _put(technical, K.TECH_BB_POS, snap.get("bb_pos"))
    _put(technical, K.TECH_VWAP_DEV, snap.get("vwap_dev"))
    _put(technical, K.TECH_VOLUME_RATIO, snap.get("vol_ratio"))

    volatility: dict = {}
    # snapshot has absolute atr but no price -> cannot derive atr_pct cleanly.
    # Pass malformed atr through; omit when not derivable.
    atr = snap.get("atr")
    if atr is not None and not _is_pos_number(atr):
        volatility[K.VOL_ATR_PCT] = atr

    valid_count = snap.get("valid_count")
    available_tfs = snap.get("available_tfs")
    tf_list = _split_tfs_passing(snap.get("tfs_passing"))

    scanner_ctx: dict = {}
    _put(scanner_ctx, K.SCAN_VALID_COUNT, valid_count)
    _put(scanner_ctx, K.SCAN_AVAILABLE_TIMEFRAMES, available_tfs)
    _put(scanner_ctx, K.SCAN_REQUIRED_COUNT, snap.get("min_valid"))
    if tf_list is not None:
        scanner_ctx[K.SCAN_VALID_TIMEFRAMES] = tf_list

    timeframe_ctx: dict = {}
    _put(timeframe_ctx, K.TF_AVAILABLE, available_tfs)
    if tf_list is not None:
        timeframe_ctx[K.TF_VALID] = tf_list
    elif valid_count is not None:
        timeframe_ctx[K.TF_VALID] = valid_count

    liquidity_ctx: dict = {}
    if liquidity is not None:
        for k, v in dict(liquidity).items():
            _put(liquidity_ctx, k, v)

    regime_ctx = dict(regime) if regime is not None else {}
    data_quality_ctx = dict(data_quality) if data_quality is not None else {}

    return SignalCandidateInput(
        symbol=symbol, side=side, signal_timestamp_utc=ts,
        timeframe_context=timeframe_ctx,
        scanner_context=scanner_ctx,
        technical_context=technical,
        volatility_context=volatility,
        liquidity_context=liquidity_ctx,
        risk_preview={},
        regime_context=regime_ctx,
        data_quality_context=data_quality_ctx,
        ml_context={}, advisory_context={},
    )


# ─────────────────────── ML prediction merge ───────────────────────
def merge_ml_prediction(
    candidate_input: SignalCandidateInput,
    prediction: Mapping[str, Any],
) -> SignalCandidateInput:
    """Return a NEW SignalCandidateInput with ml_context populated from a
    PredictionResult. Raw probability is NEVER copied into calibrated. The
    calibration_applied flag is taken from the per-batch prediction truth."""
    pred = dict(prediction)
    ml = dict(candidate_input.ml_context)  # copy existing; never mutate original

    _put(ml, K.ML_MODEL_ID, pred.get("model_id"))
    # raw and calibrated kept distinct; no cross-copy.
    _put(ml, K.ML_PRED_RAW, pred.get("prediction_raw"))
    if "prediction_calibrated" in pred and pred.get("prediction_calibrated") is not None:
        ml[K.ML_PRED_CALIBRATED] = pred["prediction_calibrated"]
    # calibration_applied: per-batch truth. Accept either field name; prefer the
    # prediction-level one. Pass present-but-malformed values through.
    applied = None
    if "prediction_calibration_applied" in pred:
        applied = pred.get("prediction_calibration_applied")
    elif "predict_time_calibration_applied" in pred:
        applied = pred.get("predict_time_calibration_applied")
    elif "calibration_applied" in pred:
        applied = pred.get("calibration_applied")
    _put(ml, K.ML_CALIBRATION_APPLIED, applied)
    _put(ml, K.ML_PRODUCTION_THINNESS_STATUS, pred.get("production_thinness_status"))
    _put(ml, K.ML_FEATURE_EXTRAPOLATION_COUNT, pred.get("feature_extrapolation_count"))
    _put(ml, K.ML_MODEL_READINESS_PASSED, pred.get("model_readiness_passed"))

    return _replace_blocks(candidate_input, ml_context=ml)


# ─────────────────── readiness advisory merge ───────────────────
def merge_readiness_advisories(
    candidate_input: SignalCandidateInput,
    readiness: Mapping[str, Any],
) -> SignalCandidateInput:
    """Return a NEW SignalCandidateInput with advisory_context (and PIT/price-
    adjustment ml provenance) populated from a readiness advisory dict. Metadata
    is passed through verbatim — the adapter does not interpret it."""
    rd = dict(readiness)
    advisory = dict(candidate_input.advisory_context)
    ml = dict(candidate_input.ml_context)

    _put(advisory, K.ADV_FOURH_ALIGNMENT, rd.get("fourh_bucket_alignment"))
    _put(advisory, K.ADV_ADJUSTED_PRICE_PIT_RISK, rd.get("adjusted_price_pit_risk"))
    _put(advisory, K.ADV_SHORT_SIDE_VALIDATED,
         rd.get("scanner_replica_short_side_validated"))

    _put(ml, K.ML_PRICE_ADJUSTMENT_MODE, rd.get("price_adjustment_mode"))
    _put(ml, K.ML_ALLOW_ADJUSTED_FOR_ML, rd.get("allow_adjusted_prices_for_ml"))
    if "production_thinness_status" in rd and \
            rd.get("production_thinness_status") is not None:
        ml[K.ML_PRODUCTION_THINNESS_STATUS] = rd["production_thinness_status"]

    return _replace_blocks(candidate_input, advisory_context=advisory,
                           ml_context=ml)


def _replace_blocks(ci: SignalCandidateInput, **overrides
                    ) -> SignalCandidateInput:
    """Build a NEW SignalCandidateInput, replacing only the named blocks. Never
    mutates the original (which is frozen anyway)."""
    return SignalCandidateInput(
        symbol=ci.symbol, side=ci.side,
        signal_timestamp_utc=ci.signal_timestamp_utc,
        timeframe_context=overrides.get("timeframe_context",
                                        dict(ci.timeframe_context)),
        scanner_context=overrides.get("scanner_context",
                                      dict(ci.scanner_context)),
        ml_context=overrides.get("ml_context", dict(ci.ml_context)),
        technical_context=overrides.get("technical_context",
                                        dict(ci.technical_context)),
        risk_preview=overrides.get("risk_preview", dict(ci.risk_preview)),
        regime_context=overrides.get("regime_context",
                                     dict(ci.regime_context)),
        liquidity_context=overrides.get("liquidity_context",
                                        dict(ci.liquidity_context)),
        volatility_context=overrides.get("volatility_context",
                                         dict(ci.volatility_context)),
        data_quality_context=overrides.get("data_quality_context",
                                           dict(ci.data_quality_context)),
        advisory_context=overrides.get("advisory_context",
                                       dict(ci.advisory_context)),
    )

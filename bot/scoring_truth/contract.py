"""M21.1 A01 — the scanner -> M19 truthful scoring contract (pure).

``build_truth`` turns a scanner signal plus the evidence actually available
(bar as-of record, session calendar, risk decision, price basis /
corporate-action evidence, model-registry state) into a ``TruthReport``.
``apply_truth`` then writes ONLY measured values into the M19 input and
REMOVES every gate key whose value is unknown, so M19 fails closed with
``missing_context_key`` instead of scoring on an assumption.

Nothing here can authorise execution: it produces descriptive data only,
imports no broker / risk / runtime module, and never sets a value that M19
would read as an approval without the matching evidence.

Risk Authority provenance: a ``RiskDecisionRef`` is only a POINTER (decision
id + the scope and notional the candidate would trade); it is never evidence.
The status is 'ok' / 'blocked' only from a ``PersistedRiskDecision`` -- the
row M14 itself wrote to ``risk_decisions`` -- read by the bridge through its
trusted read-only store, and only when that row matches this candidate
(trade_open, scope, symbol, side, leverage, approved notional covering the
intended notional, decided after the signal, before evaluation, unexpired).
A reference with no trusted record (caller attestation only), an id M14 never
recorded, or any mismatch is UNVERIFIED: the key is removed and M19 blocks.
"""
from __future__ import annotations

import dataclasses
import enum
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bot.scoring_truth.corporate_actions import (
    ADJUSTED_BASES, CorporateActionRecord, CorporateActionState,
    CorporateActionTruth, PriceBasis, classify_corporate_action,
)
from bot.scoring_truth.observations import (
    ObservationState, TimeframeObservation, classify_symbol, parse_utc,
)
from bot.scoring_truth.sessions import DEFAULT_SPECS, SessionCalendar, TimeframeSpec
from bot.signal_scoring import keys as K

MEASURED = "MEASURED"     # observed / computed from evidence
DECLARED = "DECLARED"     # a true statement about this pipeline (e.g. no calibration)
UNKNOWN = "UNKNOWN"       # not measurable from the evidence supplied -> key removed

_SCHEMA_REQUIRED_POSITIVE = ("entry_price", "stop_loss", "target_price", "atr")
_SCHEMA_OPTIONAL_NUMERIC = ("price", "rsi", "macd_hist", "bb_pos", "bb_width",
                            "vwap_dev", "vol_ratio", "ema20", "ema50",
                            "obv_slope", "pchg")
_SCORING_FEATURES = ("rsi", "macd_hist", "bb_pos", "vwap_dev", "vol_ratio")


@dataclass(frozen=True)
class TruthPolicy:
    required_timeframes: Tuple[str, ...] = ("1D", "4H", "1H", "15m")
    specs: Mapping[str, TimeframeSpec] = field(default_factory=lambda: dict(DEFAULT_SPECS))
    max_lag_minutes: float = 30.0
    # How the scanner evaluates bars: "in_progress" (today's scanner scores the
    # forming bar) or "completed" (finished bars only; see observations.py).
    bar_basis: str = "in_progress"
    count_incomplete_as_available: bool = False
    accepted_risk_scopes: Tuple[str, ...] = ("ibkr_paper", "ibkr_live",
                                             "etoro_paper", "etoro_real")
    max_risk_decision_age_minutes: float = 15.0
    max_leverage: int = 1

    def __post_init__(self):
        if self.bar_basis not in ("in_progress", "completed"):
            raise ValueError("unknown bar_basis %r" % (self.bar_basis,))


# ── schema ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SchemaCheck:
    ok: bool
    errors: Tuple[str, ...]
    missing_features: Tuple[str, ...]


def _finite(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def check_signal_schema(signal: Any) -> SchemaCheck:
    """Validate the scanner signal fields M19 scores from."""
    if not isinstance(signal, Mapping):
        return SchemaCheck(False, ("signal_not_a_mapping",), ())
    errs: List[str] = []
    sym = signal.get("symbol")
    if not isinstance(sym, str) or not sym.strip():
        errs.append("symbol")
    direction = signal.get("direction")
    if direction not in ("long", "short"):
        errs.append("direction")
    raw_ts = signal.get("timestamp")
    try:
        parse_utc(raw_ts)          # rejects naive / unparseable
        if isinstance(raw_ts, str):  # mirror the M19 adapter: UTC offset only
            off = datetime.fromisoformat(raw_ts.strip().replace("Z", "+00:00")).utcoffset()
            if off.total_seconds() != 0:
                errs.append("timestamp_not_utc")
    except ValueError:
        errs.append("timestamp")
    for k in _SCHEMA_REQUIRED_POSITIVE:
        v = signal.get(k)
        if not (_finite(v) and v > 0):
            errs.append(k)
    for k in _SCHEMA_OPTIONAL_NUMERIC:
        if k in signal and signal[k] is not None and not _finite(signal[k]):
            errs.append(k)
    rsi = signal.get("rsi")
    if _finite(rsi) and not (0 <= rsi <= 100):
        errs.append("rsi_range")
    for k in ("valid_count", "available_tfs"):
        v = signal.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 4):
            errs.append(k)
    e, s, t = (signal.get(k) for k in ("entry_price", "stop_loss", "target_price"))
    if all(_finite(x) for x in (e, s, t)):
        if direction == "long" and not (s < e < t):
            errs.append("levels_inconsistent_with_long")
        if direction == "short" and not (t < e < s):
            errs.append("levels_inconsistent_with_short")
    missing = tuple(k for k in _SCORING_FEATURES if signal.get(k) is None)
    return SchemaCheck(not errs, tuple(errs), missing)


# ── risk decision ───────────────────────────────────────────────────────────

class RiskAuthorityState(str, enum.Enum):
    OK = "OK"
    BLOCKED = "BLOCKED"
    NOT_EVALUATED = "NOT_EVALUATED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class RiskDecisionRef:
    """A POINTER to an M14 decision for this candidate -- never evidence.
    ``broker_scope`` and ``notional_usd`` are what the candidate would trade;
    the persisted decision must cover them."""

    decision_id: str
    broker_scope: str
    notional_usd: float


@dataclass(frozen=True)
class PersistedRiskDecision:
    """One ``risk_decisions`` row exactly as M14's audit surface wrote it,
    produced only by a trusted read-only store (see
    ``tools/signal_scoring/risk_decision_store.py``)."""

    decision_id: str
    taken_at_utc: str
    broker_scope: str
    requested_action: str
    result: str                       # 'allow' | 'block' | 'downgrade_then_block'
    authority_after: str
    audit_source: str                 # risk_decisions.source
    symbol: Optional[str]
    side: Optional[str]
    amount_usd: Optional[float]
    leverage: Optional[int]
    store: str                        # which trusted store read it


@dataclass(frozen=True)
class RiskAuthorityTruth:
    state: RiskAuthorityState
    reason: str
    store: Optional[str] = None
    decision_id: Optional[str] = None


def evaluate_risk_decision(ref: Any, *, record: Any, symbol: str, side: Optional[str],
                           signal_ts: Optional[datetime],
                           evaluated_at: Optional[datetime],
                           policy: TruthPolicy,
                           lookup_note: Optional[str] = None) -> RiskAuthorityTruth:
    if ref is None:
        return RiskAuthorityTruth(RiskAuthorityState.NOT_EVALUATED,
                                  "no_risk_decision_for_candidate")
    if not isinstance(ref, RiskDecisionRef):
        return RiskAuthorityTruth(RiskAuthorityState.UNVERIFIED,
                                  "not_a_risk_decision_reference")
    did = str(ref.decision_id)

    def u(reason, store=None):
        return RiskAuthorityTruth(RiskAuthorityState.UNVERIFIED, reason, store, did)
    if record is None:
        return u(lookup_note or "not_verified_against_m14_audit")
    if not isinstance(record, PersistedRiskDecision):
        return u("untrusted_decision_record")
    st = record.store
    if record.decision_id != did or not did.strip():
        return u("record_for_other_decision", st)
    if record.requested_action != "trade_open":
        return u("decision_not_trade_open:%s" % record.requested_action, st)
    if (record.broker_scope != ref.broker_scope
            or record.broker_scope not in policy.accepted_risk_scopes):
        return u("decision_scope_mismatch", st)
    if record.symbol is None or record.side is None:
        return u("decision_payload_unreadable", st)
    if record.symbol.strip().upper() != str(symbol).strip().upper():
        return u("decision_for_other_symbol", st)
    if record.side.strip().lower() != str(side or "").strip().lower():
        return u("decision_for_other_side", st)
    if record.result in ("block", "downgrade_then_block"):
        # a verified, matching M14 block for this candidate
        return RiskAuthorityTruth(RiskAuthorityState.BLOCKED, "m14_decision_blocked", st, did)
    if record.result != "allow":
        return u("decision_result_unknown", st)
    want = ref.notional_usd
    if (isinstance(want, bool) or not isinstance(want, (int, float))
            or not math.isfinite(want) or want <= 0):
        return u("intended_notional_invalid", st)
    got = record.amount_usd
    if (isinstance(got, bool) or not isinstance(got, (int, float))
            or not math.isfinite(got) or got <= 0):
        return u("approved_amount_invalid", st)
    if want > got + 1e-9:
        return u("intended_notional_exceeds_approved", st)
    if record.leverage is None or record.leverage > policy.max_leverage:
        return u("decision_leverage_not_allowed", st)
    if evaluated_at is None or signal_ts is None:
        return u("no_evaluation_or_signal_time", st)
    try:
        at = parse_utc(record.taken_at_utc)
    except ValueError:
        return u("decision_time_invalid", st)
    if at > evaluated_at:
        return u("decision_after_evaluation", st)
    if at < signal_ts:
        return u("decision_predates_signal", st)
    if evaluated_at - at > timedelta(minutes=policy.max_risk_decision_age_minutes):
        return u("decision_expired", st)
    return RiskAuthorityTruth(RiskAuthorityState.OK, "verified_m14_approval", st, did)


# ── report ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldTruth:
    block: str
    key: str
    status: str               # MEASURED | DECLARED | UNKNOWN
    value: Any = None
    reason: str = ""


@dataclass(frozen=True)
class TruthReport:
    symbol: str
    side: Optional[str]
    evaluated_at_utc: Optional[str]
    signal_timestamp_utc: Optional[str]
    schema: SchemaCheck
    observations: Optional[Tuple[TimeframeObservation, ...]]
    risk: RiskAuthorityTruth
    corporate_action: CorporateActionTruth
    fields: Tuple[FieldTruth, ...]

    @property
    def unknown_fields(self) -> Tuple[str, ...]:
        return tuple("%s.%s" % (f.block, f.key) for f in self.fields
                     if f.status == UNKNOWN)

    def observation_states(self) -> Dict[str, str]:
        return {o.timeframe: o.state.value for o in (self.observations or ())}

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side,
            "evaluated_at_utc": self.evaluated_at_utc,
            "signal_timestamp_utc": self.signal_timestamp_utc,
            "schema": {"ok": self.schema.ok, "errors": list(self.schema.errors),
                       "missing_features": list(self.schema.missing_features)},
            "observations": (None if self.observations is None
                             else [o.to_dict() for o in self.observations]),
            "risk": {"state": self.risk.state.value, "reason": self.risk.reason,
                     "store": self.risk.store, "decision_id": self.risk.decision_id},
            "corporate_action": {"state": self.corporate_action.state.value,
                                 "price_basis": self.corporate_action.price_basis.value,
                                 "reason": self.corporate_action.reason},
            "fields": [dict(f.__dict__) for f in self.fields],
            "unknown_fields": list(self.unknown_fields),
        }


def build_truth(signal: Mapping[str, Any], *,
                evaluated_at: Any = None,
                bar_asof: Optional[Mapping[str, Any]] = None,
                calendar: Optional[SessionCalendar] = None,
                risk_decision: Any = None,
                risk_decision_record: Any = None,
                risk_lookup_note: Optional[str] = None,
                price_basis: Any = None,
                detector_suspected: Optional[bool] = None,
                corporate_action_record: Optional[CorporateActionRecord] = None,
                lookback_start: Any = None,
                model_readiness_passed: bool = False,
                production_thinness_status: Optional[str] = None,
                policy: Optional[TruthPolicy] = None) -> TruthReport:
    policy = policy or TruthPolicy()
    sig = signal if isinstance(signal, Mapping) else {}
    schema = check_signal_schema(signal)
    symbol = str(sig.get("symbol") or "")
    side = sig.get("direction") if sig.get("direction") in ("long", "short") else None
    try:
        ev = parse_utc(evaluated_at) if evaluated_at is not None else None
    except ValueError:
        ev = None
    try:
        sts = parse_utc(sig.get("timestamp"))
    except ValueError:
        sts = None
    F: List[FieldTruth] = []

    # data quality: observations, freshness, schema
    obs = None
    if ev is not None:
        obs = classify_symbol(bar_asof, timeframes=policy.required_timeframes,
                              evaluated_at=ev, calendar=calendar, specs=policy.specs,
                              max_lag_minutes=policy.max_lag_minutes,
                              basis=policy.bar_basis)
    states = [o.state for o in (obs or ())]
    dq = "data_quality_context"
    if not schema.ok or ObservationState.INVALID in states:
        bad = list(schema.errors) + ["%s:%s" % (o.timeframe, o.reason)
                                     for o in (obs or ()) if o.state == ObservationState.INVALID]
        F.append(FieldTruth(dq, K.DQ_SCHEMA_MATCH, MEASURED, False, "; ".join(bad)))
    elif obs is None:
        F.append(FieldTruth(dq, K.DQ_SCHEMA_MATCH, UNKNOWN, None,
                            "signal schema ok; no bar as-of record / evaluation time"))
    else:
        F.append(FieldTruth(dq, K.DQ_SCHEMA_MATCH, MEASURED, True,
                            "signal schema ok; bar records parse"))
    F.append(FieldTruth(dq, K.DQ_MISSING_FEATURE_COUNT, MEASURED,
                        len(schema.missing_features),
                        ",".join(schema.missing_features)))
    timed = [o for o in (obs or ()) if o.state in (
        ObservationState.VALID, ObservationState.INCOMPLETE, ObservationState.STALE)]
    if obs is None:
        why = "no evaluation time" if ev is None else "no bar as-of record"
        F.append(FieldTruth(dq, K.DQ_STALE_DATA_FLAG, UNKNOWN, None, why))
        F.append(FieldTruth(dq, K.DQ_DATA_FRESHNESS_MINUTES, UNKNOWN, None, why))
    elif ObservationState.UNVERIFIED in states or not timed:
        why = ("calendar cannot verify bar timing"
               if ObservationState.UNVERIFIED in states else "no timed observation")
        F.append(FieldTruth(dq, K.DQ_STALE_DATA_FLAG, UNKNOWN, None, why))
        F.append(FieldTruth(dq, K.DQ_DATA_FRESHNESS_MINUTES, UNKNOWN, None, why))
    else:
        lag = max(o.lag_minutes for o in timed)
        stale = [o.timeframe for o in timed if o.state == ObservationState.STALE]
        F.append(FieldTruth(dq, K.DQ_STALE_DATA_FLAG, MEASURED, bool(stale),
                            "stale: %s" % (",".join(stale) or "none")))
        F.append(FieldTruth(dq, K.DQ_DATA_FRESHNESS_MINUTES, MEASURED, lag,
                            "max lag behind newest available market data (min)"
                            if policy.bar_basis == "in_progress" else
                            "max lag behind the newest completed bar (min)"))

    # timeframes available for THIS symbol
    if obs is None:
        F.append(FieldTruth("timeframe_context", K.TF_AVAILABLE, UNKNOWN, None,
                            "no bar as-of record / evaluation time"))
    else:
        ok_states = {ObservationState.VALID}
        if policy.count_incomplete_as_available:
            ok_states.add(ObservationState.INCOMPLETE)
        avail = [o.timeframe for o in obs if o.state in ok_states]
        F.append(FieldTruth("timeframe_context", K.TF_AVAILABLE, MEASURED, avail,
                            "per-symbol %s observations" % "/".join(sorted(s.value for s in ok_states))))

    # risk authority
    risk = evaluate_risk_decision(risk_decision, record=risk_decision_record,
                                  symbol=symbol, side=side, signal_ts=sts,
                                  evaluated_at=ev, policy=policy,
                                  lookup_note=risk_lookup_note)
    rp = "risk_preview"
    if risk.state == RiskAuthorityState.OK:
        F.append(FieldTruth(rp, K.RISK_AUTHORITY_STATUS, MEASURED, "ok", risk.reason))
    elif risk.state == RiskAuthorityState.BLOCKED:
        F.append(FieldTruth(rp, K.RISK_AUTHORITY_STATUS, MEASURED, "blocked", risk.reason))
    else:
        F.append(FieldTruth(rp, K.RISK_AUTHORITY_STATUS, UNKNOWN, None,
                            "%s: %s" % (risk.state.value, risk.reason)))

    # price basis / corporate actions
    ca = classify_corporate_action(symbol, price_basis, detector_suspected=detector_suspected,
                                   record=corporate_action_record,
                                   lookback_start=lookback_start,
                                   evaluated_at=ev if ev is not None else sts)
    ml, adv = "ml_context", "advisory_context"
    if ca.price_basis == PriceBasis.RAW:
        F.append(FieldTruth(ml, K.ML_PRICE_ADJUSTMENT_MODE, MEASURED, "raw", ca.reason))
    elif ca.price_basis in ADJUSTED_BASES:
        F.append(FieldTruth(ml, K.ML_PRICE_ADJUSTMENT_MODE, MEASURED, "adjusted",
                            ca.price_basis.value))
    else:
        F.append(FieldTruth(ml, K.ML_PRICE_ADJUSTMENT_MODE, UNKNOWN, None,
                            "price basis unknown"))
    F.append(FieldTruth(ml, K.ML_ALLOW_ADJUSTED_FOR_ML, DECLARED, False,
                        "never granted by the scoring bridge"))
    if ca.state == CorporateActionState.VERIFIED_CLEAN:
        F.append(FieldTruth(adv, K.ADV_ADJUSTED_PRICE_PIT_RISK, MEASURED, False, ca.reason))
    elif ca.state in (CorporateActionState.ADJUSTED, CorporateActionState.SUSPECTED):
        F.append(FieldTruth(adv, K.ADV_ADJUSTED_PRICE_PIT_RISK, MEASURED, True, ca.reason))
    else:
        F.append(FieldTruth(adv, K.ADV_ADJUSTED_PRICE_PIT_RISK, UNKNOWN, None, ca.reason))

    # ML state that is genuinely known for this pipeline
    F.append(FieldTruth(ml, K.ML_CALIBRATION_APPLIED, DECLARED, False, "no calibration exists"))
    F.append(FieldTruth(ml, K.ML_PRED_CALIBRATED, DECLARED, None, "no calibrated probability"))
    F.append(FieldTruth(ml, K.ML_MODEL_READINESS_PASSED, DECLARED,
                        model_readiness_passed is True, "registry state from caller"))
    if production_thinness_status in K.ALLOWED_PRODUCTION_THINNESS_STATUS:
        F.append(FieldTruth(ml, K.ML_PRODUCTION_THINNESS_STATUS, MEASURED,
                            production_thinness_status, "supplied registry check"))
    else:
        F.append(FieldTruth(ml, K.ML_PRODUCTION_THINNESS_STATUS, UNKNOWN, None,
                            "no production-thinness check supplied"))

    return TruthReport(symbol=symbol, side=side,
                       evaluated_at_utc=ev.isoformat() if ev else None,
                       signal_timestamp_utc=sts.isoformat() if sts else None,
                       schema=schema, observations=obs, risk=risk,
                       corporate_action=ca, fields=tuple(F))


_BLOCK_ATTR = {
    "ml_context": "ml_context",
    "data_quality_context": "data_quality_context",
    "advisory_context": "advisory_context",
    "timeframe_context": "timeframe_context",
    "risk_preview": "risk_preview",
}


def apply_truth(candidate_input, truth: TruthReport):
    """Return a copy of an M19 SignalCandidateInput with every truth field
    either set to its measured/declared value or removed (UNKNOWN). Other keys
    (technical, scanner, liquidity, risk levels) are left exactly as the
    adapter produced them."""
    blocks = {}
    for f in truth.fields:
        attr = _BLOCK_ATTR[f.block]
        if attr not in blocks:
            cur = getattr(candidate_input, attr)
            blocks[attr] = dict(cur) if isinstance(cur, dict) else {}
        if f.status == UNKNOWN:
            blocks[attr].pop(f.key, None)
        else:
            blocks[attr][f.key] = f.value
    return dataclasses.replace(candidate_input, **blocks)

"""M19.A schema — enums + versioned input/output contracts.

Frozen dataclasses + str-enums (stdlib only). M19.A defines structure and
round-trip + light validation ONLY — no scoring logic, no gates, no math.

Context blocks are kept as plain dicts in M19.A so later phases (B/C/F) can
populate/validate their internals without churning the top-level contract.
The top-level contracts are versioned via SCHEMA_VERSION_*.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

SCHEMA_VERSION_INPUT = "m19_signal_candidate_input_v1"
SCHEMA_VERSION_OUTPUT = "m19_scored_candidate_v1"


# ─────────────────────────── enums ───────────────────────────
class ScoringProfile(str, Enum):
    STRICT = "strict"
    RESEARCH = "research"


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class DecisionBucket(str, Enum):
    BLOCKED = "BLOCKED"
    REJECT = "REJECT"
    WATCH = "WATCH"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    ELIGIBLE = "ELIGIBLE"
    HIGH_CONVICTION = "HIGH_CONVICTION"


class ConfidenceBucket(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    MEDIUM_HIGH = "MEDIUM_HIGH"
    HIGH = "HIGH"


class PenaltySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    MAJOR = "major"
    BLOCKING = "blocking"


def _coerce_side(value: Any) -> SignalSide:
    if isinstance(value, SignalSide):
        return value
    if isinstance(value, str):
        try:
            return SignalSide(value.upper())
        except ValueError:
            pass
    raise ValueError(f"unknown signal side: {value!r}")


def _validate_timestamp(ts: Any) -> str:
    """Require an ISO-8601 UTC timestamp string. Returns it unchanged if
    valid; raises ValueError otherwise. No wall-clock is used."""
    if not isinstance(ts, str) or not ts:
        raise ValueError("signal_timestamp_utc must be a non-empty ISO string")
    norm = ts.replace("Z", "+00:00")
    try:
        _dt.datetime.fromisoformat(norm)
    except ValueError as e:
        raise ValueError(f"bad signal_timestamp_utc: {ts!r} ({e})")
    return ts


# ─────────────────────── input contract ───────────────────────
@dataclass(frozen=True)
class SignalCandidateInput:
    """Versioned input to the M19 scoring engine. Context blocks are dicts in
    M19.A (their internals are validated in later phases). The engine fetches
    nothing — every field is supplied by the caller (fetch-free)."""

    symbol: str
    side: SignalSide
    signal_timestamp_utc: str
    schema_version: str = SCHEMA_VERSION_INPUT

    timeframe_context:   Dict[str, Any] = field(default_factory=dict)
    scanner_context:     Dict[str, Any] = field(default_factory=dict)
    ml_context:          Dict[str, Any] = field(default_factory=dict)
    technical_context:   Dict[str, Any] = field(default_factory=dict)
    risk_preview:        Dict[str, Any] = field(default_factory=dict)
    regime_context:      Dict[str, Any] = field(default_factory=dict)
    liquidity_context:   Dict[str, Any] = field(default_factory=dict)
    volatility_context:  Dict[str, Any] = field(default_factory=dict)
    data_quality_context: Dict[str, Any] = field(default_factory=dict)
    advisory_context:    Dict[str, Any] = field(default_factory=dict)
    provenance_inputs:   Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.schema_version:
            raise ValueError("missing schema_version")
        if self.schema_version != SCHEMA_VERSION_INPUT:
            raise ValueError(
                f"input schema_version mismatch: got {self.schema_version!r}, "
                f"expected {SCHEMA_VERSION_INPUT!r}")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        # normalise side via object.__setattr__ (frozen dataclass)
        object.__setattr__(self, "side", _coerce_side(self.side))
        _validate_timestamp(self.signal_timestamp_utc)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignalCandidateInput":
        if "schema_version" not in d:
            raise ValueError("missing schema_version")
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)


# ─────────────────────── output contract ───────────────────────
@dataclass(frozen=True)
class ScoredSignalCandidate:
    """Versioned output. In M19.A this is a STRUCTURAL container only —
    fields exist and round-trip, but no real scoring populates them yet.
    execution_eligible defaults to False and (per the M19 short-side rule)
    must never be True for a SHORT candidate."""

    symbol: str
    side: SignalSide
    signal_timestamp_utc: str
    candidate_id: str
    profile: ScoringProfile = ScoringProfile.STRICT
    schema_version: str = SCHEMA_VERSION_OUTPUT

    decision_bucket: DecisionBucket = DecisionBucket.REJECT
    execution_eligible: bool = False

    final_score: float = 0.0
    final_score_100: float = 0.0
    confidence_bucket: ConfidenceBucket = ConfidenceBucket.LOW

    hard_gate_passed: bool = False
    hard_gate_failures: List[str] = field(default_factory=list)
    blocked_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)

    component_scores: Dict[str, float] = field(default_factory=dict)
    multipliers: Dict[str, float] = field(default_factory=dict)
    penalties: Dict[str, Any] = field(default_factory=dict)
    ml_context: Dict[str, Any] = field(default_factory=dict)
    risk_preview: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.schema_version:
            raise ValueError("missing schema_version")
        if self.schema_version != SCHEMA_VERSION_OUTPUT:
            raise ValueError(
                f"output schema_version mismatch: got {self.schema_version!r}, "
                f"expected {SCHEMA_VERSION_OUTPUT!r}")
        object.__setattr__(self, "side", _coerce_side(self.side))
        if not isinstance(self.profile, ScoringProfile):
            object.__setattr__(self, "profile", ScoringProfile(self.profile))
        if not isinstance(self.decision_bucket, DecisionBucket):
            object.__setattr__(self, "decision_bucket",
                               DecisionBucket(self.decision_bucket))
        if not isinstance(self.confidence_bucket, ConfidenceBucket):
            object.__setattr__(self, "confidence_bucket",
                               ConfidenceBucket(self.confidence_bucket))
        # Hard structural invariant (M19 short-side rule): a SHORT candidate
        # can never be execution-eligible, and can never be ELIGIBLE/
        # HIGH_CONVICTION. Enforced at the contract boundary.
        if self.side == SignalSide.SHORT:
            if self.execution_eligible:
                raise ValueError(
                    "SHORT candidate cannot be execution_eligible in M19")
            if self.decision_bucket in (DecisionBucket.ELIGIBLE,
                                        DecisionBucket.HIGH_CONVICTION):
                raise ValueError(
                    "SHORT candidate cannot be ELIGIBLE/HIGH_CONVICTION in M19")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["profile"] = self.profile.value
        d["decision_bucket"] = self.decision_bucket.value
        d["confidence_bucket"] = self.confidence_bucket.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScoredSignalCandidate":
        if "schema_version" not in d:
            raise ValueError("missing schema_version")
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

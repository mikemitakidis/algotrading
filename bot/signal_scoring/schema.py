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
    """Require an ISO-8601 *UTC* timestamp string. Accepts a trailing 'Z' or a
    '+00:00' offset only. Rejects naive (no timezone) and non-UTC offsets.
    Returns the string unchanged if valid; raises ValueError otherwise. No
    wall-clock is used."""
    if not isinstance(ts, str) or not ts:
        raise ValueError("signal_timestamp_utc must be a non-empty ISO string")
    norm = ts.replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(norm)
    except ValueError as e:
        raise ValueError(f"bad signal_timestamp_utc: {ts!r} ({e})")
    if parsed.tzinfo is None:
        raise ValueError(
            f"signal_timestamp_utc must be timezone-aware UTC, got naive: {ts!r}")
    if parsed.utcoffset() != _dt.timedelta(0):
        raise ValueError(
            f"signal_timestamp_utc must be UTC (+00:00 or Z), got offset "
            f"{parsed.utcoffset()}: {ts!r}")
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
        unknown = set(d) - known
        if unknown:
            raise ValueError(
                f"unknown top-level input field(s): {sorted(unknown)}")
        return cls(**d)


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
        # Score range invariants (M19 uses a 0-100 scale).
        for nm, v in (("final_score", self.final_score),
                      ("final_score_100", self.final_score_100)):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{nm} must be numeric, got {type(v).__name__}")
            if not (0.0 <= float(v) <= 100.0):
                raise ValueError(f"{nm} out of range [0,100]: {v}")

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
        unknown = set(d) - known
        if unknown:
            raise ValueError(
                f"unknown top-level output field(s): {sorted(unknown)}")
        return cls(**d)


# ─────────────────── M19.B hard-gate result contract ───────────────────
class GateOutcome(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True)
class GateFailure:
    """One failing hard gate. `outcome` is BLOCK or MANUAL_REVIEW (never PASS).
    detail is human-readable and must not contain secrets/PII."""
    gate_name: str
    outcome: GateOutcome
    reason_code: str
    detail: str = ""
    severity: PenaltySeverity = PenaltySeverity.BLOCKING

    def __post_init__(self):
        if not isinstance(self.outcome, GateOutcome):
            object.__setattr__(self, "outcome", GateOutcome(self.outcome))
        if self.outcome == GateOutcome.PASS:
            raise ValueError("GateFailure.outcome cannot be PASS")
        if not isinstance(self.severity, PenaltySeverity):
            object.__setattr__(self, "severity",
                               PenaltySeverity(self.severity))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GateFailure":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(
                f"unknown GateFailure field(s): {sorted(unknown)}")
        return cls(**d)


@dataclass(frozen=True)
class GateResult:
    """Outcome of the M19.B hard-gate engine. Standalone — does NOT populate
    ScoredSignalCandidate (that assembly is a later phase).

    passed == True iff there are no BLOCK and no MANUAL_REVIEW failures.
    decision_bucket is BLOCKED if any BLOCK failure exists (precedence), else
    MANUAL_REVIEW if any MANUAL_REVIEW failure exists, else None (a passing
    gate result leaves the score-based bucket to a later phase)."""
    profile: ScoringProfile
    passed: bool
    decision_bucket: Optional[DecisionBucket] = None
    failures: List[GateFailure] = field(default_factory=list)
    block_reasons: List[str] = field(default_factory=list)
    manual_review_reasons: List[str] = field(default_factory=list)
    evaluated_gates: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.profile, ScoringProfile):
            object.__setattr__(self, "profile", ScoringProfile(self.profile))
        if self.decision_bucket is not None and not isinstance(
                self.decision_bucket, DecisionBucket):
            object.__setattr__(self, "decision_bucket",
                               DecisionBucket(self.decision_bucket))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile.value,
            "passed": self.passed,
            "decision_bucket": (self.decision_bucket.value
                                if self.decision_bucket is not None else None),
            "failures": [f.to_dict() for f in self.failures],
            "block_reasons": list(self.block_reasons),
            "manual_review_reasons": list(self.manual_review_reasons),
            "evaluated_gates": list(self.evaluated_gates),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GateResult":
        known = {"profile", "passed", "decision_bucket", "failures",
                 "block_reasons", "manual_review_reasons", "evaluated_gates"}
        unknown = set(d) - known
        if unknown:
            raise ValueError(
                f"unknown GateResult field(s): {sorted(unknown)}")
        failures = [GateFailure.from_dict(f) for f in d.get("failures", [])]
        return cls(
            profile=d["profile"],
            passed=d["passed"],
            decision_bucket=d.get("decision_bucket"),
            failures=failures,
            block_reasons=list(d.get("block_reasons", [])),
            manual_review_reasons=list(d.get("manual_review_reasons", [])),
            evaluated_gates=list(d.get("evaluated_gates", [])),
        )

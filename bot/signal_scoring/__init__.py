"""M19 — Gated Anchored Composite Signal Scoring Engine.

This package is a deterministic, READ-ONLY, explainable decision-quality
layer that sits between signal generation (M17 scanner / M18 ML) and FUTURE
paper trading (M20). It scores signal candidates; it does NOT trade, call
brokers, mutate signals.db, write data/ml, train models, or weaken any
M17/M18 safety gate.

M19.A scope (this commit): stable contract foundation ONLY —
  * schema.py      — enums + versioned input/output contracts
  * config.py      — SignalScoringConfig + validated defaults + profiles
  * provenance.py  — deterministic canonical_json / hashing / candidate_id

Later phases (NOT in M19.A): gates.py, scoring.py, penalties.py, explain.py,
io.py, audit.py, adapters.py.

Safety invariant: nothing in this package may import broker/live/main/
dashboard/network modules. A static hygiene test enforces this.
"""

from bot.signal_scoring.schema import (  # noqa: F401
    SCHEMA_VERSION_INPUT,
    SCHEMA_VERSION_OUTPUT,
    ScoringProfile,
    SignalSide,
    DecisionBucket,
    ConfidenceBucket,
    PenaltySeverity,
    SignalCandidateInput,
    ScoredSignalCandidate,
    GateOutcome,
    GateFailure,
    GateResult,
    ComponentScore,
    make_component_score,
    PenaltyItem,
    PenaltyResult,
    MultiplierItem,
    MultiplierResult,
)
from bot.signal_scoring.config import (  # noqa: F401
    SignalScoringConfig,
    default_config,
    DEFAULT_PROFILE,
)
from bot.signal_scoring import provenance  # noqa: F401
from bot.signal_scoring import keys  # noqa: F401
from bot.signal_scoring.gates import (  # noqa: F401
    evaluate_hard_gates,
    GATE_ORDER,
)
from bot.signal_scoring.components import (  # noqa: F401
    COMPONENT_NAMES,
    COMPONENT_SCORERS,
    score_component,
    score_all_components,
)
from bot.signal_scoring.penalties import (  # noqa: F401
    evaluate_penalties,
    evaluate_multipliers,
    PENALTY_NAMES,
    MULTIPLIER_NAMES,
)

__all__ = [
    "SCHEMA_VERSION_INPUT",
    "SCHEMA_VERSION_OUTPUT",
    "ScoringProfile",
    "SignalSide",
    "DecisionBucket",
    "ConfidenceBucket",
    "PenaltySeverity",
    "SignalCandidateInput",
    "ScoredSignalCandidate",
    "GateOutcome",
    "GateFailure",
    "GateResult",
    "ComponentScore",
    "make_component_score",
    "PenaltyItem",
    "PenaltyResult",
    "MultiplierItem",
    "MultiplierResult",
    "SignalScoringConfig",
    "default_config",
    "DEFAULT_PROFILE",
    "provenance",
    "keys",
    "evaluate_hard_gates",
    "GATE_ORDER",
    "COMPONENT_NAMES",
    "COMPONENT_SCORERS",
    "score_component",
    "score_all_components",
    "evaluate_penalties",
    "evaluate_multipliers",
    "PENALTY_NAMES",
    "MULTIPLIER_NAMES",
]

M19_PHASE = "M19.D"

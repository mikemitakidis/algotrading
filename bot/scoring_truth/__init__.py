"""M21.1 A01 — truthful scanner -> M19 scoring contract (pure, offline).

Measures what the scoring inputs claim instead of assuming it: per-symbol,
per-timeframe bar close and freshness against an exchange session calendar,
schema validity, Risk Authority status from an actual decision, and
conservative price-basis / corporate-action state. Unknown values are reported
as UNKNOWN and removed from the M19 input, so M19 fails closed.

Pure: no I/O, no wall clock, no broker / risk / runtime imports. Nothing in
this package can authorise or trigger execution.
"""
from bot.scoring_truth.sessions import (  # noqa: F401
    BarOutOfSession, CalendarError, DEFAULT_SPECS, SessionCalendar,
    TimeframeSpec, bar_timing,
)
from bot.scoring_truth.observations import (  # noqa: F401
    ObservationState, TimeframeObservation, classify_observation,
    classify_symbol, parse_utc,
)
from bot.scoring_truth.corporate_actions import (  # noqa: F401
    CorporateActionRecord, CorporateActionState, CorporateActionTruth,
    PriceBasis, PROVIDER_PRICE_BASIS, classify_corporate_action,
    provider_price_basis, suspect_split,
)
from bot.scoring_truth.contract import (  # noqa: F401
    DECLARED, MEASURED, UNKNOWN, FieldTruth, RiskAuthorityState,
    RiskAuthorityTruth, RiskDecisionRef, SchemaCheck, TruthPolicy,
    TruthReport, apply_truth, build_truth, check_signal_schema,
    evaluate_risk_decision,
)

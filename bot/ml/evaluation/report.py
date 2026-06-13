"""bot.ml.evaluation.report — dataclasses for M18.A.7 evaluation
outputs.

Three report types:

  EvaluationReport
    One model trained on one dataset. Carries full cohort, split,
    embargo/purge, and metrics provenance so it can be serialised
    standalone for the M18.A.8 registry.

  BaselineComparisonReport
    Aggregate-level comparison of N models trained on the SAME
    cohort (same dataset_anchor_set AND same dataset_id). Row-paired
    comparisons are valid here because all models saw the same rows.

  CrossCohortComparisonReport
    Side-by-side aggregate comparison of two models trained on
    DIFFERENT cohorts (Model A vs Model B). Explicitly NOT row-
    paired. The dataclass carries a built-in disclaimer field so
    downstream consumers can't accidentally treat it as paired.

Schema version is included in each. Bumping `schema_version`
requires a coordinated update to the M18.A.8 registry layer.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional


EVALUATION_REPORT_SCHEMA_VERSION             = 2
BASELINE_COMPARISON_REPORT_SCHEMA_VERSION    = 2
CROSS_COHORT_COMPARISON_REPORT_SCHEMA_VERSION = 1

CROSS_COHORT_DISCLAIMER = (
    "Models compared here were trained on DIFFERENT cohorts "
    "(different anchor_set, different dataset_id). Metrics are "
    "aggregate-level comparisons over each model's own cohort; "
    "no row-paired equivalence is implied or computable. "
    "Treat side-by-side numbers as TWO INDEPENDENT EVALUATIONS, "
    "not as a paired difference of the same observations.")


@dataclass
class EvaluationReport:
    """Full evaluation of one model against the dataset it was
    trained on.

    Provenance fields are intentionally redundant with TrainOutputs
    and AssemblerResult.manifest so this report can stand alone
    when serialised to the registry in M18.A.8.
    """
    schema_version: int

    # Identity / provenance
    model_type: str
    train_mode: str
    target_label_id: str
    target_label_class: str
    dataset_id: str
    dataset_hash_sha256: str
    dataset_anchor_set: str

    # Cohort accounting (from manifest)
    cohort: Dict[str, Any]              # symbol, anchor_set,
                                          # anchor_count_*, fixture flags

    # Split provenance
    split: Dict[str, Any]                # train/val/test fractions,
                                          # embargo_bars, embargo_trading_days,
                                          # purge_applied flags

    # Sample sizes (echoed from TrainOutputs)
    n_train: int
    n_val: int
    n_test: int

    # Timestamp range per split — first/last anchor timestamp.
    # Empty splits → both first/last = None.
    split_timestamp_ranges: Dict[str, Dict[str, Optional[str]]]

    # Per-split ML metrics (echoed from TrainOutputs.metrics_*)
    ml_metrics: Dict[str, Dict[str, float]]

    # v2 Gap 1 — extended ML metrics incl. PR-AUC (PRIMARY M18 metric),
    # log_loss (clipped), precision/recall/F1 @0.5, confusion matrix
    ml_metrics_extended: Dict[str, Dict[str, Any]]

    # v2 Gap 2 — locked threshold ladder
    # [0.30, 0.40, 0.50, 0.60, 0.65, 0.70, 0.80]
    threshold_metrics: Dict[str, Dict[str, Any]]

    # Per-split calibration diagnostics
    calibration: Dict[str, Dict[str, Any]]

    # Per-split trading-style metrics (v2 Gap 3 — extended to the
    # 23-key contract incl. EV after costs, profit factor,
    # precision_at_k, equity_curve_metrics unavailable block)
    trading_metrics: Dict[str, Dict[str, Any]]

    # v2 Gap 4 — PSI drift diagnostics {train_to_val, train_to_test}
    drift: Dict[str, Dict[str, Any]]

    # v2 Gap 5 — permutation importance (single block; deterministic)
    permutation_importance: Dict[str, Any]

    # v2 Gap 6 — segment/regime breakdowns per split
    # (per_symbol / per_year / volatility_regime / market_regime)
    breakdowns: Dict[str, Dict[str, Any]]

    # Promotion gate echo (verbatim from TrainOutputs)
    fixture_only: bool
    promotion_eligible: bool
    promotion_blocked_reasons: List[str]

    # Determinism / provenance
    seed: int
    library_versions: Dict[str, str]
    generated_at_utc: str

    # M18.B.3 — real fitted isotonic calibration (fit on val, applied to
    # test). Backward-compatible (default_factory) so older reports that
    # predate B.3 still construct. Distinct from the diagnostic-only
    # `calibration` field above (reliability curve / ECE / MCE).
    isotonic_calibration: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineComparisonReport:
    """Aggregate comparison of N models on the SAME cohort.

    The constructor enforces cohort consistency: all input
    EvaluationReports must share dataset_id and dataset_anchor_set.
    """
    schema_version: int
    cohort_dataset_id: str
    cohort_anchor_set: str
    model_reports: List[Dict[str, Any]]      # to_dict() of each
    primary_split: str                         # 'val' by default
    primary_metric: str                        # v2: 'pr_auc' default
    primary_baseline_model_type: str           # v2: 'B0_majority'
    secondary_baseline_model_type: Optional[str]
        # v2: 'B1_scanner_replica' default; None skips B1 deltas
    per_metric: Dict[str, Dict[str, float]]   # {'pr_auc': {'B0':0.44,'B2':0.49}}
    baseline_beats: Dict[str, bool]
        # v2: BOTH _beats_B0_majority AND _beats_B1_scanner_replica
        # keys for each non-baseline model (4 entries for 3 models)
    deltas_vs_primary_baseline: Dict[str, Dict[str, float]]
        # v2: {model_type: {metric: delta_vs_B0}}; sign convention:
        # positive = candidate better, incl. LOWER_IS_BETTER metrics
        # (brier/log_loss/mse/mae/ECE/MCE) where delta = baseline - candidate
    deltas_vs_secondary_baseline: Dict[str, Dict[str, float]]
        # v2: {model_type: {metric: delta_vs_B1}}; empty when secondary None
    generated_at_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CrossCohortComparisonReport:
    """Side-by-side comparison of one Model A and one Model B
    EvaluationReport. EXPLICITLY non-row-paired."""
    schema_version: int
    disclaimer: str
    a_report: Dict[str, Any]                  # EvaluationReport.to_dict()
    b_report: Dict[str, Any]
    primary_split: str
    aggregate_metric_values: Dict[str, Dict[str, float]]
        # {'roc_auc': {'model_a_meta_label': 0.6,
        #               'model_b_candidate_quality': 0.65}}
    generated_at_utc: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

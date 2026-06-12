"""bot.ml.evaluation — M18.A.7 evaluation report generation.

Public surface:
  EvaluationReport
  BaselineComparisonReport
  CrossCohortComparisonReport
  CROSS_COHORT_DISCLAIMER

  evaluate_model(train_outputs, assembler_result) → EvaluationReport
  compare_baselines(reports, ...)                  → BaselineComparisonReport
  compare_across_cohorts(a, b, ...)                → CrossCohortComparisonReport

  calibration_report(y_true, y_proba, n_bins=10)
  expected_calibration_error / maximum_calibration_error / reliability_curve
  trading_metrics(...)

No registry side-effects in this module. Promotion → M18.A.8.
"""
from __future__ import annotations

from bot.ml.evaluation.report import (
    EvaluationReport,
    BaselineComparisonReport,
    CrossCohortComparisonReport,
    CROSS_COHORT_DISCLAIMER,
    EVALUATION_REPORT_SCHEMA_VERSION,
    BASELINE_COMPARISON_REPORT_SCHEMA_VERSION,
    CROSS_COHORT_COMPARISON_REPORT_SCHEMA_VERSION,
)
from bot.ml.evaluation.calibration import (
    calibration_report,
    expected_calibration_error,
    maximum_calibration_error,
    reliability_curve,
)
from bot.ml.evaluation.trading_metrics import (
    trading_metrics,
)
from bot.ml.evaluation.evaluator import (
    evaluate_model,
    compare_baselines,
    compare_across_cohorts,
    ALLOWED_PRIMARY_SPLITS,
)

# ── M18.A.7 v2 extended evaluation surface ───────────────────────────
from bot.ml.evaluation.ml_metrics import (
    binary_metrics_extended,
)
from bot.ml.evaluation.threshold_metrics import (
    threshold_table,
    LOCKED_THRESHOLDS,
)
from bot.ml.evaluation.drift import (
    drift_report,
)
from bot.ml.evaluation.permutation_importance import (
    permutation_importance,
    SUPPORTED_MODEL_TYPES as PI_SUPPORTED_MODEL_TYPES,
)
from bot.ml.evaluation.breakdowns import (
    all_breakdowns,
    per_symbol_breakdown,
    per_year_breakdown,
    volatility_regime_breakdown,
    market_regime_breakdown,
    MIN_SAMPLES_PER_SEGMENT,
)
from bot.ml.evaluation.trading_metrics import (
    PRECISION_AT_K_LIST,
    EQUITY_CURVE_UNAVAILABLE_REASON,
)

__all__ = [
    "EvaluationReport",
    "BaselineComparisonReport",
    "CrossCohortComparisonReport",
    "CROSS_COHORT_DISCLAIMER",
    "EVALUATION_REPORT_SCHEMA_VERSION",
    "BASELINE_COMPARISON_REPORT_SCHEMA_VERSION",
    "CROSS_COHORT_COMPARISON_REPORT_SCHEMA_VERSION",
    "calibration_report",
    "expected_calibration_error",
    "maximum_calibration_error",
    "reliability_curve",
    "trading_metrics",
    "evaluate_model",
    "compare_baselines",
    "compare_across_cohorts",
    "ALLOWED_PRIMARY_SPLITS",
    "binary_metrics_extended",
    "threshold_table",
    "LOCKED_THRESHOLDS",
    "drift_report",
    "permutation_importance",
    "PI_SUPPORTED_MODEL_TYPES",
    "all_breakdowns",
    "per_symbol_breakdown",
    "per_year_breakdown",
    "volatility_regime_breakdown",
    "market_regime_breakdown",
    "MIN_SAMPLES_PER_SEGMENT",
    "PRECISION_AT_K_LIST",
    "EQUITY_CURVE_UNAVAILABLE_REASON",
]

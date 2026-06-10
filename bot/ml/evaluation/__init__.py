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
]

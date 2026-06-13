"""bot.ml.models.thinness_gates — judgment-level training gates.

These are JUDGMENT gates: they can be --force-overridden at the
M18.A.8 promotion layer (NOT at the trainer layer). Their failure
flips `promotion_eligible=False` but does not prevent the trainer
from producing diagnostics — that's the point of a judgment gate,
the operator may still want the training output for analysis.

Per locked Q16 / Amendment 2: when `train_config.fixture_mode=True`,
these gates are SKIPPED ENTIRELY — the model is tagged fixture_only
which is a permanent non-promotable state (NOT --force-overridable).

Thresholds are deliberately conservative:
  MIN_TRAIN_SAMPLES          200    enough for any of the baselines
  MIN_VAL_SAMPLES             50    enough for stable model-selection
  MIN_TEST_SAMPLES            50    enough for a meaningful final-set
                                      metric
  MIN_MINORITY_CLASS_TRAIN    20    enough to learn the minority class
                                      pattern at all
  MAX_FEATURES_TO_TRAIN_RATIO 0.5  features <= 0.5 * train samples
                                      (10:1 rule of thumb is stricter
                                      but 2:1 catches catastrophic
                                      thinness only)

`evaluate_thinness()` returns a dict that records EVERY check (passed
or failed) — useful when the gate fails partially and the operator
needs to know exactly which checks need attention.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import numpy as np


DEFAULT_MIN_TRAIN_SAMPLES          = 200
DEFAULT_MIN_VAL_SAMPLES            = 50
DEFAULT_MIN_TEST_SAMPLES           = 50
DEFAULT_MIN_MINORITY_CLASS_TRAIN   = 20
DEFAULT_MAX_FEATURES_TO_TRAIN_RATIO = 0.5


@dataclass
class ThinnessThresholds:
    min_train_samples:           int   = DEFAULT_MIN_TRAIN_SAMPLES
    min_val_samples:             int   = DEFAULT_MIN_VAL_SAMPLES
    min_test_samples:            int   = DEFAULT_MIN_TEST_SAMPLES
    min_minority_class_train:    int   = DEFAULT_MIN_MINORITY_CLASS_TRAIN
    max_features_to_train_ratio: float = DEFAULT_MAX_FEATURES_TO_TRAIN_RATIO

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_thinness(
    *,
    y_train: np.ndarray,
    n_val: int,
    n_test: int,
    n_features: int,
    label_class: str,
    thresholds: Optional[ThinnessThresholds] = None,
) -> Dict[str, Any]:
    """Run every thinness check and return a structured report.

    Returns
    -------
    dict
      thresholds: ThinnessThresholds.to_dict() (for audit)
      label_class: str
      checks: dict each value is {"passed": bool, ...detail...}
              - sample_count_train
              - sample_count_val
              - sample_count_test
              - feature_to_train_ratio
              - minority_class_count_train  (binary / 3-way only)
      failed_checks: list[str]   names of checks that did NOT pass
      passed:        bool        True iff failed_checks is empty
    """
    if thresholds is None:
        thresholds = ThinnessThresholds()

    n_train = int(len(y_train))
    checks: Dict[str, Any] = {}

    # 1. Sample counts
    checks["sample_count_train"] = {
        "passed": n_train >= thresholds.min_train_samples,
        "value":     n_train,
        "threshold": thresholds.min_train_samples,
    }
    checks["sample_count_val"] = {
        "passed": n_val >= thresholds.min_val_samples,
        "value":     n_val,
        "threshold": thresholds.min_val_samples,
    }
    checks["sample_count_test"] = {
        "passed": n_test >= thresholds.min_test_samples,
        "value":     n_test,
        "threshold": thresholds.min_test_samples,
    }

    # 2. Feature-to-train ratio (catches "more features than samples")
    if n_train == 0:
        ratio = float("inf")
    else:
        ratio = float(n_features) / float(n_train)
    checks["feature_to_train_ratio"] = {
        "passed": ratio <= thresholds.max_features_to_train_ratio,
        "value":     float(ratio),
        "threshold": float(thresholds.max_features_to_train_ratio),
        "n_features": int(n_features),
        "n_train":    n_train,
    }

    # 3. Minority-class count (binary / 3-way classification only).
    #    For regression, this check is N/A and reported with passed=True.
    if label_class in ("binary", "classification_3way"):
        if n_train == 0:
            minority = 0
        else:
            unique, counts = np.unique(y_train, return_counts=True)
            minority = int(counts.min())
        checks["minority_class_count_train"] = {
            "passed": minority >= thresholds.min_minority_class_train,
            "value":     int(minority),
            "threshold": thresholds.min_minority_class_train,
        }
    else:
        checks["minority_class_count_train"] = {
            "passed": True,
            "value":     None,
            "threshold": None,
            "note":      f"N/A for label_class={label_class!r}",
        }

    failed_checks = [name for name, c in checks.items()
                      if not c["passed"]]

    return {
        "thresholds":    thresholds.to_dict(),
        "label_class":   label_class,
        "checks":        checks,
        "failed_checks": failed_checks,
        "passed":        len(failed_checks) == 0,
    }


# ─── Production promotion thinness gates (M18.B.4) ────────────────────
#
# The gates above are TRAINABILITY / JUDGMENT gates — deliberately weak
# so fixture/cold-start models can still TRAIN and produce diagnostics.
# The gates below are PRODUCTION PROMOTION gates — strict requirements a
# model must meet before it may become 'current' / production-quality.
#
# These are INTEGRITY-class (registered in bot.ml.registry.gates under
# the 'production:' prefix): --force may NEVER override them. A model
# can train diagnostically and still be permanently blocked from
# promotion because it does not have enough data / positives.
#
# Thresholds (locked):
#   PRODUCTION_MIN_TOTAL_ROWS       2000   train+val+test anchors
#   PRODUCTION_MIN_TRAIN_POSITIVES   500   positive (==1) labels in train
#   PRODUCTION_MIN_HOLDOUT_POSITIVES 100   positive labels in test/holdout
#   PRODUCTION_MIN_PER_SYMBOL_ROWS    50   min rows for any symbol counted
#
# Positive-label convention: the positive class is exactly 1 (1.0/True
# coerce to 1.0). Non-binary / missing / non-1 values are NOT counted as
# positives.

PRODUCTION_MIN_TOTAL_ROWS         = 2000
PRODUCTION_MIN_TRAIN_POSITIVES    = 500
PRODUCTION_MIN_HOLDOUT_POSITIVES  = 100
PRODUCTION_MIN_PER_SYMBOL_ROWS    = 50

# Stable blocked-reason strings (bare; the trainer composes them
# verbatim and registry/gates classifies the 'production:' prefix as
# integrity).
PRODUCTION_BLOCK_TOTAL_ROWS       = "production_total_rows_below_2000"
PRODUCTION_BLOCK_TRAIN_POSITIVES  = "production_train_positives_below_500"
PRODUCTION_BLOCK_HOLDOUT_POSITIVES = "production_holdout_positives_below_100"
PRODUCTION_BLOCK_PER_SYMBOL_ROWS  = "production_per_symbol_rows_below_50"


@dataclass
class ProductionThinnessThresholds:
    min_total_rows:        int = PRODUCTION_MIN_TOTAL_ROWS
    min_train_positives:   int = PRODUCTION_MIN_TRAIN_POSITIVES
    min_holdout_positives: int = PRODUCTION_MIN_HOLDOUT_POSITIVES
    min_per_symbol_rows:   int = PRODUCTION_MIN_PER_SYMBOL_ROWS

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def count_positives(y: np.ndarray, label_class: str) -> int:
    """Count positive (==1) labels. Binary only — for any other label
    class returns 0 (production promotion is a binary-model concept in
    M18). Non-finite / non-1 values are never counted."""
    if label_class != "binary":
        return 0
    arr = np.asarray(y, dtype=np.float64)
    if arr.size == 0:
        return 0
    finite = arr[np.isfinite(arr)]
    return int(np.sum(finite == 1.0))


def evaluate_production_thinness(
    *,
    total_rows: int,
    train_positives: int,
    holdout_positives: int,
    per_symbol_counts: Optional[Dict[str, int]] = None,
    label_class: str = "binary",
    thresholds: Optional[ProductionThinnessThresholds] = None,
) -> Dict[str, Any]:
    """Evaluate the strict production-promotion profile.

    Pure function over already-computed counts so it is trivially
    testable. Returns a JSON-safe structured result:

      profile: "production_promotion"
      passed:  bool
      label_class: str
      thresholds: {...}
      observed:   {total_rows, train_positives, holdout_positives,
                   min_per_symbol_rows}
      blocked_reasons: [stable strings]   (empty iff passed)

    Production promotion is a binary-model concept; for a non-binary
    label_class the profile is reported unavailable+blocked (positives
    cannot be defined), never silently passed.
    """
    if thresholds is None:
        thresholds = ProductionThinnessThresholds()

    per_symbol_counts = dict(per_symbol_counts or {})
    if per_symbol_counts:
        min_per_symbol = int(min(per_symbol_counts.values()))
    else:
        min_per_symbol = 0

    blocked: List[str] = []

    if label_class != "binary":
        blocked.append("production_unsupported_label_class")

    if int(total_rows) < thresholds.min_total_rows:
        blocked.append(PRODUCTION_BLOCK_TOTAL_ROWS)
    if int(train_positives) < thresholds.min_train_positives:
        blocked.append(PRODUCTION_BLOCK_TRAIN_POSITIVES)
    if int(holdout_positives) < thresholds.min_holdout_positives:
        blocked.append(PRODUCTION_BLOCK_HOLDOUT_POSITIVES)
    if min_per_symbol < thresholds.min_per_symbol_rows:
        blocked.append(PRODUCTION_BLOCK_PER_SYMBOL_ROWS)

    return {
        "profile":     "production_promotion",
        "passed":      len(blocked) == 0,
        "label_class": label_class,
        "thresholds": {
            "min_total_rows":        thresholds.min_total_rows,
            "min_train_positives":   thresholds.min_train_positives,
            "min_holdout_positives": thresholds.min_holdout_positives,
            "min_per_symbol_rows":   thresholds.min_per_symbol_rows,
        },
        "observed": {
            "total_rows":        int(total_rows),
            "train_positives":   int(train_positives),
            "holdout_positives": int(holdout_positives),
            "min_per_symbol_rows": int(min_per_symbol),
            "per_symbol_counts": {k: int(v) for k, v in
                                   per_symbol_counts.items()},
        },
        "blocked_reasons": blocked,
    }

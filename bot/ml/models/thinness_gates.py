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

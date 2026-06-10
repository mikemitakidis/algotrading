"""bot.ml.evaluation.drift — feature-level drift diagnostics.

Wraps the existing M18.A.5 `distribution_shift_proxy_psi()` to
produce a structured drift report:

  per_feature_psi              dict feature_name -> PSI scalar
  max_psi                       max over per_feature_psi values
  argmax_psi_feature            feature name with highest PSI
  features_over_threshold       list of {feature, psi} above threshold
  drift_warning                 True iff max_psi >= drift_warning_threshold
  reference_split               which split is the reference ("train")
  comparison_split              which split is compared ("val" or "test")
  n_reference                   sample count in reference
  n_comparison                  sample count in comparison
  threshold                     the configured drift-warning threshold
  unavailable_reason            populated when either split has too few
                                  rows or no overlapping numeric features

Rule-of-thumb PSI interpretation (industry standard):
  PSI < 0.10    : stable distribution
  0.10 - 0.25   : moderate shift
  PSI ≥ 0.25    : significant shift

Default drift_warning_threshold = 0.25 (significant). Threshold can
be overridden by the caller.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from bot.ml.dataset.adversarial_validation import (
    distribution_shift_proxy_psi,
)


DEFAULT_DRIFT_WARNING_THRESHOLD = 0.25
MIN_SAMPLES_FOR_PSI = 10   # downstream distribution_shift_proxy_psi
                              # also enforces this per-feature; the
                              # wrapper uses it for an early refusal


def drift_report(
    *,
    dataset:              pd.DataFrame,
    train_indices:        np.ndarray,
    comparison_indices:   np.ndarray,
    feature_columns:      List[str],
    comparison_split_name: str   = "val",
    drift_warning_threshold: float = DEFAULT_DRIFT_WARNING_THRESHOLD,
    n_bins:               int   = 10,
) -> Dict[str, Any]:
    """Compute per-feature PSI between train and one comparison split.

    Returns a structured drift report. If either split has fewer
    than MIN_SAMPLES_FOR_PSI rows, returns a report with
    `unavailable_reason` populated and empty metrics.
    """
    n_train      = int(len(train_indices))
    n_comparison = int(len(comparison_indices))

    if n_train < MIN_SAMPLES_FOR_PSI or n_comparison < MIN_SAMPLES_FOR_PSI:
        return {
            "reference_split":            "train",
            "comparison_split":           comparison_split_name,
            "n_reference":                n_train,
            "n_comparison":               n_comparison,
            "threshold":                  float(drift_warning_threshold),
            "per_feature_psi":            {},
            "max_psi":                    float("nan"),
            "argmax_psi_feature":         None,
            "features_over_threshold":    [],
            "drift_warning":              False,
            "unavailable_reason": (
                f"insufficient samples for PSI: "
                f"need >= {MIN_SAMPLES_FOR_PSI} rows in each split; "
                f"got n_train={n_train}, "
                f"n_{comparison_split_name}={n_comparison}"),
        }

    if not feature_columns:
        return {
            "reference_split":            "train",
            "comparison_split":           comparison_split_name,
            "n_reference":                n_train,
            "n_comparison":               n_comparison,
            "threshold":                  float(drift_warning_threshold),
            "per_feature_psi":            {},
            "max_psi":                    float("nan"),
            "argmax_psi_feature":         None,
            "features_over_threshold":    [],
            "drift_warning":              False,
            "unavailable_reason":         "no feature columns supplied",
        }

    X_train       = dataset.iloc[train_indices][feature_columns]
    X_comparison  = dataset.iloc[comparison_indices][feature_columns]

    per_feature_psi = distribution_shift_proxy_psi(
        X_train, X_comparison, n_bins=n_bins)

    if not per_feature_psi:
        return {
            "reference_split":            "train",
            "comparison_split":           comparison_split_name,
            "n_reference":                n_train,
            "n_comparison":               n_comparison,
            "threshold":                  float(drift_warning_threshold),
            "per_feature_psi":            {},
            "max_psi":                    float("nan"),
            "argmax_psi_feature":         None,
            "features_over_threshold":    [],
            "drift_warning":              False,
            "unavailable_reason": (
                "distribution_shift_proxy_psi returned an empty "
                "dict — likely all features were non-numeric or "
                "had insufficient finite values"),
        }

    max_psi = float(max(per_feature_psi.values()))
    argmax_feature = max(per_feature_psi.items(),
                           key=lambda kv: kv[1])[0]

    over_threshold = sorted(
        (
            {"feature": f, "psi": float(v)}
            for f, v in per_feature_psi.items()
            if float(v) >= float(drift_warning_threshold)
        ),
        key=lambda d: d["psi"], reverse=True)

    return {
        "reference_split":            "train",
        "comparison_split":           comparison_split_name,
        "n_reference":                n_train,
        "n_comparison":               n_comparison,
        "threshold":                  float(drift_warning_threshold),
        "per_feature_psi":            {k: float(v)
                                          for k, v in per_feature_psi.items()},
        "max_psi":                    max_psi,
        "argmax_psi_feature":         argmax_feature,
        "features_over_threshold":    over_threshold,
        "drift_warning":              bool(
            max_psi >= float(drift_warning_threshold)),
        "unavailable_reason":         None,
    }

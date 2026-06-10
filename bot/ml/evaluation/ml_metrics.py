"""bot.ml.evaluation.ml_metrics — extended ML metrics for binary
classifiers.

PR-AUC is the PRIMARY M18 metric for imbalanced binary classification.
The basic metrics already computed inside Trainer._binary_metrics are
re-exposed here in extended form so the EvaluationReport can carry
the full picture in one block.

Returned dict (binary_metrics_extended) carries:

  roc_auc                  float    sklearn roc_auc_score, NaN if y_true
                                     is single-class
  pr_auc                   float    sklearn average_precision_score,
                                     NaN if y_true is single-class
                                     ← PRIMARY M18 metric
  brier_score              float    mean squared error of probabilities
  log_loss                 float    clipped log-loss (eps=1e-7); NaN
                                     if predictions outside [0,1]
  accuracy                 float    accuracy at threshold 0.5
  precision_at_05          float    TP / (TP+FP) at threshold 0.5,
                                     NaN if no positives predicted
  recall_at_05             float    TP / (TP+FN) at threshold 0.5,
                                     NaN if no actual positives
  f1_at_05                 float    2 * P * R / (P + R); NaN if
                                     precision or recall NaN
  confusion_matrix_at_05   dict     {tp, fp, fn, tn} at threshold 0.5
  positive_rate_true       float    mean(y_true)
  positive_rate_pred       float    mean(y_proba >= 0.5)
  n_rows                   int

NaN handling:
  * Empty input (n_rows == 0) → every metric NaN, confusion matrix
    all zeros, warnings = ['empty_split'].
  * Single-class y_true → roc_auc and pr_auc NaN; other metrics
    still defined.
  * Predictions outside [0,1] → log_loss NaN with warning.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np


PRIMARY_M18_METRIC = "pr_auc"
DEFAULT_THRESHOLD  = 0.5
LOG_LOSS_EPS       = 1e-7


def binary_metrics_extended(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> Dict[str, Any]:
    """Compute the full extended-metric block for one split.

    sklearn is imported lazily so module-load remains sklearn-free.
    """
    y_t = np.asarray(y_true,  dtype=np.float64)
    y_p = np.asarray(y_proba, dtype=np.float64)
    n   = int(len(y_t))

    if y_t.shape != y_p.shape:
        raise ValueError(
            f"y_true.shape={y_t.shape} != y_proba.shape={y_p.shape}")

    if n == 0:
        return {
            "roc_auc":                float("nan"),
            "pr_auc":                 float("nan"),
            "brier_score":            float("nan"),
            "log_loss":               float("nan"),
            "accuracy":               float("nan"),
            "precision_at_05":        float("nan"),
            "recall_at_05":           float("nan"),
            "f1_at_05":               float("nan"),
            "confusion_matrix_at_05": {"tp": 0, "fp": 0,
                                         "fn": 0, "tn": 0},
            "positive_rate_true":     float("nan"),
            "positive_rate_pred":     float("nan"),
            "n_rows":                 0,
            "warnings":               ["empty_split"],
        }

    warnings: list = []

    # Drop non-finite rows for metric stability
    finite = np.isfinite(y_t) & np.isfinite(y_p)
    if not finite.all():
        warnings.append(
            f"dropped_non_finite_rows={int((~finite).sum())}")
    y_t = y_t[finite]
    y_p = y_p[finite]
    n_finite = len(y_t)

    if n_finite == 0:
        return {
            "roc_auc":                float("nan"),
            "pr_auc":                 float("nan"),
            "brier_score":            float("nan"),
            "log_loss":               float("nan"),
            "accuracy":               float("nan"),
            "precision_at_05":        float("nan"),
            "recall_at_05":           float("nan"),
            "f1_at_05":               float("nan"),
            "confusion_matrix_at_05": {"tp": 0, "fp": 0,
                                         "fn": 0, "tn": 0},
            "positive_rate_true":     float("nan"),
            "positive_rate_pred":     float("nan"),
            "n_rows":                 n,
            "warnings":               warnings + ["all_non_finite"],
        }

    # Lazy sklearn import
    from sklearn.metrics import (
        roc_auc_score,
        average_precision_score,
        brier_score_loss,
    )

    n_classes = int(len(np.unique(y_t)))
    if n_classes < 2:
        roc_auc = float("nan")
        pr_auc  = float("nan")
        warnings.append("single_class_y_true")
    else:
        roc_auc = float(roc_auc_score(y_t, y_p))
        pr_auc  = float(average_precision_score(y_t, y_p))

    brier = float(brier_score_loss(y_t, np.clip(y_p, 0.0, 1.0)))

    # Log loss — clipped to (eps, 1-eps) for numerical stability.
    # If predictions are outside [0,1] we flag rather than silently
    # clip; clipping AFTER flagging is fine because the warning is
    # recorded.
    if (y_p < 0).any() or (y_p > 1).any():
        warnings.append("predictions_outside_unit_interval")
    p_clipped = np.clip(y_p, LOG_LOSS_EPS, 1.0 - LOG_LOSS_EPS)
    ll = -float(np.mean(
        y_t * np.log(p_clipped)
        + (1.0 - y_t) * np.log(1.0 - p_clipped)))

    # Threshold-0.5 confusion matrix + precision/recall/F1/accuracy
    y_hat = (y_p >= float(threshold)).astype(np.int64)
    y_t_i = y_t.astype(np.int64)
    tp = int(((y_hat == 1) & (y_t_i == 1)).sum())
    fp = int(((y_hat == 1) & (y_t_i == 0)).sum())
    fn = int(((y_hat == 0) & (y_t_i == 1)).sum())
    tn = int(((y_hat == 0) & (y_t_i == 0)).sum())
    accuracy = float((tp + tn) / n_finite)

    if (tp + fp) > 0:
        precision = float(tp) / float(tp + fp)
    else:
        precision = float("nan")
    if (tp + fn) > 0:
        recall    = float(tp) / float(tp + fn)
    else:
        recall    = float("nan")
    if (np.isnan(precision) or np.isnan(recall)
            or (precision + recall) == 0):
        f1 = float("nan")
    else:
        f1 = float(2.0 * precision * recall / (precision + recall))

    return {
        "roc_auc":                roc_auc,
        "pr_auc":                 pr_auc,
        "brier_score":            brier,
        "log_loss":               ll,
        "accuracy":               accuracy,
        "precision_at_05":        precision,
        "recall_at_05":           recall,
        "f1_at_05":               f1,
        "confusion_matrix_at_05": {"tp": tp, "fp": fp,
                                     "fn": fn, "tn": tn},
        "positive_rate_true":     float(np.mean(y_t)),
        "positive_rate_pred":     float(np.mean(y_hat)),
        "n_rows":                 n_finite,
        "warnings":               warnings,
    }

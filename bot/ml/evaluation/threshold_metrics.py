"""bot.ml.evaluation.threshold_metrics — threshold-decision table.

Locked thresholds per M18.A.7 directive:
    0.30, 0.40, 0.50, 0.60, 0.65, 0.70, 0.80

At each threshold the table records:
    threshold
    n_predicted_positive     == accepted count
    n_filtered                == n_rows - n_predicted_positive
    precision                 TP / (TP + FP), NaN if no positives predicted
    recall                    TP / (TP + FN), NaN if no actual positives
    f1                        2 P R / (P + R), NaN if either is NaN
    confusion_matrix          {tp, fp, fn, tn}

Empty split → empty rows list with note. Single-class y_true is
handled per-threshold (precision/recall can still be 0 or NaN
depending on predictions).
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

import numpy as np


# Locked threshold ladder for M18.A.7.
LOCKED_THRESHOLDS: Sequence[float] = (0.30, 0.40, 0.50,
                                          0.60, 0.65, 0.70, 0.80)


def threshold_table(
    y_true: np.ndarray, y_proba: np.ndarray,
    *, thresholds: Sequence[float] = LOCKED_THRESHOLDS,
) -> Dict[str, Any]:
    """Return per-threshold metric rows + the threshold list used."""
    n = int(len(y_true))
    if n == 0:
        return {
            "n_rows":          0,
            "thresholds_used": list(map(float, thresholds)),
            "rows":            [],
            "note":            "empty_split",
        }

    y_t = np.asarray(y_true,  dtype=np.float64)
    y_p = np.asarray(y_proba, dtype=np.float64)

    rows: List[Dict[str, Any]] = []
    for thr in thresholds:
        y_hat = (y_p >= float(thr)).astype(np.float64)
        n_pred_pos = int(np.sum(y_hat == 1.0))
        n_filt     = int(n - n_pred_pos)

        tp = int(np.sum((y_hat == 1.0) & (y_t == 1.0)))
        fp = int(np.sum((y_hat == 1.0) & (y_t == 0.0)))
        fn = int(np.sum((y_hat == 0.0) & (y_t == 1.0)))
        tn = int(np.sum((y_hat == 0.0) & (y_t == 0.0)))

        precision = (float(tp) / (tp + fp)
                      if (tp + fp) > 0 else float("nan"))
        recall    = (float(tp) / (tp + fn)
                      if (tp + fn) > 0 else float("nan"))
        if (np.isnan(precision) or np.isnan(recall)
                or (precision + recall) == 0):
            f1 = float("nan")
        else:
            f1 = float(2.0 * precision * recall
                         / (precision + recall))

        rows.append({
            "threshold":             float(thr),
            "n_predicted_positive":  n_pred_pos,    # accepted
            "n_filtered":            n_filt,         # rejected
            "precision":             precision,
            "recall":                recall,
            "f1":                    f1,
            "confusion_matrix":      {"tp": tp, "fp": fp,
                                        "fn": fn, "tn": tn},
        })
    return {
        "n_rows":          n,
        "thresholds_used": list(map(float, thresholds)),
        "rows":            rows,
    }

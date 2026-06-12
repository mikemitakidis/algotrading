"""bot.ml.evaluation.permutation_importance — model-agnostic
permutation importance for M18.A.7.

Algorithm (standard):
  1. Refit the model on (X_train, y_train) using the same train_config
     and seed as the original training run (determinism).
  2. Compute the baseline score on the chosen evaluation split
     (X_eval, y_eval). Score = ROC AUC by default.
  3. For each feature column:
       For each repeat (default 5):
         Shuffle the column with a per-repeat seeded RNG.
         Score the model on the shuffled data.
         Record (baseline - shuffled). Positive = important.
       Record the mean and stdev across repeats.
  4. Return the top-N features by mean importance, plus full table.

Model-type support:
  B2_logistic        supported (fits via the locked LR config)
  M_lightgbm         supported when lightgbm is installed
  B0_majority        NOT supported — constant predictor uses no
                       features → all importances trivially 0;
                       returned with explicit unavailable_reason
  B1_scanner_replica NOT supported — passthrough of one specific
                       column, not a learned model → returned with
                       explicit unavailable_reason

Sample-size guard:
  If either X_train or X_eval has fewer than `min_samples` rows
  (default 50), returns an `unavailable_reason` rather than emitting
  noisy importances.

Determinism:
  RNG seed for shuffling = train_config.seed * 1000 + repeat_index.
  Same inputs + same seed → byte-identical importances.

No new dependencies — uses only numpy, pandas, sklearn (already a
project dep).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


DEFAULT_N_REPEATS  = 5
DEFAULT_N_TOP      = 20
DEFAULT_MIN_SAMPLES = 50
DEFAULT_SCORING    = "roc_auc"

# Model types where permutation importance is structurally
# meaningful: requires the model to depend on input features.
SUPPORTED_MODEL_TYPES = frozenset({"B2_logistic", "M_lightgbm"})
UNSUPPORTED_REASONS = {
    "B0_majority": (
        "B0_majority is a constant predictor (uses no features); "
        "permutation importance is trivially zero for all features"),
    "B1_scanner_replica": (
        "B1_scanner_replica is a passthrough of one specific column "
        "(scanner_replica.signal_fires); permutation importance is "
        "not a meaningful diagnostic — it would be ~1.0 for that "
        "one column and 0 for all others"),
}


def _score_binary(y_true: np.ndarray,
                   y_proba: np.ndarray,
                   scoring: str = DEFAULT_SCORING) -> float:
    """Compute the requested score. Returns NaN if y_true is
    single-class."""
    if len(np.unique(y_true)) < 2:
        return float("nan")
    if scoring == "roc_auc":
        from sklearn.metrics import roc_auc_score
        return float(roc_auc_score(y_true, y_proba))
    if scoring == "pr_auc":
        from sklearn.metrics import average_precision_score
        return float(average_precision_score(y_true, y_proba))
    raise ValueError(
        f"unsupported scoring={scoring!r}; choose "
        f"'roc_auc' or 'pr_auc'")


def permutation_importance(
    *,
    train_config,                 # TrainConfig
    assembler_result,             # AssemblerResult
    feature_columns:    List[str],
    evaluation_split:   str   = "val",
    n_repeats:          int   = DEFAULT_N_REPEATS,
    n_top:              int   = DEFAULT_N_TOP,
    min_samples:        int   = DEFAULT_MIN_SAMPLES,
    scoring:            str   = DEFAULT_SCORING,
) -> Dict[str, Any]:
    """Compute permutation importance for one model on one split.

    Refits the model from `train_config` and `assembler_result`, then
    permutes each feature column on the evaluation split and measures
    the score drop. Deterministic for fixed `train_config.seed`.
    """
    if evaluation_split not in ("val", "test"):
        return {
            "available":          False,
            "unavailable_reason": (
                f"evaluation_split={evaluation_split!r} must be "
                f"'val' or 'test'"),
        }

    mt = train_config.model_type
    if mt in UNSUPPORTED_REASONS:
        return {
            "available":          False,
            "model_type":         mt,
            "evaluation_split":   evaluation_split,
            "scoring":            scoring,
            "n_repeats":          n_repeats,
            "unavailable_reason": UNSUPPORTED_REASONS[mt],
        }
    if mt not in SUPPORTED_MODEL_TYPES:
        return {
            "available":          False,
            "model_type":         mt,
            "evaluation_split":   evaluation_split,
            "scoring":            scoring,
            "n_repeats":          n_repeats,
            "unavailable_reason": (
                f"model_type={mt!r} not supported by permutation "
                f"importance in M18.A.7"),
        }

    if assembler_result.split is None:
        return {
            "available":          False,
            "model_type":         mt,
            "evaluation_split":   evaluation_split,
            "scoring":            scoring,
            "n_repeats":          n_repeats,
            "unavailable_reason": "AssemblerResult.split is None",
        }

    # Materialise (X, y) for train + evaluation split
    from bot.ml.models.base import extract_xy_for_split
    split = assembler_result.split
    dataset = assembler_result.dataset
    train_idx = split.train_anchor_indices
    eval_idx = (split.val_anchor_indices if evaluation_split == "val"
                  else split.test_anchor_indices)

    n_train = int(len(train_idx))
    n_eval  = int(len(eval_idx))

    if n_train < min_samples or n_eval < min_samples:
        return {
            "available":          False,
            "model_type":         mt,
            "evaluation_split":   evaluation_split,
            "scoring":            scoring,
            "n_repeats":          n_repeats,
            "n_train":            n_train,
            "n_evaluation":       n_eval,
            "min_samples":        min_samples,
            "unavailable_reason": (
                f"insufficient samples: need >= {min_samples} in "
                f"both train and {evaluation_split} for stable "
                f"importances; got n_train={n_train}, "
                f"n_{evaluation_split}={n_eval}"),
        }

    target = train_config.target_label_id
    X_train, y_train = extract_xy_for_split(
        dataset, train_idx,
        target_label_id=target, feature_columns=feature_columns)
    X_eval, y_eval = extract_xy_for_split(
        dataset, eval_idx,
        target_label_id=target, feature_columns=feature_columns)

    # Refit the model. Dispatch by model_type — uses the same inner
    # trainer classes the M18.A.6 orchestrator uses.
    from bot.ml.models.baselines import LogisticRegressionTrainer
    from bot.ml.models.lightgbm_trainer import (
        LightGBMTrainer, is_lightgbm_available)
    from bot.ml.models.base import get_label_class
    label_class = get_label_class(target)
    seed = int(train_config.seed)

    if mt == "B2_logistic":
        model = LogisticRegressionTrainer()
        model.fit(X_train, y_train, label_class=label_class, seed=seed)
    elif mt == "M_lightgbm":
        if not is_lightgbm_available():
            return {
                "available":          False,
                "model_type":         mt,
                "evaluation_split":   evaluation_split,
                "scoring":            scoring,
                "n_repeats":          n_repeats,
                "unavailable_reason": (
                    "M_lightgbm requested but lightgbm is not "
                    "installed in this venv"),
            }
        model = LightGBMTrainer()
        model.fit(X_train, y_train, label_class=label_class, seed=seed,
                   hyperparameters=dict(train_config.hyperparameters))
    else:
        raise AssertionError(f"unreachable: mt={mt!r}")

    # Baseline score
    baseline_proba = model.predict_proba(X_eval)
    baseline_score = _score_binary(y_eval, baseline_proba, scoring)

    if np.isnan(baseline_score):
        return {
            "available":          False,
            "model_type":         mt,
            "evaluation_split":   evaluation_split,
            "scoring":            scoring,
            "n_repeats":          n_repeats,
            "n_train":            n_train,
            "n_evaluation":       n_eval,
            "unavailable_reason": (
                f"baseline {scoring} is NaN on the evaluation "
                f"split (likely y_{evaluation_split} is single-"
                f"class); permutation importance is not meaningful")
        }

    # Permute each feature for n_repeats and record score drop
    importances: List[Dict[str, Any]] = []
    base_seed = seed * 1000
    for j, feat in enumerate(feature_columns):
        deltas: List[float] = []
        for r in range(n_repeats):
            rng = np.random.default_rng(base_seed + r * len(feature_columns) + j)
            X_perm = X_eval.copy()
            X_perm[:, j] = rng.permutation(X_perm[:, j])
            perm_proba = model.predict_proba(X_perm)
            perm_score = _score_binary(y_eval, perm_proba, scoring)
            deltas.append(baseline_score - perm_score)
        deltas_arr = np.asarray(deltas, dtype=np.float64)
        importances.append({
            "feature":         feat,
            "importance_mean": float(np.nanmean(deltas_arr)),
            "importance_std":  float(np.nanstd(deltas_arr)),
            "n_repeats":       int(n_repeats),
        })

    # Sort by importance_mean descending. NaN means sorts to the end.
    importances.sort(
        key=lambda d: (float("-inf") if np.isnan(d["importance_mean"])
                        else d["importance_mean"]),
        reverse=True)

    return {
        "available":          True,
        "model_type":         mt,
        "evaluation_split":   evaluation_split,
        "scoring":            scoring,
        "baseline_score":     baseline_score,
        "n_repeats":          int(n_repeats),
        "n_train":            n_train,
        "n_evaluation":       n_eval,
        "n_features":         int(len(feature_columns)),
        "seed":               seed,
        "top_n":              int(n_top),
        "top_features":       importances[:int(n_top)],
        "all_features":       importances,
        "unavailable_reason": None,
    }

"""bot.ml.models.trainer — orchestrator for M18.A.6 training runs.

Inputs:
  * TrainConfig          (bot.ml.schemas.TrainConfig, locked schema)
  * AssemblerResult      (bot.ml.dataset.assembler — dataset, manifest,
                            split, AV result)

Output: TrainOutputs (bot.ml.models.base) with:
  * per-split metrics
  * predictions
  * promotion_eligible + promotion_blocked_reasons
  * fixture_only flag
  * thinness diagnostics
  * dataset_anchor_set propagated for cohort provenance

Promotion gate composition (per locked plan):
  model.promotion_eligible := dataset.promotion_eligible
                              AND not train_config.fixture_mode
                              AND thinness.passed

Reasons for blocking are namespaced so the M18.A.8 promotion layer
can distinguish:
  "dataset:<reason>"     inherited from the dataset manifest's
                           promotion_blocked_reasons (e.g.
                           coverage_degraded, adversarial_validation_
                           failed). NOT --force-overridable.
  "fixture_only"         train_config.fixture_mode True OR dataset
                           was fixture_only. NOT --force-overridable.
  "thinness:<check>"     thinness gate <check> did not pass. JUDGMENT
                           gate — M18.A.8 may --force-override.

Dual-cohort contract (model_a_meta_label vs model_b_candidate_quality)
─────────────────────────────────────────────────────────────────────
The trainer does NOT filter rows by train_mode. The cohort is
determined STRUCTURALLY by the assembler's anchor_set — the assembler
is the single source of truth for which anchors land in the dataset.

The trainer's role is to verify that the operator's train_mode tag
is CONGRUENT with the dataset's anchor_set. The mapping is 1:1:

    train_mode='model_a_meta_label'        ⟺
      manifest.anchor_set='model_a_scanner_replica'

    train_mode='model_b_candidate_quality' ⟺
      manifest.anchor_set='model_b_1h_union_candidates'

Any other combination raises M18ConfigError at train_one() time. This
catches the failure mode where an operator copies a TrainConfig from
a Model A run and points it at a Model B dataset (or vice versa)
without realising the cohort changed underneath them.

The trainer NEVER touches test data during fit. It reports test
metrics for diagnostic visibility, but model SELECTION must use val
only — that selection lives in the M18.A.7 evaluation phase, not
here. Trainer.train_one() trains ONE model with the supplied
config; it does NOT do hyperparameter search.
"""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.ml.errors import (
    InsufficientDataError,
    M18ConfigError,
)
from bot.ml.schemas import (
    TrainConfig,
    ALLOWED_MODEL_TYPES,
    ALLOWED_TRAIN_MODES,
)
from bot.ml.dataset.assembler import AssemblerResult
from bot.ml.dataset.anchors import (
    ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
    ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
)
from bot.ml.models.base import (
    TrainOutputs,
    extract_xy_for_split,
    get_label_class,
    select_feature_columns,
)
from bot.ml.models.thinness_gates import (
    ThinnessThresholds,
    evaluate_thinness,
)
from bot.ml.models.baselines import (
    MajorityClassTrainer,
    ScannerReplicaTrainer,
    LogisticRegressionTrainer,
)
from bot.ml.models.lightgbm_trainer import (
    LightGBMTrainer,
    is_lightgbm_available,
)
from bot.ml.models.random_forest_trainer import (
    RandomForestTrainer,
)


# Column name of the live-scanner passthrough used by B1.
SCANNER_FIRES_COLUMN = "scanner_replica.signal_fires"


# Model types implemented in M18. M_lightgbm is implemented
# conditionally (raises if lightgbm is absent — never silently falls
# back). M_random_forest (M18.B.1) is a sklearn-only tree model that
# does NOT require lightgbm; it trains only when explicitly requested
# via train_config.model_type == "M_random_forest".
IMPLEMENTED_MODEL_TYPES = frozenset({
    "B0_majority",
    "B1_scanner_replica",
    "B2_logistic",
    "M_lightgbm",
    "M_random_forest",
})


# ── Train-mode ↔ anchor-set mapping (Q18) ────────────────────────────
# The cohort is set STRUCTURALLY by the assembler's anchor_set; the
# trainer's train_mode tag must agree with it 1:1.
TRAIN_MODE_TO_ANCHOR_SET: Dict[str, str] = {
    "model_a_meta_label":        ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
    "model_b_candidate_quality": ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
}
ANCHOR_SET_TO_TRAIN_MODE: Dict[str, str] = {
    v: k for k, v in TRAIN_MODE_TO_ANCHOR_SET.items()}


def assert_train_mode_matches_anchor_set(
    train_mode: str, anchor_set: str,
) -> None:
    """Raise M18ConfigError if train_mode and anchor_set disagree.

    train_mode is the operator's TAG; anchor_set is the dataset's
    structural cohort identifier. The assembler is the single source
    of truth — the trainer only validates congruence.
    """
    if train_mode not in TRAIN_MODE_TO_ANCHOR_SET:
        raise M18ConfigError(
            f"train_mode={train_mode!r} not recognised; expected "
            f"one of {sorted(TRAIN_MODE_TO_ANCHOR_SET)}")
    expected = TRAIN_MODE_TO_ANCHOR_SET[train_mode]
    if anchor_set != expected:
        # Reverse-lookup the train_mode that WOULD match — useful
        # for the operator's error message.
        suggested_mode = ANCHOR_SET_TO_TRAIN_MODE.get(
            anchor_set, "(no matching train_mode)")
        raise M18ConfigError(
            f"cohort mismatch: train_mode={train_mode!r} expects "
            f"dataset anchor_set={expected!r}, but the supplied "
            f"AssemblerResult.manifest.anchor_set={anchor_set!r}. "
            f"The assembler is the single source of truth for "
            f"cohort construction — the trainer does not re-filter "
            f"rows by train_mode. Either:\n"
            f"  (a) rebuild the dataset with AssemblerConfig."
            f"anchor_set matching train_mode={train_mode!r}, or\n"
            f"  (b) change train_mode to {suggested_mode!r} so it "
            f"matches the dataset's anchor_set.")


# ─────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────

def _binary_metrics(y_true: np.ndarray,
                      y_proba: np.ndarray) -> Dict[str, float]:
    """Compute binary-classification metrics from probabilities.

    Returns a dict — values are float (NaN if not computable):
      accuracy             0/1 accuracy at threshold 0.5
      brier_score          mean squared error of probabilities
      positive_rate_true   fraction of y_true == 1
      positive_rate_pred   fraction of (y_proba >= 0.5) == 1
      roc_auc              NaN if only one class is present
    """
    if len(y_true) == 0:
        return {
            "accuracy":           float("nan"),
            "brier_score":        float("nan"),
            "positive_rate_true": float("nan"),
            "positive_rate_pred": float("nan"),
            "roc_auc":            float("nan"),
            "n_rows":             0,
        }
    y_true = y_true.astype(np.float64)
    y_proba = y_proba.astype(np.float64)
    y_pred = (y_proba >= 0.5).astype(np.float64)
    acc = float(np.mean(y_pred == y_true))
    brier = float(np.mean((y_proba - y_true) ** 2))
    pr_true = float(np.mean(y_true == 1.0))
    pr_pred = float(np.mean(y_pred == 1.0))
    n_classes = len(np.unique(y_true))
    if n_classes < 2:
        auc = float("nan")
    else:
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(y_true, y_proba))
    return {
        "accuracy":           acc,
        "brier_score":        brier,
        "positive_rate_true": pr_true,
        "positive_rate_pred": pr_pred,
        "roc_auc":            auc,
        "n_rows":             int(len(y_true)),
    }


def _regression_metrics(y_true: np.ndarray,
                          y_pred: np.ndarray) -> Dict[str, float]:
    """Compute regression metrics (MSE, MAE, R²)."""
    if len(y_true) == 0:
        return {"mse": float("nan"), "mae": float("nan"),
                "r2":  float("nan"), "n_rows": 0}
    y_true = y_true.astype(np.float64)
    y_pred = y_pred.astype(np.float64)
    err = y_pred - y_true
    mse = float(np.mean(err ** 2))
    mae = float(np.mean(np.abs(err)))
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = float("nan") if ss_tot == 0.0 else (1.0 - ss_res / ss_tot)
    return {"mse": mse, "mae": mae, "r2": r2,
            "n_rows": int(len(y_true))}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _python_library_versions() -> Dict[str, str]:
    """Pin the major versions Trainer-result determinism depends on."""
    versions: Dict[str, str] = {
        "python": sys.version.split()[0],
        "numpy":  np.__version__,
        "pandas": pd.__version__,
        "platform": platform.platform(),
    }
    try:
        import sklearn
        versions["sklearn"] = sklearn.__version__
    except ImportError:
        pass
    if is_lightgbm_available():
        import lightgbm
        versions["lightgbm"] = lightgbm.__version__
    return versions


def _extract_signal_fires(
    dataset: pd.DataFrame, indices: np.ndarray,
) -> np.ndarray:
    """For B1: pull scanner_replica.signal_fires for the given
    indices, NaN-safe (NaN → 0)."""
    if SCANNER_FIRES_COLUMN not in dataset.columns:
        raise M18ConfigError(
            f"B1_scanner_replica requires the "
            f"{SCANNER_FIRES_COLUMN!r} column in the dataset; "
            f"available columns include "
            f"{sorted(dataset.columns)[:5]}...")
    if len(indices) == 0:
        return np.empty((0,), dtype=np.float64)
    s = dataset.iloc[indices][SCANNER_FIRES_COLUMN].to_numpy(
        dtype=np.float64, copy=True)
    s[np.isnan(s)] = 0.0
    return s


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────

class Trainer:
    """Trains a single model per call to train_one().

    Stateless across calls — every train_one() is fully described by
    its (train_config, assembler_result) inputs."""

    def __init__(self,
                  thinness_thresholds: Optional[ThinnessThresholds]
                    = None):
        self.thinness_thresholds = (thinness_thresholds
                                      or ThinnessThresholds())

    # ── train_one ────────────────────────────────────────────────

    def train_one(self,
                    train_config: TrainConfig,
                    assembler_result: AssemblerResult,
                    ) -> TrainOutputs:
        # ── 0. Config sanity ────────────────────────────────────
        if train_config.model_type not in ALLOWED_MODEL_TYPES:
            raise M18ConfigError(
                f"model_type={train_config.model_type!r} not in "
                f"{sorted(ALLOWED_MODEL_TYPES)}")
        if train_config.train_mode not in ALLOWED_TRAIN_MODES:
            raise M18ConfigError(
                f"train_mode={train_config.train_mode!r} not in "
                f"{sorted(ALLOWED_TRAIN_MODES)}")

        # ── 0a. Model-type implementation scope ─────────────────
        # Reject any model_type that is in ALLOWED_MODEL_TYPES (schema)
        # but has no trainer here. Checked BEFORE the dual-cohort assert
        # so an unimplemented model surfaces the scope error even when
        # the supplied dataset's cohort also mismatches the train_mode.
        if train_config.model_type not in IMPLEMENTED_MODEL_TYPES:
            raise M18ConfigError(
                f"model_type={train_config.model_type!r} is in "
                f"ALLOWED_MODEL_TYPES but not implemented "
                f"(scope: {sorted(IMPLEMENTED_MODEL_TYPES)}). "
                f"Pick one of those.")

        manifest = assembler_result.manifest

        # ── 0b. Dual-cohort consistency (Q18) ──────────────────
        # train_mode is a tag; manifest.anchor_set is the structural
        # cohort identifier set by the assembler. They MUST agree;
        # the trainer does NOT re-filter rows by train_mode. Checked
        # BEFORE the split-is-None check so cohort mismatches surface
        # immediately even when the dataset is also too small to
        # split.
        assert_train_mode_matches_anchor_set(
            train_config.train_mode, manifest.anchor_set)

        if assembler_result.split is None:
            raise InsufficientDataError(
                "AssemblerResult has no walk-forward split; cannot "
                "train (dataset too small or split-time fractions "
                "invalid)")

        dataset  = assembler_result.dataset
        split    = assembler_result.split

        # ── 1. Materialise (X, y) for each split ────────────────
        label_class = get_label_class(train_config.target_label_id)
        feature_columns = select_feature_columns(list(dataset.columns))

        X_train, y_train = extract_xy_for_split(
            dataset, split.train_anchor_indices,
            target_label_id=train_config.target_label_id,
            feature_columns=feature_columns)
        X_val, y_val = extract_xy_for_split(
            dataset, split.val_anchor_indices,
            target_label_id=train_config.target_label_id,
            feature_columns=feature_columns)
        X_test, y_test = extract_xy_for_split(
            dataset, split.test_anchor_indices,
            target_label_id=train_config.target_label_id,
            feature_columns=feature_columns)

        n_train = int(len(y_train))
        n_val   = int(len(y_val))
        n_test  = int(len(y_test))
        n_features = int(len(feature_columns))

        # ── 2. Thinness gates ───────────────────────────────────
        # Skipped entirely when fixture_mode (Q16). When skipped, the
        # report's `passed` stays as N/A — the model is fixture_only
        # anyway and is not promotable.
        if train_config.fixture_mode:
            thinness_status = {
                "skipped":    True,
                "reason":     "train_config.fixture_mode=True (Q16)",
                "thresholds": self.thinness_thresholds.to_dict(),
            }
        else:
            thinness_status = evaluate_thinness(
                y_train=y_train,
                n_val=n_val, n_test=n_test, n_features=n_features,
                label_class=label_class,
                thresholds=self.thinness_thresholds,
            )

        # ── 3. Minority-class diagnostics (binary / 3-way) ───
        minority_count    : Optional[int]   = None
        minority_proportion: Optional[float] = None
        if label_class in ("binary", "classification_3way") and n_train > 0:
            unique, counts = np.unique(y_train, return_counts=True)
            mi = int(counts.min())
            minority_count = mi
            minority_proportion = float(mi) / float(n_train)

        # ── 4. Dispatch to the model-type-specific trainer ──────
        seed = int(train_config.seed)
        if train_config.model_type == "B0_majority":
            inner = MajorityClassTrainer()
            inner.fit(y_train, label_class=label_class, seed=seed)
            pred_train = inner.predict_proba(n_train)
            pred_val   = inner.predict_proba(n_val)
            pred_test  = inner.predict_proba(n_test)
        elif train_config.model_type == "B1_scanner_replica":
            if label_class != "binary":
                raise M18ConfigError(
                    f"B1_scanner_replica supports binary targets only; "
                    f"got label_class={label_class!r}")
            sf_train = _extract_signal_fires(
                dataset, split.train_anchor_indices)
            sf_val   = _extract_signal_fires(
                dataset, split.val_anchor_indices)
            sf_test  = _extract_signal_fires(
                dataset, split.test_anchor_indices)
            inner = ScannerReplicaTrainer()
            inner.fit(sf_train, seed=seed)
            pred_train = inner.predict_proba(sf_train)
            pred_val   = inner.predict_proba(sf_val)
            pred_test  = inner.predict_proba(sf_test)
        elif train_config.model_type == "B2_logistic":
            inner = LogisticRegressionTrainer()
            inner.fit(X_train, y_train,
                       label_class=label_class, seed=seed)
            pred_train = inner.predict_proba(X_train)
            pred_val   = inner.predict_proba(X_val)
            pred_test  = inner.predict_proba(X_test)
        elif train_config.model_type == "M_lightgbm":
            inner = LightGBMTrainer()
            inner.fit(X_train, y_train,
                       label_class=label_class, seed=seed,
                       hyperparameters=dict(
                           train_config.hyperparameters))
            pred_train = inner.predict_proba(X_train)
            pred_val   = inner.predict_proba(X_val)
            pred_test  = inner.predict_proba(X_test)
        elif train_config.model_type == "M_random_forest":
            inner = RandomForestTrainer()
            inner.fit(X_train, y_train,
                       label_class=label_class, seed=seed,
                       hyperparameters=dict(
                           train_config.hyperparameters))
            pred_train = inner.predict_proba(X_train)
            pred_val   = inner.predict_proba(X_val)
            pred_test  = inner.predict_proba(X_test)
        else:
            # Defensive: every entry in IMPLEMENTED_MODEL_TYPES has a
            # branch above, and the 0a guard already rejects anything
            # not implemented. This stays as a belt-and-suspenders.
            raise M18ConfigError(
                f"model_type={train_config.model_type!r} is in "
                f"IMPLEMENTED_MODEL_TYPES but has no train_one dispatch "
                f"branch — this is a bug.")

        # ── 5. Metrics ───────────────────────────────────────────
        if label_class == "binary":
            metrics_train = _binary_metrics(y_train, pred_train)
            metrics_val   = _binary_metrics(y_val,   pred_val)
            metrics_test  = _binary_metrics(y_test,  pred_test)
        else:
            metrics_train = _regression_metrics(y_train, pred_train)
            metrics_val   = _regression_metrics(y_val,   pred_val)
            metrics_test  = _regression_metrics(y_test,  pred_test)

        # ── 6. Promotion gate composition ────────────────────────
        promotion_blocked_reasons: List[str] = []
        # Integrity gates inherited from the dataset manifest —
        # NOT --force-overridable.
        if not manifest.promotion_eligible:
            for r in manifest.promotion_blocked_reasons:
                promotion_blocked_reasons.append(f"dataset:{r}")
        # fixture_only at trainer level — NOT --force-overridable.
        if train_config.fixture_mode:
            if "fixture_only" not in promotion_blocked_reasons:
                promotion_blocked_reasons.append("fixture_only")
        # Thinness — JUDGMENT gates, --force-overridable at M18.A.8.
        if not train_config.fixture_mode:
            for ch in thinness_status.get("failed_checks", []):
                promotion_blocked_reasons.append(f"thinness:{ch}")
        promotion_eligible = (len(promotion_blocked_reasons) == 0)

        fixture_only = bool(manifest.fixture_only
                              or train_config.fixture_mode)

        return TrainOutputs(
            train_config=train_config.to_dict(),
            dataset_id=manifest.dataset_id,
            dataset_hash_sha256=manifest.dataset_hash_sha256,
            dataset_anchor_set=manifest.anchor_set,
            model_type=train_config.model_type,
            train_mode=train_config.train_mode,
            target_label_id=train_config.target_label_id,
            target_label_class=label_class,
            n_train=n_train, n_val=n_val, n_test=n_test,
            n_features=n_features,
            minority_class_count_train=minority_count,
            minority_class_proportion_train=minority_proportion,
            metrics_train=metrics_train,
            metrics_val=metrics_val,
            metrics_test=metrics_test,
            pred_train=pred_train.tolist(),
            pred_val=pred_val.tolist(),
            pred_test=pred_test.tolist(),
            seed=seed,
            library_versions=_python_library_versions(),
            fixture_only=fixture_only,
            promotion_eligible=promotion_eligible,
            promotion_blocked_reasons=promotion_blocked_reasons,
            thinness_status=thinness_status,
        )

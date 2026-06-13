"""bot.ml.models.base — base types for M18.A.6 trainers.

Houses:
  * TrainOutputs       dataclass with every field a trainer reports
  * select_feature_columns()  helper to slice the dataset DataFrame
                                  into the feature matrix that trainers
                                  expect
  * extract_xy_for_split()    helper to materialise (X, y) from an
                                  AssemblerResult + split + target

Design constraints (locked):
  * Trainers fit on TRAIN only; val/test are never touched during fit.
  * Splits come from M18.A.5's WalkForwardSplit — chronological, no
    shuffling. Trainers MUST NOT re-shuffle.
  * Pending labels were already excluded by the assembler before the
    split was built. Trainers can assume every row in every split has
    a non-pending target.
  * Determinism: every trainer accepts an explicit `seed` and is
    pure-deterministic with that seed.

This module is library-agnostic — no sklearn, no lightgbm. Those land
in baselines.py and lightgbm_trainer.py.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.ml.errors import M18ConfigError, InsufficientDataError


# Reserved column suffixes — recognise label aux columns.
LABEL_AUX_SUFFIXES = (
    ".resolved_ts",
    ".is_pending",
    ".bars_to_resolution",
    ".return_log_at_resolution",
)


def select_feature_columns(dataset_columns: List[str]) -> List[str]:
    """Return the list of FEATURE columns in `dataset_columns`.

    A feature column is any column that:
      * appears in some ALL_FEATURE_GROUPS member's SPECS as a
        feature_id, AND
      * is present in the dataset

    Imports ALL_FEATURE_GROUPS locally to avoid pulling sklearn-touching
    modules at module-load time."""
    from bot.ml.features import ALL_FEATURE_GROUPS
    known_feature_ids = {
        s.feature_id
        for g in ALL_FEATURE_GROUPS.values()
        for s in g.SPECS
    }
    return [c for c in dataset_columns if c in known_feature_ids]


def select_label_columns(dataset_columns: List[str]) -> List[str]:
    """Return all label-related columns (labels + their aux columns)."""
    from bot.ml.labels import ALL_LABEL_GROUPS
    known_label_ids = {
        s.label_id
        for g in ALL_LABEL_GROUPS.values()
        for s in g.SPECS
    }
    out = []
    for c in dataset_columns:
        if c in known_label_ids:
            out.append(c)
            continue
        # aux column form: '<label_id>.<suffix>'
        for lid in known_label_ids:
            for sfx in LABEL_AUX_SUFFIXES:
                if c == f"{lid}{sfx}":
                    out.append(c)
                    break
            else:
                continue
            break
    return out


def get_label_class(target_label_id: str) -> str:
    """Look up the label_class for a target_label_id. Raises
    M18ConfigError if the label doesn't exist."""
    from bot.ml.labels import ALL_LABEL_GROUPS
    for g in ALL_LABEL_GROUPS.values():
        for s in g.SPECS:
            if s.label_id == target_label_id:
                return s.label_class
    raise M18ConfigError(
        f"unknown target_label_id {target_label_id!r}; valid ids: "
        f"{sorted(s.label_id for g in ALL_LABEL_GROUPS.values() for s in g.SPECS)}")


def extract_xy_for_split(
    dataset: pd.DataFrame,
    indices: np.ndarray,
    *,
    target_label_id: str,
    feature_columns: List[str],
) -> Tuple[np.ndarray, np.ndarray]:
    """Materialise (X, y) for `indices` from the joined dataset.

    NaN handling (M18.B.5 explicit missingness policy)
    --------------------------------------------------
    Feature NaN is filled per the central policy
    (bot.ml.features.missingness): a deterministic NEUTRAL fill (0.0,
    not data-derived → no leakage). Per-column missingness INDICATORS
    ('<feature>__was_missing', 1.0 where the original cell was NaN) are
    then APPENDED to the feature matrix, so the model actually trains on
    them. The combined matrix is asserted finite (no NaN/inf/object)
    before being returned, so NaN/inf can never reach a model .fit().
    The returned width is len(feature_columns) + number of indicator
    columns, identical across train/val/test for a given feature_columns
    list (indicator set is a deterministic function of feature_columns).
    Target NaN: refuse — should never happen post-assembler-exclusion.
    """
    from bot.ml.features.missingness import (
        apply_missingness_fill, assert_finite_matrix,
        missingness_indicator_names)
    if len(indices) == 0:
        n_model_cols = (len(feature_columns)
                        + len(missingness_indicator_names(feature_columns)))
        return (np.empty((0, n_model_cols), dtype=np.float64),
                np.empty((0,), dtype=np.float64))
    sub = dataset.iloc[indices]
    X = sub[feature_columns].to_numpy(dtype=np.float64, copy=True)
    # Explicit missingness policy: deterministic neutral fill + per-column
    # missingness indicators APPENDED as real model features. Replaces
    # the prior silent blanket `X[np.isnan(X)] = 0.0`.
    X_filled, indicators, _indicator_names = apply_missingness_fill(
        X, feature_columns)
    if indicators.shape[1]:
        X_model = np.column_stack([X_filled, indicators])
    else:
        X_model = X_filled
    # Guard: NaN/inf must never reach the model. inf is NOT filled by
    # the policy (it signals a feature-computation bug), so this raises
    # M18DataError on remaining inf.
    assert_finite_matrix(
        X_model, name="feature matrix (post-missingness)")
    y = sub[target_label_id].to_numpy(dtype=np.float64, copy=True)
    if np.isnan(y).any():
        raise InsufficientDataError(
            f"target label {target_label_id!r} has NaN values at "
            f"{int(np.isnan(y).sum())} positions in the supplied split "
            f"— the assembler should have excluded pending rows "
            f"upstream; this is a bug or a malformed split")
    return X_model, y


@dataclass
class TrainOutputs:
    """Result of training one model on one dataset.

    `to_dict()` produces a JSON-safe representation for the registry
    (M18.A.8). All numeric fields are plain int/float; predictions are
    serialised as Python lists.

    promotion_eligible / promotion_blocked_reasons
    ----------------------------------------------
    A trained model is promotion_eligible iff ALL of:
      * the source dataset's manifest.promotion_eligible is True
      * train_config.fixture_mode is False
      * every thinness gate passes (sample counts, minority class,
        and feature-sample ratio)

    Reasons are namespaced:
      "dataset:<reason>"   reason inherited from the dataset manifest
                            (NOT --force-overridable — integrity gate)
      "fixture_only"       train_config.fixture_mode is True
                            (NOT --force-overridable per locked Q16)
      "thinness:<reason>"  judgment gate; --force-overridable at the
                            M18.A.8 promotion layer (NOT in M18.A.6)
    """
    # Training configuration & source identity
    train_config: Dict[str, Any]
    dataset_id: str
    dataset_hash_sha256: str
    dataset_anchor_set: str       # structural cohort, from manifest
    model_type: str
    train_mode: str
    target_label_id: str
    target_label_class: str

    # Sample sizes
    n_train: int
    n_val: int
    n_test: int
    n_features: int

    # Binary-only diagnostics (None for regression)
    minority_class_count_train: Optional[int]
    minority_class_proportion_train: Optional[float]

    # Metrics per split
    metrics_train: Dict[str, float]
    metrics_val: Dict[str, float]
    metrics_test: Dict[str, float]

    # Predictions (binary: positive-class proba; regression: yhat)
    pred_train: List[float]
    pred_val: List[float]
    pred_test: List[float]

    # Determinism / provenance
    seed: int
    library_versions: Dict[str, str]

    # Tagging + promotion gate
    fixture_only: bool
    promotion_eligible: bool
    promotion_blocked_reasons: List[str]

    # Thinness diagnostics
    thinness_status: Dict[str, Any]

    # SR-8 reproducibility hash (M18.B.2). Backward-compatible: defaults
    # to None so older serialised TrainOutputs / registry entries that
    # predate v2 still construct. Populated by Trainer.train_one().
    repro_hash_v2: Optional[str] = None

    # M18.B.4 — strict production-promotion thinness profile. Backward-
    # compatible (default_factory). Distinct from the weak trainability
    # `thinness_status` above: this is evaluated even for fixture/cold-
    # start models and its failures are INTEGRITY gates (force cannot
    # override) at the registry promotion layer.
    production_thinness_status: Dict[str, Any] = field(
        default_factory=dict)

    # M18.B.5 — explicit missingness policy provenance, surfaced from the
    # dataset manifest so the model output records which policy cleaned
    # its features. Backward-compatible (default "" / default_factory).
    missingness_policy_hash: str = ""
    missingness_report: Dict[str, Any] = field(default_factory=dict)

    # M18.B.5 — model-feature breakdown. The model matrix is base
    # features + appended missingness indicators, so n_features (above)
    # is the ACTUAL model width. These record the split. Backward-
    # compatible defaults.
    base_feature_count: int = 0
    missingness_indicator_count: int = 0
    model_feature_count: int = 0
    missingness_indicator_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

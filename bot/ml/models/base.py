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

    NaN handling
    ------------
    Feature NaN: replaced with 0.0 (LR / LightGBM behaviour). NOT a
    silent-imputation trick on real data — features near warmup are
    expected to have NaN, and the assembler's pending exclusion has
    already handled label-side NaN.
    Target NaN: refuse — should never happen post-assembler-exclusion.
    """
    if len(indices) == 0:
        return (np.empty((0, len(feature_columns)), dtype=np.float64),
                np.empty((0,), dtype=np.float64))
    sub = dataset.iloc[indices]
    X = sub[feature_columns].to_numpy(dtype=np.float64, copy=True)
    # Replace feature NaN with 0 for compatibility with LR/LightGBM
    X[np.isnan(X)] = 0.0
    y = sub[target_label_id].to_numpy(dtype=np.float64, copy=True)
    if np.isnan(y).any():
        raise InsufficientDataError(
            f"target label {target_label_id!r} has NaN values at "
            f"{int(np.isnan(y).sum())} positions in the supplied split "
            f"— the assembler should have excluded pending rows "
            f"upstream; this is a bug or a malformed split")
    return X, y


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

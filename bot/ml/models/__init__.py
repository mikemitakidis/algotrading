"""bot.ml.models — model trainers and the M18.A.6 Trainer orchestrator.

Public surface:
  Trainer                     orchestrator (train_one)
  TrainOutputs                dataclass result
  ThinnessThresholds          configurable judgment-gate thresholds
  evaluate_thinness           pure thinness assessment function

  MajorityClassTrainer        B0_majority
  ScannerReplicaTrainer       B1_scanner_replica
  LogisticRegressionTrainer   B2_logistic
  LightGBMTrainer             M_lightgbm (import-gated)
  is_lightgbm_available       True iff lightgbm is importable
  RandomForestTrainer         M_random_forest (sklearn-only; M18.B.1)

Each trainer's `model_type` attribute matches the corresponding entry
in bot.ml.schemas.ALLOWED_MODEL_TYPES.

M_random_forest (M18.B.1) is a sklearn-only tree model that trains when
explicitly requested via TrainConfig.model_type == "M_random_forest".
It is NOT a silent automatic fallback and never replaces M_lightgbm on
its own.
"""
from __future__ import annotations

from bot.ml.models.base import (
    TrainOutputs,
    select_feature_columns,
    select_label_columns,
    get_label_class,
    extract_xy_for_split,
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
from bot.ml.models.trainer import (
    Trainer,
    SCANNER_FIRES_COLUMN,
    TRAIN_MODE_TO_ANCHOR_SET,
    ANCHOR_SET_TO_TRAIN_MODE,
    assert_train_mode_matches_anchor_set,
)

__all__ = [
    "Trainer",
    "TrainOutputs",
    "ThinnessThresholds",
    "evaluate_thinness",
    "MajorityClassTrainer",
    "ScannerReplicaTrainer",
    "LogisticRegressionTrainer",
    "LightGBMTrainer",
    "is_lightgbm_available",
    "RandomForestTrainer",
    "SCANNER_FIRES_COLUMN",
    "TRAIN_MODE_TO_ANCHOR_SET",
    "ANCHOR_SET_TO_TRAIN_MODE",
    "assert_train_mode_matches_anchor_set",
    "select_feature_columns",
    "select_label_columns",
    "get_label_class",
    "extract_xy_for_split",
]

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

Each trainer's `model_type` attribute matches the corresponding entry
in bot.ml.schemas.ALLOWED_MODEL_TYPES.

M_random_forest is in ALLOWED_MODEL_TYPES but is NOT implemented in
the M18.A.6 scope. Requesting it via TrainConfig raises M18ConfigError
at Trainer.train_one() time.
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
    "SCANNER_FIRES_COLUMN",
    "TRAIN_MODE_TO_ANCHOR_SET",
    "ANCHOR_SET_TO_TRAIN_MODE",
    "assert_train_mode_matches_anchor_set",
    "select_feature_columns",
    "select_label_columns",
    "get_label_class",
    "extract_xy_for_split",
]

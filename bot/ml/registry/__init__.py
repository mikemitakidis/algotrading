"""bot.ml.registry — M18.A.8 file-based model registry + read-only
predictions.

Public surface:
  Registry                          main orchestrator
  RegistryEntry                     on-disk record
  REGISTRY_ENTRY_SCHEMA_VERSION
  ALWAYS_FALSE_APPROVED_FOR_LIVE    invariant constant
  compute_model_id(train_outputs)   deterministic primary key
  infer_initial_status(train_outputs, evaluation_report)
                                     status from gates
  predict_from_registry(...)         read-only predict
  PredictionResult                   batch result dataclass

  is_integrity_gate(reason)         Q17 classifier
  is_judgment_gate(reason)
  classify_reason(reason)
  matches_override_gate(reason, gate)
  split_reasons(reasons)
  INTEGRITY_GATE_REASONS / JUDGMENT_GATE_NAMES

  make_scope_key(...)               namespace for 'current' pointers
  DATA_ML_ROOT_DEFAULT              default = "data/ml"

This module NEVER writes to signals.db. All persistence is file-based
under data/ml/ (gitignored by the project's blanket `data/` rule).
"""
from __future__ import annotations

from bot.ml.registry.entry import (
    RegistryEntry,
    REGISTRY_ENTRY_SCHEMA_VERSION,
    ALWAYS_FALSE_APPROVED_FOR_LIVE,
    compute_model_id,
    infer_initial_status,
)
from bot.ml.registry.gates import (
    INTEGRITY_GATE_REASONS,
    JUDGMENT_GATE_NAMES,
    JUDGMENT_GATE_REASON_PREFIXES,
    is_integrity_gate,
    is_judgment_gate,
    classify_reason,
    matches_override_gate,
    split_reasons,
)
from bot.ml.registry.storage import (
    DATA_ML_ROOT_DEFAULT,
    make_scope_key,
    entry_path,
    artifact_dir,
    artifact_path,
    current_pointer_path,
    current_history_path,
    predictions_dir,
    ARTIFACT_TRAIN_OUTPUTS,
    ARTIFACT_EVAL_REPORT,
    ARTIFACT_FEATURE_SUMMARY,
    ARTIFACT_X_TRAIN,
    ARTIFACT_Y_TRAIN,
    ARTIFACT_TRAINING_META,
)
from bot.ml.registry.registry import Registry
from bot.ml.registry.predictions import (
    predict_from_registry,
    PredictionResult,
)

__all__ = [
    "Registry",
    "RegistryEntry",
    "REGISTRY_ENTRY_SCHEMA_VERSION",
    "ALWAYS_FALSE_APPROVED_FOR_LIVE",
    "compute_model_id",
    "infer_initial_status",
    "predict_from_registry",
    "PredictionResult",
    "is_integrity_gate",
    "is_judgment_gate",
    "classify_reason",
    "matches_override_gate",
    "split_reasons",
    "INTEGRITY_GATE_REASONS",
    "JUDGMENT_GATE_NAMES",
    "JUDGMENT_GATE_REASON_PREFIXES",
    "make_scope_key",
    "DATA_ML_ROOT_DEFAULT",
    "entry_path",
    "artifact_dir",
    "artifact_path",
    "current_pointer_path",
    "current_history_path",
    "predictions_dir",
    "ARTIFACT_TRAIN_OUTPUTS",
    "ARTIFACT_EVAL_REPORT",
    "ARTIFACT_FEATURE_SUMMARY",
    "ARTIFACT_X_TRAIN",
    "ARTIFACT_Y_TRAIN",
    "ARTIFACT_TRAINING_META",
]

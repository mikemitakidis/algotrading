"""bot.ml.registry.storage — file-system layout and atomic I/O.

Storage layout (root = data/ml/):

  data/ml/
    registry/
      {model_id}.json                      RegistryEntry serialised
    artifacts/
      {model_id}/
        train_outputs.json
        evaluation_report.json
        training_feature_summary.json      per-feature min/max/mean/std
        training_X.parquet                  feature matrix used at fit
        training_y.parquet                  target vector used at fit
        training_metadata.json              feature_columns, label info
    current/
      {scope_key}.json                      pointer file: {"model_id": "..."}
    current_history.jsonl                   append-only transition log
    predictions/
      {model_id}/
        predictions__{batch_id}.parquet

Atomic writes:
  Every JSON write goes through `atomic_write_json()` which writes
  to a .tmp file then renames. Same for parquet via pandas. This
  matches the project's existing M16 atomic-rename pattern.

Path conventions:
  All paths returned from this module are RELATIVE to data/ml/. The
  registry record stores relative paths so registries are portable
  across machines.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import numpy as np
import pandas as pd


# Layout constants
DATA_ML_ROOT_DEFAULT = "data/ml"
REGISTRY_SUBDIR = "registry"
ARTIFACTS_SUBDIR = "artifacts"
CURRENT_SUBDIR = "current"
PREDICTIONS_SUBDIR = "predictions"
CURRENT_HISTORY_FILE = "current_history.jsonl"

# Artifact filenames within data/ml/artifacts/{model_id}/
ARTIFACT_TRAIN_OUTPUTS = "train_outputs.json"
ARTIFACT_EVAL_REPORT   = "evaluation_report.json"
ARTIFACT_FEATURE_SUMMARY = "training_feature_summary.json"
ARTIFACT_X_TRAIN       = "training_X.parquet"
ARTIFACT_Y_TRAIN       = "training_y.parquet"
ARTIFACT_TRAINING_META = "training_metadata.json"


# ─────────────────────────────────────────────────────────────────────
# Atomic writes
# ─────────────────────────────────────────────────────────────────────

def atomic_write_json(path: Path, payload: Any) -> None:
    """Write `payload` as JSON to `path` atomically (write tmp →
    rename). Creates parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, indent=2, default=_json_default)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append `record` as one JSON line to `path`, creating the
    file + parent dir if needed. Append is not atomic across
    processes, but for the registry's single-writer use case it's
    fine; for safety the line is built in memory then written +
    fsynced in one open() call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=_json_default) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _json_default(o: Any) -> Any:
    """Serialise numpy / pandas types JSON doesn't know about."""
    if isinstance(o, (np.integer,)):  return int(o)
    if isinstance(o, (np.floating,)): return float(o)
    if isinstance(o, (np.ndarray,)):  return o.tolist()
    if isinstance(o, pd.Timestamp):   return o.isoformat()
    raise TypeError(f"{type(o).__name__!r} not JSON-serialisable")


def atomic_write_parquet(path: Path, df: pd.DataFrame) -> None:
    """Write `df` as parquet to `path` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────

def entry_path(root: Path, model_id: str) -> Path:
    return root / REGISTRY_SUBDIR / f"{model_id}.json"


def artifact_dir(root: Path, model_id: str) -> Path:
    return root / ARTIFACTS_SUBDIR / model_id


def artifact_path(
    root: Path, model_id: str, artifact_name: str,
) -> Path:
    return artifact_dir(root, model_id) / artifact_name


def current_pointer_path(root: Path, scope_key: str) -> Path:
    return root / CURRENT_SUBDIR / f"{scope_key}.json"


def current_history_path(root: Path) -> Path:
    return root / CURRENT_HISTORY_FILE


def predictions_dir(root: Path, model_id: str) -> Path:
    return root / PREDICTIONS_SUBDIR / model_id


def make_scope_key(
    *,
    dataset_anchor_set: str,
    train_mode:          str,
    target_label_id:     str,
    model_type:          str,
) -> str:
    """Compose the scope_key used to namespace 'current' pointers.

    One 'current' pointer per (anchor_set, train_mode, target_label,
    model_type) combination — different model_types can each have
    their own 'current'.
    """
    return (f"{dataset_anchor_set}__"
            f"{train_mode}__"
            f"{target_label_id}__"
            f"{model_type}")


# ─────────────────────────────────────────────────────────────────────
# Iteration helpers
# ─────────────────────────────────────────────────────────────────────

def iter_entry_paths(root: Path) -> Iterator[Path]:
    """Yield every `{model_id}.json` file under registry/."""
    d = root / REGISTRY_SUBDIR
    if not d.exists():
        return
    for p in sorted(d.glob("*.json")):
        yield p


def read_current_history(root: Path) -> List[Dict[str, Any]]:
    """Read every line of current_history.jsonl in append order."""
    p = current_history_path(root)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out

"""bot.ml.registry.predictions — read-only prediction execution.

Public surface:

  predict_from_registry(
      *,
      registry, model_id,
      X_input: pd.DataFrame,
      ts_utc: Optional[pd.Series] = None,
      batch_id: Optional[str]      = None,
      list_extrapolated_features:  bool = False,   # no-op (back-compat)
  ) → PredictionResult

Q20 LOCKED prediction-row schema — ALWAYS present:
  model_id                       str    every row carries the model_id
  prediction                     float  probability of class 1
  predicted_class                int    0/1 at threshold 0.5
  feature_extrapolation_flags    list   names of features outside the
                                          training [q01, q99] envelope
                                          for that row (empty list when none)
  feature_extrapolation_count    int    == len(feature_extrapolation_flags)

Backwards-compatible aliases (also always present so older callers
work unchanged):
  pred_proba                     float  alias of `prediction`
  pred_class                     int    alias of `predicted_class`
  feature_extrapolation_flag     bool   convenience flag, == count > 0
  features_out_of_range          list   alias of `feature_extrapolation_flags`

If `ts_utc` is supplied, it is prepended as the first column.

A `PredictionResult` also carries:
  output_path: str                 absolute path of the written parquet
  batch_id:    str
  predicted_at_utc: str
  extrapolation_summary: Dict      aggregate row counters
  n_input_rows, n_features

Refit-based prediction (no pickled models):
  At predict time, we load training_X.parquet + training_y.parquet
  from the registry's artifact dir, instantiate the matching trainer
  class, refit deterministically with the same seed, then call
  predict_proba on `X_input`. This avoids joblib/sklearn version
  fragility and lets the registry stay portable.

Read-only:
  This module NEVER writes to signals.db, NEVER mutates the
  registry, and NEVER updates current. It only writes the
  predictions parquet file under data/ml/predictions/{model_id}/.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.ml.errors import M18ConfigError
from bot.ml.registry import storage as _store
from bot.ml.registry.entry import RegistryEntry


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds")


@dataclass
class PredictionResult:
    """Per-prediction batch result."""
    model_id:           str
    n_input_rows:       int
    n_features:         int
    output_path:        str          # absolute path
    batch_id:           str
    predicted_at_utc:   str
    extrapolation_summary: Dict[str, Any]  # aggregate counters
    predictions:        pd.DataFrame


# ─────────────────────────────────────────────────────────────────────
# Refit one model from a registry entry
# ─────────────────────────────────────────────────────────────────────

def _refit_model(
    entry: RegistryEntry, root: Path,
) -> Tuple[Any, List[str], List[str]]:
    """Refit the model deterministically from the artifacts.

    Returns (fitted_model, base_feature_columns, model_feature_columns).
    base_feature_columns are the raw dataset columns a caller must
    supply at predict time; model_feature_columns are base + appended
    missingness indicators (the actual fit width).

    For B0_majority: emits a constant proba (no input features).
    For B1_scanner_replica: passthrough on scanner_replica.signal_fires.
    For B2_logistic: refits sklearn LR with the saved seed.
    For M_lightgbm: refits LightGBM if installed; else raises.
    """
    # Load training metadata + X/y
    meta = _store.read_json(
        root / entry.training_metadata_path)
    # M18.B.5: training_X.parquet now stores the MODEL matrix (base +
    # missingness indicators). meta["feature_columns"] is the model
    # column list; base_feature_columns is the raw-input subset.
    model_feature_columns = list(meta["feature_columns"])
    base_feature_columns = list(meta.get(
        "base_feature_columns", model_feature_columns))
    X_train_df = pd.read_parquet(root / entry.training_X_path)
    y_train = pd.read_parquet(
        root / entry.training_y_path)[entry.target_label_id]\
        .to_numpy(dtype=np.float64)
    # The persisted model matrix is already filled + indicator-appended
    # (no NaN), so select the model columns directly.
    X_train = X_train_df[model_feature_columns].to_numpy(
        dtype=np.float64, copy=True)

    mt = entry.model_type
    seed = int(entry.seed)
    label_class = entry.target_label_class

    if mt == "B0_majority":
        from bot.ml.models.baselines import MajorityClassTrainer
        m = MajorityClassTrainer()
        m.fit(y_train, label_class=label_class, seed=seed)
        return m, base_feature_columns, model_feature_columns

    if mt == "B1_scanner_replica":
        from bot.ml.models.baselines import ScannerReplicaTrainer
        # B1 fits on the signal_fires column only (passthrough);
        # the .fit() call doesn't actually train, it just records
        # the column.
        if "scanner_replica.signal_fires" in X_train_df.columns:
            fires = X_train_df["scanner_replica.signal_fires"]\
                .to_numpy(dtype=np.float64)
            fires[np.isnan(fires)] = 0.0
        else:
            fires = np.zeros(len(X_train_df), dtype=np.float64)
        m = ScannerReplicaTrainer()
        m.fit(fires, label_class=label_class, seed=seed)
        return m, base_feature_columns, model_feature_columns

    if mt == "B2_logistic":
        from bot.ml.models.baselines import LogisticRegressionTrainer
        m = LogisticRegressionTrainer()
        m.fit(X_train, y_train, label_class=label_class, seed=seed)
        return m, base_feature_columns, model_feature_columns

    if mt == "M_lightgbm":
        from bot.ml.models.lightgbm_trainer import (
            LightGBMTrainer, is_lightgbm_available)
        if not is_lightgbm_available():
            raise M18ConfigError(
                f"model_type=M_lightgbm requires lightgbm to be "
                f"installed; predict refused")
        m = LightGBMTrainer()
        # Read hyperparams from training metadata for full
        # determinism. If not stored, use the locked defaults.
        # (M18.A.6 trainer stores them in train_config.)
        m.fit(X_train, y_train, label_class=label_class, seed=seed)
        return m, base_feature_columns, model_feature_columns

    raise M18ConfigError(
        f"model_type={mt!r} is in the registry but predict refit is "
        f"not implemented for it")


# ─────────────────────────────────────────────────────────────────────
# Extrapolation tracking
# ─────────────────────────────────────────────────────────────────────

def _compute_extrapolation(
    X_input_df:  pd.DataFrame,
    feature_summary: Dict[str, Dict[str, float]],
    feature_columns: List[str],
) -> Tuple[np.ndarray, np.ndarray, List[List[str]]]:
    """For each row of X_input_df, compute:
      - extrapolation_count: number of features outside the training
        [q01, q99] envelope (Q20 LOCK — NOT [min, max])
      - extrapolation_flag:  True iff any feature is outside [q01, q99]
      - features_out_of_range_per_row: list of feature names per row

    Q20 envelope rationale:
      Using [min, max] makes the envelope very brittle on a single
      training outlier — one extreme historical observation pushes
      the envelope wide and hides genuine extrapolation. The locked
      Q20 rule uses [1st percentile, 99th percentile] which is
      robust to the most extreme 2% of training observations.
      min/max are still kept in the summary for context but are
      NOT the envelope.

    NaN handling:
      NaN values in X_input do NOT count as extrapolated — they're a
      separate phenomenon. The summary's q01/q99 are computed from
      finite values only.
    """
    n_rows = len(X_input_df)
    counts = np.zeros(n_rows, dtype=np.int64)
    rowwise_feats: List[List[str]] = [[] for _ in range(n_rows)]
    for c in feature_columns:
        if c not in feature_summary:
            continue
        summ = feature_summary[c]
        lo = summ.get("q01")
        hi = summ.get("q99")
        if lo is None or hi is None:
            # Older artifacts without q01/q99 would fall through to
            # min/max — but Q20 forbids this, so refuse.
            raise M18ConfigError(
                f"training_feature_summary for feature {c!r} is "
                f"missing q01/q99 — incompatible with Q20-locked "
                f"extrapolation envelope. Re-register the model.")
        if not (np.isfinite(lo) and np.isfinite(hi)):
            continue
        vals = X_input_df[c].to_numpy(dtype=np.float64)
        finite = np.isfinite(vals)
        below  = finite & (vals < lo)
        above  = finite & (vals > hi)
        out    = below | above
        counts += out.astype(np.int64)
        for idx in np.where(out)[0]:
            rowwise_feats[idx].append(c)
    flags = counts > 0
    return counts, flags, rowwise_feats


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────

def predict_from_registry(
    *,
    registry,
    model_id: str,
    X_input: pd.DataFrame,
    ts_utc: Optional[pd.Series] = None,
    batch_id: Optional[str]     = None,
    list_extrapolated_features: bool = False,
    write_output: bool          = True,
) -> PredictionResult:
    """Run read-only predictions for `X_input` using `model_id`.

    Loads the registry entry, refits deterministically from artifacts,
    runs predict_proba, computes per-row feature extrapolation flags,
    and writes the resulting DataFrame to
    `data/ml/predictions/{model_id}/predictions__{batch_id}.parquet`.

    Returns a PredictionResult containing the predictions DataFrame
    and the output file path.
    """
    entry = registry.get_entry(model_id)
    root  = registry.root

    # Load feature_summary and refit
    feature_summary = _store.read_json(
        root / entry.training_feature_summary_path)
    model, base_feature_columns, model_feature_columns = _refit_model(
        entry, root)

    # M18.B.5: the caller supplies BASE feature columns; the missingness
    # indicators are DERIVED here (identical policy as training) so the
    # model receives base + indicators. Validate base columns only.
    missing = [c for c in base_feature_columns
               if c not in X_input.columns]
    if missing:
        raise M18ConfigError(
            f"X_input is missing {len(missing)} base feature columns "
            f"that were used at training time: {missing[:5]}"
            f"{'...' if len(missing) > 5 else ''}")

    X_base_df = X_input[base_feature_columns].reset_index(drop=True)
    X_base_arr = X_base_df.to_numpy(dtype=np.float64, copy=True)
    # Derive the model matrix exactly as extract_xy_for_split does:
    # neutral fill + appended indicators (deterministic, same order).
    from bot.ml.features.missingness import (
        apply_missingness_fill, assert_finite_matrix)
    X_filled, indicators, _ind_names = apply_missingness_fill(
        X_base_arr, base_feature_columns)
    if indicators.shape[1]:
        X_arr = np.column_stack([X_filled, indicators])
    else:
        X_arr = X_filled
    assert_finite_matrix(X_arr, name="predict feature matrix")
    # Extrapolation is computed on the BASE features only (indicators
    # are 0/1 flags, not continuous values with a [q01,q99] envelope).
    X_df_aligned = X_base_df

    # Predict — dispatch on model_type because B0/B1 have specialised
    # predict_proba signatures (B0 takes n_rows, B1 takes signal_fires)
    mt = entry.model_type
    if mt == "B0_majority":
        proba = np.asarray(model.predict_proba(len(X_arr)),
                            dtype=np.float64)
    elif mt == "B1_scanner_replica":
        if "scanner_replica.signal_fires" in X_df_aligned.columns:
            fires = X_df_aligned["scanner_replica.signal_fires"]\
                .to_numpy(dtype=np.float64)
            fires[np.isnan(fires)] = 0.0
        else:
            fires = np.zeros(len(X_df_aligned), dtype=np.float64)
        proba = np.asarray(model.predict_proba(fires),
                            dtype=np.float64)
    else:
        proba = np.asarray(model.predict_proba(X_arr),
                            dtype=np.float64)

    # Defensive flatten in case predict_proba returns (n,) or (n,2)
    if proba.ndim == 2:
        if proba.shape[1] == 2:
            proba = proba[:, 1]
        elif proba.shape[1] == 1:
            proba = proba[:, 0]
        else:
            raise M18ConfigError(
                f"predict_proba returned shape {proba.shape}; "
                f"expected (n,) or (n, 2)")

    # Extrapolation
    counts, flags, rowwise = _compute_extrapolation(
        X_df_aligned, feature_summary, base_feature_columns)

    # ─── Build output frame ─────────────────────────────────────────
    # Q20 LOCKED prediction-row schema:
    #   model_id                       str    every row carries the
    #                                            model_id
    #   prediction                     float  probability of class 1
    #   predicted_class                int    0/1 at threshold 0.5
    #   feature_extrapolation_flags    list   names of features
    #                                            outside [q01, q99]
    #                                            for that row;
    #                                            empty list when none
    #   feature_extrapolation_count    int    == len(feature_extrapolation_flags)
    #
    # Backwards-compatible aliases (kept so callers written against
    # the prior schema continue to work):
    #   pred_proba                     alias of `prediction`
    #   pred_class                     alias of `predicted_class`
    #   feature_extrapolation_flag     bool   == count > 0
    #                                            (singular convenience flag)
    #   features_out_of_range          alias of `feature_extrapolation_flags`
    #                                            (kept under the prior name)
    n_rows = len(X_arr)
    pred_class = (proba >= 0.5).astype(np.int8)
    flags_list = [list(f) for f in rowwise]

    out = pd.DataFrame({
        "model_id":                     [model_id] * n_rows,
        # Q20 locked names ─────────────────────────────────────────
        "prediction":                   proba,
        "predicted_class":              pred_class,
        "feature_extrapolation_flags":  flags_list,
        "feature_extrapolation_count":  counts,
        # Backwards-compatible aliases ─────────────────────────────
        "pred_proba":                   proba,
        "pred_class":                   pred_class,
        "feature_extrapolation_flag":   flags.astype(bool),
        "features_out_of_range":        flags_list,
    })
    if ts_utc is not None:
        out.insert(0, "ts_utc",
            pd.to_datetime(ts_utc, utc=True)
                .reset_index(drop=True).astype(str))
    # `list_extrapolated_features` is now a no-op kept for back-
    # compat: `feature_extrapolation_flags` and `features_out_of_range`
    # are ALWAYS present per Q20.

    # Write parquet
    bid = batch_id or _utc_now_iso().replace(":", "-").replace("+", "Z")
    out_path = (_store.predictions_dir(root, model_id)
                  / f"predictions__{bid}.parquet")
    if write_output:
        _store.atomic_write_parquet(out_path, out)

    # Aggregate summary
    extrap_summary = {
        "n_rows":                       n_rows,
        "n_extrapolated_rows":          int(flags.sum()),
        "fraction_extrapolated_rows":   (float(flags.mean())
                                           if n_rows > 0 else 0.0),
        "mean_extrapolation_count_per_row": (float(counts.mean())
                                               if n_rows > 0 else 0.0),
        "max_extrapolation_count":      int(counts.max()) if n_rows > 0 else 0,
    }

    return PredictionResult(
        model_id=model_id,
        n_input_rows=n_rows,
        n_features=len(model_feature_columns),
        output_path=str(out_path),
        batch_id=bid,
        predicted_at_utc=_utc_now_iso(),
        extrapolation_summary=extrap_summary,
        predictions=out,
    )

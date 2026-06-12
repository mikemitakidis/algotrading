"""bot.ml.evaluation.evaluator — main M18.A.7 entry points.

Three public functions:

  evaluate_model(train_outputs, assembler_result)
    → EvaluationReport. Combines TrainOutputs and AssemblerResult
      into a fully-self-contained report including cohort accounting,
      split provenance, embargo/purge, ML metrics, calibration, and
      trading metrics.

  compare_baselines(reports, primary_metric, primary_split)
    → BaselineComparisonReport. ENFORCES that all input reports
      share dataset_id AND dataset_anchor_set — row-paired comparison
      is meaningful only on the SAME cohort.

  compare_across_cohorts(a_report, b_report, primary_split)
    → CrossCohortComparisonReport. EXPLICITLY non-row-paired.
      Raises M18ConfigError if the two reports have the same
      dataset_anchor_set (in which case compare_baselines is the
      right function).

Locked contract — Model A vs Model B comparisons:
  Per the operator's M18.A.7 directive, Model A and Model B must
  NEVER be compared as if they have identical train/val/test rows.
  compare_baselines() enforces same-cohort; compare_across_cohorts()
  enforces different-cohort and attaches a built-in disclaimer.

No registry side-effects in this module. Promotion lives in M18.A.8.
"""
from __future__ import annotations

import datetime as _dt
import platform
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.ml.errors import M18ConfigError
from bot.ml.models.base import TrainOutputs
from bot.ml.dataset.assembler import AssemblerResult
from bot.ml.evaluation.calibration import calibration_report
from bot.ml.evaluation.trading_metrics import trading_metrics
from bot.ml.evaluation.ml_metrics import binary_metrics_extended
from bot.ml.evaluation.threshold_metrics import threshold_table
from bot.ml.evaluation.drift import drift_report
from bot.ml.evaluation.permutation_importance import (
    permutation_importance,
)
from bot.ml.evaluation.breakdowns import all_breakdowns
from bot.ml.evaluation.report import (
    EvaluationReport,
    BaselineComparisonReport,
    CrossCohortComparisonReport,
    EVALUATION_REPORT_SCHEMA_VERSION,
    BASELINE_COMPARISON_REPORT_SCHEMA_VERSION,
    CROSS_COHORT_COMPARISON_REPORT_SCHEMA_VERSION,
    CROSS_COHORT_DISCLAIMER,
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _utc_now_isoformat() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds")


def _ts_range_for_indices(
    dataset: pd.DataFrame, indices: np.ndarray,
) -> Dict[str, Optional[str]]:
    """First / last anchor timestamp for a split, as ISO 8601 UTC.

    Empty split → both None. NaT defensively handled."""
    if indices is None or len(indices) == 0:
        return {"first": None, "last": None, "count": 0}
    sub = dataset.iloc[indices]
    ts = sub["ts_utc"]
    if len(ts) == 0:
        return {"first": None, "last": None, "count": 0}
    first = ts.min()
    last  = ts.max()
    return {
        "first": (None if pd.isna(first)
                   else pd.Timestamp(first).isoformat()),
        "last":  (None if pd.isna(last)
                   else pd.Timestamp(last).isoformat()),
        "count": int(len(indices)),
    }


def _build_cohort_block(manifest) -> Dict[str, Any]:
    """Extract the cohort-accounting fields from the manifest."""
    return {
        "symbol":                     manifest.symbol,
        "anchor_set":                 manifest.anchor_set,
        "requested_anchor_tf":        manifest.requested_anchor_tf,
        "actual_anchor_tf":           manifest.actual_anchor_tf,
        "anchor_count_raw":           manifest.anchor_count_raw,
        "anchor_count_pending_excluded":
            manifest.anchor_count_pending_excluded,
        "anchor_count_total":         manifest.anchor_count_total,
        "anchor_count_train":         manifest.anchor_count_train,
        "anchor_count_val":           manifest.anchor_count_val,
        "anchor_count_test":          manifest.anchor_count_test,
        "anchor_count_purged":        manifest.anchor_count_purged,
        "anchor_count_embargoed":     manifest.anchor_count_embargoed,
        "fixture_mode_invocation":    manifest.fixture_mode_invocation,
        "fixture_only":               manifest.fixture_only,
        "coverage_degraded":          manifest.coverage_degraded,
    }


def _build_split_block(manifest) -> Dict[str, Any]:
    """Extract split provenance — fractions + embargo/purge."""
    wf = dict(manifest.walk_forward)
    return {
        "train_frac":                wf.get("train_frac"),
        "val_frac":                  wf.get("val_frac"),
        "test_frac":                 wf.get("test_frac"),
        "embargo_bars":              wf.get("embargo_bars"),
        "embargo_trading_days":      wf.get("embargo_trading_days"),
        "label_resolved_ts_purge_applied":
            wf.get("label_resolved_ts_purge_applied"),
        "split_built":               wf.get("split_built"),
    }


# ─────────────────────────────────────────────────────────────────────
# evaluate_model
# ─────────────────────────────────────────────────────────────────────

def evaluate_model(
    train_outputs: TrainOutputs,
    assembler_result: AssemblerResult,
    *,
    n_calibration_bins: int = 10,
    cost_per_trade_log_return: float = 0.001,
    permutation_n_repeats: int = 5,
    permutation_n_top: int = 20,
    breakdowns_min_samples: int = 50,
) -> EvaluationReport:
    """Build a complete EvaluationReport for one trained model.

    Verifies that the train_outputs and assembler_result reference
    the same dataset — if dataset_id or dataset_hash_sha256 differ,
    raises M18ConfigError (otherwise trading metrics would be
    computed against the wrong rows).
    """
    manifest = assembler_result.manifest
    split    = assembler_result.split
    dataset  = assembler_result.dataset

    # Provenance sanity: train_outputs must reference THIS dataset
    if train_outputs.dataset_id != manifest.dataset_id:
        raise M18ConfigError(
            f"train_outputs.dataset_id={train_outputs.dataset_id!r} "
            f"does not match assembler_result.manifest.dataset_id="
            f"{manifest.dataset_id!r}; cannot evaluate against a "
            f"different dataset (trading metrics would index into "
            f"the wrong rows)")
    if (train_outputs.dataset_hash_sha256
            != manifest.dataset_hash_sha256):
        raise M18ConfigError(
            f"dataset hash mismatch between train_outputs "
            f"({train_outputs.dataset_hash_sha256[:8]}…) and "
            f"assembler_result.manifest "
            f"({manifest.dataset_hash_sha256[:8]}…); the dataset "
            f"appears to have been rebuilt since training")
    if split is None:
        raise M18ConfigError(
            "AssemblerResult.split is None; evaluator cannot index "
            "into per-split rows")

    # Materialise per-split y_true and y_proba
    target = train_outputs.target_label_id
    def _y_true(indices):
        if len(indices) == 0:
            return np.empty((0,), dtype=np.float64)
        return dataset.iloc[indices][target]\
            .to_numpy(dtype=np.float64)

    y_true_train = _y_true(split.train_anchor_indices)
    y_true_val   = _y_true(split.val_anchor_indices)
    y_true_test  = _y_true(split.test_anchor_indices)

    y_proba_train = np.asarray(train_outputs.pred_train,
                                 dtype=np.float64)
    y_proba_val   = np.asarray(train_outputs.pred_val,
                                 dtype=np.float64)
    y_proba_test  = np.asarray(train_outputs.pred_test,
                                 dtype=np.float64)

    # Calibration per split (binary only — guard for label class)
    if train_outputs.target_label_class == "binary":
        calibration = {
            "train": calibration_report(y_true_train, y_proba_train,
                                          n_bins=n_calibration_bins),
            "val":   calibration_report(y_true_val,   y_proba_val,
                                          n_bins=n_calibration_bins),
            "test":  calibration_report(y_true_test,  y_proba_test,
                                          n_bins=n_calibration_bins),
        }
    else:
        calibration = {
            "train": {"unavailable_for_label_class":
                        train_outputs.target_label_class},
            "val":   {"unavailable_for_label_class":
                        train_outputs.target_label_class},
            "test":  {"unavailable_for_label_class":
                        train_outputs.target_label_class},
        }

    # Trading metrics per split (binary only)
    if train_outputs.target_label_class == "binary":
        trading = {
            "train": trading_metrics(
                y_true=y_true_train, y_proba=y_proba_train,
                target_label_id=target, dataset=dataset,
                split_indices=split.train_anchor_indices,
                cost_per_trade_log_return=cost_per_trade_log_return),
            "val":   trading_metrics(
                y_true=y_true_val, y_proba=y_proba_val,
                target_label_id=target, dataset=dataset,
                split_indices=split.val_anchor_indices,
                cost_per_trade_log_return=cost_per_trade_log_return),
            "test":  trading_metrics(
                y_true=y_true_test, y_proba=y_proba_test,
                target_label_id=target, dataset=dataset,
                split_indices=split.test_anchor_indices,
                cost_per_trade_log_return=cost_per_trade_log_return),
        }
    else:
        trading = {
            "train": {"unavailable_for_label_class":
                        train_outputs.target_label_class},
            "val":   {"unavailable_for_label_class":
                        train_outputs.target_label_class},
            "test":  {"unavailable_for_label_class":
                        train_outputs.target_label_class},
        }

    split_ts_ranges = {
        "train": _ts_range_for_indices(dataset,
                                          split.train_anchor_indices),
        "val":   _ts_range_for_indices(dataset,
                                          split.val_anchor_indices),
        "test":  _ts_range_for_indices(dataset,
                                          split.test_anchor_indices),
    }

    # ── v2 blocks (M18.A.7 amend, 2ca0b46) ──────────────────────────

    # Gap 1 — extended ML metrics (PR-AUC primary) per split
    if train_outputs.target_label_class == "binary":
        ml_metrics_extended = {
            "train": binary_metrics_extended(y_true_train,
                                                y_proba_train),
            "val":   binary_metrics_extended(y_true_val,
                                                y_proba_val),
            "test":  binary_metrics_extended(y_true_test,
                                                y_proba_test),
        }
        # Gap 2 — locked threshold table per split
        threshold_metrics = {
            "train": threshold_table(y_true_train, y_proba_train),
            "val":   threshold_table(y_true_val,   y_proba_val),
            "test":  threshold_table(y_true_test,  y_proba_test),
        }
        # Gap 6 — segment/regime breakdowns per split
        breakdowns = {
            "train": all_breakdowns(
                dataset=dataset,
                indices=split.train_anchor_indices,
                y_true=y_true_train, y_proba=y_proba_train,
                min_samples=breakdowns_min_samples),
            "val":   all_breakdowns(
                dataset=dataset,
                indices=split.val_anchor_indices,
                y_true=y_true_val, y_proba=y_proba_val,
                min_samples=breakdowns_min_samples),
            "test":  all_breakdowns(
                dataset=dataset,
                indices=split.test_anchor_indices,
                y_true=y_true_test, y_proba=y_proba_test,
                min_samples=breakdowns_min_samples),
        }
    else:
        _unav = {"unavailable_for_label_class":
                  train_outputs.target_label_class}
        ml_metrics_extended = {"train": dict(_unav),
                                 "val": dict(_unav),
                                 "test": dict(_unav)}
        threshold_metrics   = {"train": dict(_unav),
                                 "val": dict(_unav),
                                 "test": dict(_unav)}
        breakdowns          = {"train": dict(_unav),
                                 "val": dict(_unav),
                                 "test": dict(_unav)}

    # Gap 4 — PSI drift: train→val and train→test on feature columns
    from bot.ml.models.base import select_feature_columns
    feature_columns = select_feature_columns(list(dataset.columns))
    drift = {
        "train_to_val": drift_report(
            dataset=dataset,
            train_indices=split.train_anchor_indices,
            comparison_indices=split.val_anchor_indices,
            feature_columns=feature_columns,
            comparison_split_name="val"),
        "train_to_test": drift_report(
            dataset=dataset,
            train_indices=split.train_anchor_indices,
            comparison_indices=split.test_anchor_indices,
            feature_columns=feature_columns,
            comparison_split_name="test"),
    }

    # Gap 5 — permutation importance (deterministic; refits from the
    # echoed train_config). B0/B1 yield available=False with reasons.
    from bot.ml.schemas import TrainConfig as _TrainConfig
    try:
        _tc = _TrainConfig.from_dict(dict(train_outputs.train_config))
        perm_importance = permutation_importance(
            train_config=_tc,
            assembler_result=assembler_result,
            feature_columns=feature_columns,
            n_repeats=permutation_n_repeats,
            n_top=permutation_n_top)
    except Exception as _e:  # defensive: never sink the whole report
        perm_importance = {
            "available":          False,
            "unavailable_reason": (
                f"permutation importance failed: "
                f"{type(_e).__name__}: {_e}"),
        }

    return EvaluationReport(
        schema_version=EVALUATION_REPORT_SCHEMA_VERSION,
        model_type=train_outputs.model_type,
        train_mode=train_outputs.train_mode,
        target_label_id=train_outputs.target_label_id,
        target_label_class=train_outputs.target_label_class,
        dataset_id=train_outputs.dataset_id,
        dataset_hash_sha256=train_outputs.dataset_hash_sha256,
        dataset_anchor_set=train_outputs.dataset_anchor_set,
        cohort=_build_cohort_block(manifest),
        split=_build_split_block(manifest),
        n_train=train_outputs.n_train,
        n_val=train_outputs.n_val,
        n_test=train_outputs.n_test,
        split_timestamp_ranges=split_ts_ranges,
        ml_metrics={
            "train": train_outputs.metrics_train,
            "val":   train_outputs.metrics_val,
            "test":  train_outputs.metrics_test,
        },
        ml_metrics_extended=ml_metrics_extended,
        threshold_metrics=threshold_metrics,
        calibration=calibration,
        trading_metrics=trading,
        drift=drift,
        permutation_importance=perm_importance,
        breakdowns=breakdowns,
        fixture_only=train_outputs.fixture_only,
        promotion_eligible=train_outputs.promotion_eligible,
        promotion_blocked_reasons=list(
            train_outputs.promotion_blocked_reasons),
        seed=train_outputs.seed,
        library_versions=dict(train_outputs.library_versions),
        generated_at_utc=_utc_now_isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────
# compare_baselines — same-cohort row-paired comparison
# ─────────────────────────────────────────────────────────────────────

ALLOWED_PRIMARY_SPLITS = frozenset({"train", "val", "test"})


def compare_baselines(
    reports: List[EvaluationReport],
    *,
    primary_metric: str = "pr_auc",
    primary_split: str  = "val",
    primary_baseline_model_type: str = "B0_majority",
    secondary_baseline_model_type: Optional[str] =
        "B1_scanner_replica",
    baseline_model_type: Optional[str] = None,
) -> BaselineComparisonReport:
    """Aggregate same-cohort comparison (v2 — M18.A.7 amend).

    Enforces that all reports share dataset_id AND dataset_anchor_set.
    Raises M18ConfigError on any mismatch.

    v2 contract (Gap 7):
      * primary_metric defaults to 'pr_auc' (the PRIMARY M18 metric;
        was 'roc_auc' in v1).
      * TWO baselines: primary (B0_majority) and secondary
        (B1_scanner_replica by default; pass None to skip).
      * baseline_beats carries keys against BOTH baselines —
        4 entries for 3 models.
      * deltas_vs_primary_baseline / deltas_vs_secondary_baseline:
        {model_type: {metric: delta}} with the locked sign
        convention: POSITIVE = candidate better. For LOWER_IS_BETTER
        metrics (brier/log_loss/mse/mae/ECE/MCE),
        delta = baseline - candidate; otherwise
        delta = candidate - baseline.
      * Metric values are sourced from ml_metrics_extended (which
        carries pr_auc) merged over ml_metrics.

    `baseline_model_type` is the deprecated v1 alias for
    `primary_baseline_model_type`; if supplied it takes precedence.
    """
    if baseline_model_type is not None:
        primary_baseline_model_type = baseline_model_type
    if not reports:
        raise M18ConfigError("compare_baselines: reports list is empty")
    if primary_split not in ALLOWED_PRIMARY_SPLITS:
        raise M18ConfigError(
            f"primary_split={primary_split!r} not in "
            f"{sorted(ALLOWED_PRIMARY_SPLITS)}")

    # Cohort-consistency check (CRITICAL — same dataset)
    ds_ids = {r.dataset_id for r in reports}
    if len(ds_ids) > 1:
        raise M18ConfigError(
            f"compare_baselines requires SAME dataset_id across all "
            f"reports (row-paired comparison only valid on the same "
            f"cohort); got {sorted(ds_ids)}")
    anchor_sets = {r.dataset_anchor_set for r in reports}
    if len(anchor_sets) > 1:
        raise M18ConfigError(
            f"compare_baselines requires SAME dataset_anchor_set; "
            f"got {sorted(anchor_sets)} — use "
            f"compare_across_cohorts() for cross-cohort comparison")

    by_model: Dict[str, EvaluationReport] = {}
    for r in reports:
        if r.model_type in by_model:
            raise M18ConfigError(
                f"compare_baselines: duplicate model_type "
                f"{r.model_type!r} in reports list")
        by_model[r.model_type] = r

    if primary_baseline_model_type not in by_model:
        raise M18ConfigError(
            f"primary_baseline_model_type="
            f"{primary_baseline_model_type!r} not in reports "
            f"(available: {sorted(by_model)})")
    if (secondary_baseline_model_type is not None
            and secondary_baseline_model_type not in by_model):
        raise M18ConfigError(
            f"secondary_baseline_model_type="
            f"{secondary_baseline_model_type!r} not in reports "
            f"(available: {sorted(by_model)}); pass None to skip")

    def _split_metrics(r: EvaluationReport) -> Dict[str, Any]:
        """Merge basic + extended metrics for the primary split.
        Extended wins on key collisions (it carries pr_auc)."""
        merged: Dict[str, Any] = {}
        merged.update(r.ml_metrics.get(primary_split, {}) or {})
        ext = getattr(r, "ml_metrics_extended", None) or {}
        ext_split = ext.get(primary_split, {}) or {}
        for k, v in ext_split.items():
            if isinstance(v, (int, float)):
                merged[k] = v
        return merged

    # Collect the primary_split metric value for each model
    per_metric: Dict[str, Dict[str, float]] = {}
    candidate_metrics = sorted(
        {k for r in reports for k in _split_metrics(r).keys()}
    )
    for metric_name in candidate_metrics:
        per_metric[metric_name] = {}
        for mt, r in by_model.items():
            v = _split_metrics(r).get(metric_name)
            per_metric[metric_name][mt] = (
                float(v) if v is not None and not (
                    isinstance(v, float) and np.isnan(v))
                else float("nan"))

    LOWER_IS_BETTER = frozenset({"brier_score", "log_loss",
                                   "mse", "mae",
                                   "expected_calibration_error",
                                   "maximum_calibration_error"})

    def _beats(cand_v: float, base_v: float, metric: str) -> bool:
        if np.isnan(base_v) or np.isnan(cand_v):
            return False
        if metric in LOWER_IS_BETTER:
            return cand_v < base_v
        return cand_v > base_v

    def _delta(cand_v: float, base_v: float, metric: str) -> float:
        """Locked sign convention: positive = candidate better."""
        if np.isnan(base_v) or np.isnan(cand_v):
            return float("nan")
        if metric in LOWER_IS_BETTER:
            return float(base_v - cand_v)
        return float(cand_v - base_v)

    baselines = [primary_baseline_model_type]
    if (secondary_baseline_model_type is not None
            and secondary_baseline_model_type
                != primary_baseline_model_type):
        baselines.append(secondary_baseline_model_type)

    # baseline_beats: each model vs each baseline it isn't
    baseline_beats: Dict[str, bool] = {}
    for base_mt in baselines:
        base_v = per_metric.get(primary_metric, {}).get(
            base_mt, float("nan"))
        for mt in by_model:
            if mt == base_mt:
                continue
            cand_v = per_metric.get(primary_metric, {}).get(
                mt, float("nan"))
            key = (f"{mt}_beats_{base_mt}_on_"
                    f"{primary_split}_{primary_metric}")
            baseline_beats[key] = bool(
                _beats(cand_v, base_v, primary_metric))

    # deltas blocks: every metric, every non-baseline model
    def _deltas_vs(base_mt: str) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for mt in by_model:
            if mt == base_mt:
                continue
            out[mt] = {}
            for metric_name in candidate_metrics:
                base_v = per_metric[metric_name].get(
                    base_mt, float("nan"))
                cand_v = per_metric[metric_name].get(
                    mt, float("nan"))
                out[mt][metric_name] = _delta(
                    cand_v, base_v, metric_name)
        return out

    deltas_vs_primary = _deltas_vs(primary_baseline_model_type)
    deltas_vs_secondary: Dict[str, Dict[str, float]] = {}
    if (secondary_baseline_model_type is not None
            and secondary_baseline_model_type in by_model):
        deltas_vs_secondary = _deltas_vs(
            secondary_baseline_model_type)

    return BaselineComparisonReport(
        schema_version=BASELINE_COMPARISON_REPORT_SCHEMA_VERSION,
        cohort_dataset_id=reports[0].dataset_id,
        cohort_anchor_set=reports[0].dataset_anchor_set,
        model_reports=[r.to_dict() for r in reports],
        primary_split=primary_split,
        primary_metric=primary_metric,
        primary_baseline_model_type=primary_baseline_model_type,
        secondary_baseline_model_type=secondary_baseline_model_type,
        per_metric=per_metric,
        baseline_beats=baseline_beats,
        deltas_vs_primary_baseline=deltas_vs_primary,
        deltas_vs_secondary_baseline=deltas_vs_secondary,
        generated_at_utc=_utc_now_isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────
# compare_across_cohorts — explicit non-row-paired comparison
# ─────────────────────────────────────────────────────────────────────

def compare_across_cohorts(
    a_report: EvaluationReport,
    b_report: EvaluationReport,
    *,
    primary_split: str = "val",
) -> CrossCohortComparisonReport:
    """Side-by-side aggregate of two reports trained on DIFFERENT
    cohorts. Refuses to run if both reports share the same
    anchor_set — that's a same-cohort case, use compare_baselines.
    """
    if primary_split not in ALLOWED_PRIMARY_SPLITS:
        raise M18ConfigError(
            f"primary_split={primary_split!r} not in "
            f"{sorted(ALLOWED_PRIMARY_SPLITS)}")
    if a_report.dataset_anchor_set == b_report.dataset_anchor_set:
        raise M18ConfigError(
            f"compare_across_cohorts called with the SAME anchor_set "
            f"({a_report.dataset_anchor_set!r}) for both reports; "
            f"use compare_baselines() for same-cohort comparison")

    # Aggregate the primary_split's metrics, labeled by train_mode
    candidate_metrics = sorted(
        set(a_report.ml_metrics.get(primary_split, {}).keys()) |
        set(b_report.ml_metrics.get(primary_split, {}).keys())
    )
    aggregate: Dict[str, Dict[str, float]] = {}
    for m in candidate_metrics:
        a_v = a_report.ml_metrics.get(
            primary_split, {}).get(m, float("nan"))
        b_v = b_report.ml_metrics.get(
            primary_split, {}).get(m, float("nan"))
        aggregate[m] = {
            a_report.train_mode: (float(a_v)
                                    if a_v is not None else float("nan")),
            b_report.train_mode: (float(b_v)
                                    if b_v is not None else float("nan")),
        }

    return CrossCohortComparisonReport(
        schema_version=CROSS_COHORT_COMPARISON_REPORT_SCHEMA_VERSION,
        disclaimer=CROSS_COHORT_DISCLAIMER,
        a_report=a_report.to_dict(),
        b_report=b_report.to_dict(),
        primary_split=primary_split,
        aggregate_metric_values=aggregate,
        generated_at_utc=_utc_now_isoformat(),
    )

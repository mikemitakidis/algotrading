"""bot.ml.evaluation.calibration — calibration diagnostics for binary
classifiers.

Three quantities:

  Reliability curve
    Bin predicted probabilities into n_bins equal-width bins on
    [0, 1]. For each bin, record: count, mean predicted probability,
    mean actual outcome (0/1). Bins with zero count are kept in the
    output but flagged.

  Expected Calibration Error (ECE)
    Sum over bins of:
        (n_bin / N) * |mean_pred_bin - mean_actual_bin|
    where N is total sample count. ECE measures the average gap
    between predicted and observed positive rate, weighted by bin
    population. Lower is better; perfect calibration → 0.

  Maximum Calibration Error (MCE)
    max over non-empty bins of |mean_pred - mean_actual|.

Zero-handling:
  * Empty input (n_rows == 0) → ECE/MCE = NaN, curve has 0-count bins.
  * Single class (all 1s or all 0s) → calibration still defined; ECE
    measures how close predictions get to that constant rate.
  * All-NaN predictions → ECE/MCE = NaN.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np


DEFAULT_N_BINS = 10


def reliability_curve(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    *,
    n_bins: int = DEFAULT_N_BINS,
) -> List[Dict[str, Any]]:
    """Bin probabilities and return per-bin diagnostics.

    Bin edges: equal-width on [0, 1]. Bin index for predicted
    probability p ∈ [0, 1] is floor(p * n_bins), clamped to n_bins-1.
    Returns list of n_bins dicts, one per bin, in bin-index order:
        {bin_index, bin_lo, bin_hi, count, mean_pred, mean_actual}
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")
    y_true = np.asarray(y_true, dtype=np.float64)
    y_proba = np.asarray(y_proba, dtype=np.float64)
    if y_true.shape != y_proba.shape:
        raise ValueError(
            f"y_true and y_proba must have the same shape; "
            f"got {y_true.shape} vs {y_proba.shape}")

    edges = np.linspace(0.0, 1.0, n_bins + 1)

    # Map p to bin index. Clip p first so NaN → NaN (preserved),
    # otherwise floor(p * n_bins) with the n_bins index folded down.
    finite_mask = np.isfinite(y_proba) & np.isfinite(y_true)
    y_t = y_true[finite_mask]
    y_p = y_proba[finite_mask]

    bin_idx = np.clip(np.floor(y_p * n_bins).astype(int),
                       0, n_bins - 1)

    out: List[Dict[str, Any]] = []
    for b in range(n_bins):
        in_bin = (bin_idx == b)
        n_in   = int(in_bin.sum())
        if n_in > 0:
            mp = float(np.mean(y_p[in_bin]))
            ma = float(np.mean(y_t[in_bin]))
        else:
            mp = float("nan")
            ma = float("nan")
        out.append({
            "bin_index":   b,
            "bin_lo":      float(edges[b]),
            "bin_hi":      float(edges[b + 1]),
            "count":       n_in,
            "mean_pred":   mp,
            "mean_actual": ma,
        })
    return out


def expected_calibration_error(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    *,
    n_bins: int = DEFAULT_N_BINS,
) -> float:
    """Population-weighted mean absolute gap between predicted and
    observed positive rate, binned. Returns NaN on empty inputs."""
    y_true  = np.asarray(y_true,  dtype=np.float64)
    y_proba = np.asarray(y_proba, dtype=np.float64)
    finite_mask = np.isfinite(y_proba) & np.isfinite(y_true)
    n = int(finite_mask.sum())
    if n == 0:
        return float("nan")
    curve = reliability_curve(y_true, y_proba, n_bins=n_bins)
    weighted = 0.0
    for bin_ in curve:
        if bin_["count"] == 0:
            continue
        gap = abs(bin_["mean_pred"] - bin_["mean_actual"])
        weighted += (bin_["count"] / n) * gap
    return float(weighted)


def maximum_calibration_error(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    *,
    n_bins: int = DEFAULT_N_BINS,
) -> float:
    """Largest absolute gap between predicted and observed positive
    rate across non-empty bins. NaN on empty inputs."""
    y_true  = np.asarray(y_true,  dtype=np.float64)
    y_proba = np.asarray(y_proba, dtype=np.float64)
    finite_mask = np.isfinite(y_proba) & np.isfinite(y_true)
    if finite_mask.sum() == 0:
        return float("nan")
    curve = reliability_curve(y_true, y_proba, n_bins=n_bins)
    gaps = [abs(b["mean_pred"] - b["mean_actual"])
             for b in curve if b["count"] > 0]
    return float(max(gaps)) if gaps else float("nan")


def calibration_report(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    *,
    n_bins: int = DEFAULT_N_BINS,
) -> Dict[str, Any]:
    """Bundle ECE, MCE, and the reliability curve for one split."""
    return {
        "n_rows":                     int(len(y_true)),
        "n_bins":                     int(n_bins),
        "expected_calibration_error": expected_calibration_error(
            y_true, y_proba, n_bins=n_bins),
        "maximum_calibration_error":  maximum_calibration_error(
            y_true, y_proba, n_bins=n_bins),
        "reliability_curve": reliability_curve(
            y_true, y_proba, n_bins=n_bins),
    }

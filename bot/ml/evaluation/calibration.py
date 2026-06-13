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

from typing import Any, Dict, List, Mapping, Optional, Tuple

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


# ─── Real isotonic calibration (M18.B.3) ─────────────────────────────
#
# The functions above are DIAGNOSTIC only (reliability curve / ECE /
# MCE). M18.B.3 adds a real fitted calibrator:
#
#   * fit IsotonicRegression on the VALIDATION split only (never train),
#   * apply the fitted mapping to the TEST split (out-of-sample),
#   * report pre/post Brier/ECE/MCE for both val and test,
#   * persist a JSON-safe artifact (x_thresholds / y_thresholds) so the
#     mapping can be re-applied later WITHOUT pickling a live sklearn
#     object (mirrors the registry's refit-on-demand philosophy).
#
# Leakage rule (most important): the calibrator is fit on
# (val_prob, val_y) and applied to test_prob. Train is never used.

DEFAULT_MIN_VALIDATION_ROWS = 20


def _brier(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    """Mean squared error of probabilities (clipped to [0,1])."""
    if len(y_true) == 0:
        return float("nan")
    p = np.clip(np.asarray(y_proba, dtype=np.float64), 0.0, 1.0)
    y = np.asarray(y_true, dtype=np.float64)
    return float(np.mean((p - y) ** 2))


def _split_metrics(y_true: np.ndarray, y_proba: np.ndarray,
                    *, n_bins: int = DEFAULT_N_BINS) -> Dict[str, float]:
    return {
        "brier": _brier(y_true, y_proba),
        "ece":   expected_calibration_error(y_true, y_proba,
                                              n_bins=n_bins),
        "mce":   maximum_calibration_error(y_true, y_proba,
                                             n_bins=n_bins),
    }


def _nan_to_none(obj: Any) -> Any:
    """Recursively replace NaN/inf floats with None so the result is
    strict-JSON-safe (json.dumps(..., allow_nan=False) succeeds)."""
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nan_to_none(v) for v in obj]
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    return obj


def apply_isotonic_artifact(
    probabilities: np.ndarray,
    artifact: Dict[str, Any],
) -> np.ndarray:
    """Apply a persisted isotonic artifact to new probabilities.

    Uses linear interpolation over the fitted (x_thresholds,
    y_thresholds) breakpoints, clipping out-of-range inputs to the
    fitted domain (out_of_bounds='clip'). Output is clipped to [0,1].
    Lets future prediction code reuse the calibration WITHOUT a live
    sklearn object.
    """
    p = np.asarray(probabilities, dtype=np.float64)
    if not isinstance(artifact, Mapping):
        raise ValueError(
            "apply_isotonic_artifact: artifact must be a mapping")
    if "x_thresholds" not in artifact or "y_thresholds" not in artifact:
        raise ValueError(
            "apply_isotonic_artifact: artifact missing x_thresholds / "
            "y_thresholds")
    xs = np.asarray(artifact["x_thresholds"], dtype=np.float64)
    ys = np.asarray(artifact["y_thresholds"], dtype=np.float64)
    if xs.shape[0] != ys.shape[0]:
        raise ValueError(
            f"apply_isotonic_artifact: x_thresholds (n={xs.shape[0]}) "
            f"and y_thresholds (n={ys.shape[0]}) length mismatch")
    if xs.size and (not np.all(np.isfinite(xs))
                     or not np.all(np.isfinite(ys))):
        raise ValueError(
            "apply_isotonic_artifact: thresholds contain non-finite "
            "values")
    if xs.size and np.any(np.diff(xs) < 0):
        raise ValueError(
            "apply_isotonic_artifact: x_thresholds must be "
            "non-decreasing (monotonic)")
    if p.size == 0:
        return np.empty((0,), dtype=np.float64)
    if xs.size == 0 or ys.size == 0:
        # Degenerate artifact: identity (clipped).
        return np.clip(p, 0.0, 1.0)
    # np.interp clips to the endpoints outside [xs[0], xs[-1]], which is
    # exactly out_of_bounds='clip'.
    out = np.interp(np.clip(p, 0.0, 1.0), xs, ys)
    return np.clip(out, 0.0, 1.0).astype(np.float64)


def fit_isotonic_calibration(
    *,
    val_prob: np.ndarray,
    val_y: np.ndarray,
    test_prob: Optional[np.ndarray] = None,
    test_y: Optional[np.ndarray] = None,
    label_class: str = "binary",
    min_validation_rows: int = DEFAULT_MIN_VALIDATION_ROWS,
    n_bins: int = DEFAULT_N_BINS,
) -> Dict[str, Any]:
    """Fit isotonic calibration on the validation split and apply to
    test. Returns a JSON-safe result dict.

    NEVER fits on train. Binary label_class only. On any unsuitable
    input the result is {available: False, unavailable_reason: ...}
    rather than raising, so it can never crash the full evaluation.
    """
    def _unavailable(reason: str) -> Dict[str, Any]:
        return {
            "method":             "isotonic",
            "available":          False,
            "fitted_on_split":    "val",
            "unavailable_reason": reason,
        }

    if label_class != "binary":
        return _unavailable("unsupported_label_class")

    vp = np.asarray(val_prob, dtype=np.float64)
    vy = np.asarray(val_y, dtype=np.float64)

    if vp.shape != vy.shape:
        return _unavailable("validation_shape_mismatch")
    if vp.shape[0] < min_validation_rows:
        return _unavailable("too_few_validation_rows")
    if not np.all(np.isfinite(vp)):
        return _unavailable("non_finite_probability")
    if not np.all(np.isfinite(vy)):
        return _unavailable("non_finite_label")
    val_classes = {float(v) for v in np.unique(vy).tolist()}
    if not val_classes.issubset({0.0, 1.0}):
        return _unavailable("non_binary_validation_labels")
    if len(val_classes) < 2:
        return _unavailable("one_class_validation_labels")

    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        return _unavailable("sklearn_unavailable")

    try:
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0,
                                  y_max=1.0)
        # Fit on VALIDATION ONLY.
        iso.fit(np.clip(vp, 0.0, 1.0), vy)
        xs = np.asarray(iso.X_thresholds_, dtype=np.float64)
        ys = np.asarray(iso.y_thresholds_, dtype=np.float64)
        artifact = {
            "x_thresholds": [float(x) for x in xs],
            "y_thresholds": [float(y) for y in ys],
            "out_of_bounds": "clip",
        }

        n_pos = int(np.sum(vy == 1.0))
        n_neg = int(np.sum(vy == 0.0))

        # Validation pre/post (in-sample — shows the fitted behaviour).
        val_cal = apply_isotonic_artifact(vp, artifact)
        val_pre = _split_metrics(vy, vp, n_bins=n_bins)
        val_post = _split_metrics(vy, val_cal, n_bins=n_bins)

        result: Dict[str, Any] = {
            "method":          "isotonic",
            "available":       True,
            "fitted_on_split": "val",
            "n_fit_rows":      int(vp.shape[0]),
            "n_positive":      n_pos,
            "n_negative":      n_neg,
            "out_of_bounds":   "clip",
            "artifact":        artifact,
            "validation": {
                "pre_brier":  val_pre["brier"],
                "post_brier": val_post["brier"],
                "pre_ece":    val_pre["ece"],
                "post_ece":   val_post["ece"],
                "pre_mce":    val_pre["mce"],
                "post_mce":   val_post["mce"],
            },
        }

        # Test pre/post (out-of-sample — the meaningful calibration
        # check). The test section is ALWAYS present with an explicit
        # reason when it can't be computed.
        if test_prob is None or test_y is None:
            result["test"] = {
                "unavailable_reason": "test_split_not_supplied"}
        else:
            tp = np.asarray(test_prob, dtype=np.float64)
            ty = np.asarray(test_y, dtype=np.float64)
            if tp.shape != ty.shape:
                result["test"] = {
                    "unavailable_reason": "test_shape_mismatch"}
            elif tp.shape[0] == 0:
                result["test"] = {
                    "unavailable_reason": "empty_test_split"}
            elif not (np.all(np.isfinite(tp))
                       and np.all(np.isfinite(ty))):
                result["test"] = {
                    "unavailable_reason": "non_finite_test_split"}
            elif not {float(v) for v in
                       np.unique(ty).tolist()}.issubset({0.0, 1.0}):
                result["test"] = {
                    "unavailable_reason": "non_binary_test_labels"}
            else:
                test_cal = apply_isotonic_artifact(tp, artifact)
                test_pre = _split_metrics(ty, tp, n_bins=n_bins)
                test_post = _split_metrics(ty, test_cal, n_bins=n_bins)
                result["test"] = {
                    "pre_brier":  test_pre["brier"],
                    "post_brier": test_post["brier"],
                    "pre_ece":    test_pre["ece"],
                    "post_ece":   test_post["ece"],
                    "pre_mce":    test_pre["mce"],
                    "post_mce":   test_post["mce"],
                    "n_rows":     int(tp.shape[0]),
                }

        # Strict JSON-safety: convert any NaN metric to None so the
        # result survives json.dumps(..., allow_nan=False).
        result = _nan_to_none(result)
        return result
    except Exception as e:  # pragma: no cover - defensive
        return _unavailable(f"unexpected_exception:{type(e).__name__}")

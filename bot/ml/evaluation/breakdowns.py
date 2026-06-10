"""bot.ml.evaluation.breakdowns — segment/regime metric breakdowns
for M18.A.7.

Four breakdowns when underlying fields are available:

  per_symbol         skip symbols with < 50 samples
                      (single-symbol datasets just report that one)
  per_year           binned by year of anchor ts_utc
                      (also includes per-quarter breakdown when there
                       are enough samples per quarter)
  volatility_regime  binned by vol_regime.atr_percentile_60 quartiles
                      (or by vol_regime.vol_regime_flag if present)
  market_regime      binned by market_context.spy_above_ema200_1d
                      (or qqq_above_ema200_1d as fallback)

When a required field is missing, the corresponding breakdown is
emitted as a dict with `unavailable_reason` populated.

For each segment that meets the min_samples threshold, the per-bin
metric block contains:
  n_samples
  n_predicted_positive
  positive_rate_pred
  positive_rate_true
  precision_at_05  / recall_at_05 / f1_at_05
  roc_auc          (NaN if single-class y_true in segment)

Segments below the min_samples threshold are dropped with a note in
the breakdown's `skipped_segments` field.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


MIN_SAMPLES_PER_SEGMENT = 50

# Feature column names this module probes the dataset for.
ATR_PERCENTILE_COL    = "vol_regime.atr_percentile_60"
VOL_REGIME_FLAG_COL   = "vol_regime.vol_regime_flag"
MARKET_SPY_REGIME_COL = "market_context.spy_above_ema200_1d"
MARKET_QQQ_REGIME_COL = "market_context.qqq_above_ema200_1d"


def _segment_metrics(
    y_true: np.ndarray, y_proba: np.ndarray,
) -> Dict[str, Any]:
    """Compact metric block for one segment. Returns NaN-safe."""
    n = int(len(y_true))
    if n == 0:
        return {
            "n_samples":           0,
            "n_predicted_positive": 0,
            "positive_rate_pred":  float("nan"),
            "positive_rate_true":  float("nan"),
            "precision_at_05":     float("nan"),
            "recall_at_05":        float("nan"),
            "f1_at_05":            float("nan"),
            "roc_auc":             float("nan"),
        }
    y_t = np.asarray(y_true,  dtype=np.float64)
    y_p = np.asarray(y_proba, dtype=np.float64)
    y_hat = (y_p >= 0.5).astype(np.float64)

    n_pred_pos = int(np.sum(y_hat == 1.0))
    tp = int(np.sum((y_hat == 1.0) & (y_t == 1.0)))
    fp = int(np.sum((y_hat == 1.0) & (y_t == 0.0)))
    fn = int(np.sum((y_hat == 0.0) & (y_t == 1.0)))

    precision = (float(tp) / (tp + fp)
                  if (tp + fp) > 0 else float("nan"))
    recall    = (float(tp) / (tp + fn)
                  if (tp + fn) > 0 else float("nan"))
    if (np.isnan(precision) or np.isnan(recall)
            or (precision + recall) == 0):
        f1 = float("nan")
    else:
        f1 = float(2.0 * precision * recall / (precision + recall))

    if len(np.unique(y_t)) < 2:
        roc_auc = float("nan")
    else:
        from sklearn.metrics import roc_auc_score
        roc_auc = float(roc_auc_score(y_t, y_p))

    return {
        "n_samples":           n,
        "n_predicted_positive": n_pred_pos,
        "positive_rate_pred":  float(n_pred_pos) / n,
        "positive_rate_true":  float(np.mean(y_t == 1.0)),
        "precision_at_05":     precision,
        "recall_at_05":        recall,
        "f1_at_05":            f1,
        "roc_auc":             roc_auc,
    }


def _bin_by_grouper(
    grouper: pd.Series,
    y_true:  np.ndarray,
    y_proba: np.ndarray,
    min_samples: int = MIN_SAMPLES_PER_SEGMENT,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Group `(y_true, y_proba)` by `grouper` and compute segment
    metrics. Returns (segment_metrics, skipped_segments).
    """
    by: Dict[str, Any] = {}
    skipped: List[Dict[str, Any]] = []
    for key, sub_idx in grouper.groupby(grouper).indices.items():
        ys = y_true[sub_idx]
        ps = y_proba[sub_idx]
        if len(ys) < min_samples:
            skipped.append({"segment": str(key),
                              "n_samples": int(len(ys)),
                              "reason": "below_min_samples"})
            continue
        by[str(key)] = _segment_metrics(ys, ps)
    return by, skipped


def per_symbol_breakdown(
    *,
    dataset: pd.DataFrame,
    indices: np.ndarray,
    y_true: np.ndarray,
    y_proba: np.ndarray,
    min_samples: int = MIN_SAMPLES_PER_SEGMENT,
) -> Dict[str, Any]:
    """Per-symbol breakdown. M18.A.5 assembler is single-symbol so
    this typically degenerates to one segment, but the structure
    keeps the API stable for the multi-symbol future."""
    if "symbol" not in dataset.columns:
        return {
            "available":          False,
            "unavailable_reason": (
                "no 'symbol' column in the dataset; M18.A.5 "
                "assembler does not currently emit a symbol column "
                "per-row (the symbol is on the manifest). "
                "Will become a richer breakdown when multi-symbol "
                "datasets land."),
            "min_samples":        int(min_samples),
        }
    grouper = dataset.iloc[indices]["symbol"].astype(str).reset_index(
        drop=True)
    segments, skipped = _bin_by_grouper(
        grouper, y_true, y_proba, min_samples=min_samples)
    return {
        "available":          True,
        "min_samples":        int(min_samples),
        "segments":           segments,
        "skipped_segments":   skipped,
    }


def per_year_breakdown(
    *,
    dataset: pd.DataFrame,
    indices: np.ndarray,
    y_true: np.ndarray,
    y_proba: np.ndarray,
    min_samples: int = MIN_SAMPLES_PER_SEGMENT,
) -> Dict[str, Any]:
    """Per-year and per-quarter breakdown using the ts_utc column."""
    if "ts_utc" not in dataset.columns:
        return {
            "available":          False,
            "unavailable_reason": "no 'ts_utc' column in the dataset",
            "min_samples":        int(min_samples),
        }
    ts = pd.to_datetime(dataset.iloc[indices]["ts_utc"], utc=True)
    year_grouper    = ts.dt.year.reset_index(drop=True).astype(str)
    quarter_grouper = (
        ts.dt.year.astype(str) + "Q" + ts.dt.quarter.astype(str)
    ).reset_index(drop=True)
    year_segments, year_skipped = _bin_by_grouper(
        year_grouper, y_true, y_proba, min_samples=min_samples)
    qtr_segments, qtr_skipped = _bin_by_grouper(
        quarter_grouper, y_true, y_proba, min_samples=min_samples)
    return {
        "available":            True,
        "min_samples":          int(min_samples),
        "per_year":             {
            "segments":         year_segments,
            "skipped_segments": year_skipped,
        },
        "per_quarter":          {
            "segments":         qtr_segments,
            "skipped_segments": qtr_skipped,
        },
    }


def volatility_regime_breakdown(
    *,
    dataset: pd.DataFrame,
    indices: np.ndarray,
    y_true: np.ndarray,
    y_proba: np.ndarray,
    min_samples: int = MIN_SAMPLES_PER_SEGMENT,
) -> Dict[str, Any]:
    """Volatility-regime breakdown.

    Preferred field: vol_regime.atr_percentile_60 (binned into 4
    quartiles). Fallback: vol_regime.vol_regime_flag (discrete flag).
    """
    cols = list(dataset.columns)
    if VOL_REGIME_FLAG_COL in cols:
        flag = dataset.iloc[indices][VOL_REGIME_FLAG_COL]\
            .astype(str).reset_index(drop=True)
        segments, skipped = _bin_by_grouper(
            flag, y_true, y_proba, min_samples=min_samples)
        return {
            "available":        True,
            "binning_field":    VOL_REGIME_FLAG_COL,
            "min_samples":      int(min_samples),
            "segments":         segments,
            "skipped_segments": skipped,
        }
    if ATR_PERCENTILE_COL in cols:
        pct = dataset.iloc[indices][ATR_PERCENTILE_COL]\
            .to_numpy(dtype=np.float64)
        # Bin into quartiles: low (<0.25), mid-low [0.25,0.5),
        # mid-high [0.5,0.75), high (>=0.75)
        labels = np.full(len(pct), "unknown", dtype=object)
        finite = np.isfinite(pct)
        labels[finite & (pct <  0.25)]                       = "low"
        labels[finite & (pct >= 0.25) & (pct < 0.50)]        = "mid_low"
        labels[finite & (pct >= 0.50) & (pct < 0.75)]        = "mid_high"
        labels[finite & (pct >= 0.75)]                        = "high"
        grouper = pd.Series(labels)
        segments, skipped = _bin_by_grouper(
            grouper, y_true, y_proba, min_samples=min_samples)
        return {
            "available":        True,
            "binning_field":    ATR_PERCENTILE_COL,
            "bins":             ["low", "mid_low", "mid_high", "high"],
            "min_samples":      int(min_samples),
            "segments":         segments,
            "skipped_segments": skipped,
        }
    return {
        "available":          False,
        "unavailable_reason": (
            f"neither {VOL_REGIME_FLAG_COL!r} nor "
            f"{ATR_PERCENTILE_COL!r} present in dataset columns; "
            f"volatility regime breakdown unavailable"),
        "min_samples":        int(min_samples),
    }


def market_regime_breakdown(
    *,
    dataset: pd.DataFrame,
    indices: np.ndarray,
    y_true: np.ndarray,
    y_proba: np.ndarray,
    min_samples: int = MIN_SAMPLES_PER_SEGMENT,
) -> Dict[str, Any]:
    """Market-regime breakdown using market_context features.

    Preferred field: market_context.spy_above_ema200_1d (boolean
    treated as a discrete regime). Fallback: qqq_above_ema200_1d.
    """
    cols = list(dataset.columns)
    binning_col: Optional[str] = None
    for c in (MARKET_SPY_REGIME_COL, MARKET_QQQ_REGIME_COL):
        if c in cols:
            binning_col = c
            break
    if binning_col is None:
        return {
            "available":          False,
            "unavailable_reason": (
                f"neither {MARKET_SPY_REGIME_COL!r} nor "
                f"{MARKET_QQQ_REGIME_COL!r} present; market regime "
                f"breakdown unavailable (M18.A.3 market_context "
                f"features may have been computed without benchmark "
                f"data)"),
            "min_samples":        int(min_samples),
        }
    series = dataset.iloc[indices][binning_col]
    # Treat values as boolean → "bull" / "bear" / "unknown" buckets
    labels = np.full(len(series), "unknown", dtype=object)
    arr = series.to_numpy()
    labels[arr == 1] = "above_ema200"
    labels[arr == 0] = "below_ema200"
    grouper = pd.Series(labels)
    segments, skipped = _bin_by_grouper(
        grouper, y_true, y_proba, min_samples=min_samples)
    return {
        "available":        True,
        "binning_field":    binning_col,
        "bins":             ["above_ema200", "below_ema200"],
        "min_samples":      int(min_samples),
        "segments":         segments,
        "skipped_segments": skipped,
    }


def all_breakdowns(
    *,
    dataset: pd.DataFrame,
    indices: np.ndarray,
    y_true: np.ndarray,
    y_proba: np.ndarray,
    min_samples: int = MIN_SAMPLES_PER_SEGMENT,
) -> Dict[str, Any]:
    """Bundle all four breakdowns for one split."""
    return {
        "min_samples_per_segment": int(min_samples),
        "per_symbol":              per_symbol_breakdown(
            dataset=dataset, indices=indices,
            y_true=y_true, y_proba=y_proba, min_samples=min_samples),
        "per_year":                per_year_breakdown(
            dataset=dataset, indices=indices,
            y_true=y_true, y_proba=y_proba, min_samples=min_samples),
        "volatility_regime":       volatility_regime_breakdown(
            dataset=dataset, indices=indices,
            y_true=y_true, y_proba=y_proba, min_samples=min_samples),
        "market_regime":           market_regime_breakdown(
            dataset=dataset, indices=indices,
            y_true=y_true, y_proba=y_proba, min_samples=min_samples),
    }

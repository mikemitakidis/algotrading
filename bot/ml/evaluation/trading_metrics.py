"""bot.ml.evaluation.trading_metrics — trading-style metrics for the
binary triple-barrier-won target.

Uses the aux columns the M18.A.4 labels write into the dataset:

  triple_barrier_atr_2_3_50.return_log_at_resolution
    Log return at label resolution (TP/SL/timeout). NaN for pending
    rows (which the assembler already excluded).

  triple_barrier_atr_2_3_50.bars_to_resolution
    Number of anchor-TF bars between anchor and resolution.

Metrics computed (per split):

  n_rows                            int      sample count in split
  n_predicted_positive              int      count of y_proba >= 0.5
  n_actual_positive                 int      count of y_true == 1
  positive_rate_pred                float    n_predicted_positive / n
  positive_rate_true                float    n_actual_positive / n
  precision_at_05                   float    TP / (TP + FP), NaN if no positives predicted
  recall_at_05                      float    TP / (TP + FN), NaN if no actual positives
  mean_log_return_predicted_positive
                                    float    NaN if no positives predicted
                                              or no aux column available
  sum_log_return_predicted_positive float    NaN if no positives predicted
  mean_bars_to_resolution_predicted_positive
                                    float    average holding-period proxy
                                              NaN if no positives predicted
  zero_trade_warnings               list[str]
                                             populated when n_predicted_positive==0
                                             or n_actual_positive==0 (etc.)

Triple-barrier label naming convention:
  The "won" label is `{primary}_won` where `primary` is the triple-
  barrier label_id (e.g. "triple_barrier_atr_2_3_50"). The aux
  columns live on the PRIMARY label, not on the _won variant.
  E.g. `triple_barrier_atr_2_3_50.return_log_at_resolution` exists,
  but `triple_barrier_atr_2_3_50_won.return_log_at_resolution`
  does not.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


# Default threshold for binarising probabilities into trade decisions.
DEFAULT_THRESHOLD = 0.5

# Suffix the triple-barrier label uses for its "won" sibling.
TB_WON_SUFFIX = "_won"

# Aux column names from M18.A.4 labels (primary triple-barrier label).
AUX_RETURN_LOG     = "return_log_at_resolution"
AUX_BARS_TO_RESOLN = "bars_to_resolution"


def _resolve_primary_tb_label(target_label_id: str) -> Optional[str]:
    """Map e.g. 'triple_barrier_atr_2_3_50_won' → 'triple_barrier_atr_2_3_50'.

    Returns None if the target_label_id does not look like a
    triple-barrier-won label — caller should treat the trading
    metrics as unavailable in that case."""
    if not target_label_id.startswith("triple_barrier_"):
        return None
    if not target_label_id.endswith(TB_WON_SUFFIX):
        # Could be the 3-way triple-barrier label itself (e.g.
        # 'triple_barrier_atr_2_3_50') — trading metrics are for the
        # binary _won variant only.
        return None
    return target_label_id[:-len(TB_WON_SUFFIX)]


PRECISION_AT_K_LIST = (5, 10, 20, 50)

# Default per-trade cost in log-return units (~10 bps), matching the
# cost assumption used by the cost_adjusted_fwd_return_5b label.
DEFAULT_COST_PER_TRADE_LOG_RETURN = 0.001

EQUITY_CURVE_UNAVAILABLE_REASON = (
    "requires per-bar equity curve, not just per-trade log returns; "
    "deferred to the backtester-driven evaluation phase")


def _precision_at_k(y_true, y_proba):
    """Precision over the top-K rows ranked by y_proba (desc).

    K values are locked (5, 10, 20, 50). When the split has fewer
    than K rows, the entry is NaN."""
    import numpy as _np
    out = {}
    n = len(y_true)
    order = _np.argsort(-_np.asarray(y_proba, dtype=_np.float64),
                          kind="stable")
    y_sorted = _np.asarray(y_true, dtype=_np.float64)[order]
    for k in PRECISION_AT_K_LIST:
        if n >= k:
            out[f"k_{k}"] = float(_np.mean(y_sorted[:k]))
        else:
            out[f"k_{k}"] = float("nan")
    return out


def trading_metrics(
    *,
    y_true:           np.ndarray,
    y_proba:          np.ndarray,
    target_label_id:  str,
    dataset:          Optional[pd.DataFrame] = None,
    split_indices:    Optional[np.ndarray]   = None,
    threshold:        float                  = DEFAULT_THRESHOLD,
    cost_per_trade_log_return: float =
        DEFAULT_COST_PER_TRADE_LOG_RETURN,
) -> Dict[str, Any]:
    """Compute per-split trading metrics.

    `dataset` + `split_indices` are required to access aux columns
    (`.return_log_at_resolution`, `.bars_to_resolution`). When
    either is None, the return-based metrics are NaN but the
    classifier-side metrics (precision, recall, counts) are still
    populated.
    """
    n = int(len(y_true))
    warnings: List[str] = []
    if n == 0:
        warnings.append("empty_split")
        return {
            "n_rows":                                       0,
            "n_predicted_positive":                         0,
            "n_actual_positive":                            0,
            "n_filtered":                                   0,
            "positive_rate_pred":                           float("nan"),
            "positive_rate_true":                           float("nan"),
            "precision_at_threshold":                       float("nan"),
            "recall_at_threshold":                          float("nan"),
            "threshold":                                    float(threshold),
            "win_rate_by_return":                           float("nan"),
            "average_log_return_win":                       float("nan"),
            "average_log_return_loss":                      float("nan"),
            "profit_factor":                                float("nan"),
            "expected_value_after_costs":                   float("nan"),
            "cost_per_trade_log_return":
                float(cost_per_trade_log_return),
            "precision_at_k": {f"k_{k}": float("nan")
                                 for k in PRECISION_AT_K_LIST},
            "equity_curve_metrics": {
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "max_drawdown": None,
                "unavailable_reason": EQUITY_CURVE_UNAVAILABLE_REASON},
            "mean_log_return_predicted_positive":           float("nan"),
            "sum_log_return_predicted_positive":            float("nan"),
            "mean_bars_to_resolution_predicted_positive":   float("nan"),
            "trading_metrics_available":                    False,
            "primary_label_id":                             None,
            "zero_trade_warnings":                          warnings,
        }

    y_t = np.asarray(y_true,  dtype=np.float64)
    y_p = np.asarray(y_proba, dtype=np.float64)
    pred_positive_mask = (y_p >= float(threshold))
    actual_positive_mask = (y_t == 1.0)
    tp_mask = pred_positive_mask & actual_positive_mask

    n_pred_pos   = int(pred_positive_mask.sum())
    n_actual_pos = int(actual_positive_mask.sum())
    n_tp         = int(tp_mask.sum())

    if n_pred_pos == 0:
        warnings.append("zero_predicted_positive")
    if n_actual_pos == 0:
        warnings.append("zero_actual_positive")

    precision = (float(n_tp) / n_pred_pos
                  if n_pred_pos > 0 else float("nan"))
    recall    = (float(n_tp) / n_actual_pos
                  if n_actual_pos > 0 else float("nan"))

    # Return / holding-period metrics: require dataset + split_indices
    # and a triple-barrier _won target label.
    primary = _resolve_primary_tb_label(target_label_id)
    available = False
    mean_log_return = float("nan")
    sum_log_return  = float("nan")
    mean_bars       = float("nan")
    win_rate_by_return         = float("nan")
    average_log_return_win     = float("nan")
    average_log_return_loss    = float("nan")
    profit_factor              = float("nan")
    expected_value_after_costs = float("nan")

    if dataset is None or split_indices is None:
        warnings.append("aux_columns_not_provided")
    elif primary is None:
        warnings.append(
            f"target_label_id={target_label_id!r} is not a "
            f"triple-barrier _won label; return-based metrics "
            f"unavailable")
    else:
        ret_col  = f"{primary}.{AUX_RETURN_LOG}"
        bars_col = f"{primary}.{AUX_BARS_TO_RESOLN}"
        missing_aux = [c for c in (ret_col, bars_col)
                        if c not in dataset.columns]
        if missing_aux:
            warnings.append(
                f"aux columns missing from dataset: {missing_aux}")
        else:
            available = True
            if n_pred_pos > 0:
                ret_vals  = dataset.iloc[split_indices][ret_col]\
                    .to_numpy(dtype=np.float64)[pred_positive_mask]
                bars_vals = dataset.iloc[split_indices][bars_col]\
                    .to_numpy(dtype=np.float64)[pred_positive_mask]
                # Drop NaN aux values (defensive)
                ret_finite  = ret_vals[np.isfinite(ret_vals)]
                bars_finite = bars_vals[np.isfinite(bars_vals)]
                if len(ret_finite) > 0:
                    mean_log_return = float(np.mean(ret_finite))
                    sum_log_return  = float(np.sum(ret_finite))
                    wins   = ret_finite[ret_finite >  0.0]
                    losses = ret_finite[ret_finite <= 0.0]
                    win_rate_by_return = (
                        float(len(wins)) / len(ret_finite))
                    if len(wins) > 0:
                        average_log_return_win = float(np.mean(wins))
                    if len(losses) > 0:
                        average_log_return_loss = float(
                            np.mean(losses))
                    loss_sum = float(np.sum(np.abs(losses)))
                    if loss_sum > 0.0:
                        profit_factor = (float(np.sum(wins))
                                          / loss_sum)
                    else:
                        warnings.append(
                            "profit_factor_undefined_no_losses")
                    expected_value_after_costs = (
                        mean_log_return
                        - float(cost_per_trade_log_return))
                if len(bars_finite) > 0:
                    mean_bars       = float(np.mean(bars_finite))

    return {
        "n_rows":                                       n,
        "n_predicted_positive":                         n_pred_pos,
        "n_actual_positive":                            n_actual_pos,
        "n_filtered":                                   n - n_pred_pos,
        "positive_rate_pred":                           float(n_pred_pos) / n,
        "positive_rate_true":                           float(n_actual_pos) / n,
        "precision_at_threshold":                       precision,
        "recall_at_threshold":                          recall,
        "threshold":                                    float(threshold),
        "win_rate_by_return":                           win_rate_by_return,
        "average_log_return_win":                       average_log_return_win,
        "average_log_return_loss":                      average_log_return_loss,
        "profit_factor":                                profit_factor,
        "expected_value_after_costs":
            expected_value_after_costs,
        "cost_per_trade_log_return":
            float(cost_per_trade_log_return),
        "precision_at_k":                               _precision_at_k(
                                                            y_t, y_p),
        "equity_curve_metrics": {
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "max_drawdown": None,
            "unavailable_reason": EQUITY_CURVE_UNAVAILABLE_REASON},
        "mean_log_return_predicted_positive":           mean_log_return,
        "sum_log_return_predicted_positive":            sum_log_return,
        "mean_bars_to_resolution_predicted_positive":   mean_bars,
        "trading_metrics_available":                    available,
        "primary_label_id":                             primary,
        "zero_trade_warnings":                          warnings,
    }

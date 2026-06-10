"""bot.ml.labels.forward_returns — forward returns and the
cost-adjusted variant.

Three labels (locked M18 plan):
  fwd_return_5b                 regression
      Forward log return over 5 bars after entry:
        log(close[i+5] / open[i+1])

  fwd_return_20b                regression
      Forward log return over 20 bars after entry:
        log(close[i+20] / open[i+1])

  cost_adjusted_fwd_return_5b   regression
      fwd_return_5b minus a round-trip cost approximation (10 bps
      by default). For small fractional costs c, the additive form
      (raw_log_return - c) is a tight approximation of the
      mathematically exact log(1 - c) ≈ -c. cost_model_applied=True
      on this LabelSpec.

Per-row output columns for each label_id L:
    L                  the forward log return (NaN if pending)
    L.resolved_ts      UTC ts of exit bar (NaT if pending)
    L.is_pending       int8 (0 resolved, 1 pending)

Pending semantics:
  At horizon h, anchor i resolves at exit bar i+h. Pending iff
  i + h >= n (no exit bar available) OR i + 1 >= n (no entry bar).

Entry semantics: entry = open[i+1] (next-bar-open). Same as every
other M18 label.

Why no 1-bar horizon: the locked plan covers 5 and 20 only. A
1-bar fwd_return would conflate signal lag with same-bar noise.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import LabelSpec
from bot.ml.labels.base import (
    align_to_bars,
    empty_resolved_ts_column,
)


# Round-trip cost in fractional terms (10 bps).
DEFAULT_ROUND_TRIP_COST = 0.0010


def _raw_spec(horizon_bars: int) -> LabelSpec:
    return LabelSpec(
        label_id=f"fwd_return_{horizon_bars}b",
        label_schema_version=1,
        label_class="regression",
        horizon_bars=horizon_bars,
        horizon_unit="bars_at_anchor_tf",
        leak_class="future_label_only",
        computed_from=("open", "close"),
        description=(
            f"Forward log return over {horizon_bars} bars after "
            f"entry: log(close[i+{horizon_bars}] / open[i+1])."
        ),
        cost_model_applied=False,
        tested_in="test_m18_ml.py::G3_ForwardReturns",
    )


def _cost_spec(horizon_bars: int) -> LabelSpec:
    return LabelSpec(
        label_id=f"cost_adjusted_fwd_return_{horizon_bars}b",
        label_schema_version=1,
        label_class="regression",
        horizon_bars=horizon_bars,
        horizon_unit="bars_at_anchor_tf",
        leak_class="future_label_only",
        computed_from=("open", "close"),
        description=(
            f"Cost-adjusted forward log return over {horizon_bars} "
            f"bars: fwd_return_{horizon_bars}b minus a 10 bp "
            f"round-trip cost approximation."
        ),
        cost_model_applied=True,
        tested_in="test_m18_ml.py::G3_ForwardReturns",
    )


# Locked: raw at 5 and 20 bars; cost-adjusted at 5 bars only.
SPECS: tuple = (
    _raw_spec(5),
    _raw_spec(20),
    _cost_spec(5),
)


def _compute_one_horizon(open_arr: np.ndarray,
                          close_arr: np.ndarray,
                          anchor_ts: np.ndarray,
                          horizon: int,
                          cost: float = 0.0):
    """Compute (label_values, resolved_ts_list, is_pending) for a
    single horizon. cost is subtracted from the log return when > 0.
    """
    n = len(open_arr)
    label    = np.full(n, np.nan, dtype=np.float64)
    pending  = np.ones(n, dtype=np.int8)
    resolved = list(empty_resolved_ts_column(n))

    for i in range(n):
        exit_idx = i + horizon
        if i + 1 >= n or exit_idx >= n:
            continue
        entry = open_arr[i + 1]
        exitp = close_arr[exit_idx]
        if not (np.isfinite(entry) and np.isfinite(exitp)
                  and entry > 0 and exitp > 0):
            continue
        raw_log = float(np.log(exitp / entry))
        label[i] = raw_log - cost
        pending[i] = 0
        resolved[i] = pd.Timestamp(anchor_ts[exit_idx])
    return label, resolved, pending


def compute(bars: pd.DataFrame, *,
              round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
              ) -> pd.DataFrame:
    """Compute all three forward-return labels for `bars`.

    Parameters
    ----------
    bars            anchor-TF bars with ts_utc / open / close.
    round_trip_cost fractional cost subtracted from
                      cost_adjusted_fwd_return_5b (default 10 bps).
    """
    open_arr  = bars["open"].astype(float).to_numpy()
    close_arr = bars["close"].astype(float).to_numpy()
    anchor_ts = pd.to_datetime(bars["ts_utc"], utc=True).to_numpy()

    out = pd.DataFrame(index=bars.index)

    # fwd_return_5b
    lab, ts, pend = _compute_one_horizon(
        open_arr, close_arr, anchor_ts, 5, cost=0.0)
    out["fwd_return_5b"] = lab
    out["fwd_return_5b.resolved_ts"] = pd.array(
        ts, dtype="datetime64[ns, UTC]")
    out["fwd_return_5b.is_pending"] = pend

    # fwd_return_20b
    lab, ts, pend = _compute_one_horizon(
        open_arr, close_arr, anchor_ts, 20, cost=0.0)
    out["fwd_return_20b"] = lab
    out["fwd_return_20b.resolved_ts"] = pd.array(
        ts, dtype="datetime64[ns, UTC]")
    out["fwd_return_20b.is_pending"] = pend

    # cost_adjusted_fwd_return_5b
    lab, ts, pend = _compute_one_horizon(
        open_arr, close_arr, anchor_ts, 5,
        cost=round_trip_cost)
    out["cost_adjusted_fwd_return_5b"] = lab
    out["cost_adjusted_fwd_return_5b.resolved_ts"] = pd.array(
        ts, dtype="datetime64[ns, UTC]")
    out["cost_adjusted_fwd_return_5b.is_pending"] = pend

    return align_to_bars(out, bars, group_name="forward_returns")

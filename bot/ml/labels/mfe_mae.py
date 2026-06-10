"""bot.ml.labels.mfe_mae — maximum favorable / adverse excursion labels.

Over a forward window of 50 bars starting at entry (entry = open[i+1]):
  MFE = max(high[i+1 .. i+50])  -  entry_price
  MAE = entry_price            -  min(low[i+1 .. i+50])

Both are NON-NEGATIVE by construction (max/min over the forward
window relative to entry).

Four labels (locked M18 plan):
  mfe_50b              regression  raw MFE in price units (>= 0)
  mae_50b              regression  raw MAE in price units (>= 0)
  mfe_over_atr_50b     regression  MFE / ATR[anchor]  (dimensionless)
  mae_over_atr_50b     regression  MAE / ATR[anchor]  (dimensionless)

The locked plan does NOT include fractional-of-entry pct variants —
ATR-normalization is the canonical scale-free form for this project.

Per-row output columns for each label_id L:
    L                  the label value (NaN if pending)
    L.resolved_ts      UTC ts of forward-window end (NaT if pending)
    L.is_pending       int8

Pending: i + HORIZON >= n  OR  i + 1 >= n.

ATR semantics:
  atr_series must be aligned with bars. NaN ATR yields NaN for the
  over_atr labels but raw mfe_50b/mae_50b still resolve.

Note: a 50-bar horizon matches the triple_barrier timeout, so
MFE_50b/MAE_50b describe the same forward window the triple-barrier
label is observing. This makes them directly interpretable as "the
best/worst the trade looked during the triple-barrier window."
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from bot.ml.schemas import LabelSpec
from bot.ml.labels.base import (
    align_to_bars,
    empty_resolved_ts_column,
)


HORIZON = 50


def _spec(name: str, desc: str,
           computed_from=("open", "high", "low")) -> LabelSpec:
    return LabelSpec(
        label_id=name,
        label_schema_version=1,
        label_class="regression",
        horizon_bars=HORIZON,
        horizon_unit="bars_at_anchor_tf",
        leak_class="future_label_only",
        computed_from=tuple(computed_from),
        description=desc,
        cost_model_applied=False,
        tested_in="test_m18_ml.py::G3_MFE_MAE",
    )


SPECS: tuple = (
    _spec("mfe_50b",
            "Maximum favorable excursion over forward 50 bars "
            "(max(high[i+1..i+50]) - open[i+1]); >= 0."),
    _spec("mae_50b",
            "Maximum adverse excursion over forward 50 bars "
            "(open[i+1] - min(low[i+1..i+50])); >= 0."),
    _spec("mfe_over_atr_50b",
            "mfe_50b / ATR[anchor] — dimensionless. NaN when ATR "
            "is NaN.",
            computed_from=("open", "high", "low",
                            "vol_regime.atr_14_sma_true_range")),
    _spec("mae_over_atr_50b",
            "mae_50b / ATR[anchor] — dimensionless. NaN when ATR "
            "is NaN.",
            computed_from=("open", "high", "low",
                            "vol_regime.atr_14_sma_true_range")),
)


def compute(bars: pd.DataFrame, *,
              atr_series: Optional[pd.Series] = None,
              ) -> pd.DataFrame:
    """Compute MFE/MAE labels at the 50-bar forward horizon.

    Parameters
    ----------
    bars         anchor-TF bars with ts_utc / open / high / low.
    atr_series   optional ATR series aligned with bars. Required for
                   the *_over_atr_50b columns; if omitted, those
                   columns are all NaN but mfe_50b/mae_50b still
                   compute fully.
    """
    n = len(bars)
    if atr_series is not None and len(atr_series) != n:
        raise ValueError(
            f"atr_series length {len(atr_series)} != bars length {n}")

    open_  = bars["open"].astype(float).to_numpy()
    high   = bars["high"].astype(float).to_numpy()
    low    = bars["low"].astype(float).to_numpy()
    anchor_ts = pd.to_datetime(bars["ts_utc"], utc=True).to_numpy()
    atr_arr = (atr_series.astype(float).to_numpy()
                if atr_series is not None else None)

    label_ids = ("mfe_50b", "mae_50b",
                  "mfe_over_atr_50b", "mae_over_atr_50b")
    values = {lid: np.full(n, np.nan, dtype=np.float64)
              for lid in label_ids}
    pending = np.ones(n, dtype=np.int8)
    resolved = list(empty_resolved_ts_column(n))

    for i in range(n):
        if i + 1 >= n:
            continue
        if i + HORIZON >= n:
            continue
        entry = open_[i + 1]
        if not (np.isfinite(entry) and entry > 0):
            continue
        window_hi = high[i + 1 : i + 1 + HORIZON]
        window_lo = low[i + 1 : i + 1 + HORIZON]
        max_hi = float(np.nanmax(window_hi))
        min_lo = float(np.nanmin(window_lo))
        mfe = max_hi - entry
        mae = entry  - min_lo
        # MFE/MAE non-negative by construction; clamp floating-point
        # near-zero negatives.
        if mfe < 0: mfe = 0.0
        if mae < 0: mae = 0.0

        values["mfe_50b"][i] = mfe
        values["mae_50b"][i] = mae
        if atr_arr is not None and np.isfinite(atr_arr[i]) and atr_arr[i] > 0:
            values["mfe_over_atr_50b"][i] = mfe / atr_arr[i]
            values["mae_over_atr_50b"][i] = mae / atr_arr[i]

        pending[i] = 0
        resolved[i] = pd.Timestamp(anchor_ts[i + HORIZON])

    out = pd.DataFrame(index=bars.index)
    for lid in label_ids:
        out[lid] = values[lid]
        out[f"{lid}.resolved_ts"] = pd.array(
            resolved, dtype="datetime64[ns, UTC]")
        out[f"{lid}.is_pending"] = pending
    return align_to_bars(out, bars, group_name="mfe_mae")

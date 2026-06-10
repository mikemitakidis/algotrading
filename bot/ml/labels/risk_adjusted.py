"""bot.ml.labels.risk_adjusted — risk-adjusted forward return label.

Single label (locked M18 plan):

  risk_adjusted_fwd_return_5b      regression
      Forward 5-bar log return divided by the fractional ATR at the
      anchor:
          fwd_log_return_5b  /  (ATR_at_anchor / entry_price)

      This gives a dimensionless "return in units of ATR-implied
      risk" — comparable across symbols and time periods, and
      directly meaningful given the project's ATR-based risk-sizing
      philosophy (M17.B ATR-stop sizing).

Per-row output columns:
    risk_adjusted_fwd_return_5b                 the value (NaN if pending)
    risk_adjusted_fwd_return_5b.resolved_ts     UTC ts of exit bar
                                                  (= ts_utc[i+5])
                                                  NaT if pending
    risk_adjusted_fwd_return_5b.is_pending      int8

NaN policy:
  - Pending: value=NaN, resolved_ts=NaT, is_pending=1.
  - Denominator non-finite or <= 0: value=NaN BUT is_pending=0 if
    the forward return itself resolved. resolved_ts is the forward
    exit bar's ts. This distinguishes "no future data" (pending)
    from "no valid denominator" (resolved-but-undefined).

Entry semantics: entry = open[i+1] — same as every other M18 label.
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


LABEL_ID = "risk_adjusted_fwd_return_5b"
HORIZON  = 5


SPECS: tuple = (
    LabelSpec(
        label_id=LABEL_ID,
        label_schema_version=1,
        label_class="regression",
        horizon_bars=HORIZON,
        horizon_unit="bars_at_anchor_tf",
        leak_class="future_label_only",
        computed_from=("open", "close",
                        "vol_regime.atr_14_sma_true_range"),
        description=(
            "Forward 5-bar log return divided by fractional ATR at "
            "the anchor (ATR / entry_price). Dimensionless; "
            "comparable across symbols and time."
        ),
        cost_model_applied=False,
        tested_in="test_m18_ml.py::G3_RiskAdjusted",
    ),
)


def compute(bars: pd.DataFrame, *,
              atr_series: Optional[pd.Series] = None,
              ) -> pd.DataFrame:
    """Compute risk_adjusted_fwd_return_5b for `bars`.

    Parameters
    ----------
    bars         anchor-TF bars with ts_utc / open / close.
    atr_series   ATR at each anchor (typical:
                   vol_regime.atr_14_sma_true_range). NaN values
                   yield NaN labels even where the forward return
                   resolves. Length MUST equal len(bars).
    """
    n = len(bars)
    if atr_series is not None and len(atr_series) != n:
        raise ValueError("atr_series length mismatch")

    open_  = bars["open"].astype(float).to_numpy()
    close  = bars["close"].astype(float).to_numpy()
    anchor_ts = pd.to_datetime(bars["ts_utc"], utc=True).to_numpy()
    atr_arr = (atr_series.astype(float).to_numpy()
                if atr_series is not None else None)

    value    = np.full(n, np.nan, dtype=np.float64)
    pending  = np.ones(n, dtype=np.int8)
    resolved = list(empty_resolved_ts_column(n))

    for i in range(n):
        if i + 1 >= n or i + HORIZON >= n:
            continue
        entry = open_[i + 1]
        exitp = close[i + HORIZON]
        if not (np.isfinite(entry) and np.isfinite(exitp)
                  and entry > 0 and exitp > 0):
            continue
        fwd_log = float(np.log(exitp / entry))
        pending[i]  = 0
        resolved[i] = pd.Timestamp(anchor_ts[i + HORIZON])

        if atr_arr is not None:
            a = atr_arr[i]
            if np.isfinite(a) and a > 0:
                # over_atr: divide by ATR-as-fraction-of-entry
                value[i] = fwd_log / (a / entry)

    out = pd.DataFrame(index=bars.index)
    out[LABEL_ID]                    = value
    out[f"{LABEL_ID}.resolved_ts"]   = pd.array(
        resolved, dtype="datetime64[ns, UTC]")
    out[f"{LABEL_ID}.is_pending"]    = pending
    return align_to_bars(out, bars, group_name="risk_adjusted")

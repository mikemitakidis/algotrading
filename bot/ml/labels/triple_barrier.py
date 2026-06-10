"""bot.ml.labels.triple_barrier — triple-barrier labels.

Two labels in this group, both keyed by the same triple-barrier
resolution loop:

  triple_barrier_atr_2_3_50          (classification_3way)
      Direction:   LONG-side (M17.B execution is long-only)
      TP multiple: 3 * ATR  (target ABOVE entry)
      SL multiple: 2 * ATR  (stop BELOW entry)
      Timeout:     50 bars after entry
      Tie:         pessimistic_stop_first (same-bar high>=target
                   AND low<=stop → label = -1)

      Outcome:
         +1   take-profit hit before stop within timeout window
         -1   stop hit before target (or same-bar tie)
          0   neither hit within timeout window
         NaN  pending (window exceeds available bars)

      Per-row output columns (5):
        triple_barrier_atr_2_3_50                    label
        triple_barrier_atr_2_3_50.resolved_ts        UTC ts of resolution
        triple_barrier_atr_2_3_50.bars_to_resolution int16
        triple_barrier_atr_2_3_50.return_log_at_resolution
                                                     float64
        triple_barrier_atr_2_3_50.is_pending         int8

  triple_barrier_atr_2_3_50_won      (binary)
      The 3-way label collapsed for binary baselines / logistic
      training:
         1    target hit
         0    stop or timeout
         NaN  pending

      Per-row output columns (3):
        triple_barrier_atr_2_3_50_won                label
        triple_barrier_atr_2_3_50_won.resolved_ts    UTC ts
        triple_barrier_atr_2_3_50_won.is_pending     int8

Both labels share the same resolution event — the binary label is a
deterministic projection of the 3-way label and resolves at the
same bar. This is important: a binary training run sees the SAME
anchor set with the SAME pending mask as the 3-way training run,
which keeps the two model families directly comparable.

Note on the label name:
  '_2_3_50' is a fixed string token. The numeric values inside are
  the literal characters '2', '3', '50' that identify the variant.
  The CODE values are TP_MULT=3.0, SL_MULT=2.0 per the locked M18
  schema. (The name reads as 'atr_<tp_token>_<sl_token>_<timeout>'
  by historical convention in the trading literature; the locked
  M18 plan inverted the token order, which is the source of the
  prior drift. The schema below is the canonical source of truth.)

Entry semantics:
  entry_price = open[anchor + 1] — same as every other M18 label.
  Same-bar fills would be look-ahead; the signal closes at bar i,
  the earliest realistic fill is bar i+1's open.

Pending semantics:
  Pending iff EITHER:
    - i + 1 >= n (no next bar to enter on)
    - i + 1 + TIMEOUT_BARS > n (forward window exceeds bars)
    - atr_series[i] is NaN or <= 0 (cannot size barriers)
  Pending rows: label=NaN, resolved_ts=NaT, bars_to_resolution=0,
  return_log_at_resolution=NaN, is_pending=1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.ml.schemas import LabelSpec
from bot.ml.labels.base import (
    align_to_bars,
    empty_resolved_ts_column,
    log_return,
)


LABEL_ID_3WAY  = "triple_barrier_atr_2_3_50"
LABEL_ID_WON   = "triple_barrier_atr_2_3_50_won"
LABEL_SCHEMA_VERSION = 1
TP_MULT       = 3.0
SL_MULT       = 2.0
TIMEOUT_BARS  = 50


SPECS: tuple = (
    LabelSpec(
        label_id=LABEL_ID_3WAY,
        label_schema_version=LABEL_SCHEMA_VERSION,
        label_class="classification_3way",
        horizon_bars=TIMEOUT_BARS,
        horizon_unit="bars_at_anchor_tf",
        leak_class="future_label_only",
        computed_from=("open", "high", "low", "close",
                        "vol_regime.atr_14_sma_true_range"),
        description=(
            "Triple-barrier label, long-side: TP=3*ATR above entry, "
            "SL=2*ATR below entry, timeout=50 bars. Entry at "
            "open[i+1]. Same-bar high>=target AND low<=stop resolves "
            "as STOP (pessimistic_stop_first)."
        ),
        cost_model_applied=False,
        target_values={
            "+1": "target_hit",
            "-1": "stop_hit_or_same_bar_tie",
            "0":  "timeout",
        },
        tp_mult=TP_MULT,
        sl_mult=SL_MULT,
        atr_source="vol_regime.atr_14_sma_true_range",
        entry_price_source="next_bar_open_after_anchor",
        tie_breaker="pessimistic_stop_first",
        tested_in="test_m18_ml.py::G3_TripleBarrier",
    ),
    LabelSpec(
        label_id=LABEL_ID_WON,
        label_schema_version=LABEL_SCHEMA_VERSION,
        label_class="binary",
        horizon_bars=TIMEOUT_BARS,
        horizon_unit="bars_at_anchor_tf",
        leak_class="future_label_only",
        computed_from=("open", "high", "low", "close",
                        "vol_regime.atr_14_sma_true_range"),
        description=(
            "Binary collapse of triple_barrier_atr_2_3_50: "
            "1 if target hit, 0 if stop or timeout. Shares the "
            "resolution event with the 3-way label."
        ),
        cost_model_applied=False,
        target_values={"1": "target_hit", "0": "stop_or_timeout"},
        tp_mult=TP_MULT,
        sl_mult=SL_MULT,
        atr_source="vol_regime.atr_14_sma_true_range",
        entry_price_source="next_bar_open_after_anchor",
        tie_breaker="pessimistic_stop_first",
        tested_in="test_m18_ml.py::G3_TripleBarrier",
    ),
)


def compute(bars: pd.DataFrame, *,
              atr_series: pd.Series) -> pd.DataFrame:
    """Compute the triple-barrier labels for every anchor in `bars`.

    Returns
    -------
    pd.DataFrame indexed identically to `bars` with both labels'
    output columns. The 3-way label has 5 columns; the binary
    label has 3 columns (it omits bars_to_resolution and
    return_log_at_resolution since those are already on the 3-way
    side and identical at the same anchor).
    """
    if len(atr_series) != len(bars):
        raise ValueError(
            f"atr_series length {len(atr_series)} != bars length "
            f"{len(bars)}; must be aligned")

    n = len(bars)
    open_  = bars["open"].astype(float).to_numpy()
    high   = bars["high"].astype(float).to_numpy()
    low    = bars["low"].astype(float).to_numpy()
    close  = bars["close"].astype(float).to_numpy()
    atr    = atr_series.astype(float).to_numpy()
    anchor_ts = pd.to_datetime(bars["ts_utc"], utc=True).to_numpy()

    # Outputs for the 3-way label (defaults = pending)
    label3      = np.full(n, np.nan, dtype=np.float64)
    bars_to_res = np.zeros(n, dtype=np.int16)
    ret_at_res  = np.full(n, np.nan, dtype=np.float64)
    is_pending3 = np.ones(n, dtype=np.int8)
    resolved_ts_list = list(empty_resolved_ts_column(n))

    # Binary collapse defaults
    label_won   = np.full(n, np.nan, dtype=np.float64)
    is_pending_won = np.ones(n, dtype=np.int8)

    for i in range(n):
        # Need a next bar to enter on, AND a valid ATR.
        if i + 1 >= n:
            continue
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        # Need at least TIMEOUT_BARS forward bars to fully resolve.
        # If fewer are available, mark as PENDING — do NOT
        # opportunistically resolve as timeout (we don't actually
        # know the outcome).
        if i + 1 + TIMEOUT_BARS > n:
            continue

        entry_price = open_[i + 1]
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue

        target_price = entry_price + TP_MULT * atr[i]  # TP=3*ATR
        stop_price   = entry_price - SL_MULT * atr[i]  # SL=2*ATR

        # Scan forward bars [i+1 .. i+TIMEOUT_BARS]
        resolved_at_j = -1
        label_val = 0  # default = timeout (if no hit)
        for j in range(i + 1, i + 1 + TIMEOUT_BARS):
            hit_target = high[j] >= target_price
            hit_stop   = low[j]  <= stop_price
            if hit_target and hit_stop:
                # Same-bar tie: pessimistic_stop_first.
                label_val = -1
                resolved_at_j = j
                break
            if hit_stop:
                label_val = -1
                resolved_at_j = j
                break
            if hit_target:
                label_val = +1
                resolved_at_j = j
                break

        if resolved_at_j < 0:
            # Walked the full window without hit → genuine timeout.
            resolved_at_j = i + TIMEOUT_BARS
            label_val = 0

        label3[i]       = float(label_val)
        bars_to_res[i]  = int(resolved_at_j - i)
        ret_at_res[i]   = log_return(close[resolved_at_j], entry_price)
        is_pending3[i]  = 0
        resolved_ts_list[i] = pd.Timestamp(anchor_ts[resolved_at_j])
        # Binary collapse: 1 if won (target hit) else 0.
        label_won[i]    = 1.0 if label_val == 1 else 0.0
        is_pending_won[i] = 0

    out = pd.DataFrame(index=bars.index)
    # 3-way label and its aux columns
    out[LABEL_ID_3WAY] = label3
    out[f"{LABEL_ID_3WAY}.resolved_ts"] = pd.array(
        resolved_ts_list, dtype="datetime64[ns, UTC]")
    out[f"{LABEL_ID_3WAY}.bars_to_resolution"] = bars_to_res
    out[f"{LABEL_ID_3WAY}.return_log_at_resolution"] = ret_at_res
    out[f"{LABEL_ID_3WAY}.is_pending"] = is_pending3
    # Binary label and its aux columns (shares resolved_ts with 3-way)
    out[LABEL_ID_WON] = label_won
    out[f"{LABEL_ID_WON}.resolved_ts"] = pd.array(
        resolved_ts_list, dtype="datetime64[ns, UTC]")
    out[f"{LABEL_ID_WON}.is_pending"] = is_pending_won
    return align_to_bars(out, bars, group_name="triple_barrier")

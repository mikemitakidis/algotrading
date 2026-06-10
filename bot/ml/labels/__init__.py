"""bot.ml.labels — label compute subpackage.

Every label group is a pure function: `bars: DataFrame, **kwargs
-> labels: DataFrame`. The output has THE SAME row count as `bars`
and includes auxiliary columns per label_id:
    <label_id>                  — the label value (NaN if pending)
    <label_id>.resolved_ts      — UTC ts of resolution (NaT if pending)
    <label_id>.is_pending       — int8 (0 resolved, 1 pending)

Triple-barrier adds two more:
    <label_id>.bars_to_resolution
    <label_id>.return_log_at_resolution

Hard rules (enforced by G3 tests in test_m18_ml.py):
  * leak_class="future_label_only" for every LabelSpec.
  * resolved_ts > anchor_ts STRICTLY for every resolved row
    (label-resolved-after-anchor invariant).
  * Pending rows have value=NaN, resolved_ts=NaT, is_pending=1.
  * Entry semantics: open[anchor+1] — never close[anchor].
  * Same-bar stop/target tie in triple_barrier: pessimistic_stop_first.
  * No bot.backtesting.execution/portfolio/runner imports
    (triple-barrier is reimplemented HERE, not delegated to the
    M17.B executor).

Groups shipped in M18.A.4:
  triple_barrier      primary: triple_barrier_atr_2_3_50
                        (classification_3way; TP=2*ATR, SL=3*ATR,
                        timeout=50 bars)
  forward_returns     fwd_log_ret_{1,5,20} + cost-adjusted variants
                        (regression; cost_model_applied for the
                        _cost_10bps suffix labels)
  mfe_mae             mfe_20, mae_20 raw + pct + ATR-normalized
                        (regression)
  risk_adjusted       fwd_log_ret_20 / fractional-ATR and
                        fwd_log_ret_20 / realized_vol_20
                        (regression)

The dataset assembler (M18.A.5) will register these via
ALL_LABEL_GROUPS and produce one big label table per anchor.
"""
from __future__ import annotations

from bot.ml.labels import base  # noqa: F401
from bot.ml.labels import triple_barrier as _triple_barrier
from bot.ml.labels import forward_returns as _forward_returns
from bot.ml.labels import mfe_mae as _mfe_mae
from bot.ml.labels import risk_adjusted as _risk_adjusted


PRIMARY_LABEL_GROUPS = {
    "triple_barrier": _triple_barrier,
}

SECONDARY_LABEL_GROUPS = {
    "forward_returns": _forward_returns,
    "mfe_mae":         _mfe_mae,
    "risk_adjusted":   _risk_adjusted,
}

ALL_LABEL_GROUPS = {
    **PRIMARY_LABEL_GROUPS,
    **SECONDARY_LABEL_GROUPS,
}

__all__ = ["base", "PRIMARY_LABEL_GROUPS",
            "SECONDARY_LABEL_GROUPS", "ALL_LABEL_GROUPS"]

"""bot.ml.labels.base — shared helpers for label group modules.

Every label group module exposes:
  SPECS: tuple[LabelSpec, ...]
  compute(bars, **kwargs) -> pd.DataFrame

Output column convention:
  <label_id>                  the label value (NaN if pending)
  <label_id>.resolved_ts      tz-aware UTC pd.Timestamp of resolution
                                (NaT if pending)
  <label_id>.is_pending       int8: 0 if resolved, 1 if pending

INVARIANT (asserted by G3 tests):
  For every row where is_pending == 0:
    resolved_ts must be strictly > anchor_ts (the bar's own ts_utc).
  This is the canonical "no same-bar leak" check — a label that
  resolves AT the anchor would be using close-time information that
  isn't yet known at signal time.
"""
from __future__ import annotations

from typing import Protocol, Tuple

import numpy as np
import pandas as pd

from bot.ml.schemas import LabelSpec


class LabelGroupModule(Protocol):
    """Each bot/ml/labels/<group>.py module must satisfy this contract."""
    SPECS: Tuple[LabelSpec, ...]

    def compute(self, bars: pd.DataFrame, **kwargs) -> pd.DataFrame:
        ...


def align_to_bars(out: pd.DataFrame, bars: pd.DataFrame,
                   *, group_name: str) -> pd.DataFrame:
    """Defensive: assert the output frame has the same row count as
    the input bars. Same contract as features.base.align_to_bars but
    we don't enforce the ts_utc cross-check because labels add their
    own resolved_ts columns that differ from input ts_utc."""
    if len(out) != len(bars):
        raise ValueError(
            f"label group {group_name!r}: output rows ({len(out)}) "
            f"!= input bars ({len(bars)}). Label compute must NOT "
            f"drop rows — emit is_pending=1 + NaN for windows that "
            f"exceed available data.")
    return out


def assert_label_resolved_after_anchor(
    bars: pd.DataFrame,
    label_id: str,
    label_df: pd.DataFrame,
) -> None:
    """For every row where <label_id>.is_pending == 0, verify that
    <label_id>.resolved_ts > bars["ts_utc"]. Raises AssertionError
    listing offenders.

    This is the canonical M18.A.4 invariant. Every label group's
    G3 test calls this as a final check.
    """
    pending_col = f"{label_id}.is_pending"
    ts_col      = f"{label_id}.resolved_ts"
    if pending_col not in label_df.columns:
        raise AssertionError(
            f"label group missing {pending_col!r} column")
    if ts_col not in label_df.columns:
        raise AssertionError(
            f"label group missing {ts_col!r} column")
    resolved_mask = label_df[pending_col].to_numpy() == 0
    if not resolved_mask.any():
        return  # nothing resolved → trivially holds
    anchor_ts = pd.to_datetime(
        bars["ts_utc"], utc=True).to_numpy()
    resolved_ts = pd.to_datetime(
        label_df[ts_col], utc=True).to_numpy()
    # Compare element-wise on resolved rows only.
    offenders_idx = np.where(
        resolved_mask & ~(resolved_ts > anchor_ts))[0]
    if len(offenders_idx) > 0:
        # Build a small offender table to help debugging.
        offending_rows = [
            (int(i), str(anchor_ts[i]), str(resolved_ts[i]))
            for i in offenders_idx[:5]
        ]
        raise AssertionError(
            f"label {label_id!r}: resolved_ts must be > anchor_ts "
            f"for every resolved row; found {len(offenders_idx)} "
            f"offenders (showing up to 5): {offending_rows}")


def empty_resolved_ts_column(n: int) -> pd.Series:
    """A length-n column of NaT (tz-aware UTC) — the default for
    resolved_ts before any label resolves."""
    return pd.Series(
        pd.array([pd.NaT] * n, dtype="datetime64[ns, UTC]"))


def log_return(p1: float, p0: float) -> float:
    """log(p1 / p0) with NaN on non-positive prices."""
    if p0 <= 0 or p1 <= 0 or not np.isfinite(p0) or not np.isfinite(p1):
        return float("nan")
    return float(np.log(p1 / p0))

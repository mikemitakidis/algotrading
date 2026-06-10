"""bot.ml.features.base — common helpers for feature group modules.

Every feature group module exports:
  SPECS: tuple[FeatureSpec, ...]   the schema for this group
  compute(bars) -> DataFrame        the computation

This module provides:
  * FeatureGroupModule  Protocol for the above contract
  * align_to_bars       ensure output DataFrame matches input row count
  * assert_no_lookahead  defensive check that no output column has a
                           non-NaN value before its first valid index
                           (used in G2 tests, not at runtime)
"""
from __future__ import annotations

from typing import Protocol, Tuple

import pandas as pd

from bot.ml.schemas import FeatureSpec


class FeatureGroupModule(Protocol):
    """Each bot/ml/features/<group>.py module must expose this contract."""
    SPECS: Tuple[FeatureSpec, ...]

    def compute(self, bars: pd.DataFrame) -> pd.DataFrame:
        ...


def align_to_bars(out: pd.DataFrame, bars: pd.DataFrame,
                   *, group_name: str) -> pd.DataFrame:
    """Defensive: assert the output frame has the same row count as
    the input bars and the same ts_utc column. Returns `out` unchanged
    on success; raises on mismatch.

    This catches accidental .dropna() / .iloc filter bugs in feature
    code before they cause silent row misalignment when groups are
    joined on ts_utc by the dataset assembler.
    """
    if len(out) != len(bars):
        raise ValueError(
            f"feature group {group_name!r}: output rows ({len(out)}) "
            f"!= input bars ({len(bars)}). Feature compute must NOT "
            f"drop rows — emit NaN at the warmup boundary instead."
        )
    # Same ts_utc values (defensive — out.index should mirror bars.index)
    if "ts_utc" in out.columns and "ts_utc" in bars.columns:
        if not (out["ts_utc"].reset_index(drop=True) ==
                  bars["ts_utc"].reset_index(drop=True)).all():
            raise ValueError(
                f"feature group {group_name!r}: ts_utc values differ "
                f"between output and input")
    return out


def compute_log_return(close: pd.Series, periods: int) -> pd.Series:
    """log(close_t / close_{t-periods}). NaN at warmup boundary.

    Used by price_return and reused by momentum (acceleration).
    Implementation here keeps it in one place so all callers agree
    on edge cases (e.g. zero-or-negative prices → NaN).
    """
    import numpy as np
    if periods <= 0:
        raise ValueError(f"periods must be positive, got {periods}")
    prev = close.shift(periods)
    # log returns require strictly positive prices; replace
    # non-positive with NaN to avoid -inf / NaN-in-log noise.
    safe_close = close.where(close > 0)
    safe_prev = prev.where(prev > 0)
    return (safe_close / safe_prev).apply(
        lambda x: float("nan") if pd.isna(x) else float(
            __import__("math").log(x)))


def compute_simple_return(close: pd.Series, periods: int) -> pd.Series:
    """(close_t / close_{t-periods}) - 1. NaN at warmup boundary."""
    if periods <= 0:
        raise ValueError(f"periods must be positive, got {periods}")
    prev = close.shift(periods)
    return (close / prev) - 1.0

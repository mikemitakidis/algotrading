"""bot.ml.dataset.walk_forward — purged walk-forward split with embargo.

Produces a SINGLE chronological train/val/test split (multi-fold
walk-forward can layer on later in M18.A.6 if needed). Encodes:

  * Pending exclusion (must be applied UPSTREAM by the assembler
    before passing anchor_indices in; this module trusts its inputs)
  * Embargo zone on either side of val/test on the train side
  * label_resolved_ts overlap purge: any train anchor whose label
    resolution timestamp falls inside [val_start_ts, test_end_ts]
    is purged from the train set (it would otherwise leak future
    information into the train fold)

Inputs are integer positions (indices) into a base bar series — NOT
the bar series itself — to keep this module data-shape-agnostic.

Outputs include explicit purge/embargo counts so the manifest can
record them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class WalkForwardSplit:
    """Result of a single train/val/test walk-forward split."""
    train_anchor_indices: np.ndarray
    val_anchor_indices:   np.ndarray
    test_anchor_indices:  np.ndarray
    train_start_ts:       pd.Timestamp
    train_end_ts:         pd.Timestamp
    val_start_ts:         pd.Timestamp
    val_end_ts:           pd.Timestamp
    test_start_ts:        pd.Timestamp
    test_end_ts:          pd.Timestamp
    purged_count:    int   # train rows purged due to label overlap
    embargoed_count: int   # train rows excluded by embargo zone
    embargo_bars:    int


def _slice_by_fraction(
    n: int, train_frac: float, val_frac: float, test_frac: float,
) -> Tuple[int, int, int, int]:
    """Return (train_lo, train_hi, val_hi, test_hi) integer slice
    indices for a chronological n-anchor sequence.

    Slices use Python half-open semantics: train = [0, train_hi),
    val = [train_hi, val_hi), test = [val_hi, test_hi).
    """
    if not (0.0 < train_frac < 1.0 and 0.0 < val_frac < 1.0
              and 0.0 < test_frac < 1.0):
        raise ValueError(
            "train_frac / val_frac / test_frac each must be in (0, 1)")
    tot = train_frac + val_frac + test_frac
    if abs(tot - 1.0) > 1e-9:
        raise ValueError(
            f"train_frac + val_frac + test_frac must sum to 1.0, "
            f"got {tot}")
    train_hi = int(n * train_frac)
    val_hi   = int(n * (train_frac + val_frac))
    test_hi  = n
    if train_hi < 1 or val_hi <= train_hi or test_hi <= val_hi:
        raise ValueError(
            f"Anchor count too small for the requested split fractions: "
            f"n={n}, train_frac={train_frac}, val_frac={val_frac}, "
            f"test_frac={test_frac}; computed indices "
            f"train_hi={train_hi}, val_hi={val_hi}, test_hi={test_hi}")
    return 0, train_hi, val_hi, test_hi


def make_walk_forward_split(
    anchor_indices: np.ndarray,
    anchor_ts:      np.ndarray,
    *,
    label_resolved_ts: Dict[str, pd.Series],
    train_frac: float = 0.6,
    val_frac:   float = 0.2,
    test_frac:  float = 0.2,
    embargo_bars: int = 130,
) -> WalkForwardSplit:
    """Build a chronological train/val/test split with purge + embargo.

    Parameters
    ----------
    anchor_indices : np.ndarray
        Sorted, unique integer positions into the BASE bar series.
        Pending anchors must already be EXCLUDED upstream.
    anchor_ts : np.ndarray
        UTC datetime64[ns] aligned with anchor_indices (same length).
    label_resolved_ts : dict
        Map label_id -> pd.Series of UTC timestamps aligned with
        anchor_indices. NaT is allowed (won't trigger overlap purge).
        Used to compute the overlap purge: any train anchor where
        ANY label's resolved_ts >= val_start_ts is purged.
    train_frac / val_frac / test_frac
        Must each be in (0, 1) and sum to 1.0.
    embargo_bars : int
        Bars of buffer on EACH side of the val/test boundary on the
        train side. Anchors within `embargo_bars` of the val_start
        (in anchor-index space) are removed from train.

    Returns
    -------
    WalkForwardSplit
    """
    n = len(anchor_indices)
    if len(anchor_ts) != n:
        raise ValueError(
            f"anchor_ts length {len(anchor_ts)} != anchor_indices "
            f"length {n}")
    if not np.all(np.diff(anchor_indices) > 0):
        raise ValueError("anchor_indices must be strictly increasing")
    if embargo_bars < 0:
        raise ValueError(
            f"embargo_bars must be >= 0, got {embargo_bars}")
    for lid, ts_series in label_resolved_ts.items():
        if len(ts_series) != n:
            raise ValueError(
                f"label_resolved_ts[{lid!r}] length {len(ts_series)} "
                f"!= anchor_indices length {n}")

    train_lo, train_hi, val_hi, test_hi = _slice_by_fraction(
        n, train_frac, val_frac, test_frac)

    # Raw slices (anchor positions)
    raw_train_pos = np.arange(train_lo, train_hi)
    val_pos       = np.arange(train_hi, val_hi)
    test_pos      = np.arange(val_hi, test_hi)

    val_start_ts  = pd.Timestamp(anchor_ts[train_hi])
    val_end_ts    = pd.Timestamp(anchor_ts[val_hi - 1])
    test_start_ts = pd.Timestamp(anchor_ts[val_hi])
    test_end_ts   = pd.Timestamp(anchor_ts[test_hi - 1])
    train_start_ts = pd.Timestamp(anchor_ts[train_lo])
    train_end_ts   = pd.Timestamp(anchor_ts[train_hi - 1])

    # Embargo: drop train rows whose anchor position is within
    # `embargo_bars` of train_hi (the val_start boundary). In
    # anchor-INDEX space, that's positions in [train_hi - embargo_bars,
    # train_hi).
    embargo_lo_pos = max(train_lo, train_hi - embargo_bars)
    embargo_mask_train = raw_train_pos < embargo_lo_pos
    after_embargo_pos = raw_train_pos[embargo_mask_train]
    embargoed_count = int(len(raw_train_pos) - len(after_embargo_pos))

    # Purge: any remaining train row whose label_resolved_ts >=
    # val_start_ts leaks into the future cohort.
    train_keep_mask = np.ones(len(after_embargo_pos), dtype=bool)
    for lid, ts_series in label_resolved_ts.items():
        # Map positions to the timestamps; NaT is treated as "no
        # resolution yet" → cannot leak → keep.
        ts_at_pos = pd.to_datetime(
            ts_series.iloc[after_embargo_pos], utc=True)
        # NaT-safe comparison: NaT >= anything is False in pandas
        # when wrapped through fillna(False) — pandas returns a
        # masked NA otherwise.
        is_leak = (ts_at_pos >= val_start_ts).fillna(False).to_numpy()
        train_keep_mask &= ~is_leak

    train_after_purge = after_embargo_pos[train_keep_mask]
    purged_count = int(np.sum(~train_keep_mask))

    # Convert positions back to base-series indices via the supplied
    # anchor_indices mapping.
    train_idx = anchor_indices[train_after_purge]
    val_idx   = anchor_indices[val_pos]
    test_idx  = anchor_indices[test_pos]

    return WalkForwardSplit(
        train_anchor_indices=train_idx,
        val_anchor_indices=val_idx,
        test_anchor_indices=test_idx,
        train_start_ts=train_start_ts,
        train_end_ts=train_end_ts,
        val_start_ts=val_start_ts,
        val_end_ts=val_end_ts,
        test_start_ts=test_start_ts,
        test_end_ts=test_end_ts,
        purged_count=purged_count,
        embargoed_count=embargoed_count,
        embargo_bars=int(embargo_bars),
    )


# Standard "5 trading days" embargo defaults, per anchor TF.
# US regular hours = 6.5 hr/day. 15m → 26 bars/day; 1H → 7 bars/day
# (rounded up); 4H → 2 bars/day; 1D → 1 bar/day.
_BARS_PER_TRADING_DAY = {"15m": 26, "1H": 7, "4H": 2, "1D": 1}


def default_embargo_bars(anchor_tf: str,
                          trading_days: int = 5) -> int:
    """Convert a trading-day embargo into anchor-TF bars."""
    if anchor_tf not in _BARS_PER_TRADING_DAY:
        raise ValueError(
            f"unknown anchor_tf {anchor_tf!r}; expected one of "
            f"{sorted(_BARS_PER_TRADING_DAY)}")
    return int(trading_days * _BARS_PER_TRADING_DAY[anchor_tf])

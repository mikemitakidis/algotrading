"""bot.ml.dataset.anchors — anchor-set enumeration for ML training.

Two anchor sets are supported, per the locked SR-6 / Q18 plan:

  ANCHOR_SET_MODEL_A_SCANNER_REPLICA  ("model_a_scanner_replica"):
      The meta-label cohort. Anchors are exactly the rows where
      scanner_replica.signal_fires == 1 — i.e. the bars where the
      live scanner would have produced a candidate. Used to train
      "given the scanner already says yes, will the trade work?"

  ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES  ("model_b_1h_union_candidates"):
      The candidate-quality cohort. Per Q18 correction:
          anchors = (all 1H anchors mapped to anchor-TF index)
                    UNION (all scanner_replica candidate anchors)
      NOT a 1H-as-superset rule. The union catches setups the scanner
      would have missed (1H-only) AND setups the scanner already
      flagged (scanner candidates).

Both functions operate on integer positions into the anchor-TF
DataFrame (the 15m bar series typically). The caller is responsible
for ensuring `scanner_replica_fires` is the int8 feature column from
bot.ml.features.scanner_replica, aligned by row to the anchor bars.

For 1H→anchor mapping in Model B: each 1H bar's close ts_utc is
located in the anchor-TF ts_utc array via at-or-equal lookup (the
1H close should coincide with a 15m bar boundary in the live
scanner's cadence; if not, we use the most-recent 15m bar at-or-
before, matching MultiTimeframeContext.snapshot_at semantics).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


ANCHOR_SET_MODEL_A_SCANNER_REPLICA      = "model_a_scanner_replica"
ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES  = "model_b_1h_union_candidates"

ALLOWED_ANCHOR_SETS = frozenset({
    ANCHOR_SET_MODEL_A_SCANNER_REPLICA,
    ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
})


def enumerate_model_a_anchors(scanner_replica_fires: pd.Series,
                                ) -> np.ndarray:
    """Return integer positions where scanner_replica.signal_fires == 1.

    Parameters
    ----------
    scanner_replica_fires : pd.Series
        The int8 column from bot.ml.features.scanner_replica.compute
        (column name 'scanner_replica.signal_fires'), with values
        in {0, 1}. NaN-valued rows are treated as 0 (no candidate).

    Returns
    -------
    np.ndarray
        Sorted integer positions (0-indexed into the original series)
        where the scanner would have fired a candidate.
    """
    if not isinstance(scanner_replica_fires, pd.Series):
        raise TypeError(
            "scanner_replica_fires must be a pd.Series, got "
            f"{type(scanner_replica_fires).__name__}")
    arr = scanner_replica_fires.to_numpy()
    # Treat NaN as 0 (no candidate). Comparison with NaN is False.
    mask = (arr == 1)
    return np.flatnonzero(mask).astype(np.int64)


def enumerate_model_b_anchors(
    anchor_ts: pd.Series,
    one_hour_ts: pd.Series,
    scanner_replica_fires: pd.Series,
) -> np.ndarray:
    """Return the Model B anchor set per Q18:
        (1H bar closes mapped to anchor-TF index)
        UNION
        (scanner_replica candidate anchor-TF index)

    Parameters
    ----------
    anchor_ts : pd.Series
        UTC-aware ts_utc of every bar in the anchor TF (e.g. 15m).
    one_hour_ts : pd.Series
        UTC-aware ts_utc of every 1H bar. May be empty (then the
        Model B set degenerates to the scanner candidate set).
    scanner_replica_fires : pd.Series
        scanner_replica.signal_fires aligned with anchor_ts.

    Returns
    -------
    np.ndarray
        Sorted unique integer positions (0-indexed into anchor_ts).
    """
    if len(scanner_replica_fires) != len(anchor_ts):
        raise ValueError(
            "scanner_replica_fires length must equal anchor_ts length")

    # 1H-mapped indices: for each 1H ts, find the at-or-before
    # position in anchor_ts via searchsorted. -1 (no anchor at-or-
    # before that 1H close) is dropped.
    if len(one_hour_ts) > 0:
        anchor_arr = pd.to_datetime(anchor_ts, utc=True).to_numpy()
        oh_arr     = pd.to_datetime(one_hour_ts, utc=True).to_numpy()
        # searchsorted side='right' minus 1 gives at-or-before index
        positions = np.searchsorted(anchor_arr, oh_arr,
                                       side="right") - 1
        oh_idx = positions[positions >= 0]
    else:
        oh_idx = np.array([], dtype=np.int64)

    scanner_idx = enumerate_model_a_anchors(scanner_replica_fires)

    # Union, sorted, unique
    union = np.union1d(oh_idx, scanner_idx)
    return union.astype(np.int64)


def enumerate_anchors(
    anchor_set: str,
    anchor_ts: pd.Series,
    scanner_replica_fires: pd.Series,
    *,
    one_hour_ts: Optional[pd.Series] = None,
) -> np.ndarray:
    """Dispatch over the two allowed anchor-set strings."""
    if anchor_set not in ALLOWED_ANCHOR_SETS:
        raise ValueError(
            f"anchor_set must be one of "
            f"{sorted(ALLOWED_ANCHOR_SETS)}, got {anchor_set!r}")
    if anchor_set == ANCHOR_SET_MODEL_A_SCANNER_REPLICA:
        return enumerate_model_a_anchors(scanner_replica_fires)
    # Model B
    if one_hour_ts is None:
        raise ValueError(
            "one_hour_ts is required for Model B anchor enumeration")
    return enumerate_model_b_anchors(
        anchor_ts=anchor_ts,
        one_hour_ts=one_hour_ts,
        scanner_replica_fires=scanner_replica_fires,
    )

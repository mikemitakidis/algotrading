"""bot.backtesting.mtf_context — Multi-timeframe context for scanner_replica.

Phase 3 of M17.B. The live scanner runs once per "cycle" — at each
cycle it looks at the most recent bar on each timeframe and scores
them. To reproduce that in a backtest we need:

1. A canonical "cycle anchor" sequence — when does each backtest cycle
   happen? Per Sharpened Rule #3 the anchor is the FINEST enabled TF's
   bar close (default 15m).

2. At each anchor, a look-ahead-safe snapshot of the most-recent-bar
   from EACH timeframe whose close is at or before the anchor. Higher
   TF bars don't align with the anchor; we use 'most recent closed
   bar with ts_utc <= anchor_ts'.

Per Sharpened Rule #2 (performance discipline):
  * Indicators are precomputed once per TF as full vectorized Series
    by the caller (M17.B.1 indicators are already vectorised).
  * snapshot_at(anchor_ts) does NO recomputation — pure searchsorted
    index lookup, O(log n) per TF.
  * No rolling-window arithmetic inside the per-anchor loop.

Per Sharpened Rule #4 (no live imports):
  * This module imports only stdlib + pandas + numpy + sibling modules.
  * It does NOT import bot.scanner / bot.strategy / bot.feature_engine
    or any live module. The AST guard enforces this.

Public API:
    MultiTimeframeContext(per_tf_bars: dict, anchor_tf: str)
        .anchors() -> Iterator[pd.Timestamp]
        .snapshot_at(anchor_ts) -> dict[tf_label -> SnapshotBar | None]

The SnapshotBar contract: a frozen dataclass holding the bar's
ts_utc + the bar INDEX into its source DataFrame. Callers (M17.B.4
scanner_replica) use the index to read precomputed indicator Series
without re-slicing the bars DataFrame.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, Optional, Tuple

import numpy as np
import pandas as pd

from bot.backtesting.errors import BacktestError


__all__ = [
    "MultiTimeframeContext",
    "SnapshotBar",
    "MtfContextError",
]


class MtfContextError(BacktestError):
    """Raised when the multi-TF context cannot be constructed (e.g.,
    anchor TF missing from the provided bars dict, or anchor TF has
    no bars)."""


@dataclass(frozen=True)
class SnapshotBar:
    """The 'most recent closed bar at or before anchor' for one TF.

    `idx` is the integer position into the source TF's bars DataFrame
    — used by scanner_replica to index precomputed indicator Series
    without rolling-recomputation. `ts_utc` is the bar's own close
    timestamp (never the anchor's).
    """
    timeframe: str
    idx:       int                 # position into per_tf_bars[tf]
    ts_utc:    pd.Timestamp        # the bar's own ts_utc (UTC-aware)


class MultiTimeframeContext:
    """Look-ahead-safe access to per-TF bars at each cycle anchor.

    Construct ONCE from the dict returned by load_multi_tf_bars.per_tf_bars
    (or a compatible subset). Then iterate anchors() and call
    snapshot_at() per anchor. All work is pre-bucketed at __init__;
    per-anchor calls are O(log n) per TF.
    """

    def __init__(
        self,
        per_tf_bars: Dict[str, pd.DataFrame],
        anchor_tf: str,
    ):
        """Build the context.

        Args:
            per_tf_bars: dict TF label -> bars DataFrame. Each DataFrame
                must have a 'ts_utc' column (UTC-aware, sorted ascending).
                None values are tolerated (PARTIAL mode placeholders) —
                that TF is silently dropped from the available set.
                Empty DataFrames are tolerated similarly (dropped).
            anchor_tf:   TF label whose bar closes drive the cycle
                cadence. Per Sharpened Rule #3 this is the finest
                enabled TF (default 15m when present).

        Raises:
            MtfContextError if anchor_tf is missing from per_tf_bars,
            or anchor_tf has None / empty bars.
        """
        if anchor_tf not in per_tf_bars:
            raise MtfContextError(
                f"anchor_tf={anchor_tf!r} not in per_tf_bars keys "
                f"{sorted(per_tf_bars.keys())}; cannot anchor without "
                f"anchor TF data")
        anchor_bars = per_tf_bars[anchor_tf]
        if anchor_bars is None or len(anchor_bars) == 0:
            raise MtfContextError(
                f"anchor_tf={anchor_tf!r} has no bars; cannot enumerate "
                f"cycle anchors")

        # Filter out None / empty entries — PARTIAL mode may pass us
        # placeholders. Drop them so snapshot_at() never has to.
        self._per_tf_bars: Dict[str, pd.DataFrame] = {}
        self._per_tf_ts:   Dict[str, np.ndarray]   = {}
        for tf, df in per_tf_bars.items():
            if df is None or len(df) == 0:
                continue
            # Normalise ts_utc to a numpy datetime64[ns] array for
            # searchsorted. Tz-aware pd.Timestamp arrays can be searched
            # directly via pd.DatetimeIndex.searchsorted but using the
            # underlying numpy values keeps lookups fastest.
            ts_col = pd.to_datetime(df["ts_utc"], utc=True)
            self._per_tf_bars[tf] = df.reset_index(drop=True)
            self._per_tf_ts[tf]   = ts_col.values

        self._anchor_tf = anchor_tf
        # The anchor sequence is the anchor TF's ts_utc values —
        # one anchor per bar close.
        self._anchor_ts = self._per_tf_ts[anchor_tf]

    # ── Public properties ────────────────────────────────────────────

    @property
    def anchor_tf(self) -> str:
        return self._anchor_tf

    @property
    def available_timeframes(self) -> Tuple[str, ...]:
        """TFs with actual bars available (None/empty entries dropped
        at __init__)."""
        return tuple(self._per_tf_bars.keys())

    @property
    def num_anchors(self) -> int:
        return int(self._anchor_ts.shape[0])

    # ── Iteration ────────────────────────────────────────────────────

    def anchors(self) -> Iterator[pd.Timestamp]:
        """Yield each cycle anchor as a UTC-aware pd.Timestamp, in
        chronological order."""
        for v in self._anchor_ts:
            yield pd.Timestamp(v).tz_localize("UTC") \
                  if pd.Timestamp(v).tz is None \
                  else pd.Timestamp(v).tz_convert("UTC")

    # ── Snapshot ─────────────────────────────────────────────────────

    def snapshot_at(
        self, anchor_ts: pd.Timestamp,
    ) -> Dict[str, Optional[SnapshotBar]]:
        """Most-recent-bar-per-TF whose close is at or before
        `anchor_ts`. Returns dict tf_label -> SnapshotBar or None.

        Look-ahead guarantee: no bar with ts > anchor_ts is ever
        returned. For the anchor TF specifically, the snapshot IS the
        bar whose ts_utc equals anchor_ts (i.e. idx is at the anchor
        position, not before it).

        Performance: O(log n_tf) per TF via numpy.searchsorted. No
        rolling recomputation. No DataFrame slicing.

        If a TF has no bar at or before the anchor (e.g. the anchor
        is earlier than the TF's first bar), the entry is None — the
        caller decides whether that counts as "TF unavailable at this
        anchor" (per Sharpened Rule #3 partial-anchor semantics).
        """
        # Normalise anchor_ts to UTC-aware
        if not isinstance(anchor_ts, pd.Timestamp):
            anchor_ts = pd.Timestamp(anchor_ts)
        if anchor_ts.tz is None:
            anchor_ts = anchor_ts.tz_localize("UTC")
        else:
            anchor_ts = anchor_ts.tz_convert("UTC")
        # Use the underlying numpy datetime64 for the search; convert
        # the anchor to the same dtype.
        anchor_np = np.datetime64(anchor_ts.tz_convert("UTC").tz_localize(None))

        out: Dict[str, Optional[SnapshotBar]] = {}
        for tf, ts_arr in self._per_tf_ts.items():
            # searchsorted side='right' gives the insertion point for
            # the anchor — the count of bars with ts <= anchor. We
            # want the latest such bar, so idx = side_right - 1.
            # Normalize tz: if ts_arr is datetime64[ns, UTC], strip tz
            # for searchsorted, since numpy doesn't allow comparing
            # tz-aware to tz-naive.
            if hasattr(ts_arr, "dtype") and "datetime64[ns," in str(ts_arr.dtype):
                # tz-aware numpy array — pandas-style; compare via
                # pd.DatetimeIndex which respects tz
                idx_right = pd.DatetimeIndex(ts_arr).searchsorted(
                    anchor_ts, side="right")
            else:
                # tz-naive numpy datetime64
                idx_right = int(np.searchsorted(ts_arr, anchor_np,
                                                  side="right"))
            i = int(idx_right) - 1
            if i < 0:
                out[tf] = None
                continue
            ts_i = pd.Timestamp(ts_arr[i])
            if ts_i.tz is None:
                ts_i = ts_i.tz_localize("UTC")
            else:
                ts_i = ts_i.tz_convert("UTC")
            out[tf] = SnapshotBar(timeframe=tf, idx=i, ts_utc=ts_i)
        return out

"""bot.ml.features.signal_history — past-only signal-outcome features.

All features here are leak_class="requires_past_flywheel_only".
They depend on the flywheel-DB signal_outcomes table, which records
RESOLVED signal outcomes (WIN / LOSS / TIMEOUT). The reader applies
a strict `resolved_at < anchor_ts` filter, so no information after
the anchor leaks in.

When the DB is missing OR the symbol has no resolved history in the
lookback window, every feature returns NaN for that row. This is the
"no-history regime" — the model can learn that "I have no past data
on this symbol" is its own signal (often the early-life regime).

Features (5 — for each lookback window we expose count + win_rate):
  signals_count_30d        int16   resolved signals in past 30 days
  signals_count_90d        int16   resolved signals in past 90 days
  win_rate_30d             float64 wins / total in past 30 days
                                     (NaN if signals_count_30d == 0)
  win_rate_90d             float64 wins / total in past 90 days
                                     (NaN if signals_count_90d == 0)
  avg_return_pct_90d       float64 mean(return_pct) over past 90 days
                                     resolved signals (NaN if none)

Why no 60d window? The 30d / 90d pair gives a "very recent" vs
"medium-term" view. Adding 60d would add a feature that's highly
correlated with both and inflates the feature count without adding
information. Operator can request 60d in a later phase if eval shows
it useful.

DEPENDENCY: bot.ml.dataset.flywheel_reader (M18-owned). Production
bot/ml/* does NOT import bot.flywheel or bot.db.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd

from bot.ml.dataset.flywheel_reader import FlywheelReader
from bot.ml.schemas import FeatureSpec
from bot.ml.features.base import align_to_bars


GROUP_NAME = "signal_history"
GROUP_VERSION = 1


def _spec(name: str, *, dtype: str, desc: str,
           value_range=None) -> FeatureSpec:
    return FeatureSpec(
        feature_id=f"{GROUP_NAME}.{name}",
        feature_group=GROUP_NAME,
        feature_group_version=GROUP_VERSION,
        dtype=dtype,
        leak_class="requires_past_flywheel_only",
        lookback_bars=0,    # the lookback unit is DAYS, not bars
        lookback_unit="bars_at_this_tf",
        computed_from=("__flywheel_signal_outcomes__",),
        description=desc,
        value_range=value_range,
        live_compatible=False,
        live_compatible_with=None,
        tested_in="test_m18_ml.py::G2_SignalHistory",
    )


SPECS: tuple = (
    _spec("signals_count_30d", dtype="int16",
            desc="count of resolved signals (WIN/LOSS/TIMEOUT) for "
                  "this symbol in the 30 days strictly before the anchor"),
    _spec("signals_count_90d", dtype="int16",
            desc="count of resolved signals in the 90 days before anchor"),
    _spec("win_rate_30d", dtype="float64",
            desc="wins / count in past 30 days (NaN if count=0)",
            value_range=(0.0, 1.0)),
    _spec("win_rate_90d", dtype="float64",
            desc="wins / count in past 90 days (NaN if count=0)",
            value_range=(0.0, 1.0)),
    _spec("avg_return_pct_90d", dtype="float64",
            desc="mean return_pct over past 90 days (NaN if count=0)"),
)


def _aggregate(rows: pd.DataFrame, *, upper: pd.Timestamp,
                lookback_days: int) -> dict:
    """Compute (count, win_rate, avg_return) restricted to rows
    whose resolved_at is in [upper - lookback_days, upper).

    `rows` is the already-filtered 90-day frame from the reader; we
    further restrict by date here for the 30-day bucket (cheap
    in-memory). Returns dict with the three values; NaN for win_rate
    / avg_return when count is 0.
    """
    if rows.empty:
        return {"count": 0, "win_rate": float("nan"),
                "avg_return": float("nan")}
    lower = (upper - pd.Timedelta(days=lookback_days))
    # resolved_at is a TEXT ISO timestamp; compare via pd.to_datetime
    ts = pd.to_datetime(rows["resolved_at"], utc=True, errors="coerce")
    mask = (ts >= lower) & (ts < upper)
    sub = rows.loc[mask.values]
    n = len(sub)
    if n == 0:
        return {"count": 0, "win_rate": float("nan"),
                "avg_return": float("nan")}
    wins = int((sub["outcome"] == "WIN").sum())
    avg_ret_series = pd.to_numeric(sub["return_pct"], errors="coerce")
    avg_ret = float(avg_ret_series.mean()) if not avg_ret_series.isna(
        ).all() else float("nan")
    return {"count": n, "win_rate": wins / n, "avg_return": avg_ret}


def compute(bars: pd.DataFrame, *, symbol: str,
              flywheel_reader: Optional[FlywheelReader] = None,
              db_path: Optional[Union[str, Path]] = None,
              ) -> pd.DataFrame:
    """Compute signal_history features for `bars`.

    Parameters
    ----------
    bars             anchor-TF bars; index defines output rows.
    symbol           symbol to look up.
    flywheel_reader  pre-constructed reader. If None and db_path is
                       also None, all features come out as NaN for
                       every row (the "no-history regime").
    db_path          alternative to flywheel_reader — path to
                       signals.db. A FlywheelReader is constructed
                       on demand.

    Returns
    -------
    pd.DataFrame indexed identically to `bars` with the 5 features.
    """
    n = len(bars)
    out = pd.DataFrame(index=bars.index)

    # Resolve the reader. If we have neither AND no db_path, we are in
    # no-history mode → emit all NaN/zero.
    reader: Optional[FlywheelReader] = flywheel_reader
    if reader is None and db_path is not None:
        reader = FlywheelReader(db_path)

    cnt_30  = np.zeros(n, dtype=np.int16)
    cnt_90  = np.zeros(n, dtype=np.int16)
    win_30  = np.full(n, np.nan, dtype=np.float64)
    win_90  = np.full(n, np.nan, dtype=np.float64)
    avg_90  = np.full(n, np.nan, dtype=np.float64)

    if reader is not None and reader.is_available():
        anchor_ts_series = pd.to_datetime(bars["ts_utc"], utc=True)
        for i in range(n):
            anchor_ts = anchor_ts_series.iloc[i]
            # Pull the 90d window in one query, then aggregate both
            # the 30d and 90d buckets in memory.
            window = reader.closed_outcomes_for_symbol(
                symbol, before_ts=anchor_ts, lookback_days=90)
            stats_90 = _aggregate(window, upper=anchor_ts,
                                    lookback_days=90)
            stats_30 = _aggregate(window, upper=anchor_ts,
                                    lookback_days=30)
            cnt_30[i] = int(stats_30["count"])
            cnt_90[i] = int(stats_90["count"])
            win_30[i] = stats_30["win_rate"]
            win_90[i] = stats_90["win_rate"]
            avg_90[i] = stats_90["avg_return"]

    out[f"{GROUP_NAME}.signals_count_30d"]  = cnt_30
    out[f"{GROUP_NAME}.signals_count_90d"]  = cnt_90
    out[f"{GROUP_NAME}.win_rate_30d"]       = win_30
    out[f"{GROUP_NAME}.win_rate_90d"]       = win_90
    out[f"{GROUP_NAME}.avg_return_pct_90d"] = avg_90
    return align_to_bars(out, bars, group_name=GROUP_NAME)

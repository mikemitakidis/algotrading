"""bot/data/timeframes.py — UTC arithmetic + resampling.

All timestamps are tz-aware UTC. Naive datetimes are rejected at every
boundary (provider adapters convert before storage; readers receive
tz-aware DataFrames).

4H resampling (D-α correction):
  Source timeframe:     1H
  Derivation method:    resample
  Resample rule version: 1
  UTC bucket alignment: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC
  OHLC reduction:       open=first, high=max, low=min, close=last
  Volume:               sum
  Coverage requirement: bucket is emitted only if it has at least 1
                        source 1H bar. If the bucket would have had
                        4 expected 1H bars but only 2 are present,
                        a `resample_source_incomplete` quality event
                        is recorded for that bucket (the bucket is
                        still emitted with what's available — being
                        explicit about incompleteness is the design
                        intent).

This module is pure-logic — no I/O, no SQL. It receives a 1H DataFrame
and returns a 4H DataFrame plus a list of quality events to write.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import pandas as pd


log = logging.getLogger(__name__)


RESAMPLE_RULE_VERSION = 1
RESAMPLE_4H_SOURCE = "1H"
RESAMPLE_4H_BUCKETS_PER_DAY = 6  # 00,04,08,12,16,20 UTC
EXPECTED_BARS_PER_4H_BUCKET = 4  # 4 x 1H bars per 4H bucket


def ensure_utc(ts) -> pd.Timestamp:
    """Coerce a value to a tz-aware UTC pd.Timestamp.

    Accepts:
      * pd.Timestamp (any tz; if naive raises)
      * datetime (any tz; if naive raises)
      * ISO-8601 string with explicit '+00:00' or 'Z'

    NAIVE INPUT IS REJECTED. Storage and computation are UTC-only.
    """
    if isinstance(ts, str):
        # Accept 'Z' as '+00:00'
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        ts = pd.Timestamp(s)
    elif isinstance(ts, datetime):
        ts = pd.Timestamp(ts)
    elif not isinstance(ts, pd.Timestamp):
        ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        raise ValueError(
            f"naive timestamp rejected: {ts!r}. All M16 timestamps must "
            f"be tz-aware UTC. Provider adapters must convert before "
            f"calling storage."
        )
    if str(ts.tzinfo) != "UTC" and ts.tzinfo.utcoffset(None) != timedelta(0):
        ts = ts.tz_convert("UTC")
    return ts


def floor_to_4h_bucket(ts: pd.Timestamp) -> pd.Timestamp:
    """Return the UTC 4H bucket start that contains ts.

    Buckets: [00,04), [04,08), [08,12), [12,16), [16,20), [20,24) UTC.
    """
    ts = ensure_utc(ts)
    h = ts.hour - (ts.hour % 4)
    return ts.replace(hour=h, minute=0, second=0, microsecond=0, nanosecond=0)


@dataclass(frozen=True)
class ResampleIssue:
    """Recorded when a 4H bucket has fewer 1H bars than expected."""
    bucket_start_utc: pd.Timestamp
    expected_source_bars: int
    actual_source_bars: int
    kind: str = "resample_source_incomplete"


def resample_1h_to_4h(
    df_1h: pd.DataFrame,
) -> Tuple[pd.DataFrame, List[ResampleIssue]]:
    """Resample a 1H OHLCV DataFrame into a 4H OHLCV DataFrame.

    Input columns required:
      ts_utc, open, high, low, close, volume
    Optional input columns preserved through resample:
      adj_close, adjustment_ratio, is_adjusted, provider, ingested_at_utc

    Output columns:
      ts_utc (4H bucket start UTC), open, high, low, close, volume,
      adj_close (last of bucket), adjustment_ratio (last of bucket),
      is_adjusted, provider (last of bucket), ingested_at_utc (last of
      bucket), quality_flags (or-aggregated).

    Returns (resampled_df, issues). Issues list `resample_source_incomplete`
    for any 4H bucket built from fewer than EXPECTED_BARS_PER_4H_BUCKET
    source bars (typically because the source 1H DataFrame had gaps).
    """
    if df_1h is None or len(df_1h) == 0:
        return _empty_4h_frame(df_1h), []

    if "ts_utc" not in df_1h.columns:
        raise ValueError("input DataFrame missing ts_utc column")

    df = df_1h.copy()
    # Ensure tz-aware UTC.
    df["ts_utc"] = df["ts_utc"].apply(ensure_utc)
    df = df.sort_values("ts_utc").reset_index(drop=True)
    df["_bucket"] = df["ts_utc"].apply(floor_to_4h_bucket)

    agg = {
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }
    # Optional columns
    optional_last = ("adj_close", "adjustment_ratio", "provider",
                       "ingested_at_utc", "is_adjusted")
    for col in optional_last:
        if col in df.columns:
            agg[col] = "last"
    if "quality_flags" in df.columns:
        # Bitwise-OR aggregation: any source flag carries into the bucket.
        agg["quality_flags"] = lambda s: int(s.fillna(0).astype(int).pipe(
            lambda x: x.iloc[0] if len(x) == 1 else
                       int.from_bytes(
                         bytes([0]), "big") | int(
                             pd.Series(x).pipe(_reduce_or))))
        # Simpler implementation:
        agg["quality_flags"] = _reduce_or

    grouped = df.groupby("_bucket").agg(agg).reset_index().rename(
        columns={"_bucket": "ts_utc"})
    # Recompute adjustment_ratio for bucket if adj_close+close present.
    if {"adj_close", "close"}.issubset(grouped.columns):
        try:
            mask = grouped["close"].notna() & grouped["adj_close"].notna() & \
                   (grouped["close"] != 0)
            grouped.loc[mask, "adjustment_ratio"] = (
                grouped.loc[mask, "adj_close"] / grouped.loc[mask, "close"]
            )
        except Exception:  # noqa: BLE001 - defensive
            pass

    # Count source bars per bucket to detect incompleteness.
    source_counts = df.groupby("_bucket").size()
    issues: List[ResampleIssue] = []
    for bucket_start, actual in source_counts.items():
        if int(actual) < EXPECTED_BARS_PER_4H_BUCKET:
            issues.append(ResampleIssue(
                bucket_start_utc=bucket_start,
                expected_source_bars=EXPECTED_BARS_PER_4H_BUCKET,
                actual_source_bars=int(actual),
            ))

    return grouped, issues


def _reduce_or(series: pd.Series) -> int:
    """Bitwise-OR reduce a pandas Series. Used as a groupby agg."""
    out = 0
    for v in series.fillna(0).astype(int).tolist():
        out |= int(v)
    return int(out)


def _empty_4h_frame(template: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return an empty DataFrame with the resampled-4H column shape."""
    cols = ["ts_utc", "open", "high", "low", "close", "volume"]
    if template is not None:
        for c in ("adj_close", "adjustment_ratio", "is_adjusted",
                    "provider", "ingested_at_utc", "quality_flags"):
            if c in template.columns:
                cols.append(c)
    return pd.DataFrame(columns=cols)


# -- expected-bar-count helpers (used by missing_bar quality rule) ----------

def expected_bars_per_day(timeframe: str) -> Optional[int]:
    """Return the count of bars per UTC calendar day for a timeframe.

    Calendar-day-based, NOT market-session-based. For US-equity 1D this
    is 1; for 1H it's 24; for 15m it's 96; for 4H it's 6.

    Returns None for unknown timeframes.

    NOTE: this is a *naive* over-estimate for intraday timeframes — a
    US-equity 1H bar series will only have ~7 bars per market day, not
    24. The quality rule that uses this comparison applies a market-
    session correction; this helper is the maximum possible.
    """
    return {"1D": 1, "4H": RESAMPLE_4H_BUCKETS_PER_DAY,
             "1H": 24, "15m": 96}.get(timeframe)


def utc_now() -> pd.Timestamp:
    """Return the current UTC moment as a tz-aware pd.Timestamp."""
    return pd.Timestamp(datetime.now(timezone.utc))

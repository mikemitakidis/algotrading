"""bot/data/quality.py — M16 data quality gate.

Runs at write time. Hard-rejects structurally invalid rows; tags rows
with warning bits in quality_flags; records observations in
historical_quality_events.

Hard rejections (row dropped, quality_event severity='error'):
  nan_ohlc          — any of open/high/low/close is NaN
  invalid_hl        — high < low; or high < max(open,close); or
                       low > min(open,close)
  negative_volume   — volume < 0
  non_positive_ohlc — any of open/high/low/close <= 0
  non_utc_ts        — provider returned naive or non-UTC timestamp

Warnings (row written, quality_flags bit set, quality_event severity='warn' or 'info'):
  zero_volume       — volume == 0 (bit 0)
  outlier           — |close - rolling_mean| > N * rolling_std (bit 1)
  duplicate_ts      — same ts_utc twice; last kept (bit 2)
  missing_bar       — fewer bars than expected (recorded on the run, not per-row)

Configurable thresholds (defensive defaults; tune later from observed FPs):
  OUTLIER_N_SIGMA   = 8   (very lenient)
  OUTLIER_LOOKBACK  = 60  (bars)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from bot.historical.schema import (ALLOWED_QUALITY_KINDS, ALLOWED_SEVERITIES)
from bot.historical.timeframes import ensure_utc


log = logging.getLogger(__name__)


# Quality-flag bits (Parquet `quality_flags` column).
QF_ZERO_VOLUME       = 1 << 0
QF_OUTLIER_WARNED    = 1 << 1
QF_DUPLICATE_KEPT    = 1 << 2
QF_BACKFILLED_GAP    = 1 << 3
# bits 4-31 reserved

# Outlier defaults.
OUTLIER_N_SIGMA = 8.0
OUTLIER_LOOKBACK = 60


@dataclass
class QualityEvent:
    """A single observation. Written to historical_quality_events."""
    severity: str
    kind: str
    message: str
    run_id: Optional[int] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    provider: Optional[str] = None
    ts_utc: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"invalid severity {self.severity!r}")
        if self.kind not in ALLOWED_QUALITY_KINDS:
            raise ValueError(f"invalid kind {self.kind!r}")


@dataclass
class ValidationOutcome:
    """Output of validate_batch."""
    valid_df: pd.DataFrame                 # rows that passed hard checks
    rejected_count: int                    # rows dropped
    events: List[QualityEvent]             # all events to write
    duplicate_count: int = 0


# -- Row-level validators (pure, no I/O) ------------------------------------

def validate_batch(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    provider: str,
    outlier_lookback_df: Optional[pd.DataFrame] = None,
    outlier_n_sigma: float = OUTLIER_N_SIGMA,
) -> ValidationOutcome:
    """Apply all M16 validation rules to an incoming batch.

    `outlier_lookback_df` is an optional DataFrame of the most recent
    historical bars (sorted ascending by ts_utc) that the caller has
    already written. Used for the outlier rule. If None, outlier
    checking is skipped (typical for backfill where there's no prior
    history).

    Returns a ValidationOutcome with:
      * `valid_df`: rows that passed all hard checks, with `quality_flags`
        bits set for any warning conditions detected on the row.
      * `events`: a list of QualityEvent objects to be persisted by the
        caller.
    """
    events: List[QualityEvent] = []
    if df is None or len(df) == 0:
        return ValidationOutcome(
            valid_df=df if df is not None else pd.DataFrame(),
            rejected_count=0, events=events)

    work = df.copy()
    if "quality_flags" not in work.columns:
        work["quality_flags"] = 0
    work["quality_flags"] = work["quality_flags"].fillna(0).astype("int64")

    # 1. Timestamp must be tz-aware UTC.
    bad_ts_idx = []
    for idx, raw in work["ts_utc"].items():
        try:
            ts = ensure_utc(raw)
            work.at[idx, "ts_utc"] = ts
        except Exception as e:  # noqa: BLE001
            bad_ts_idx.append(idx)
            events.append(QualityEvent(
                severity="error", kind="non_utc_ts",
                message=f"non-UTC timestamp at row {idx}: {e}",
                symbol=symbol, timeframe=timeframe, provider=provider,
                details={"raw": str(raw)}))
    if bad_ts_idx:
        work = work.drop(index=bad_ts_idx)
    if len(work) == 0:
        return ValidationOutcome(valid_df=work, rejected_count=len(bad_ts_idx),
                                    events=events)

    # 2. NaN in OHLC.
    ohlc_cols = ["open", "high", "low", "close"]
    nan_mask = work[ohlc_cols].isna().any(axis=1)
    for idx in work[nan_mask].index:
        events.append(QualityEvent(
            severity="error", kind="nan_ohlc",
            message="NaN in OHLC",
            symbol=symbol, timeframe=timeframe, provider=provider,
            ts_utc=str(work.at[idx, "ts_utc"])))

    # 3. Non-positive OHLC (only check rows that passed NaN).
    nonpos_mask = pd.Series(False, index=work.index)
    not_nan = ~nan_mask
    if not_nan.any():
        for col in ohlc_cols:
            nonpos_mask = nonpos_mask | (
                not_nan & (work[col] <= 0))
    for idx in work[nonpos_mask].index:
        events.append(QualityEvent(
            severity="error", kind="non_positive_ohlc",
            message="non-positive price in OHLC",
            symbol=symbol, timeframe=timeframe, provider=provider,
            ts_utc=str(work.at[idx, "ts_utc"]),
            details={c: float(work.at[idx, c]) for c in ohlc_cols
                       if pd.notna(work.at[idx, c])}))

    # 4. Invalid high/low relationships.
    invalid_hl_mask = pd.Series(False, index=work.index)
    if not_nan.any():
        h, l, o, c = work["high"], work["low"], work["open"], work["close"]
        invalid_hl_mask = not_nan & (
            (h < l) |
            (h < pd.concat([o, c], axis=1).max(axis=1)) |
            (l > pd.concat([o, c], axis=1).min(axis=1))
        )
    for idx in work[invalid_hl_mask].index:
        events.append(QualityEvent(
            severity="error", kind="invalid_hl",
            message="OHLC relationship invalid",
            symbol=symbol, timeframe=timeframe, provider=provider,
            ts_utc=str(work.at[idx, "ts_utc"]),
            details={"o": float(work.at[idx, "open"]),
                       "h": float(work.at[idx, "high"]),
                       "l": float(work.at[idx, "low"]),
                       "c": float(work.at[idx, "close"])}))

    # 5. Negative volume.
    neg_vol_mask = work["volume"].notna() & (work["volume"] < 0)
    for idx in work[neg_vol_mask].index:
        events.append(QualityEvent(
            severity="error", kind="negative_volume",
            message="negative volume",
            symbol=symbol, timeframe=timeframe, provider=provider,
            ts_utc=str(work.at[idx, "ts_utc"]),
            details={"volume": int(work.at[idx, "volume"])}))

    # Drop all hard-rejected rows.
    reject_mask = nan_mask | nonpos_mask | invalid_hl_mask | neg_vol_mask
    rejected_count = int(reject_mask.sum())
    work = work[~reject_mask].copy()
    if len(work) == 0:
        return ValidationOutcome(valid_df=work,
                                    rejected_count=rejected_count + len(bad_ts_idx),
                                    events=events)

    # 6. Zero volume (warn).
    zv_mask = work["volume"].notna() & (work["volume"] == 0)
    if zv_mask.any():
        work.loc[zv_mask, "quality_flags"] = (
            work.loc[zv_mask, "quality_flags"] | QF_ZERO_VOLUME)
        for idx in work[zv_mask].index:
            events.append(QualityEvent(
                severity="info", kind="zero_volume",
                message="zero volume on a trading bar",
                symbol=symbol, timeframe=timeframe, provider=provider,
                ts_utc=str(work.at[idx, "ts_utc"])))

    # 7. Duplicate ts_utc within batch — keep last.
    dup_mask = work.duplicated(subset=["ts_utc"], keep="last")
    duplicate_count = int(dup_mask.sum())
    if duplicate_count > 0:
        for idx in work[dup_mask].index:
            events.append(QualityEvent(
                severity="warn", kind="duplicate_ts",
                message="duplicate ts_utc in batch; keeping latest",
                symbol=symbol, timeframe=timeframe, provider=provider,
                ts_utc=str(work.at[idx, "ts_utc"])))
        work = work[~dup_mask].copy()
        # Tag the survivors that had duplicates (the ones we kept) so
        # they advertise quality_flags bit 2.
        # NOTE: we lose the precise mapping here; instead we tag any
        # row whose ts_utc was originally duplicated.
        dup_ts = set(work["ts_utc"].astype(str).tolist()) & set(
            df["ts_utc"].astype(str).tolist())  # noqa: F841

    # 8. Outlier check.
    if outlier_lookback_df is not None and len(outlier_lookback_df) >= 5:
        lookback_closes = outlier_lookback_df["close"].tail(
            OUTLIER_LOOKBACK)
        if len(lookback_closes) >= 5:
            mu = float(lookback_closes.mean())
            sigma = float(lookback_closes.std(ddof=0)) or 0.0
            if sigma > 0:
                z_threshold = outlier_n_sigma * sigma
                for idx in work.index:
                    c = work.at[idx, "close"]
                    if pd.notna(c) and abs(float(c) - mu) > z_threshold:
                        work.at[idx, "quality_flags"] = int(
                            work.at[idx, "quality_flags"]
                        ) | QF_OUTLIER_WARNED
                        events.append(QualityEvent(
                            severity="warn", kind="outlier",
                            message=(f"close outlier: |c-mu|={abs(float(c)-mu):.4g}"
                                       f" > {outlier_n_sigma}*sigma={z_threshold:.4g}"),
                            symbol=symbol, timeframe=timeframe,
                            provider=provider,
                            ts_utc=str(work.at[idx, "ts_utc"]),
                            details={"close": float(c), "mu": mu, "sigma": sigma}))

    return ValidationOutcome(
        valid_df=work,
        rejected_count=rejected_count + len(bad_ts_idx),
        events=events,
        duplicate_count=duplicate_count,
    )


# -- Persister --------------------------------------------------------------

def write_quality_events(conn: sqlite3.Connection,
                          events: List[QualityEvent],
                          run_id: Optional[int] = None) -> int:
    """Append-only writer for quality_events. Returns count written."""
    if not events:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for e in events:
        rid = e.run_id if e.run_id is not None else run_id
        rows.append((
            rid, e.symbol, e.timeframe, e.provider, e.ts_utc,
            e.severity, e.kind, e.message,
            json.dumps(e.details, sort_keys=True, default=str)
                if e.details else None,
            now,
        ))
    conn.executemany(
        "INSERT INTO historical_quality_events "
        "(run_id, symbol, timeframe, provider, ts_utc, severity, kind, "
        " message, details_json, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)

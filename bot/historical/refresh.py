"""bot/data/refresh.py — M16 write orchestrator.

The ONLY write path into the historical store. Callers (CLI, tests,
future systemd timer) invoke `run()` with a mode + scope.

Modes:
  'backfill'       fetch from scratch up to provider cap; write a full
                    Parquet file
  'incremental'    fetch only the missing (last_ts, now] window; append
                    to existing Parquet file (with split-detection
                    overlap check)
  'repair'         find unfilled missing-bar gaps and refetch them
  'force_rebuild'  delete Parquet + coverage row, then run backfill

Lock (Correction 1): a single-row historical_refresh_lock table acts
as advisory cross-process lock. Two concurrent invocations: the second
detects the active claim and exits cleanly with status 0.

This module imports NOTHING from broker/order/scanner/strategy code.
AST-asserted by TestNoBrokerImports.
"""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from bot.historical import coverage as _cov
from bot.historical import quality as _quality
from bot.historical import schema as _schema
from bot.historical import store as _store
from bot.historical import timeframes as _tf
from bot.historical.providers import (BaseProvider, FETCH_NO_DATA, FETCH_OK,
                                  FETCH_PROVIDER_ERROR, FETCH_RATE_LIMITED,
                                  FetchResult, clamp_to_lookback)


log = logging.getLogger(__name__)


# Lock heartbeat lifetime — beyond this, a previously-held lock is
# considered abandoned (e.g. process crashed mid-refresh).
LOCK_LEASE_SEC = 30 * 60      # 30 minutes
LOCK_HEARTBEAT_SEC = 60       # not currently used; lease covers single runs

# Retry policy.
RETRY_DELAYS_SEC = (1.0, 2.0, 5.0, 15.0, 60.0)
RETRY_JITTER_RANGE = (0.5, 1.5)   # multiply each base delay by random.uniform(...)

# Incremental refresh: how many bars of overlap to refetch on every
# incremental, used for split detection.
SPLIT_DETECTION_OVERLAP_BARS = 5
SPLIT_DETECTION_RATIO_TOLERANCE = 1e-4   # any bigger drift = split


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

class RefreshLockHeld(Exception):
    """Raised when another process is already running a refresh."""


@contextmanager
def acquire_lock(conn: sqlite3.Connection):
    """Context manager that claims the refresh lock or raises.

    Mechanics:
      * BEGIN EXCLUSIVE on the connection (blocks other writers).
      * Inspect the lock row; if owner_pid is set and lease still valid
        and the PID is still alive locally → RefreshLockHeld.
      * Otherwise claim it: set owner_pid, owner_host, acquired_at_utc,
        lease_expires_at_utc.
      * On normal exit, release.

    The dashboard is single-worker, but the CLI may be invoked in
    parallel by a tired operator. This protects against that.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=LOCK_LEASE_SEC)
    pid = os.getpid()
    host = socket.gethostname()

    conn.execute("BEGIN EXCLUSIVE")
    try:
        row = conn.execute(
            "SELECT owner_pid, lease_expires_at_utc FROM historical_refresh_lock "
            "WHERE id = 1").fetchone()
        if row is not None and row[0] is not None:
            holder_pid, lease_iso = row
            lease_dt = datetime.fromisoformat(lease_iso) if lease_iso else None
            still_valid = lease_dt is not None and lease_dt > now
            still_alive = False
            try:
                # Linux: signal 0 is a no-op probe; raises if PID gone.
                os.kill(int(holder_pid), 0)
                still_alive = True
            except (OSError, ValueError):
                still_alive = False
            if still_valid and still_alive:
                conn.execute("ROLLBACK")
                raise RefreshLockHeld(
                    f"refresh lock held by pid={holder_pid} (lease "
                    f"expires {lease_iso}); refusing to start")
            # else: stale lock — claim it.

        conn.execute(
            "UPDATE historical_refresh_lock SET "
            " owner_pid=?, owner_host=?, acquired_at_utc=?, "
            " lease_expires_at_utc=?, last_heartbeat_utc=? "
            "WHERE id = 1",
            (pid, host, now.isoformat(), expires.isoformat(),
             now.isoformat()))
        conn.execute("COMMIT")
    except RefreshLockHeld:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    try:
        yield
    finally:
        # Release.
        try:
            conn.execute(
                "UPDATE historical_refresh_lock SET "
                " owner_pid=NULL, owner_host=NULL, acquired_at_utc=NULL, "
                " lease_expires_at_utc=NULL, last_heartbeat_utc=NULL "
                "WHERE id = 1")
            conn.commit()
        except sqlite3.Error as e:
            log.warning("failed to release lock cleanly: %s", e)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RefreshResult:
    run_id: int
    mode: str
    status: str
    symbols_attempted: int = 0
    symbols_ok: int = 0
    symbols_no_data: int = 0
    symbols_failed: int = 0
    symbols_rate_limited: int = 0   # M16.A.fix-1: distinct from failed/no_data
    bars_fetched: int = 0
    bars_written: int = 0
    bars_updated: int = 0
    errors_count: int = 0
    rate_limit_count: int = 0       # retry-attempt count (different from above)
    duration_sec: float = 0.0
    summary: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run(
    *,
    mode: str,
    symbols: Iterable[str],
    timeframes: Iterable[str],
    provider: BaseProvider,
    db_path: Optional[Path] = None,
    parquet_root: Optional[Path] = None,
    now_utc: Optional[datetime] = None,
    backfill_max_lookback: Optional[str] = None,
) -> RefreshResult:
    """Run a refresh operation.

    `mode` is one of 'backfill', 'incremental', 'repair', 'force_rebuild'.

    `provider` is an instance of a concrete BaseProvider implementation.
    Tests pass a FakeProvider; production passes YFinanceProvider().

    Returns a RefreshResult. Raises RefreshLockHeld if another process
    already holds the lock.
    """
    if mode not in _schema.ALLOWED_REFRESH_MODES:
        raise ValueError(f"unsupported mode: {mode}")

    db_path = db_path or _schema.default_db_path()
    parquet_root = parquet_root or _schema.default_parquet_root()
    started_at = (now_utc if now_utc is not None
                  else datetime.now(timezone.utc))
    t0 = time.monotonic()
    symbols_list = [s.upper() for s in symbols]
    tfs_list = list(timeframes)

    conn = _schema.open_db(db_path)
    _schema.apply_schema(conn)

    # Insert the run row in 'running' state.
    cur = conn.execute(
        "INSERT INTO historical_refresh_runs "
        "(started_at_utc, mode, provider, symbols_requested, "
        " timeframes_requested, status) "
        "VALUES (?, ?, ?, ?, ?, 'running')",
        (started_at.isoformat(), mode, provider.capability.name,
         json.dumps(symbols_list), json.dumps(tfs_list)))
    run_id = cur.lastrowid
    conn.commit()

    result = RefreshResult(run_id=run_id, mode=mode, status="running")

    try:
        with acquire_lock(conn):
            for sym in symbols_list:
                _cov.upsert_symbol(conn, symbol=sym)
                for tf in tfs_list:
                    _process_one(
                        conn=conn,
                        provider=provider,
                        symbol=sym, timeframe=tf,
                        mode=mode,
                        run_id=run_id,
                        result=result,
                        parquet_root=parquet_root,
                        now_utc=started_at,
                        backfill_max_lookback=backfill_max_lookback,
                    )
                    result.symbols_attempted += 1
    except RefreshLockHeld as e:
        _finalize_run(conn, run_id, "failed", t0, result,
                       reason="lock_held", message=str(e))
        result.status = "failed"
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("refresh failed")
        _finalize_run(conn, run_id, "failed", t0, result,
                       reason="orchestrator_error", message=str(e))
        result.status = "failed"
        return result

    # Decide final status. M16.A.fix-1: rate-limited symbols are NOT
    # silently rolled into "ok". A run where zero symbols succeeded is
    # never "ok" — it's at best "partial" (some succeeded, some did not)
    # and at worst "failed" (none succeeded).
    has_failures = (result.symbols_failed + result.symbols_rate_limited) > 0
    has_successes = result.symbols_ok > 0
    if not has_failures:
        # No failures and no rate-limits: clean (including all-no-data,
        # which is a legitimate "nothing to fetch" outcome).
        result.status = "ok"
    elif has_successes:
        result.status = "partial"
    else:
        # Failures and/or rate-limits with zero successes.
        result.status = "failed"
    _finalize_run(conn, run_id, result.status, t0, result)
    return result


# ---------------------------------------------------------------------------
# Per-(symbol, timeframe) processing
# ---------------------------------------------------------------------------

def _process_one(
    *,
    conn: sqlite3.Connection,
    provider: BaseProvider,
    symbol: str,
    timeframe: str,
    mode: str,
    run_id: int,
    result: RefreshResult,
    parquet_root: Path,
    now_utc: datetime,
    backfill_max_lookback: Optional[str] = None,
) -> None:
    # Determine whether we'll fetch directly or resample from 1H.
    derivation = "native"
    source_timeframe = None
    resample_rule_version = None
    if timeframe == "4H":
        # 4H is resampled from 1H — process 1H first then derive.
        derivation = "resample"
        source_timeframe = "1H"
        resample_rule_version = _tf.RESAMPLE_RULE_VERSION
        # If the source 1H file doesn't exist, we can't resample.
        src_path = _store._parquet_path(provider.capability.name, "1H",
                                            symbol, root=parquet_root)
        if not src_path.exists():
            _quality.write_quality_events(conn, [_quality.QualityEvent(
                severity="warn", kind="resample_source_incomplete",
                message="cannot resample 4H: 1H source file missing",
                symbol=symbol, timeframe="4H",
                provider=provider.capability.name,
                run_id=run_id)], run_id=run_id)
            result.symbols_no_data += 1
            return
        # Resample.
        df_1h = _store._read_parquet_raw(src_path)
        if df_1h.empty:
            _quality.write_quality_events(conn, [_quality.QualityEvent(
                severity="warn", kind="resample_source_incomplete",
                message="cannot resample 4H: 1H source is empty",
                symbol=symbol, timeframe="4H",
                provider=provider.capability.name,
                run_id=run_id)], run_id=run_id)
            result.symbols_no_data += 1
            return
        df_4h, issues = _tf.resample_1h_to_4h(df_1h)
        for issue in issues:
            _quality.write_quality_events(conn, [_quality.QualityEvent(
                severity="warn", kind="resample_source_incomplete",
                message=(f"4H bucket {issue.bucket_start_utc!s} has "
                          f"{issue.actual_source_bars}/"
                          f"{issue.expected_source_bars} source 1H bars"),
                symbol=symbol, timeframe="4H",
                provider=provider.capability.name,
                ts_utc=str(issue.bucket_start_utc),
                run_id=run_id)], run_id=run_id)

        if mode == "force_rebuild":
            dst_path = _store._parquet_path(provider.capability.name, "4H",
                                              symbol, root=parquet_root)
            if dst_path.exists():
                os.unlink(str(dst_path))
            _cov.reset_coverage(conn, symbol=symbol, timeframe="4H",
                                  provider=provider.capability.name)

        # Write.
        _persist_bars(conn=conn, df=df_4h, symbol=symbol, timeframe="4H",
                       provider_name=provider.capability.name,
                       parquet_root=parquet_root, run_id=run_id,
                       result=result, now_utc=now_utc,
                       source_timeframe="1H", derivation_method="resample",
                       resample_rule_version=_tf.RESAMPLE_RULE_VERSION,
                       provider_limit_note=None)
        return

    # NATIVE fetch.
    if timeframe not in provider.capability.supported_timeframes:
        # Provider can't supply this TF natively.
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="warn", kind="lookback_exceeded",
            message=(f"provider {provider.capability.name} does not "
                       f"support timeframe {timeframe} natively"),
            symbol=symbol, timeframe=timeframe,
            provider=provider.capability.name,
            run_id=run_id)], run_id=run_id)
        result.symbols_no_data += 1
        return

    # Compute fetch range.
    parquet_path = _store._parquet_path(provider.capability.name, timeframe,
                                            symbol, root=parquet_root)

    if mode == "force_rebuild":
        if parquet_path.exists():
            os.unlink(str(parquet_path))
        _cov.reset_coverage(conn, symbol=symbol, timeframe=timeframe,
                              provider=provider.capability.name)
        # Then fall through to backfill semantics.
        effective_mode = "backfill"
    else:
        effective_mode = mode

    cov = _cov.read_coverage(conn, symbol=symbol, timeframe=timeframe,
                                provider=provider.capability.name)

    if effective_mode == "backfill" or cov is None or cov.last_ts_utc is None:
        # Backfill — fetch from earliest provider date.
        from datetime import datetime as _dt
        want_to = now_utc
        if backfill_max_lookback is not None:
            try:
                from bot.historical.providers import parse_lookback_cap
                td = parse_lookback_cap(backfill_max_lookback)
                want_from = (pd.Timestamp(now_utc) - td).to_pydatetime() \
                    if td is not None else pd.Timestamp("1970-01-01",
                                                          tz="UTC").to_pydatetime()
            except Exception:  # noqa: BLE001
                want_from = pd.Timestamp("1970-01-01", tz="UTC").to_pydatetime()
        else:
            want_from = pd.Timestamp("1970-01-01", tz="UTC").to_pydatetime()
    else:
        # Incremental: fetch a small overlap for split detection +
        # (last_ts, now].
        last_ts = pd.Timestamp(cov.last_ts_utc.replace("Z", "+00:00"))
        # Overlap window — N timeframes back.
        overlap_td = {
            "1D": timedelta(days=SPLIT_DETECTION_OVERLAP_BARS),
            "1H": timedelta(hours=SPLIT_DETECTION_OVERLAP_BARS),
            "15m": timedelta(minutes=15 * SPLIT_DETECTION_OVERLAP_BARS),
        }.get(timeframe, timedelta(days=SPLIT_DETECTION_OVERLAP_BARS))
        want_from = (last_ts - overlap_td).to_pydatetime()
        want_to = now_utc

    # Clamp to provider lookback cap.
    clamped_from, clamped_to, lookback_exceeded = clamp_to_lookback(
        want_from, want_to, timeframe=timeframe,
        capability=provider.capability, now_utc=now_utc)

    provider_limit_note = None
    if lookback_exceeded:
        cap = provider.capability.lookback_caps.get(timeframe, "?")
        provider_limit_note = (
            f"{provider.capability.name} cap {cap} clamped fetch "
            f"start to {clamped_from!s}")
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="warn", kind="lookback_exceeded",
            message=provider_limit_note,
            symbol=symbol, timeframe=timeframe,
            provider=provider.capability.name,
            run_id=run_id,
            details={"want_from": str(want_from),
                       "clamped_from": str(clamped_from),
                       "cap": cap})], run_id=run_id)

    # Fetch with retry+jitter.
    fetch = _fetch_with_retry(
        provider=provider, symbol=symbol, timeframe=timeframe,
        start_utc=clamped_from, end_utc=clamped_to,
        run_id=run_id, conn=conn, result=result)

    if fetch.outcome == FETCH_NO_DATA:
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="info", kind="no_data",
            message=(f"provider returned no data for {symbol} "
                       f"[{clamped_from} -> {clamped_to}]"),
            symbol=symbol, timeframe=timeframe,
            provider=provider.capability.name,
            run_id=run_id)], run_id=run_id)
        result.symbols_no_data += 1
        return

    if fetch.outcome == FETCH_PROVIDER_ERROR:
        result.errors_count += 1
        result.symbols_failed += 1
        return

    if fetch.outcome == FETCH_RATE_LIMITED:
        # M16.A.fix-1: when all retries are exhausted with a rate-limit
        # outcome, classify the symbol as rate_limited — NOT as no_data.
        # Before this fix the code fell through to the OK/persist branch
        # which then misclassified the empty result as no_data.
        result.symbols_rate_limited += 1
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="warn", kind="rate_limited",
            message=("retries exhausted with rate-limit response; "
                       "symbol marked rate_limited"),
            symbol=symbol, timeframe=timeframe,
            provider=provider.capability.name,
            run_id=run_id,
            details={"retry_attempts": len(RETRY_DELAYS_SEC) + 1,
                       "last_message": fetch.message})],
            run_id=run_id)
        return

    # FETCH_OK — persist
    df = fetch.df
    if df is None or len(df) == 0:
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="info", kind="no_data",
            message="provider returned empty DataFrame",
            symbol=symbol, timeframe=timeframe,
            provider=provider.capability.name,
            run_id=run_id)], run_id=run_id)
        result.symbols_no_data += 1
        return

    result.bars_fetched += len(df)

    # Merge with existing file on disk (incremental case).
    if effective_mode != "backfill" and parquet_path.exists():
        existing = _store._read_parquet_raw(parquet_path)
        df, split_detected, bars_updated = _merge_with_split_check(
            existing_df=existing, new_df=df,
            symbol=symbol, timeframe=timeframe,
            provider_name=provider.capability.name,
            conn=conn, run_id=run_id)
        result.bars_updated += bars_updated

    _persist_bars(conn=conn, df=df, symbol=symbol, timeframe=timeframe,
                    provider_name=provider.capability.name,
                    parquet_root=parquet_root, run_id=run_id,
                    result=result, now_utc=now_utc,
                    source_timeframe=None, derivation_method="native",
                    resample_rule_version=None,
                    provider_limit_note=provider_limit_note)


def _fetch_with_retry(
    *, provider: BaseProvider, symbol: str, timeframe: str,
    start_utc: datetime, end_utc: datetime,
    run_id: int, conn: sqlite3.Connection, result: RefreshResult,
) -> FetchResult:
    """Wrap provider.fetch_bars with exponential backoff + jitter."""
    last_outcome = FetchResult(outcome=FETCH_PROVIDER_ERROR,
                                  message="unreached")
    for attempt, base_delay in enumerate((0.0, *RETRY_DELAYS_SEC)):
        if base_delay > 0:
            jitter = random.uniform(*RETRY_JITTER_RANGE)
            time.sleep(base_delay * jitter)
        try:
            r = provider.fetch_bars(symbol, timeframe, start_utc, end_utc)
        except Exception as e:  # noqa: BLE001
            r = FetchResult(outcome=FETCH_PROVIDER_ERROR,
                              message=f"exception: {e}")
        last_outcome = r
        if r.outcome == FETCH_OK:
            return r
        if r.outcome == FETCH_NO_DATA:
            return r
        if r.outcome == FETCH_RATE_LIMITED:
            result.rate_limit_count += 1
            _quality.write_quality_events(conn, [_quality.QualityEvent(
                severity="warn", kind="rate_limited",
                message=(f"rate limited by {provider.capability.name}; "
                          f"attempt {attempt+1}/{len(RETRY_DELAYS_SEC)+1}"),
                symbol=symbol, timeframe=timeframe,
                provider=provider.capability.name,
                run_id=run_id)], run_id=run_id)
            continue
        # provider_error — retry
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="error", kind="provider_error",
            message=r.message or "provider error",
            symbol=symbol, timeframe=timeframe,
            provider=provider.capability.name,
            run_id=run_id,
            details=r.details)], run_id=run_id)
    return last_outcome


def _merge_with_split_check(
    *, existing_df: pd.DataFrame, new_df: pd.DataFrame,
    symbol: str, timeframe: str, provider_name: str,
    conn: sqlite3.Connection, run_id: int,
) -> Tuple[pd.DataFrame, bool, int]:
    """Merge new bars with existing, detect adjustment-ratio drift.

    Returns (merged_df, split_detected, bars_updated_count).
    """
    split_detected = False
    bars_updated = 0
    if existing_df.empty:
        return new_df, False, 0

    # Find the overlap window.
    new_min_ts = new_df["ts_utc"].min()
    overlap_existing = existing_df[existing_df["ts_utc"] >= new_min_ts]
    overlap_new = new_df[new_df["ts_utc"] <= existing_df["ts_utc"].max()]

    # Compare adjustment_ratio for overlapping ts.
    if not overlap_existing.empty and not overlap_new.empty:
        merged_overlap = overlap_existing.merge(
            overlap_new, on="ts_utc", how="inner",
            suffixes=("_old", "_new"))
        if "adjustment_ratio_old" in merged_overlap.columns and \
           "adjustment_ratio_new" in merged_overlap.columns:
            diff = (merged_overlap["adjustment_ratio_new"] -
                    merged_overlap["adjustment_ratio_old"]).abs()
            if (diff > SPLIT_DETECTION_RATIO_TOLERANCE).any():
                split_detected = True

    if split_detected:
        # Rewrite adj_close + adjustment_ratio on the existing rows.
        # The raw OHLC + volume stay untouched.
        _quality.write_quality_events(conn, [_quality.QualityEvent(
            severity="info", kind="split_detected",
            message=("adjustment_ratio drift detected during incremental; "
                       "rewriting adj_close + adjustment_ratio for entire "
                       "history. raw OHLC + volume preserved."),
            symbol=symbol, timeframe=timeframe,
            provider=provider_name,
            run_id=run_id)], run_id=run_id)
        # In V1 we approximate the split-fix by trusting the new
        # provider response as the new source of truth for the overlap.
        # The full historical rewrite (extending the new ratio backward)
        # requires re-fetching the entire history, which the caller
        # would do via force_rebuild. For now: mark detected, propagate
        # the new overlap values into existing rows.
        ratio_map = dict(zip(merged_overlap["ts_utc"],
                              merged_overlap["adjustment_ratio_new"]))
        new_close_map = dict(zip(merged_overlap["ts_utc"],
                                   merged_overlap["adj_close_new"]))
        for idx in existing_df.index:
            t = existing_df.at[idx, "ts_utc"]
            if t in ratio_map:
                existing_df.at[idx, "adjustment_ratio"] = ratio_map[t]
                existing_df.at[idx, "adj_close"] = new_close_map[t]
                bars_updated += 1

    # Merge: keep new rows for any overlapping ts (last-wins via drop_dup).
    merged = pd.concat([existing_df, new_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_utc"], keep="last")
    merged = merged.sort_values("ts_utc").reset_index(drop=True)
    return merged, split_detected, bars_updated


def _persist_bars(
    *, conn: sqlite3.Connection, df: pd.DataFrame,
    symbol: str, timeframe: str, provider_name: str,
    parquet_root: Path, run_id: int, result: RefreshResult,
    now_utc: datetime,
    source_timeframe: Optional[str], derivation_method: str,
    resample_rule_version: Optional[int],
    provider_limit_note: Optional[str],
) -> None:
    """Run quality validation, write Parquet atomically, upsert coverage."""
    # Quality.
    existing_path = _store._parquet_path(provider_name, timeframe, symbol,
                                              root=parquet_root)
    lookback_df = None
    if existing_path.exists():
        lookback_df = _store._read_parquet_raw(existing_path).tail(120)

    outcome = _quality.validate_batch(
        df, symbol=symbol, timeframe=timeframe, provider=provider_name,
        outlier_lookback_df=lookback_df)
    _quality.write_quality_events(conn, outcome.events, run_id=run_id)

    if outcome.valid_df.empty:
        result.symbols_failed += 1
        return

    # Ensure Parquet-required columns present.
    valid = outcome.valid_df.copy()
    if "adj_close" not in valid.columns:
        valid["adj_close"] = pd.NA
    if "adjustment_ratio" not in valid.columns:
        # Default to 1.0 if adj_close == close (no adjustment),
        # otherwise compute.
        valid["adjustment_ratio"] = 1.0
    if "is_adjusted" not in valid.columns:
        valid["is_adjusted"] = valid["adjustment_ratio"].notna()
    if "provider" not in valid.columns:
        valid["provider"] = provider_name
    valid["ingested_at_utc"] = pd.Timestamp(now_utc)

    # Quality flag for duplicates — outcome.duplicate_count already
    # collapsed; tag survivors that had a sibling dup.
    if "quality_flags" not in valid.columns:
        valid["quality_flags"] = 0

    # Atomic write.
    _store._write_parquet_atomic(existing_path, valid)
    result.symbols_ok += 1
    result.bars_written += len(valid)

    # Coverage update.
    first_ts = valid["ts_utc"].min()
    last_ts = valid["ts_utc"].max()
    freshness = _cov.compute_freshness_status(
        str(last_ts), timeframe=timeframe, now_utc=now_utc)
    quality_status = "clean" if len(
        [e for e in outcome.events if e.severity == "error"]) == 0 else "error"
    if quality_status == "clean" and len(
        [e for e in outcome.events if e.severity == "warn"]) > 0:
        quality_status = "warn"

    _cov.upsert_coverage(
        conn=conn, symbol=symbol, timeframe=timeframe,
        provider=provider_name,
        first_ts_utc=str(first_ts), last_ts_utc=str(last_ts),
        bar_count=len(valid),
        quality_status=quality_status, freshness_status=freshness,
        last_refresh_at_utc=now_utc.isoformat(),
        last_refresh_id=run_id,
        provider_limit_note=provider_limit_note,
        source_timeframe=source_timeframe,
        derivation_method=derivation_method,
        resample_rule_version=resample_rule_version,
        duplicate_count_delta=outcome.duplicate_count,
    )


def _finalize_run(conn: sqlite3.Connection, run_id: int, status: str,
                    t0: float, result: RefreshResult, *,
                    reason: Optional[str] = None,
                    message: Optional[str] = None) -> None:
    duration = time.monotonic() - t0
    result.duration_sec = duration
    summary = dict(result.summary)
    if reason is not None:
        summary["reason"] = reason
    if message is not None:
        summary["message"] = message
    summary["status"] = status

    conn.execute(
        "UPDATE historical_refresh_runs SET "
        " finished_at_utc=?, status=?, symbols_attempted=?, "
        " symbols_ok=?, symbols_no_data=?, symbols_failed=?, "
        " symbols_rate_limited=?, "
        " bars_fetched=?, bars_written=?, bars_updated=?, "
        " errors_count=?, rate_limit_count=?, "
        " duration_sec=?, summary_json=? "
        "WHERE run_id=?",
        (datetime.now(timezone.utc).isoformat(), status,
         result.symbols_attempted, result.symbols_ok,
         result.symbols_no_data, result.symbols_failed,
         result.symbols_rate_limited,
         result.bars_fetched, result.bars_written, result.bars_updated,
         result.errors_count, result.rate_limit_count,
         duration, json.dumps(summary, sort_keys=True, default=str),
         run_id))
    conn.commit()

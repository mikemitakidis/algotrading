"""bot/historical/coverage.py — coverage state queries and updates.

Reads + writes the historical_coverage table.
Pure I/O on SQLite + the Parquet store; no provider calls.

Used by:
  * refresh.py — to find what already exists and update after writes
  * store.py   — to read coverage for the dashboard endpoints
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd


log = logging.getLogger(__name__)


# Freshness thresholds — bar age beyond which the coverage row is 'stale'.
# Calibration: deliberately lenient to avoid false-stale warnings on
# weekends/holidays. The dashboard surfaces stale; nothing breaks if
# it's a little late.
FRESHNESS_THRESHOLDS = {
    "1D":  timedelta(days=3),       # ~2 trading days + weekend headroom
    "4H":  timedelta(hours=18),
    "1H":  timedelta(hours=6),
    "15m": timedelta(hours=2),
}


@dataclass
class CoverageRow:
    symbol: str
    timeframe: str
    provider: str
    first_ts_utc: Optional[str]
    last_ts_utc: Optional[str]
    bar_count: int
    missing_count: int
    duplicate_count: int
    quality_status: str
    freshness_status: str
    last_refresh_at_utc: Optional[str]
    last_refresh_id: Optional[int]
    provider_limit_note: Optional[str]
    source_timeframe: Optional[str]
    derivation_method: str
    resample_rule_version: Optional[int]


def read_coverage(conn: sqlite3.Connection, *,
                    symbol: str, timeframe: str, provider: str,
                    ) -> Optional[CoverageRow]:
    row = conn.execute(
        "SELECT symbol, timeframe, provider, first_ts_utc, last_ts_utc, "
        "  bar_count, missing_count, duplicate_count, quality_status, "
        "  freshness_status, last_refresh_at_utc, last_refresh_id, "
        "  provider_limit_note, source_timeframe, derivation_method, "
        "  resample_rule_version "
        "FROM historical_coverage "
        "WHERE symbol = ? AND timeframe = ? AND provider = ?",
        (symbol.upper(), timeframe, provider),
    ).fetchone()
    if row is None:
        return None
    return CoverageRow(*row)


def upsert_coverage(
    conn: sqlite3.Connection, *,
    symbol: str, timeframe: str, provider: str,
    first_ts_utc: Optional[str], last_ts_utc: Optional[str],
    bar_count: int,
    quality_status: str = "clean",
    freshness_status: str = "fresh",
    last_refresh_at_utc: Optional[str] = None,
    last_refresh_id: Optional[int] = None,
    provider_limit_note: Optional[str] = None,
    source_timeframe: Optional[str] = None,
    derivation_method: str = "native",
    resample_rule_version: Optional[int] = None,
    missing_count_delta: int = 0,
    duplicate_count_delta: int = 0,
) -> None:
    """Insert-or-update one coverage row.

    Delta-style counters: missing_count and duplicate_count are
    incremented (not replaced) since they're cumulative observations.
    Other fields are replaced.
    """
    sym = symbol.upper()
    # Read existing to combine cumulative counters.
    existing = read_coverage(conn, symbol=sym, timeframe=timeframe,
                                provider=provider)
    if existing is None:
        new_missing = max(0, missing_count_delta)
        new_dup = max(0, duplicate_count_delta)
        conn.execute(
            "INSERT INTO historical_coverage "
            "(symbol, timeframe, provider, first_ts_utc, last_ts_utc, "
            " bar_count, missing_count, duplicate_count, quality_status, "
            " freshness_status, last_refresh_at_utc, last_refresh_id, "
            " provider_limit_note, source_timeframe, derivation_method, "
            " resample_rule_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sym, timeframe, provider, first_ts_utc, last_ts_utc, bar_count,
             new_missing, new_dup, quality_status, freshness_status,
             last_refresh_at_utc, last_refresh_id, provider_limit_note,
             source_timeframe, derivation_method, resample_rule_version))
    else:
        new_missing = max(0, int(existing.missing_count) + int(missing_count_delta))
        new_dup = max(0, int(existing.duplicate_count) + int(duplicate_count_delta))
        conn.execute(
            "UPDATE historical_coverage SET "
            " first_ts_utc=?, last_ts_utc=?, bar_count=?, "
            " missing_count=?, duplicate_count=?, quality_status=?, "
            " freshness_status=?, last_refresh_at_utc=?, "
            " last_refresh_id=?, provider_limit_note=?, "
            " source_timeframe=?, derivation_method=?, "
            " resample_rule_version=? "
            "WHERE symbol=? AND timeframe=? AND provider=?",
            (first_ts_utc, last_ts_utc, bar_count,
             new_missing, new_dup, quality_status, freshness_status,
             last_refresh_at_utc, last_refresh_id, provider_limit_note,
             source_timeframe, derivation_method, resample_rule_version,
             sym, timeframe, provider))
    conn.commit()


def reset_coverage(conn: sqlite3.Connection, *,
                     symbol: str, timeframe: str, provider: str) -> None:
    """Force-rebuild: wipe the coverage row entirely."""
    conn.execute(
        "DELETE FROM historical_coverage "
        "WHERE symbol = ? AND timeframe = ? AND provider = ?",
        (symbol.upper(), timeframe, provider))
    conn.commit()


def compute_freshness_status(last_ts_utc: Optional[str], *,
                                timeframe: str,
                                now_utc: Optional[datetime] = None) -> str:
    """Return 'fresh' | 'stale' | 'never' based on last_ts_utc."""
    if last_ts_utc is None:
        return "never"
    threshold = FRESHNESS_THRESHOLDS.get(timeframe)
    if threshold is None:
        return "unknown"
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    last = pd.Timestamp(last_ts_utc.replace("Z", "+00:00"))
    age = pd.Timestamp(now) - last
    return "fresh" if age <= threshold else "stale"


def upsert_symbol(conn: sqlite3.Connection, *,
                    symbol: str, asset_class: str = "us_equity",
                    is_active: bool = True) -> None:
    """Idempotently ensure a row in historical_symbols."""
    sym = symbol.upper()
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT first_seen_at FROM historical_symbols WHERE symbol = ?",
        (sym,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO historical_symbols "
            "(symbol, asset_class, is_active, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sym, asset_class, 1 if is_active else 0, now, now))
    else:
        conn.execute(
            "UPDATE historical_symbols SET last_seen_at=?, is_active=? "
            "WHERE symbol=?",
            (now, 1 if is_active else 0, sym))
    conn.commit()

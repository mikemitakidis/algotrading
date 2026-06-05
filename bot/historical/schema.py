"""bot/data/schema.py — M16 historical-data SQLite DDL.

Lives in its own DB file `data/historical.db`, completely separate from
`data/signals.db` (the M15.3 audit DB). No table from this schema is
referenced by, or references, anything in signals.db.

Schema version 1:
  historical_schema_version    — single-row version marker
  historical_symbols           — symbol universe (incl. active/delisted flag)
  historical_coverage          — per-(symbol, timeframe, provider) state,
                                  including derivation metadata for resampled
                                  rows (D-α correction)
  historical_refresh_runs      — one row per refresh invocation
  historical_quality_events    — append-only quality observations
  historical_refresh_lock      — single-row advisory lock (Correction 1):
                                  guarantees single-writer semantics across
                                  process boundaries via SELECT...FOR UPDATE
                                  semantics on an exclusive SQLite txn.

All timestamp columns store ISO-8601 UTC strings (with '+00:00' offset).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


SCHEMA_VERSION = 1

# Closed set — adding a new value requires bumping SCHEMA_VERSION.
ALLOWED_TIMEFRAMES = ("1D", "4H", "1H", "15m")

# Closed set — asserted by TestAllowedQualityKinds in the test suite.
# When a new kind is added it MUST be appended here, the schema CHECK
# is *deliberately* permissive (no DB-level CHECK on `kind`) so a
# code-side enum drift doesn't break a live DB write; the test guards
# the constant + the writer.
ALLOWED_QUALITY_KINDS = (
    # provider-outcome kinds
    "no_data",
    "provider_error",
    "rate_limited",
    "lookback_exceeded",
    # row-level data quality
    "nan_ohlc",
    "invalid_hl",
    "negative_volume",
    "non_positive_ohlc",
    "non_utc_ts",
    "zero_volume",
    "outlier",
    "duplicate_ts",
    "missing_bar",
    # coverage-level
    "stale",
    "split_detected",
    "resample_source_incomplete",
)

ALLOWED_SEVERITIES = ("info", "warn", "error")
ALLOWED_REFRESH_MODES = ("backfill", "incremental", "repair", "force_rebuild")
ALLOWED_RUN_STATUSES = ("running", "ok", "partial", "failed")
ALLOWED_QUALITY_STATUSES = ("clean", "warn", "error", "unknown")
ALLOWED_FRESHNESS_STATUSES = ("fresh", "stale", "never", "unknown")
ALLOWED_DERIVATION_METHODS = ("native", "resample")


# -- DDL ---------------------------------------------------------------------

_DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS historical_schema_version (
    version          INTEGER PRIMARY KEY,
    applied_at_utc   TEXT NOT NULL
)
"""

_DDL_SYMBOLS = """
CREATE TABLE IF NOT EXISTS historical_symbols (
    symbol          TEXT PRIMARY KEY,
    asset_class     TEXT NOT NULL,
    is_active       INTEGER NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    CHECK (is_active IN (0, 1))
)
"""

# Per-(symbol, timeframe, provider) coverage. The PK includes provider
# (D-δ + Correction 1 of the prior approval pass): future second
# providers can coexist without touching yfinance rows.
#
# `source_timeframe` and `derivation_method` (D-α correction) record
# whether the bars were natively fetched or resampled. For 4H rows
# resampled from 1H:
#   source_timeframe       = '1H'
#   derivation_method      = 'resample'
#   resample_rule_version  = 1
# For natively-fetched bars:
#   source_timeframe       = NULL
#   derivation_method      = 'native'
#   resample_rule_version  = NULL
_DDL_COVERAGE = """
CREATE TABLE IF NOT EXISTS historical_coverage (
    symbol                   TEXT NOT NULL,
    timeframe                TEXT NOT NULL,
    provider                 TEXT NOT NULL,
    first_ts_utc             TEXT,
    last_ts_utc              TEXT,
    bar_count                INTEGER NOT NULL DEFAULT 0,
    missing_count            INTEGER NOT NULL DEFAULT 0,
    duplicate_count          INTEGER NOT NULL DEFAULT 0,
    quality_status           TEXT NOT NULL DEFAULT 'unknown',
    freshness_status         TEXT NOT NULL DEFAULT 'unknown',
    last_refresh_at_utc      TEXT,
    last_refresh_id          INTEGER,
    provider_limit_note      TEXT,
    source_timeframe         TEXT,
    derivation_method        TEXT NOT NULL DEFAULT 'native',
    resample_rule_version    INTEGER,
    PRIMARY KEY (symbol, timeframe, provider),
    FOREIGN KEY (symbol) REFERENCES historical_symbols(symbol),
    CHECK (timeframe IN ('1D', '4H', '1H', '15m')),
    CHECK (quality_status IN ('clean','warn','error','unknown')),
    CHECK (freshness_status IN ('fresh','stale','never','unknown')),
    CHECK (derivation_method IN ('native','resample'))
)
"""

_DDL_REFRESH_RUNS = """
CREATE TABLE IF NOT EXISTS historical_refresh_runs (
    run_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc       TEXT NOT NULL,
    finished_at_utc      TEXT,
    mode                 TEXT NOT NULL,
    provider             TEXT NOT NULL,
    symbols_requested    TEXT NOT NULL,
    timeframes_requested TEXT NOT NULL,
    status               TEXT NOT NULL,
    symbols_attempted    INTEGER NOT NULL DEFAULT 0,
    symbols_ok           INTEGER NOT NULL DEFAULT 0,
    symbols_no_data      INTEGER NOT NULL DEFAULT 0,
    symbols_failed       INTEGER NOT NULL DEFAULT 0,
    bars_fetched         INTEGER NOT NULL DEFAULT 0,
    bars_written         INTEGER NOT NULL DEFAULT 0,
    bars_updated         INTEGER NOT NULL DEFAULT 0,
    errors_count         INTEGER NOT NULL DEFAULT 0,
    rate_limit_count     INTEGER NOT NULL DEFAULT 0,
    duration_sec         REAL,
    summary_json         TEXT,
    CHECK (mode IN ('backfill','incremental','repair','force_rebuild')),
    CHECK (status IN ('running','ok','partial','failed'))
)
"""

_DDL_QUALITY_EVENTS = """
CREATE TABLE IF NOT EXISTS historical_quality_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER REFERENCES historical_refresh_runs(run_id),
    symbol           TEXT,
    timeframe        TEXT,
    provider         TEXT,
    ts_utc           TEXT,
    severity         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    message          TEXT NOT NULL,
    details_json     TEXT,
    created_at_utc   TEXT NOT NULL,
    CHECK (severity IN ('info','warn','error'))
)
"""

# Refresh lock (Correction 1): one row, id=1, claimed by acquiring an
# exclusive transaction and inserting/updating its `owner_pid` +
# `acquired_at_utc`. A second refresh process that finds owner_pid
# non-NULL with a recent acquired_at_utc exits cleanly.
#
# `lease_expires_at_utc` is the deadline at which a stale lock is
# considered abandoned (so a crashed previous holder doesn't deadlock
# the system forever).
_DDL_REFRESH_LOCK = """
CREATE TABLE IF NOT EXISTS historical_refresh_lock (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    owner_pid             INTEGER,
    owner_host            TEXT,
    acquired_at_utc       TEXT,
    lease_expires_at_utc  TEXT,
    last_heartbeat_utc    TEXT
)
"""

# Indexes
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_hist_coverage_last_ts "
    "ON historical_coverage(last_ts_utc)",

    "CREATE INDEX IF NOT EXISTS idx_hist_coverage_quality "
    "ON historical_coverage(quality_status)",

    "CREATE INDEX IF NOT EXISTS idx_hist_coverage_freshness "
    "ON historical_coverage(freshness_status)",

    "CREATE INDEX IF NOT EXISTS idx_hist_refresh_runs_started "
    "ON historical_refresh_runs(started_at_utc DESC)",

    "CREATE INDEX IF NOT EXISTS idx_hist_refresh_runs_status "
    "ON historical_refresh_runs(status)",

    "CREATE INDEX IF NOT EXISTS idx_hist_quality_events_created "
    "ON historical_quality_events(created_at_utc DESC)",

    "CREATE INDEX IF NOT EXISTS idx_hist_quality_events_symbol "
    "ON historical_quality_events(symbol, timeframe)",

    "CREATE INDEX IF NOT EXISTS idx_hist_quality_events_severity "
    "ON historical_quality_events(severity)",

    "CREATE INDEX IF NOT EXISTS idx_hist_quality_events_kind "
    "ON historical_quality_events(kind)",
)


# -- Public API --------------------------------------------------------------

def apply_schema(conn: sqlite3.Connection) -> int:
    """Idempotently apply all M16 DDL. Returns the schema_version on disk.

    Safe to call multiple times — CREATE TABLE IF NOT EXISTS + idempotent
    version-row insert.
    """
    cur = conn.cursor()
    cur.execute(_DDL_SCHEMA_VERSION)
    cur.execute(_DDL_SYMBOLS)
    cur.execute(_DDL_COVERAGE)
    cur.execute(_DDL_REFRESH_RUNS)
    cur.execute(_DDL_QUALITY_EVENTS)
    cur.execute(_DDL_REFRESH_LOCK)
    for stmt in _INDEXES:
        cur.execute(stmt)

    # Seed the lock row if absent.
    cur.execute(
        "INSERT OR IGNORE INTO historical_refresh_lock (id) VALUES (1)")

    # Insert the schema_version row if absent (idempotent).
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT OR IGNORE INTO historical_schema_version "
        "(version, applied_at_utc) VALUES (?, ?)",
        (SCHEMA_VERSION, now),
    )
    conn.commit()

    actual = cur.execute(
        "SELECT MAX(version) FROM historical_schema_version").fetchone()[0]
    if actual is None:
        actual = 0
    return int(actual)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest schema_version on disk, or 0 if uninitialised."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM historical_schema_version"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Open the M16 metadata DB. Caller MUST close it.

    Uses WAL journaling for better reader/writer concurrency (refresh
    writer + dashboard read endpoints + CLI status).
    """
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)  # explicit txns
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")  # 5s
    return conn


# Default DB path resolver — callers can override (tests use tempfiles).
def default_db_path(repo_root: Optional[Path] = None) -> Path:
    """Return the canonical historical DB path: <repo>/data/historical.db."""
    if repo_root is None:
        # Climb from this file: bot/data/schema.py -> bot/data -> bot -> repo
        repo_root = Path(__file__).resolve().parent.parent.parent
    return Path(repo_root) / "data" / "historical.db"


def default_parquet_root(repo_root: Optional[Path] = None) -> Path:
    """Return the canonical Parquet root: <repo>/data/historical/."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
    return Path(repo_root) / "data" / "historical"

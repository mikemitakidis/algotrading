"""
bot/flywheel.py
Data flywheel — Milestone 10.

Captures every decision point the bot makes, not just final signals.
This creates the labeled dataset that serious ML systems train on.

Three event types logged to SQLite:

1. candidate_snapshots  — every symbol at every stage each cycle
   Stage values: 'scanned' | 'partial_confluence' | 'final_signal' | 'rejected'
   Links to signal_id when the candidate became a final signal.

2. execution_intents    — every order intent before and after broker submission
   Status: 'pending' | 'risk_rejected' | 'paper_logged' | 'accepted' |
           'rejected' | 'filled' | 'cancelled' | 'error' | 'not_implemented'

3. signal_outcomes      — reserved for outcome linkage (M11+)
   Links signal_id → execution_intent_id → outcome
   Not populated in M10 but schema exists for future use.

Schema is additive and safe — existing tables unaffected.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Schemas ───────────────────────────────────────────────────────────────────

CANDIDATE_SCHEMA = """
CREATE TABLE candidate_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id         INTEGER NOT NULL DEFAULT 0,
    timestamp        TEXT    NOT NULL DEFAULT '',
    symbol           TEXT    NOT NULL DEFAULT '',
    direction        TEXT    NOT NULL DEFAULT '',
    stage            TEXT    NOT NULL DEFAULT '',
    -- stage: scanned | partial_confluence | final_signal | rejected
    valid_count      INTEGER DEFAULT 0,
    tfs_passing      TEXT    DEFAULT '',   -- '+'-joined list
    available_tfs    INTEGER DEFAULT 0,
    min_valid        INTEGER DEFAULT 0,
    rejection_reason TEXT    DEFAULT '',
    route            TEXT    DEFAULT '',
    strategy_version INTEGER DEFAULT 1,
    signal_id        INTEGER DEFAULT NULL, -- FK to signals.id when became a signal
    -- Point-in-time features (signal-time only, no leakage)
    rsi              REAL,
    macd_hist        REAL,
    atr              REAL,
    bb_pos           REAL,
    vwap_dev         REAL,
    vol_ratio        REAL
)
"""

INTENT_SCHEMA = """
CREATE TABLE execution_intents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL DEFAULT 0,
    timestamp        TEXT    NOT NULL DEFAULT '',
    symbol           TEXT    NOT NULL DEFAULT '',
    direction        TEXT    NOT NULL DEFAULT '',
    route            TEXT    NOT NULL DEFAULT '',
    entry_price      REAL,
    stop_loss        REAL,
    target_price     REAL,
    position_size    REAL,
    risk_usd         REAL,
    valid_count      INTEGER DEFAULT 0,
    strategy_version INTEGER DEFAULT 1,
    broker           TEXT    DEFAULT 'paper',
    status           TEXT    DEFAULT 'pending',
    -- status: pending | risk_rejected | paper_logged | accepted |
    --         rejected | filled | cancelled | error | not_implemented |
    --         live_safety_blocked | account_mismatch | connection_failed |
    --         broker_unready
    -- broker_unready (M15.1): set BEFORE submission when the gateway
    --   watchdog flags broker infrastructure unhealthy. Distinct from
    --   connection_failed: connection_failed = "we tried to submit and
    --   IB API rejected"; broker_unready = "watchdog said no, we never
    --   attempted submission". Pair with rejection_reason='gateway_unhealthy_block'.
    broker_order_id  TEXT    DEFAULT NULL,
    rejection_reason TEXT    DEFAULT NULL,
    risk_checks      TEXT    DEFAULT '{}'  ,  -- JSON
    -- Order lifecycle tracking (M12)
    submitted_at     TEXT    DEFAULT NULL,
    filled_at        TEXT    DEFAULT NULL,
    fill_price       REAL    DEFAULT NULL,
    fill_qty         REAL    DEFAULT NULL,
    cancelled_at     TEXT    DEFAULT NULL,
    lifecycle_json   TEXT    DEFAULT '{}'   -- full event log JSON
)
"""

OUTCOME_SCHEMA = """
CREATE TABLE signal_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL DEFAULT 0,  -- FK to signals.id
    intent_id        INTEGER DEFAULT NULL,          -- FK to execution_intents.id
    symbol           TEXT    NOT NULL DEFAULT '',
    direction        TEXT    NOT NULL DEFAULT '',
    entry_price      REAL,
    exit_price       REAL,
    return_pct       REAL,
    outcome          TEXT    DEFAULT NULL,  -- WIN | LOSS | TIMEOUT | OPEN
    bars_held        INTEGER DEFAULT NULL,
    resolved_at      TEXT    DEFAULT NULL,
    resolution_method TEXT   DEFAULT NULL   -- 'stop' | 'target' | 'timeout' | 'manual'
)
"""

# ── Initialisation ────────────────────────────────────────────────────────────


DAILY_STATE_SCHEMA = """
CREATE TABLE daily_state (
    date                     TEXT PRIMARY KEY,
    realised_pnl_usd         REAL DEFAULT 0,
    realised_pnl_pct         REAL DEFAULT 0,
    daily_pnl_source         TEXT DEFAULT 'unavailable',
    daily_pnl_available      INTEGER DEFAULT 0,
    daily_loss_block_active  INTEGER DEFAULT 0,
    daily_loss_alert_sent    INTEGER DEFAULT 0,
    updated_at               TEXT
)
"""

PORTFOLIO_RISK_STATE_SCHEMA = """
CREATE TABLE portfolio_risk_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
)
"""

PORTFOLIO_RISK_SNAPSHOT_SCHEMA = """
CREATE TABLE portfolio_risk_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT NOT NULL,
    cycle_id                INTEGER DEFAULT 0,
    broker                  TEXT DEFAULT '',
    portfolio_value         REAL,
    portfolio_value_source  TEXT DEFAULT 'config',
    daily_realised_pnl      REAL,
    daily_pnl_available     INTEGER DEFAULT 0,
    open_trade_count        INTEGER DEFAULT 0,
    symbol_exposures_json   TEXT DEFAULT '{}',
    sector_exposures_json   TEXT DEFAULT '{}',
    loss_streak             INTEGER DEFAULT 0,
    cooldown_until          TEXT DEFAULT NULL,
    kill_switch_active      INTEGER DEFAULT 0,
    risk_status             TEXT DEFAULT 'ok',
    warnings_json           TEXT DEFAULT '[]',
    policy_json             TEXT DEFAULT '{}'
)
"""

# M15.1 — Gateway watchdog tables.
# gateway_state: latest watchdog snapshot (single row, key='current').
# gateway_events: append-only audit trail of state transitions and recovery
#                 decisions. Indexed on ts, event_type, broker_mode (explicit;
#                 NEVER added to _indexed loop because it has no signal_id/
#                 symbol columns — repeat of M14 f09dbc6 lesson would error).
GATEWAY_STATE_SCHEMA = """
CREATE TABLE gateway_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""

GATEWAY_EVENTS_SCHEMA = """
CREATE TABLE gateway_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    broker_mode   TEXT NOT NULL,
    status_before TEXT,
    status_after  TEXT,
    details_json  TEXT
)
"""

# ── M14.B: Risk Intelligence Layer schema (additive, non-destructive) ────────
# Three new tables. The legacy daily_state table is NOT modified. Existing
# readers (get_daily_state, set_daily_loss_block) continue to use daily_state
# as the source of truth. daily_state_per_broker carries forward the legacy
# fields so the M14 engine has a faithful per-broker / GLOBAL view.

M14_B_SCHEMA_VERSION = 1
M14_B_SENTINEL_KEY   = "schema_version_daily_state_per_broker"
M14_B_SAVEPOINT_NAME = "m14_b_schema"

_VALID_BROKER_SCOPES   = "'ibkr_live','ibkr_paper','etoro_real','etoro_paper','GLOBAL'"
_VALID_DSPB_SOURCES    = "'backfill','ingested','reconciled','manual_fallback','rollup'"
_VALID_RS_SOURCES      = "'scheduled','on_demand','pre_decision'"
_VALID_RD_ACTIONS      = "'trade_open','trade_close','query_authority'"
_VALID_RD_RESULTS      = "'allow','block','downgrade_then_block'"
_VALID_RD_SOURCES      = "'auto','manual','reconciled','manual_reset'"
_VALID_AUTHORITY       = "'OFF','SIGNAL_ONLY','PAPER_ONLY','ONE_SHOT_MANUAL','AUTO_ALLOWED'"

DAILY_STATE_PER_BROKER_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS daily_state_per_broker (
    date                     TEXT    NOT NULL,
    broker_scope             TEXT    NOT NULL
                              CHECK (broker_scope IN ({_VALID_BROKER_SCOPES})),
    -- Legacy-compatible fields carried forward from daily_state so the
    -- GLOBAL backfill preserves the existing daily-state surface.
    realised_pnl_usd         REAL    NOT NULL DEFAULT 0,
    realised_pnl_pct         REAL    NOT NULL DEFAULT 0,
    daily_pnl_source         TEXT    NOT NULL DEFAULT 'unavailable',
    daily_pnl_available      INTEGER NOT NULL DEFAULT 0,
    daily_loss_block_active  INTEGER NOT NULL DEFAULT 0,
    daily_loss_alert_sent    INTEGER NOT NULL DEFAULT 0,
    -- M14 new fields. Populated by M14.C/D/E ingestion; M14.B leaves them
    -- at defaults for backfill rows.
    realised_daily_loss      REAL    NOT NULL DEFAULT 0,
    open_positions           INTEGER NOT NULL DEFAULT 0,
    capital_deployed         REAL    NOT NULL DEFAULT 0,
    peak_equity              REAL,
    drawdown_from_peak       REAL    NOT NULL DEFAULT 0,
    source                   TEXT    NOT NULL DEFAULT 'backfill'
                              CHECK (source IN ({_VALID_DSPB_SOURCES})),
    last_ingested_at         TEXT,
    fresh_reads_count        INTEGER NOT NULL DEFAULT 0,
    lifecycle_json           TEXT,
    updated_at               TEXT    NOT NULL,
    PRIMARY KEY (date, broker_scope)
)
"""

RISK_SNAPSHOTS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS risk_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at           TEXT    NOT NULL,
    policy_version     INTEGER,
    snapshot_json      TEXT    NOT NULL,
    freshness_summary  TEXT,
    source             TEXT    NOT NULL
                        CHECK (source IN ({_VALID_RS_SOURCES})),
    created_at         TEXT    NOT NULL
)
"""

# Note: risk_decisions.snapshot_id is a SOFT FK to risk_snapshots(id).
# Real FK enforcement requires PRAGMA foreign_keys=ON, which is a separate,
# wider change (would affect every existing table). M14.B intentionally
# does not flip that pragma; integrity is checked in the engine (M14.E).
RISK_DECISIONS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id        TEXT    PRIMARY KEY,
    taken_at           TEXT    NOT NULL,
    broker_scope       TEXT    NOT NULL
                        CHECK (broker_scope IN ({_VALID_BROKER_SCOPES})),
    requested_action   TEXT    NOT NULL
                        CHECK (requested_action IN ({_VALID_RD_ACTIONS})),
    request_json       TEXT,
    result             TEXT    NOT NULL
                        CHECK (result IN ({_VALID_RD_RESULTS})),
    authority_before   TEXT    NOT NULL
                        CHECK (authority_before IN ({_VALID_AUTHORITY})),
    authority_after    TEXT    NOT NULL
                        CHECK (authority_after IN ({_VALID_AUTHORITY})),
    reason_codes       TEXT    NOT NULL,
    recovery_paths     TEXT,
    snapshot_id        INTEGER,
    source             TEXT    NOT NULL
                        CHECK (source IN ({_VALID_RD_SOURCES})),
    actor              TEXT,
    explainer          TEXT,
    created_at         TEXT    NOT NULL
)
"""


def ensure_daily_state_per_broker_migrations(conn: sqlite3.Connection) -> dict:
    """M14.B — additive schema + one-time backfill.

    Creates three new tables (daily_state_per_broker, risk_snapshots,
    risk_decisions) and backfills every existing daily_state row into
    daily_state_per_broker with broker_scope='GLOBAL'. Idempotent on
    repeated calls.

    Key properties (per M14.B ChatGPT review corrections):

      * Additive: the legacy daily_state table is NEVER modified.
      * Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS
        run every call. The version sentinel controls ONLY whether the
        one-time backfill runs again; it never hides missing DDL.
      * Nested-safe: wrapped in a SAVEPOINT so it works whether or not
        the caller is already inside a transaction.
      * Legacy-shape compatible: the GLOBAL backfill carries forward the
        full legacy daily_state field surface (daily_pnl_source,
        daily_pnl_available, daily_loss_block_active, daily_loss_alert_sent)
        so bot/risk_authority/state.get_daily_state_compat can return the
        same dict shape as bot.flywheel.get_daily_state.

    Returns a dict summary: {'created_tables', 'created_indexes',
    'backfilled_rows', 'sentinel_version'}.
    """
    from datetime import datetime, timezone

    started_tx = not conn.in_transaction
    if started_tx:
        conn.execute("BEGIN")
    sp = M14_B_SAVEPOINT_NAME
    conn.execute(f"SAVEPOINT {sp}")
    try:
        # 1) DDL is always run idempotently — version sentinel never hides
        #    missing tables or indexes.
        existing_tables_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for name, schema in (
            ("daily_state_per_broker", DAILY_STATE_PER_BROKER_SCHEMA),
            ("risk_snapshots",         RISK_SNAPSHOTS_SCHEMA),
            ("risk_decisions",         RISK_DECISIONS_SCHEMA),
        ):
            conn.execute(schema)

        # Indexes — minimal set. PK covers (date, broker_scope) lookups;
        # composite (broker_scope, date) supports per-scope history.
        # No redundant single-column indexes (per M14.B correction #5).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_dspb_scope_date "
            "ON daily_state_per_broker(broker_scope, date)"
        )
        # risk_decisions: one composite for the most likely audit query.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_rd_scope_taken "
            "ON risk_decisions(broker_scope, taken_at)"
        )

        existing_tables_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        created_tables = sorted(existing_tables_after - existing_tables_before)

        # 2) Version sentinel — controls ONLY whether to backfill.
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='portfolio_risk_state'"
        ).fetchone()
        sentinel_ok = False
        if cur:
            row = conn.execute(
                "SELECT value FROM portfolio_risk_state WHERE key=?",
                (M14_B_SENTINEL_KEY,),
            ).fetchone()
            sentinel_ok = bool(row and str(row[0]) == str(M14_B_SCHEMA_VERSION))

        backfilled_rows = 0
        if not sentinel_ok:
            # Only run backfill if the source table actually exists. On a
            # fresh DB with no daily_state, this is a no-op.
            ds = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_state'"
            ).fetchone()
            if ds:
                before = conn.execute(
                    "SELECT COUNT(*) FROM daily_state_per_broker"
                ).fetchone()[0]
                conn.execute(
                    "INSERT OR IGNORE INTO daily_state_per_broker "
                    "(date, broker_scope, "
                    " realised_pnl_usd, realised_pnl_pct, "
                    " daily_pnl_source, daily_pnl_available, "
                    " daily_loss_block_active, daily_loss_alert_sent, "
                    " realised_daily_loss, source, updated_at) "
                    "SELECT date, 'GLOBAL', "
                    "       COALESCE(realised_pnl_usd, 0), "
                    "       COALESCE(realised_pnl_pct, 0), "
                    "       COALESCE(daily_pnl_source, 'unavailable'), "
                    "       COALESCE(daily_pnl_available, 0), "
                    "       COALESCE(daily_loss_block_active, 0), "
                    "       COALESCE(daily_loss_alert_sent, 0), "
                    "       0, 'backfill', "
                    "       COALESCE(updated_at, ?) "
                    "FROM daily_state",
                    (datetime.now(timezone.utc).isoformat(),)
                )
                after = conn.execute(
                    "SELECT COUNT(*) FROM daily_state_per_broker"
                ).fetchone()[0]
                backfilled_rows = after - before

            # Write sentinel. portfolio_risk_state must exist for this; it
            # is created by init_flywheel_tables's _plain loop.
            if cur:
                conn.execute(
                    "INSERT OR REPLACE INTO portfolio_risk_state "
                    "(key, value, updated_at) VALUES (?, ?, ?)",
                    (M14_B_SENTINEL_KEY, str(M14_B_SCHEMA_VERSION),
                     datetime.now(timezone.utc).isoformat()),
                )

        conn.execute(f"RELEASE {sp}")
        if started_tx:
            conn.commit()
        if created_tables:
            log.info("[FLYWHEEL] M14.B created tables: %s", created_tables)
        if backfilled_rows:
            log.info("[FLYWHEEL] M14.B backfilled %d daily_state rows -> GLOBAL",
                     backfilled_rows)
        return {
            "created_tables":   created_tables,
            "created_indexes":  ["ix_dspb_scope_date", "ix_rd_scope_taken"],
            "backfilled_rows":  backfilled_rows,
            "sentinel_version": M14_B_SCHEMA_VERSION,
        }
    except Exception:
        # SAVEPOINT rollback undoes all DDL + the sentinel write.
        conn.execute(f"ROLLBACK TO {sp}")
        conn.execute(f"RELEASE {sp}")
        if started_tx:
            conn.rollback()
        raise


# ── M14.D: broker_positions schema (additive, idempotent) ────────────────────
# Append-only per-position snapshot table. Each ingest run shares one
# exposure_batch_id (UUID populated by the orchestrator), so M14.E can
# fetch "latest snapshot per scope" via MAX(fetched_at_utc) -> SELECT
# WHERE exposure_batch_id = that.
#
# CHECK constraints enforce the broker_scope and side enums. The PK is
# position_row_id (per-row autoincrement); exposure_batch_id is a
# regular column that groups rows belonging to one snapshot.

M14_D_SCHEMA_VERSION = 1
M14_D_SENTINEL_KEY   = "schema_version_broker_positions"
M14_D_SAVEPOINT_NAME = "m14_d_schema"

_M14D_VALID_SCOPES = "'ibkr_live','ibkr_paper','etoro_real','etoro_paper'"
_M14D_VALID_SIDES  = "'long','short'"

BROKER_POSITIONS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS broker_positions (
    position_row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    exposure_batch_id    TEXT    NOT NULL,
    broker_scope         TEXT    NOT NULL
                          CHECK (broker_scope IN ({_M14D_VALID_SCOPES})),
    date                 TEXT    NOT NULL,
    fetched_at_utc       TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,
    side                 TEXT    NOT NULL
                          CHECK (side IN ({_M14D_VALID_SIDES})),
    qty                  REAL    NOT NULL,
    exposure_usd         REAL    NOT NULL,
    avg_price            REAL,
    mark_price           REAL,
    unrealised_pnl_usd   REAL,
    opened_at            TEXT,
    instrument_id        INTEGER,
    raw_evidence         TEXT,
    created_at           TEXT    NOT NULL
)
"""


def ensure_broker_positions_migration(conn: sqlite3.Connection) -> dict:
    """M14.D — additive append-only per-position snapshot table.

    Idempotent. Uses a SAVEPOINT so it is nested-safe whether or not the
    caller is already in a transaction. The version sentinel does NOT
    skip DDL — CREATE TABLE/INDEX IF NOT EXISTS run every call so a
    missing object is recreated. The sentinel is used only to mark that
    we have visited this migration (no backfill is performed; the table
    is fresh).

    Returns a dict summary suitable for logging.
    """
    from datetime import datetime, timezone

    started_tx = not conn.in_transaction
    if started_tx:
        conn.execute("BEGIN")
    sp = M14_D_SAVEPOINT_NAME
    conn.execute(f"SAVEPOINT {sp}")
    try:
        existing_before = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.execute(BROKER_POSITIONS_SCHEMA)
        # Indexes, idempotent — recreate if dropped.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bp_scope_date_fetched "
            "ON broker_positions(broker_scope, date, fetched_at_utc)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bp_scope_batch "
            "ON broker_positions(broker_scope, exposure_batch_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bp_symbol "
            "ON broker_positions(symbol)"
        )
        existing_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        created_tables = sorted(existing_after - existing_before)

        # Write the version sentinel into portfolio_risk_state.
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='portfolio_risk_state'"
        ).fetchone()
        if cur:
            conn.execute(
                "INSERT OR REPLACE INTO portfolio_risk_state "
                "(key, value, updated_at) VALUES (?, ?, ?)",
                (M14_D_SENTINEL_KEY, str(M14_D_SCHEMA_VERSION),
                 datetime.now(timezone.utc).isoformat()),
            )

        conn.execute(f"RELEASE {sp}")
        if started_tx:
            conn.commit()
        if created_tables:
            log.info("[FLYWHEEL] M14.D created tables: %s", created_tables)
        return {
            "created_tables":  created_tables,
            "created_indexes": ["ix_bp_scope_date_fetched",
                                "ix_bp_scope_batch", "ix_bp_symbol"],
            "sentinel_version": M14_D_SCHEMA_VERSION,
        }
    except Exception:
        conn.execute(f"ROLLBACK TO {sp}")
        conn.execute(f"RELEASE {sp}")
        if started_tx:
            conn.rollback()
        raise


def init_flywheel_tables(conn: sqlite3.Connection) -> None:
    """Create flywheel tables if they don't exist. Safe to call on startup."""
    # Tables with signal_id + symbol indexes
    _indexed = [
        ('candidate_snapshots', CANDIDATE_SCHEMA),
        ('execution_intents',   INTENT_SCHEMA),
        ('signal_outcomes',     OUTCOME_SCHEMA),
    ]
    # M14 + M15.1 tables — no signal_id/symbol columns, no generic indexes
    _plain = [
        ('daily_state',              DAILY_STATE_SCHEMA),
        ('portfolio_risk_state',     PORTFOLIO_RISK_STATE_SCHEMA),
        ('portfolio_risk_snapshots', PORTFOLIO_RISK_SNAPSHOT_SCHEMA),
        ('gateway_state',            GATEWAY_STATE_SCHEMA),
        ('gateway_events',           GATEWAY_EVENTS_SCHEMA),
    ]
    for name, schema in _indexed:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if not exists:
            conn.execute(schema)
            conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{name}_signal_id '
                         f'ON {name}(signal_id)')
            conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{name}_symbol '
                         f'ON {name}(symbol)')
            conn.commit()
            log.info('[FLYWHEEL] Created table: %s', name)
    # M15 schema hardening — bring pre-M12 execution_intents tables up to date
    # with the lifecycle columns declared in INTENT_SCHEMA. Idempotent.
    ensure_execution_intents_migrations(conn)
    for name, schema in _plain:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if not exists:
            conn.execute(schema)
            conn.commit()
            log.info('[FLYWHEEL] Created table: %s', name)
    # M14.B — additive Risk Intelligence schema (new tables only; backfill
    # daily_state into per-broker GLOBAL rows). Idempotent; never touches the
    # legacy daily_state table.
    ensure_daily_state_per_broker_migrations(conn)
    # M14.D — additive broker_positions snapshot table (append-only).
    # Idempotent; no backfill. Fresh table created in dependency order
    # after portfolio_risk_state (M14.B sentinel target).
    ensure_broker_positions_migration(conn)
    # M15.1 — explicit indexes for gateway_events. NOT added via _indexed loop:
    # that loop adds generic signal_id/symbol indexes which would fail here
    # (M14 f09dbc6 lesson, codified).
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gateway_events_ts '
                 'ON gateway_events(ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gateway_events_event_type '
                 'ON gateway_events(event_type)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gateway_events_broker_mode '
                 'ON gateway_events(broker_mode)')
    conn.commit()


def ensure_execution_intents_migrations(conn: sqlite3.Connection) -> list:
    """
    M15 schema hardening — idempotent ALTER TABLE for M12 lifecycle columns.

    The live execution_intents table predates the M12 lifecycle columns
    declared in INTENT_SCHEMA. CREATE TABLE IF NOT EXISTS does not migrate
    existing tables, which left update_intent_status writing to columns
    that did not exist on disk (silent partial writes).

    Idempotent: first call adds missing columns, subsequent calls return [].
    Never drops or recreates the table. Never deletes data.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_intents'"
    )
    if not cur.fetchone():
        # Table does not exist yet — init_flywheel_tables will CREATE it with
        # the full INTENT_SCHEMA. Nothing to migrate.
        return []
    cur.execute("PRAGMA table_info(execution_intents)")
    existing = {row[1] for row in cur.fetchall()}
    required = [
        ("submitted_at",   "TEXT"),
        ("filled_at",      "TEXT"),
        ("fill_price",     "REAL"),
        ("fill_qty",       "REAL"),
        ("cancelled_at",   "TEXT"),
        ("lifecycle_json", "TEXT"),
    ]
    added: list = []
    for col, sqltype in required:
        if col not in existing:
            cur.execute(
                f"ALTER TABLE execution_intents ADD COLUMN {col} {sqltype}"
            )
            added.append(col)
            log.info(
                '[FLYWHEEL] ensure_execution_intents_migrations: added %s %s',
                col, sqltype,
            )
    if added:
        conn.commit()
    return added


# ── Candidate snapshot ────────────────────────────────────────────────────────

def log_candidate(
    conn: sqlite3.Connection,
    cycle_id: int,
    symbol: str,
    direction: str,
    stage: str,
    valid_count: int,
    tfs_passing: list,
    available_tfs: int,
    min_valid: int,
    rejection_reason: str = '',
    route: str = '',
    strategy_version: int = 1,
    signal_id: Optional[int] = None,
    ind: Optional[dict] = None,
) -> Optional[int]:
    """
    Log one candidate snapshot. Called for every symbol at every stage.
    Returns inserted row id or None on failure.
    """
    try:
        ind = ind or {}
        cur = conn.execute(
            """INSERT INTO candidate_snapshots
               (cycle_id, timestamp, symbol, direction, stage,
                valid_count, tfs_passing, available_tfs, min_valid,
                rejection_reason, route, strategy_version, signal_id,
                rsi, macd_hist, atr, bb_pos, vwap_dev, vol_ratio)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cycle_id,
                datetime.now(timezone.utc).isoformat(),
                symbol, direction, stage,
                valid_count,
                '+'.join(tfs_passing) if tfs_passing else '',
                available_tfs, min_valid,
                rejection_reason, route, strategy_version, signal_id,
                ind.get('rsi'), ind.get('macd_hist'), ind.get('atr'),
                ind.get('bb_pos'), ind.get('vwap_dev'), ind.get('vol_ratio'),
            )
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        log.warning('[FLYWHEEL] log_candidate failed: %s', e)
        return None


# ── Execution intent ──────────────────────────────────────────────────────────

def log_intent(
    conn: sqlite3.Connection,
    signal_id: int,
    symbol: str,
    direction: str,
    route: str,
    entry_price: float,
    stop_loss: float,
    target_price: float,
    position_size: Optional[float],
    risk_usd: Optional[float],
    valid_count: int,
    strategy_version: int,
    broker: str,
    status: str,
    broker_order_id: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    risk_checks: Optional[dict] = None,
) -> Optional[int]:
    """Log one execution intent. Returns inserted row id or None."""
    try:
        cur = conn.execute(
            """INSERT INTO execution_intents
               (signal_id, timestamp, symbol, direction, route,
                entry_price, stop_loss, target_price, position_size, risk_usd,
                valid_count, strategy_version, broker, status,
                broker_order_id, rejection_reason, risk_checks)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal_id,
                datetime.now(timezone.utc).isoformat(),
                symbol, direction, route,
                entry_price, stop_loss, target_price,
                position_size, risk_usd,
                valid_count, strategy_version,
                broker, status,
                broker_order_id, rejection_reason,
                json.dumps(risk_checks or {}),
            )
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        log.warning('[FLYWHEEL] log_intent failed: %s', e)
        return None


# ── Convenience query ─────────────────────────────────────────────────────────

def recent_candidates(conn: sqlite3.Connection, limit: int = 50) -> list:
    """Return recent candidate snapshots as list of dicts."""
    try:
        cur = conn.execute(
            'SELECT * FROM candidate_snapshots ORDER BY id DESC LIMIT ?', (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def update_intent_status(
    conn: sqlite3.Connection,
    intent_id: int,
    status: str,
    fill_price: float = None,
    fill_qty: float = None,
    event: str = None,
) -> None:
    """
    Update an execution_intent row with lifecycle event.
    Appends the event to lifecycle_json for full audit trail.
    """
    import json as _json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        # Read current lifecycle_json
        row = conn.execute(
            'SELECT lifecycle_json FROM execution_intents WHERE id=?', (intent_id,)
        ).fetchone()
        lifecycle = _json.loads(row[0] or '{}') if row else {}
        events = lifecycle.get('events', [])
        events.append({'ts': now, 'status': status, 'event': event or status})
        lifecycle['events'] = events
        lifecycle['last_status'] = status

        updates = ['status=?', 'lifecycle_json=?']
        values  = [status, _json.dumps(lifecycle)]

        if status == 'filled' and fill_price:
            updates += ['filled_at=?', 'fill_price=?', 'fill_qty=?']
            values  += [now, fill_price, fill_qty]
        elif status == 'cancelled':
            updates += ['cancelled_at=?']
            values  += [now]
        elif status in ('accepted', 'paper_logged'):
            updates += ['submitted_at=?']
            values  += [now]

        values.append(intent_id)
        conn.execute(
            f'UPDATE execution_intents SET {", ".join(updates)} WHERE id=?',
            values
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        log.error(
            '[FLYWHEEL] update_intent_status: SQL failure (schema mismatch?): %s '
            '| intent_id=%r status=%r', e, intent_id, status
        )
        raise
    except Exception as e:
        log.warning('[FLYWHEEL] update_intent_status failed: %s', e)


def recent_intents(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Return recent execution intents as list of dicts."""
    try:
        cur = conn.execute(
            'SELECT * FROM execution_intents ORDER BY id DESC LIMIT ?', (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


# ── M14 Portfolio Risk Writers ────────────────────────────────────────────────

def get_daily_state(conn: sqlite3.Connection) -> dict:
    """Get or create today's daily_state row."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row = conn.execute(
        'SELECT date,realised_pnl_usd,realised_pnl_pct,daily_pnl_source,'
        'daily_pnl_available,daily_loss_block_active,daily_loss_alert_sent '
        'FROM daily_state WHERE date=?', (today,)
    ).fetchone()
    if row:
        return {'date': row[0], 'realised_pnl_usd': row[1], 'realised_pnl_pct': row[2],
                'daily_pnl_source': row[3], 'daily_pnl_available': row[4],
                'daily_loss_block_active': row[5], 'daily_loss_alert_sent': row[6]}
    # Create fresh row for today
    conn.execute(
        "INSERT OR IGNORE INTO daily_state (date,updated_at) VALUES (?,?)",
        (today, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    return {'date': today, 'realised_pnl_usd': 0, 'realised_pnl_pct': 0,
            'daily_pnl_source': 'unavailable', 'daily_pnl_available': 0,
            'daily_loss_block_active': 0, 'daily_loss_alert_sent': 0}


def set_daily_loss_block(conn: sqlite3.Connection, active: bool,
                          alert_sent: bool = False) -> None:
    from datetime import datetime, timezone
    # Ensure today's row exists first, then UPDATE
    get_daily_state(conn)  # creates row if missing
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn.execute(
        "UPDATE daily_state "
        "SET daily_loss_block_active=?, daily_loss_alert_sent=?, updated_at=? "
        "WHERE date=?",
        (int(active), int(alert_sent), datetime.now(timezone.utc).isoformat(), today)
    )
    conn.commit()


def get_persistent_state(conn: sqlite3.Connection) -> dict:
    """Read all portfolio_risk_state key-value pairs."""
    rows = conn.execute('SELECT key, value FROM portfolio_risk_state').fetchall()
    return {r[0]: r[1] for r in rows}


def set_persistent_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    from datetime import datetime, timezone
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_risk_state (key,value,updated_at) VALUES (?,?,?)",
        (key, value, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def write_portfolio_snapshot(conn: sqlite3.Connection, cycle_id: int,
                              broker: str, ctx,
                              checks: dict | None = None) -> None:
    """
    Write one portfolio_risk_snapshots row.
    Called every scan cycle regardless of whether any signal fired.
    ctx is a PortfolioRiskContext instance.
    checks is the last evaluate() checks dict if available.
    """
    import json
    from datetime import datetime, timezone
    from bot.kill_switch import is_kill_switch_active

    daily = ctx.daily_state
    risk_status = 'ok'
    if checks:
        if checks.get('verdict') == 'reject':
            risk_status = 'blocked'
        elif ctx.warnings:
            risk_status = 'warning'

    # Build symbol and sector exposure summaries
    sym_exp = {}
    sec_exp = {}
    if checks:
        sym_exp = {'symbol': getattr(ctx, '_last_symbol', ''),
                   'pct': checks.get('symbol_exposure_pct'),
                   'estimated': checks.get('symbol_exposure_estimated')}
        sec_exp = {'sector': checks.get('sector'),
                   'pct': checks.get('sector_exposure_pct'),
                   'estimated': checks.get('sector_exposure_estimated')}

    loss_streak = 0
    cooldown = None
    if checks:
        streak_detail = checks.get('loss_streak', {})
        loss_streak = streak_detail.get('streak', 0)
        cooldown = streak_detail.get('cooldown_until')

    policy = {
        'max_daily_loss_pct': float(os.getenv('RISK_MAX_DAILY_LOSS_PCT', '3.0')),
        'max_open_trades': int(os.getenv('RISK_MAX_OPEN_POSITIONS', '10')),
        'max_symbol_pct': float(os.getenv('RISK_MAX_SYMBOL_EXPOSURE_PCT', '10.0')),
        'max_sector_pct': float(os.getenv('RISK_MAX_SECTOR_EXPOSURE_PCT', '30.0')),
    }

    conn.execute(
        """INSERT INTO portfolio_risk_snapshots
           (created_at, cycle_id, broker, portfolio_value, portfolio_value_source,
            daily_realised_pnl, daily_pnl_available, open_trade_count,
            symbol_exposures_json, sector_exposures_json, loss_streak,
            cooldown_until, kill_switch_active, risk_status, warnings_json, policy_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            cycle_id, broker,
            ctx.portfolio_value, ctx.portfolio_value_source,
            daily.get('realised_pnl_usd', 0), daily.get('daily_pnl_available', 0),
            checks.get('open_trade_count', 0) if checks else 0,
            json.dumps(sym_exp), json.dumps(sec_exp),
            loss_streak, cooldown,
            int(is_kill_switch_active()),
            risk_status,
            json.dumps(ctx.warnings),
            json.dumps(policy),
        )
    )
    conn.commit()


# ── M15.1 Gateway watchdog helpers ────────────────────────────────────────────

def write_gateway_state(state: dict, conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None) -> None:
    """M15.1 — upsert latest watchdog state into gateway_state (key='current').

    Caller may pass either an open connection or a db_path. If neither is
    given, falls back to the path from the SIGNALS_DB_PATH env var or the
    canonical data/signals.db.
    """
    own = conn is None
    if own:
        path = db_path or os.getenv('SIGNALS_DB_PATH') or str(
            Path(__file__).resolve().parent.parent / 'data' / 'signals.db'
        )
        conn = sqlite3.connect(path)
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
        conn.execute(
            "INSERT INTO gateway_state(key, value, updated_at) "
            "VALUES('current', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (json.dumps(state, default=str), ts),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        log.error('[FLYWHEEL] write_gateway_state: SQL failure: %s', e)
        raise
    except Exception as e:
        log.warning('[FLYWHEEL] write_gateway_state failed: %s', e)
    finally:
        if own and conn is not None:
            conn.close()


def read_gateway_state(conn: Optional[sqlite3.Connection] = None,
                       db_path: Optional[str] = None) -> dict:
    """M15.1 — fetch latest watchdog state. Returns {} if absent."""
    own = conn is None
    if own:
        path = db_path or os.getenv('SIGNALS_DB_PATH') or str(
            Path(__file__).resolve().parent.parent / 'data' / 'signals.db'
        )
        conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM gateway_state WHERE key='current'"
        ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row[0])
        except Exception:
            payload = {}
        payload['_persisted_at'] = row[1]
        return payload
    except Exception as e:
        log.warning('[FLYWHEEL] read_gateway_state failed: %s', e)
        return {}
    finally:
        if own and conn is not None:
            conn.close()


def write_gateway_event(event_type: str, broker_mode: str,
                        status_before: Optional[str] = None,
                        status_after: Optional[str] = None,
                        details: Optional[dict] = None,
                        conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None) -> Optional[int]:
    """M15.1 — append-only audit row in gateway_events."""
    own = conn is None
    if own:
        path = db_path or os.getenv('SIGNALS_DB_PATH') or str(
            Path(__file__).resolve().parent.parent / 'data' / 'signals.db'
        )
        conn = sqlite3.connect(path)
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
        cur = conn.execute(
            "INSERT INTO gateway_events("
            "ts, event_type, broker_mode, status_before, status_after, details_json"
            ") VALUES(?, ?, ?, ?, ?, ?)",
            (ts, event_type, broker_mode, status_before, status_after,
             json.dumps(details or {}, default=str)),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.OperationalError as e:
        log.error('[FLYWHEEL] write_gateway_event: SQL failure: %s', e)
        raise
    except Exception as e:
        log.warning('[FLYWHEEL] write_gateway_event failed: %s', e)
        return None
    finally:
        if own and conn is not None:
            conn.close()


def read_gateway_events(limit: int = 20,
                        conn: Optional[sqlite3.Connection] = None,
                        db_path: Optional[str] = None) -> list:
    """M15.1 — most recent gateway_events rows, newest first."""
    own = conn is None
    if own:
        path = db_path or os.getenv('SIGNALS_DB_PATH') or str(
            Path(__file__).resolve().parent.parent / 'data' / 'signals.db'
        )
        conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT id, ts, event_type, broker_mode, status_before, "
            "status_after, details_json "
            "FROM gateway_events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            try:
                d = json.loads(r[6]) if r[6] else {}
            except Exception:
                d = {}
            out.append({
                'id': r[0], 'ts': r[1], 'event_type': r[2],
                'broker_mode': r[3], 'status_before': r[4],
                'status_after': r[5], 'details': d,
            })
        return out
    except Exception as e:
        log.warning('[FLYWHEEL] read_gateway_events failed: %s', e)
        return []
    finally:
        if own and conn is not None:
            conn.close()

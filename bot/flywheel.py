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
    --         rejected | filled | cancelled | error | not_implemented
    broker_order_id  TEXT    DEFAULT NULL,
    rejection_reason TEXT    DEFAULT NULL,
    risk_checks      TEXT    DEFAULT '{}'   -- JSON
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

def init_flywheel_tables(conn: sqlite3.Connection) -> None:
    """Create flywheel tables if they don't exist. Safe to call on startup."""
    for name, schema in [
        ('candidate_snapshots', CANDIDATE_SCHEMA),
        ('execution_intents',   INTENT_SCHEMA),
        ('signal_outcomes',     OUTCOME_SCHEMA),
    ]:
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

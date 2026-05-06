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
    --         live_safety_blocked | account_mismatch | connection_failed
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

def init_flywheel_tables(conn: sqlite3.Connection) -> None:
    """Create flywheel tables if they don't exist. Safe to call on startup."""
    # Tables with signal_id + symbol indexes
    _indexed = [
        ('candidate_snapshots', CANDIDATE_SCHEMA),
        ('execution_intents',   INTENT_SCHEMA),
        ('signal_outcomes',     OUTCOME_SCHEMA),
    ]
    # M14 tables — no signal_id/symbol columns, no generic indexes
    _plain = [
        ('daily_state',              DAILY_STATE_SCHEMA),
        ('portfolio_risk_state',     PORTFOLIO_RISK_STATE_SCHEMA),
        ('portfolio_risk_snapshots', PORTFOLIO_RISK_SNAPSHOT_SCHEMA),
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
    for name, schema in _plain:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if not exists:
            conn.execute(schema)
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

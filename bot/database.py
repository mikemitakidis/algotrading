"""
bot/database.py
SQLite storage with automatic schema migration.

On every startup:
1. Create the signals table if it does not exist
2. Check every required column — ALTER TABLE to add any that are missing
3. Never drop existing columns (no data loss)
4. Log every migration action clearly

This means schema changes in code are automatically applied to
existing databases without manual intervention.
"""
import sqlite3
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Canonical schema: column name -> SQLite type
# Order here is insertion order for new tables only.
# Migrations use ALTER TABLE ADD COLUMN for any missing column.
SCHEMA = [
    ('id',          'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('timestamp',   'TEXT NOT NULL DEFAULT ""'),
    ('symbol',      'TEXT NOT NULL DEFAULT ""'),
    ('direction',   'TEXT NOT NULL DEFAULT ""'),
    ('tf_15m',      'INTEGER DEFAULT 0'),
    ('tf_1h',       'INTEGER DEFAULT 0'),
    ('tf_4h',       'INTEGER DEFAULT 0'),
    ('tf_1d',       'INTEGER DEFAULT 0'),
    ('valid_count', 'INTEGER DEFAULT 0'),
    ('route',       'TEXT DEFAULT ""'),
    ('rsi',         'REAL'),
    ('macd_hist',   'REAL'),
    ('ema20',       'REAL'),
    ('ema50',       'REAL'),
    ('bb_pos',      'REAL'),
    ('bb_width',    'REAL'),
    ('vwap_dev',    'REAL'),
    ('obv_slope',   'REAL'),
    ('atr',         'REAL'),
    ('vol_ratio',   'REAL'),
    ('price',       'REAL'),
    ('pchg',            'REAL'),
    # Risk levels computed at signal time (ATR-based, shadow mode only)
    ('entry_price',     'REAL'),
    ('stop_loss',       'REAL'),
    ('target_price',    'REAL'),
    # Strategy version at time of signal (for ML/backtesting)
    ('strategy_version','INTEGER DEFAULT 1'),
    # Sentiment fields (Milestone 8) — nullable, default NULL
    ('sentiment_enabled', 'INTEGER DEFAULT 0'),
    ('sentiment_mode',    'TEXT DEFAULT "off"'),
    ('sentiment_score',   'REAL'),
    ('sentiment_label',   'TEXT DEFAULT "unavailable"'),
    ('sentiment_source',  'TEXT DEFAULT "disabled"'),
    ('sentiment_status',  'TEXT DEFAULT "disabled"'),
]

# Columns that must exist for inserts to work
REQUIRED_COLS = {col for col, _ in SCHEMA if col != 'id'}


def _get_existing_columns(conn: sqlite3.Connection) -> set:
    """Return set of column names currently in the signals table."""
    try:
        rows = conn.execute('PRAGMA table_info(signals)').fetchall()
        return {row[1] for row in rows}
    except Exception:
        return set()


def _migrate(conn: sqlite3.Connection):
    """Add any missing columns to the signals table. Never drops columns."""
    existing = _get_existing_columns(conn)
    if not existing:
        return  # table doesn't exist yet — will be created below

    added = []
    for col, col_type in SCHEMA:
        if col == 'id':
            continue
        if col not in existing:
            # Use a safe default so NOT NULL constraints don't break
            safe_type = col_type.replace('NOT NULL', '').strip()
            try:
                conn.execute(f'ALTER TABLE signals ADD COLUMN {col} {safe_type}')
                added.append(col)
                log.info('[DB] Migration: added column "%s"', col)
            except Exception as e:
                log.warning('[DB] Could not add column "%s": %s', col, e)

    if added:
        conn.commit()
        log.info('[DB] Migration complete. Added %d columns: %s', len(added), added)
    else:
        log.info('[DB] Schema up to date. No migration needed.')


def init_db(db_path: str) -> sqlite3.Connection:
    """
    Open database, create table if needed, migrate schema if needed.
    Returns open connection safe for use in a single thread.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)

    # Check if table exists
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()

    if not exists:
        # Fresh install — create full schema
        cols_sql = ',\n    '.join(f'{col} {typ}' for col, typ in SCHEMA)
        conn.execute(f'CREATE TABLE signals (\n    {cols_sql}\n)')
        conn.commit()
        log.info('[DB] Created signals table with %d columns', len(SCHEMA))
    else:
        # Existing table — migrate any missing columns
        _migrate(conn)

    # Final verification
    existing = _get_existing_columns(conn)
    missing = REQUIRED_COLS - existing
    if missing:
        log.error('[DB] CRITICAL: columns still missing after migration: %s', missing)
    else:
        log.info('[DB] Initialised at %s | %d columns verified', db_path, len(existing))

    return conn


def insert_signal(conn: sqlite3.Connection, signal: dict) -> int | None:
    """
    Insert a signal. Verifies required keys are present before inserting.
    Returns new row id or None on failure.
    """
    required = ('timestamp', 'symbol', 'direction', 'price', 'rsi', 'atr')
    missing = [k for k in required if not signal.get(k)]
    if missing:
        log.warning('[DB] Skipping insert — missing fields: %s', missing)
        return None

    try:
        cursor = conn.execute(
            '''INSERT INTO signals
               (timestamp, symbol, direction,
                tf_15m, tf_1h, tf_4h, tf_1d, valid_count, route,
                rsi, macd_hist, ema20, ema50,
                bb_pos, bb_width, vwap_dev, obv_slope,
                atr, vol_ratio, price, pchg,
                entry_price, stop_loss, target_price, strategy_version,
                sentiment_enabled, sentiment_mode, sentiment_score,
                sentiment_label, sentiment_source, sentiment_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                signal.get('timestamp', ''),
                signal.get('symbol', ''),
                signal.get('direction', ''),
                signal.get('tf_15m', 0),
                signal.get('tf_1h', 0),
                signal.get('tf_4h', 0),
                signal.get('tf_1d', 0),
                signal.get('valid_count', 0),
                signal.get('route', ''),
                signal.get('rsi'),
                signal.get('macd_hist'),
                signal.get('ema20'),
                signal.get('ema50'),
                signal.get('bb_pos'),
                signal.get('bb_width'),
                signal.get('vwap_dev'),
                signal.get('obv_slope'),
                signal.get('atr'),
                signal.get('vol_ratio'),
                signal.get('price'),
                signal.get('pchg'),
                signal.get('entry_price'),
                signal.get('stop_loss'),
                signal.get('target_price'),
                signal.get('strategy_version', 1),
                signal.get('sentiment_enabled', 0),
                signal.get('sentiment_mode',    'off'),
                signal.get('sentiment_score'),
                signal.get('sentiment_label',   'unavailable'),
                signal.get('sentiment_source',  'disabled'),
                signal.get('sentiment_status',  'disabled'),
            )
        )
        conn.commit()
        log.info('[DB] Inserted: id=%d  %s %s %s',
                 cursor.lastrowid,
                 signal.get('symbol'),
                 signal.get('direction', '').upper(),
                 signal.get('route', ''))
        return cursor.lastrowid
    except Exception as e:
        log.error('[DB] Insert failed for %s: %s', signal.get('symbol'), e)
        return None


def recent_signals(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Return most recent signals as list of dicts."""
    try:
        cur = conn.execute('SELECT * FROM signals ORDER BY id DESC LIMIT ?', (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning('[DB] recent_signals error: %s', e)
        return []


def signal_counts(conn: sqlite3.Connection) -> dict:
    """Return total, IBKR, ETORO signal counts."""
    try:
        total = conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        ibkr  = conn.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        etoro = conn.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        return {'total': total, 'ibkr': ibkr, 'etoro': etoro}
    except Exception:
        return {'total': 0, 'ibkr': 0, 'etoro': 0}


# ── signal_features table (Milestone 7) ──────────────────────────────────────
# One row per signal event. Linked to signals via signal_id.
# Stores ML-only features separately from the main signals table so:
#   - main signals table stays clean and backward-compatible
#   - ML features are queryable as structured columns
#   - old signal rows without feature data remain intact

FEATURES_SCHEMA = [
    ('id',               'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('signal_id',        'INTEGER NOT NULL DEFAULT 0'),   # FK to signals.id
    ('symbol',           'TEXT NOT NULL DEFAULT ""'),
    ('timestamp',        'TEXT NOT NULL DEFAULT ""'),
    ('direction',        'TEXT NOT NULL DEFAULT ""'),
    ('strategy_version', 'INTEGER DEFAULT 1'),
    # ML-only features (not used in live decisions)
    ('bb_pos',      'REAL'), ('bb_width',   'REAL'), ('obv_slope',  'REAL'),
    ('pchg_20',     'REAL'), ('pchg_1',     'REAL'), ('pchg_3',     'REAL'),
    ('pchg_5',      'REAL'), ('adx',        'REAL'), ('di_plus',    'REAL'),
    ('di_minus',    'REAL'), ('stoch_k',    'REAL'), ('stoch_d',    'REAL'),
    ('roc_10',      'REAL'), ('cci_20',     'REAL'), ('mfi_14',     'REAL'),
    ('atr_pct',     'REAL'), ('ema20_dist', 'REAL'), ('ema50_dist', 'REAL'),
    ('vol_zscore',  'REAL'), ('body_pct',   'REAL'), ('upper_wick', 'REAL'),
    ('lower_wick',  'REAL'),
]


def init_features_table(conn: sqlite3.Connection) -> None:
    """Create signal_features table if it does not exist. Safe to call repeatedly."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_features'"
    ).fetchone()
    if not exists:
        cols_sql = ',\n    '.join(f'{col} {typ}' for col, typ in FEATURES_SCHEMA)
        conn.execute(f'CREATE TABLE signal_features (\n    {cols_sql}\n)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sf_signal_id ON signal_features(signal_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sf_symbol ON signal_features(symbol)')
        conn.commit()
        log.info('[DB] Created signal_features table with %d columns', len(FEATURES_SCHEMA))
    else:
        # Additive migration only
        existing = {r[1] for r in conn.execute('PRAGMA table_info(signal_features)').fetchall()}
        added = []
        for col, col_type in FEATURES_SCHEMA:
            if col == 'id' or col in existing:
                continue
            safe = col_type.replace('NOT NULL', '').strip()
            try:
                conn.execute(f'ALTER TABLE signal_features ADD COLUMN {col} {safe}')
                added.append(col)
            except Exception as e:
                log.warning('[DB] signal_features: could not add %s: %s', col, e)
        if added:
            conn.commit()
            log.info('[DB] signal_features migration: added %s', added)


def insert_signal_features(
    conn: sqlite3.Connection,
    signal_id: int,
    signal: dict,
    ml_features: dict,
) -> None:
    """
    Insert one row into signal_features for a given signal_id.
    ml_features: the .ml dict from FeatureSet.
    Silently skips if no ml_features provided.
    """
    if not ml_features or not signal_id:
        return
    try:
        cols = [col for col, _ in FEATURES_SCHEMA if col not in ('id', 'signal_id',
                'symbol', 'timestamp', 'direction', 'strategy_version')]
        placeholders = ','.join(['?'] * (5 + len(cols)))
        col_names    = 'signal_id, symbol, timestamp, direction, strategy_version, ' + ', '.join(cols)
        values = [
            signal_id,
            signal.get('symbol', ''),
            signal.get('timestamp', ''),
            signal.get('direction', ''),
            signal.get('strategy_version', 1),
        ] + [ml_features.get(c) for c in cols]
        conn.execute(
            f'INSERT INTO signal_features ({col_names}) VALUES ({placeholders})',
            values
        )
        conn.commit()
    except Exception as e:
        log.warning('[DB] insert_signal_features failed for signal %d: %s', signal_id, e)

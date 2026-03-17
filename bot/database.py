"""
bot/database.py
SQLite operations: init, insert, query.
No business logic. No indicators. No Flask.
"""
import sqlite3
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    tf_15m       INTEGER DEFAULT 0,
    tf_1h        INTEGER DEFAULT 0,
    tf_4h        INTEGER DEFAULT 0,
    tf_1d        INTEGER DEFAULT 0,
    valid_count  INTEGER DEFAULT 0,
    route        TEXT,
    rsi          REAL,
    macd_hist    REAL,
    ema20        REAL,
    ema50        REAL,
    bb_pos       REAL,
    bb_width     REAL,
    vwap_dev     REAL,
    obv_slope    REAL,
    atr          REAL,
    vol_ratio    REAL,
    price        REAL,
    pchg         REAL
)
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Create DB and table if not exists. Return open connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(CREATE_TABLE)
    conn.commit()
    log.info(f"[DB] Initialised at {db_path}")
    return conn


def insert_signal(conn: sqlite3.Connection, signal: dict) -> int | None:
    """
    Insert a signal dict into the database.
    Returns new row id, or None if insert failed.
    Refuses to insert if indicator values are missing.
    """
    required_indicator_keys = ('rsi', 'macd_hist', 'ema20', 'ema50', 'price', 'atr')
    for key in required_indicator_keys:
        if signal.get(key) is None:
            log.warning(f"[DB] Refusing insert for {signal.get('symbol')}: missing key '{key}'")
            return None

    try:
        cursor = conn.execute(
            """INSERT INTO signals
               (timestamp, symbol, direction,
                tf_15m, tf_1h, tf_4h, tf_1d, valid_count, route,
                rsi, macd_hist, ema20, ema50,
                bb_pos, bb_width, vwap_dev, obv_slope,
                atr, vol_ratio, price, pchg)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal['timestamp'], signal['symbol'], signal['direction'],
                signal.get('tf_15m', 0), signal.get('tf_1h', 0),
                signal.get('tf_4h', 0),  signal.get('tf_1d', 0),
                signal.get('valid_count', 0), signal.get('route'),
                signal.get('rsi'),       signal.get('macd_hist'),
                signal.get('ema20'),     signal.get('ema50'),
                signal.get('bb_pos'),    signal.get('bb_width'),
                signal.get('vwap_dev'),  signal.get('obv_slope'),
                signal.get('atr'),       signal.get('vol_ratio'),
                signal.get('price'),     signal.get('pchg'),
            )
        )
        conn.commit()
        log.info(f"[DB] Signal inserted: id={cursor.lastrowid} {signal['symbol']} {signal['direction']}")
        return cursor.lastrowid
    except Exception as e:
        log.error(f"[DB] Insert failed for {signal.get('symbol')}: {e}")
        return None


def recent_signals(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Return most recent signals as list of dicts."""
    try:
        cursor = conn.execute(
            'SELECT * FROM signals ORDER BY id DESC LIMIT ?', (limit,)
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception:
        return []


def signal_counts(conn: sqlite3.Connection) -> dict:
    """Return total, IBKR, and ETORO signal counts."""
    try:
        total = conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        ibkr  = conn.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        etoro = conn.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        return {'total': total, 'ibkr': ibkr, 'etoro': etoro}
    except Exception:
        return {'total': 0, 'ibkr': 0, 'etoro': 0}

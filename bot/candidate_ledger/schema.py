"""M21.1 A02 — candidate-decision ledger schema and its explicit migration.

Additive only: three NEW tables and their indexes. No existing table is
altered, dropped or written. ``migrate()`` is never called implicitly (no
import-time or writer-time DDL); deploying it to any non-fixture database is
an operator decision.

Joins to existing data use ids already present there:
  candidate_ledger.signal_id  -> signals.id          (== candidate_snapshots.signal_id)
  candidate_ledger.intent_id  -> execution_intents.id (execution_intents.signal_id == signal_id)
  candidate_ledger.outcome_id -> signal_outcomes.id   (signal_outcomes.signal_id == signal_id)
  candidate_ledger.risk_decision_id -> risk_decisions.decision_id (M14, when one exists)
"""
from __future__ import annotations

import sqlite3

from bot.candidate_ledger.stages import STAGES, TERMINAL_KINDS

SCHEMA_VERSION = "1"
LEDGER_TABLES = ("candidate_ledger", "candidate_ledger_events", "candidate_ledger_meta")


def _in(values) -> str:
    return ",".join("'%s'" % v for v in values)


DDL = (
    """CREATE TABLE IF NOT EXISTS candidate_ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS candidate_ledger (
    candidate_id      TEXT PRIMARY KEY CHECK (candidate_id LIKE 'C-%%'),
    cycle_key         TEXT NOT NULL,
    cycle_id          INTEGER,
    symbol            TEXT NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('long','short')),
    strategy_id       TEXT NOT NULL,
    decision_ts_utc   TEXT NOT NULL,
    scanner_stage     TEXT NOT NULL,
    bar_asof_json     TEXT,
    first_seen_utc    TEXT NOT NULL,
    signal_id         INTEGER,
    intent_id         INTEGER,
    risk_decision_id  TEXT,
    outcome_id        INTEGER,
    terminal_kind     TEXT CHECK (terminal_kind IS NULL OR terminal_kind IN (%s)),
    terminal_at_utc   TEXT,
    created_by        TEXT NOT NULL,
    CHECK ((terminal_kind IS NULL) = (terminal_at_utc IS NULL))
)""" % _in(TERMINAL_KINDS),
    """CREATE TABLE IF NOT EXISTS candidate_ledger_events (
    event_id      TEXT PRIMARY KEY CHECK (event_id LIKE 'E-%%'),
    candidate_id  TEXT NOT NULL REFERENCES candidate_ledger(candidate_id),
    stage         TEXT NOT NULL CHECK (stage IN (%s)),
    attempt       INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    at_utc        TEXT NOT NULL,
    reason_codes  TEXT NOT NULL DEFAULT '[]',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    writer        TEXT NOT NULL,
    UNIQUE (candidate_id, stage, attempt)
)""" % _in(STAGES),
    "CREATE INDEX IF NOT EXISTS ix_cl_cycle ON candidate_ledger(cycle_key)",
    "CREATE INDEX IF NOT EXISTS ix_cl_signal ON candidate_ledger(signal_id)",
    "CREATE INDEX IF NOT EXISTS ix_cl_intent ON candidate_ledger(intent_id)",
    "CREATE INDEX IF NOT EXISTS ix_cl_terminal ON candidate_ledger(terminal_kind)",
    "CREATE INDEX IF NOT EXISTS ix_cle_candidate ON candidate_ledger_events(candidate_id)",
)


class LedgerSchemaError(RuntimeError):
    """The database holds an unexpected ledger schema version."""


def schema_present(conn: sqlite3.Connection) -> bool:
    """True only if all ledger tables exist at the expected version."""
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(LEDGER_TABLES) <= names:
            return False
        row = conn.execute("SELECT value FROM candidate_ledger_meta "
                           "WHERE key='schema_version'").fetchone()
        return bool(row) and row[0] == SCHEMA_VERSION
    except sqlite3.Error:
        return False


def migrate(conn: sqlite3.Connection, *, now_utc: str) -> str:
    """Create the ledger tables if absent (idempotent, all-or-nothing).
    Refuses a database whose ledger records a different schema version."""
    conn.execute("SAVEPOINT a02_ledger_migrate")
    try:
        for stmt in DDL:
            conn.execute(stmt)
        row = conn.execute("SELECT value FROM candidate_ledger_meta "
                           "WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO candidate_ledger_meta (key, value) VALUES "
                         "('schema_version', ?), ('created_at_utc', ?)",
                         (SCHEMA_VERSION, now_utc))
        elif row[0] != SCHEMA_VERSION:
            raise LedgerSchemaError("ledger schema version %r != %r"
                                    % (row[0], SCHEMA_VERSION))
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT a02_ledger_migrate")
        conn.execute("RELEASE SAVEPOINT a02_ledger_migrate")
        raise
    conn.execute("RELEASE SAVEPOINT a02_ledger_migrate")
    return SCHEMA_VERSION

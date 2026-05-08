"""
M15 schema hardening — execution_intents migration tests.
Run: python3 test_m15_schema.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.flywheel import ensure_execution_intents_migrations


REQUIRED = [
    ("submitted_at",   "TEXT"),
    ("filled_at",      "TEXT"),
    ("fill_price",     "REAL"),
    ("fill_qty",       "REAL"),
    ("cancelled_at",   "TEXT"),
    ("lifecycle_json", "TEXT"),
]


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class TestExecutionIntentsMigration(unittest.TestCase):

    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.db = f.name
        # Simulate live DB: pre-M12 schema, no lifecycle columns, with data.
        c = sqlite3.connect(self.db)
        c.execute(
            """CREATE TABLE execution_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                status TEXT DEFAULT 'pending',
                rejection_reason TEXT,
                broker_order_id TEXT,
                risk_checks TEXT DEFAULT '{}'
            )"""
        )
        c.execute(
            "INSERT INTO execution_intents(symbol, direction, status) "
            "VALUES('TEST', 'LONG', 'pending')"
        )
        c.commit()
        c.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _conn(self):
        return sqlite3.connect(self.db)

    def test_pre_state_columns_missing(self):
        with self._conn() as c:
            cols = _columns(c, "execution_intents")
        for name, _ in REQUIRED:
            self.assertNotIn(name, cols, f"setup invalid: {name} should be missing")

    def test_migration_adds_all_columns(self):
        with self._conn() as c:
            added = ensure_execution_intents_migrations(c)
        self.assertEqual(set(added), {n for n, _ in REQUIRED})
        with self._conn() as c:
            cols = _columns(c, "execution_intents")
        for name, _ in REQUIRED:
            self.assertIn(name, cols)

    def test_migration_idempotent(self):
        with self._conn() as c:
            ensure_execution_intents_migrations(c)
        with self._conn() as c:
            self.assertEqual(ensure_execution_intents_migrations(c), [])

    def test_migration_preserves_existing_rows(self):
        with self._conn() as c:
            before = c.execute(
                "SELECT id, symbol, direction, status FROM execution_intents"
            ).fetchall()
            ensure_execution_intents_migrations(c)
            after = c.execute(
                "SELECT id, symbol, direction, status FROM execution_intents"
            ).fetchall()
        self.assertEqual(before, after)

    def test_table_absent_returns_empty(self):
        with self._conn() as c:
            c.execute("DROP TABLE execution_intents")
            c.commit()
        with self._conn() as c:
            self.assertEqual(ensure_execution_intents_migrations(c), [])

    def test_partial_migration_completes_only_missing(self):
        with self._conn() as c:
            c.execute("ALTER TABLE execution_intents ADD COLUMN submitted_at TEXT")
            c.execute("ALTER TABLE execution_intents ADD COLUMN filled_at TEXT")
            c.commit()
        with self._conn() as c:
            added = ensure_execution_intents_migrations(c)
        self.assertEqual(
            set(added),
            {"fill_price", "fill_qty", "cancelled_at", "lifecycle_json"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

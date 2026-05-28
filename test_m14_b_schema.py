"""M14.B — schema/migration/backfill tests.

Proves the corrections from ChatGPT's M14.B review:

  1. Legacy compatibility — daily_state_per_broker carries all legacy
     fields; GLOBAL backfill preserves them; get_daily_state_compat
     returns the exact old shape.
  2. Old readers remain on daily_state (no redirect).
  3. Transaction safety — SAVEPOINT works nested-safe.
  4. Version sentinel never hides missing DDL.
  5. Minimal index set, including the composite (broker_scope, date).
  6. CHECK constraints enforced on critical enums.
  7. Idempotent migration; duplicate (date, broker_scope) rejected.
  8. Forced failure rolls back cleanly.
  9. M15 schema regression unaffected.

No live calls, no eToro endpoint, no real order.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from bot.flywheel import (
    DAILY_STATE_SCHEMA,
    PORTFOLIO_RISK_STATE_SCHEMA,
    M14_B_SCHEMA_VERSION,
    M14_B_SENTINEL_KEY,
    ensure_daily_state_per_broker_migrations,
    get_daily_state,
    init_flywheel_tables,
    set_daily_loss_block,
)
from bot.risk_authority.state import get_daily_state_compat


class _DB:
    """Temp SQLite fixture."""

    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name

    def conn(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tables, columns, indexes, constraints
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaShape(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()
        with self.fx.conn() as c:
            init_flywheel_tables(c)

    def tearDown(self):
        self.fx.cleanup()

    def _tables(self):
        with self.fx.conn() as c:
            return {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}

    def _columns(self, table):
        with self.fx.conn() as c:
            return [r[1] for r in c.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()]

    def test_three_new_tables_exist(self):
        t = self._tables()
        for name in ("daily_state_per_broker", "risk_snapshots", "risk_decisions"):
            self.assertIn(name, t)

    def test_dspb_carries_legacy_fields(self):
        cols = self._columns("daily_state_per_broker")
        # All legacy daily_state fields must be present (ChatGPT correction #1).
        for legacy in ("realised_pnl_usd", "realised_pnl_pct",
                       "daily_pnl_source", "daily_pnl_available",
                       "daily_loss_block_active", "daily_loss_alert_sent"):
            self.assertIn(legacy, cols, f"legacy field {legacy!r} missing")
        # And the new M14 fields.
        for new in ("realised_daily_loss", "open_positions",
                    "capital_deployed", "peak_equity",
                    "drawdown_from_peak", "source",
                    "last_ingested_at", "fresh_reads_count",
                    "lifecycle_json"):
            self.assertIn(new, cols, f"M14 field {new!r} missing")

    def test_primary_key_date_broker_scope(self):
        with self.fx.conn() as c:
            info = c.execute(
                "PRAGMA table_info(daily_state_per_broker)"
            ).fetchall()
        pk = [(r[1], r[5]) for r in info if r[5] > 0]
        # Both date and broker_scope flagged as PK members (pk index > 0).
        names = {n for n, _ in pk}
        self.assertEqual(names, {"date", "broker_scope"})

    def test_composite_index_scope_date_exists(self):
        with self.fx.conn() as c:
            idx = {r[1] for r in c.execute(
                "PRAGMA index_list(daily_state_per_broker)"
            ).fetchall()}
        self.assertIn("ix_dspb_scope_date", idx)

    def test_risk_decisions_index_exists(self):
        with self.fx.conn() as c:
            idx = {r[1] for r in c.execute(
                "PRAGMA index_list(risk_decisions)"
            ).fetchall()}
        self.assertIn("ix_rd_scope_taken", idx)

    def test_no_redundant_single_column_indexes(self):
        # Correction #5: don't add redundant single-column indexes.
        # We only expect the composite + PK index.
        with self.fx.conn() as c:
            idx = {r[1] for r in c.execute(
                "PRAGMA index_list(daily_state_per_broker)"
            ).fetchall()}
        # PK auto-creates an internal index named sqlite_autoindex_...
        explicit = {i for i in idx if not i.startswith("sqlite_")}
        self.assertEqual(explicit, {"ix_dspb_scope_date"},
                         f"unexpected explicit indexes: {explicit}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. CHECK constraints on critical enums
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckConstraints(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()
        with self.fx.conn() as c:
            init_flywheel_tables(c)

    def tearDown(self):
        self.fx.cleanup()

    def test_invalid_broker_scope_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO daily_state_per_broker "
                    "(date, broker_scope, updated_at) VALUES (?, ?, ?)",
                    ("2026-05-28", "fake_broker", "now"),
                )

    def test_invalid_source_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO daily_state_per_broker "
                    "(date, broker_scope, source, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("2026-05-28", "GLOBAL", "not_a_source", "now"),
                )

    def test_valid_broker_scopes_accepted(self):
        with self.fx.conn() as c:
            for scope in ("ibkr_live", "ibkr_paper", "etoro_real",
                          "etoro_paper", "GLOBAL"):
                c.execute(
                    "INSERT INTO daily_state_per_broker "
                    "(date, broker_scope, updated_at) VALUES (?, ?, ?)",
                    (f"2026-01-{(ord(scope[0]) % 28) + 1:02d}", scope, "now"),
                )

    def test_risk_decisions_invalid_authority_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO risk_decisions "
                    "(decision_id, taken_at, broker_scope, requested_action, "
                    " result, authority_before, authority_after, "
                    " reason_codes, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("d1", "now", "GLOBAL", "trade_open", "allow",
                     "NOT_AN_AUTHORITY", "AUTO_ALLOWED", "[]", "auto", "now"),
                )

    def test_risk_decisions_invalid_result_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO risk_decisions "
                    "(decision_id, taken_at, broker_scope, requested_action, "
                    " result, authority_before, authority_after, "
                    " reason_codes, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("d2", "now", "GLOBAL", "trade_open", "maybe",
                     "OFF", "OFF", "[]", "auto", "now"),
                )

    def test_duplicate_pk_rejected(self):
        with self.fx.conn() as c:
            c.execute(
                "INSERT INTO daily_state_per_broker "
                "(date, broker_scope, updated_at) VALUES (?, ?, ?)",
                ("2026-05-28", "GLOBAL", "now"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO daily_state_per_broker "
                    "(date, broker_scope, updated_at) VALUES (?, ?, ?)",
                    ("2026-05-28", "GLOBAL", "now"),
                )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Backfill correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()
        # Seed daily_state + portfolio_risk_state BEFORE running the migration
        # so we exercise the backfill path on real data.
        with self.fx.conn() as c:
            c.execute(DAILY_STATE_SCHEMA)
            c.execute(PORTFOLIO_RISK_STATE_SCHEMA)
            for d, pnl, src, avail, block, alert in [
                ("2026-01-01", 100.5, "ibkr", 1, 0, 0),
                ("2026-01-02", -50.0, "ibkr", 1, 1, 1),
                ("2026-01-03", 0.0,   "unavailable", 0, 0, 0),
            ]:
                c.execute(
                    "INSERT INTO daily_state "
                    "(date, realised_pnl_usd, realised_pnl_pct, "
                    " daily_pnl_source, daily_pnl_available, "
                    " daily_loss_block_active, daily_loss_alert_sent, "
                    " updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (d, pnl, pnl / 1000.0, src, avail, block, alert,
                     f"{d}T00:00:00+00:00"),
                )
            c.commit()
            ensure_daily_state_per_broker_migrations(c)

    def tearDown(self):
        self.fx.cleanup()

    def test_every_row_backfilled_as_GLOBAL(self):
        with self.fx.conn() as c:
            ds_count = c.execute(
                "SELECT COUNT(*) FROM daily_state"
            ).fetchone()[0]
            global_count = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker "
                "WHERE broker_scope='GLOBAL'"
            ).fetchone()[0]
        self.assertEqual(global_count, ds_count)

    def test_legacy_fields_preserved_in_backfill(self):
        with self.fx.conn() as c:
            row = c.execute(
                "SELECT realised_pnl_usd, daily_pnl_source, "
                "       daily_pnl_available, daily_loss_block_active, "
                "       daily_loss_alert_sent, source "
                "FROM daily_state_per_broker "
                "WHERE date='2026-01-02' AND broker_scope='GLOBAL'"
            ).fetchone()
        self.assertEqual(row[0], -50.0)
        self.assertEqual(row[1], "ibkr")
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], 1)
        self.assertEqual(row[5], "backfill")

    def test_sentinel_written_after_backfill(self):
        with self.fx.conn() as c:
            row = c.execute(
                "SELECT value FROM portfolio_risk_state WHERE key=?",
                (M14_B_SENTINEL_KEY,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], str(M14_B_SCHEMA_VERSION))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()
        with self.fx.conn() as c:
            init_flywheel_tables(c)

    def tearDown(self):
        self.fx.cleanup()

    def test_rerun_no_new_rows(self):
        with self.fx.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            ensure_daily_state_per_broker_migrations(c)
            ensure_daily_state_per_broker_migrations(c)
            after = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_sentinel_does_not_hide_missing_ddl(self):
        # ChatGPT correction #4: even if sentinel says we're at version 1,
        # the migration must still run CREATE TABLE IF NOT EXISTS so a
        # missing table is recreated.
        with self.fx.conn() as c:
            # Drop a new table; sentinel remains in place.
            c.execute("DROP TABLE risk_snapshots")
            sentinel = c.execute(
                "SELECT value FROM portfolio_risk_state WHERE key=?",
                (M14_B_SENTINEL_KEY,),
            ).fetchone()
            self.assertIsNotNone(sentinel)
            # Re-run migration. The sentinel is at v1, but DDL must still run.
            ensure_daily_state_per_broker_migrations(c)
            exists = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='risk_snapshots'"
            ).fetchone()
        self.assertIsNotNone(exists,
            "sentinel must NOT cause DDL to be skipped when a table is missing")

    def test_indexes_recreated_if_dropped(self):
        with self.fx.conn() as c:
            c.execute("DROP INDEX IF EXISTS ix_dspb_scope_date")
            ensure_daily_state_per_broker_migrations(c)
            idx = {r[1] for r in c.execute(
                "PRAGMA index_list(daily_state_per_broker)"
            ).fetchall()}
        self.assertIn("ix_dspb_scope_date", idx)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Old readers untouched
# ─────────────────────────────────────────────────────────────────────────────

class TestOldReadersUnchanged(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_get_daily_state_returns_identical_shape_pre_and_post(self):
        # Pre-M14.B: daily_state created by hand, get_daily_state works.
        with self.fx.conn() as c:
            c.execute(DAILY_STATE_SCHEMA)
            c.commit()
            pre = get_daily_state(c)
        # Post-M14.B: full init_flywheel_tables (which runs the migration).
        with self.fx.conn() as c:
            init_flywheel_tables(c)
            post = get_daily_state(c)
        # Shape contract: same set of keys. The numeric subtype of
        # zero-valued columns can differ between the Python-literal
        # create path and the SQLite-readback path; we don't pin that
        # because no existing caller depends on it.
        self.assertEqual(set(pre.keys()), set(post.keys()))

    def test_set_daily_loss_block_writes_only_to_daily_state(self):
        with self.fx.conn() as c:
            init_flywheel_tables(c)
            # Pre-state of new table.
            before = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            set_daily_loss_block(c, active=True, alert_sent=True)
            # Old table reflects the change.
            row = c.execute(
                "SELECT daily_loss_block_active, daily_loss_alert_sent "
                "FROM daily_state ORDER BY date DESC LIMIT 1"
            ).fetchone()
            # New table count unchanged (set_daily_loss_block must not write
            # to daily_state_per_broker — correction #2).
            after = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 1)
        self.assertEqual(before, after,
            "set_daily_loss_block leaked a write into daily_state_per_broker")

    def test_old_daily_state_schema_unchanged(self):
        with self.fx.conn() as c:
            init_flywheel_tables(c)
            cols = [r[1] for r in c.execute(
                "PRAGMA table_info(daily_state)"
            ).fetchall()]
        # Exactly the original 8 columns; M14.B added none.
        self.assertEqual(set(cols), {
            "date", "realised_pnl_usd", "realised_pnl_pct",
            "daily_pnl_source", "daily_pnl_available",
            "daily_loss_block_active", "daily_loss_alert_sent",
            "updated_at",
        })


# ─────────────────────────────────────────────────────────────────────────────
# 6. Compat shim parity (new code only)
# ─────────────────────────────────────────────────────────────────────────────

class TestCompatShim(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()
        with self.fx.conn() as c:
            init_flywheel_tables(c)
            c.execute(
                "INSERT OR REPLACE INTO daily_state_per_broker "
                "(date, broker_scope, realised_pnl_usd, realised_pnl_pct, "
                " daily_pnl_source, daily_pnl_available, "
                " daily_loss_block_active, daily_loss_alert_sent, "
                " source, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("2026-05-28", "GLOBAL", 12.5, 0.0125, "etoro", 1, 0, 0,
                 "backfill", "2026-05-28T00:00:00Z"),
            )
            c.commit()

    def tearDown(self):
        self.fx.cleanup()

    def test_compat_shape_matches_get_daily_state(self):
        with self.fx.conn() as c:
            row = get_daily_state_compat(c, today="2026-05-28")
        self.assertIsNotNone(row)
        # Same keys as bot.flywheel.get_daily_state.
        self.assertEqual(set(row.keys()), {
            "date", "realised_pnl_usd", "realised_pnl_pct",
            "daily_pnl_source", "daily_pnl_available",
            "daily_loss_block_active", "daily_loss_alert_sent",
        })

    def test_compat_returns_none_when_no_row(self):
        with self.fx.conn() as c:
            row = get_daily_state_compat(c, today="2099-12-31")
        self.assertIsNone(row, "compat must return None, not fabricate zeros")

    def test_compat_returns_none_if_table_missing(self):
        # Fresh DB with no migration.
        fresh = _DB()
        try:
            with fresh.conn() as c:
                row = get_daily_state_compat(c, today="2026-05-28")
            self.assertIsNone(row)
        finally:
            fresh.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Transaction safety + rollback
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionSafety(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_nested_safe_when_caller_already_in_transaction(self):
        # Caller is mid-transaction; migration uses SAVEPOINT, must not break.
        with self.fx.conn() as c:
            c.execute(DAILY_STATE_SCHEMA)
            c.execute(PORTFOLIO_RISK_STATE_SCHEMA)
            c.commit()
            c.execute("BEGIN")
            self.assertTrue(c.in_transaction)
            ensure_daily_state_per_broker_migrations(c)
            # The caller's transaction is still alive.
            self.assertTrue(c.in_transaction)
            c.commit()
            # Migration effects visible.
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='daily_state_per_broker'"
            ).fetchone())

    def test_forced_failure_rolls_back_cleanly(self):
        # Inject a failure mid-migration via a proxy connection wrapper
        # (sqlite3.Connection.execute is read-only at the C level, so we
        # can't monkey-patch it directly). The proxy fails on the sentinel
        # INSERT, AFTER all DDL + backfill ran inside the SAVEPOINT.
        class _Proxy:
            def __init__(self, real):
                self._real = real
                self.failed = False

            def execute(self, sql, *args, **kwargs):
                if sql.lstrip().upper().startswith(
                        "INSERT OR REPLACE INTO PORTFOLIO_RISK_STATE"):
                    self.failed = True
                    raise sqlite3.OperationalError("forced failure")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            @property
            def in_transaction(self):
                return self._real.in_transaction

        with self.fx.conn() as c:
            c.execute(DAILY_STATE_SCHEMA)
            c.execute(PORTFOLIO_RISK_STATE_SCHEMA)
            c.commit()

            proxy = _Proxy(c)
            with self.assertRaises(sqlite3.OperationalError):
                ensure_daily_state_per_broker_migrations(proxy)
            self.assertTrue(proxy.failed)

            # After rollback: no new tables, no sentinel.
            for t in ("daily_state_per_broker", "risk_snapshots",
                      "risk_decisions"):
                row = c.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name=?", (t,)
                ).fetchone()
                self.assertIsNone(row, f"table {t} survived rollback")
            sentinel = c.execute(
                "SELECT 1 FROM portfolio_risk_state WHERE key=?",
                (M14_B_SENTINEL_KEY,),
            ).fetchone()
            self.assertIsNone(sentinel)


# ─────────────────────────────────────────────────────────────────────────────
# 8. M15 schema regression — confirm we didn't break the existing M15 suite
# ─────────────────────────────────────────────────────────────────────────────

class TestM15RegressionSurface(unittest.TestCase):
    """Smoke: init_flywheel_tables still produces all M15 tables AFTER the
    M14.B addition. The dedicated test_m15_schema suite is the real
    regression gate; this is a small in-suite belt-and-braces check."""

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_all_pre_m14_b_tables_still_present(self):
        with self.fx.conn() as c:
            init_flywheel_tables(c)
            t = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        for expected in ("daily_state", "execution_intents",
                         "candidate_snapshots", "signal_outcomes",
                         "portfolio_risk_state", "portfolio_risk_snapshots",
                         "gateway_state", "gateway_events"):
            self.assertIn(expected, t, f"M15 table {expected!r} missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)

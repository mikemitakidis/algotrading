"""M13.5.B — Reconciliation CLI tests.

The reconciler updates lifecycle via bot.etoro.lifecycle ONLY — never
raw SQL, never an eToro write. These tests drive the CLI's command
functions directly with a temp DB.

NOTE: this test must NOT import bot.etoro.live_broker before importing
tools.etoro_reconcile (the reconcile module has an import-time guard).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import reconcile FIRST (before any live_broker import) to satisfy the guard.
import tools.etoro_reconcile as recon
from bot.flywheel import init_flywheel_tables, log_intent
from bot.etoro.lifecycle import apply_transition, get_lifecycle


def _ns(**kw):
    return argparse.Namespace(**kw)


class _DB:
    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name
        with sqlite3.connect(self.path) as c:
            init_flywheel_tables(c)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def make_unverified(self) -> int:
        with sqlite3.connect(self.path) as c:
            iid = log_intent(
                c, signal_id=0, symbol="SPY", direction="long",
                route="ETORO", entry_price=0.0, stop_loss=0.0,
                target_price=0.0, position_size=0.0, risk_usd=0.0,
                valid_count=0, strategy_version=0,
                broker="etoro_real", status="pending_live_write",
                broker_order_id=None, rejection_reason=None,
                risk_checks={},
            )
            apply_transition(c, iid, "submitted", broker_order_id="500")
            apply_transition(c, iid, "unverified",
                             event="poll_exhausted")
        return iid


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.db = _DB()

    def tearDown(self):
        self.db.cleanup()

    def test_no_live_broker_import(self):
        # The reconcile module must not pull in the live broker.
        self.assertNotIn("bot.etoro.live_broker", sys.modules,
                         "reconcile path must not import live_broker")

    def test_mark_filled(self):
        iid = self.db.make_unverified()
        evidence = {"position_id": 999, "fill_price": 201.5,
                    "fill_qty": 0.05, "order_id": "500"}
        rc = recon.cmd_mark_filled(_ns(
            db=self.db.path, intent_id=iid,
            evidence=json.dumps(evidence), note="manual check",
            allow_terminal_override=False,
        ))
        self.assertEqual(rc, 0)
        with sqlite3.connect(self.db.path) as c:
            row = c.execute("SELECT status, fill_price, fill_qty, filled_at "
                            "FROM execution_intents WHERE id=?",
                            (iid,)).fetchone()
        self.assertEqual(row[0], "filled")
        self.assertEqual(row[1], 201.5)
        self.assertEqual(row[2], 0.05)
        self.assertIsNotNone(row[3])

    def test_mark_filled_missing_fields_rejected(self):
        iid = self.db.make_unverified()
        rc = recon.cmd_mark_filled(_ns(
            db=self.db.path, intent_id=iid,
            evidence=json.dumps({"position_id": 1}),  # missing price/qty
            note=None, allow_terminal_override=False,
        ))
        self.assertEqual(rc, 2)

    def test_mark_filled_evidence_from_file(self):
        iid = self.db.make_unverified()
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump({"position_id": 7, "fill_price": 10.0,
                       "fill_qty": 1.0}, f)
            fpath = f.name
        try:
            rc = recon.cmd_mark_filled(_ns(
                db=self.db.path, intent_id=iid, evidence=fpath,
                note=None, allow_terminal_override=False,
            ))
            self.assertEqual(rc, 0)
        finally:
            os.unlink(fpath)

    def test_mark_rejected(self):
        iid = self.db.make_unverified()
        rc = recon.cmd_mark_rejected(_ns(
            db=self.db.path, intent_id=iid, evidence=None,
            note="broker said no", allow_terminal_override=False,
        ))
        self.assertEqual(rc, 0)
        with sqlite3.connect(self.db.path) as c:
            row = c.execute("SELECT status FROM execution_intents WHERE id=?",
                            (iid,)).fetchone()
        self.assertEqual(row[0], "broker_rejected")

    def test_mark_closed_manual(self):
        iid = self.db.make_unverified()
        rc = recon.cmd_mark_closed_manual(_ns(
            db=self.db.path, intent_id=iid, evidence=None,
            note="closed in web UI", allow_terminal_override=False,
        ))
        self.assertEqual(rc, 0)
        with sqlite3.connect(self.db.path) as c:
            row = c.execute("SELECT status, cancelled_at "
                            "FROM execution_intents WHERE id=?",
                            (iid,)).fetchone()
        self.assertEqual(row[0], "closed_manual")
        self.assertIsNotNone(row[1])

    def test_mark_closed_manual_after_filled_no_override(self):
        # M13.5.B blocker-2 fix: closing a filled position via the web UI
        # and recording it (runbook step 5) must work WITHOUT the override
        # flag — exactly as the runbook command shows.
        iid = self.db.make_unverified()
        recon.cmd_mark_filled(_ns(
            db=self.db.path, intent_id=iid,
            evidence=json.dumps({"position_id": 1, "fill_price": 1.0,
                                 "fill_qty": 1.0}),
            note=None, allow_terminal_override=False,
        ))
        rc = recon.cmd_mark_closed_manual(_ns(
            db=self.db.path, intent_id=iid, evidence=None,
            note="closed in web UI", allow_terminal_override=False,
        ))
        self.assertEqual(rc, 0)
        with sqlite3.connect(self.db.path) as c:
            row = c.execute("SELECT status FROM execution_intents WHERE id=?",
                            (iid,)).fetchone()
        self.assertEqual(row[0], "closed_manual")

    def test_terminal_override_still_required_for_other_terminals(self):
        # Protection preserved: broker_rejected -> closed_manual is NOT a
        # whitelisted transition, so it still needs the override flag.
        iid = self.db.make_unverified()
        recon.cmd_mark_rejected(_ns(
            db=self.db.path, intent_id=iid, evidence=None,
            note=None, allow_terminal_override=False,
        ))
        from bot.etoro.lifecycle import LifecycleError
        with self.assertRaises(LifecycleError):
            recon.cmd_mark_closed_manual(_ns(
                db=self.db.path, intent_id=iid, evidence=None,
                note=None, allow_terminal_override=False,
            ))
        # WITH override -> succeeds.
        rc = recon.cmd_mark_closed_manual(_ns(
            db=self.db.path, intent_id=iid, evidence=None,
            note=None, allow_terminal_override=True,
        ))
        self.assertEqual(rc, 0)

    def test_show(self):
        iid = self.db.make_unverified()
        rc = recon.cmd_show(_ns(db=self.db.path, intent_id=iid))
        self.assertEqual(rc, 0)

    def test_show_missing(self):
        rc = recon.cmd_show(_ns(db=self.db.path, intent_id=999999))
        self.assertEqual(rc, 2)

    def test_lifecycle_records_reconciled_by(self):
        iid = self.db.make_unverified()
        recon.cmd_mark_filled(_ns(
            db=self.db.path, intent_id=iid,
            evidence=json.dumps({"position_id": 5, "fill_price": 2.0,
                                 "fill_qty": 3.0}),
            note=None, allow_terminal_override=False,
        ))
        with sqlite3.connect(self.db.path) as c:
            lc = get_lifecycle(c, iid)
        self.assertEqual(lc.get("reconciled_by"), "tools/etoro_reconcile.py")
        self.assertEqual(lc.get("position_id"), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

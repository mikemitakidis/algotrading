"""M13.5.B — Lifecycle writer tests (eToro intent rows).

These tests verify:
  * submitted_at IS set when transitioning to 'submitted' (ChatGPT
    audit finding — flywheel.update_intent_status only set it for
    'accepted' / 'paper_logged').
  * filled_at / cancelled_at set correctly.
  * lifecycle_json.events appended with prev_status link.
  * terminal status refuses further transition (unless override).
  * client_intent_id idempotency lookup works.
  * Unknown status rejected.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.etoro.lifecycle import (
    apply_transition,
    attach_evidence,
    find_by_client_intent_id,
    get_lifecycle,
    LifecycleError,
)
from bot.flywheel import init_flywheel_tables as ensure_schema, log_intent


class _DB:
    """Minimal DB fixture creating execution_intents via ensure_schema."""
    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name
        with sqlite3.connect(self.path) as c:
            ensure_schema(c)

    def conn(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def make_intent(self, *, status="pending_live_write",
                    broker="etoro_real", symbol="SPY") -> int:
        with self.conn() as c:
            iid = log_intent(
                c, signal_id=0, symbol=symbol, direction="long",
                route="ETORO", entry_price=0.0, stop_loss=0.0,
                target_price=0.0, position_size=0.0, risk_usd=0.0,
                valid_count=0, strategy_version=0,
                broker=broker, status=status,
                broker_order_id=None, rejection_reason=None,
                risk_checks={"source": "test"},
            )
        assert iid is not None
        return iid


class _DBFixtureMixin:
    @classmethod
    def setUpClass(cls):
        cls.fx = _DB()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()


class TestApplyTransition(_DBFixtureMixin, unittest.TestCase):
    def test_unknown_status_rejected(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            with self.assertRaises(LifecycleError):
                apply_transition(c, iid, "not_a_real_status")

    def test_submitted_sets_submitted_at_chatgpt_audit_fix(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            row = apply_transition(c, iid, "submitted",
                                   broker_order_id="100001")
        self.assertEqual(row["status"], "submitted")
        self.assertIsNotNone(row["submitted_at"],
                             msg="submitted_at must be set on 'submitted' "
                                 "transition (ChatGPT audit finding)")
        self.assertEqual(row["broker_order_id"], "100001")

    def test_filled_sets_filled_at_and_fields(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted", broker_order_id="200")
            row = apply_transition(c, iid, "filled",
                                   fill_price=99.5, fill_qty=0.1,
                                   broker_order_id="200")
        self.assertEqual(row["status"], "filled")
        self.assertIsNotNone(row["filled_at"])
        self.assertEqual(row["fill_price"], 99.5)
        self.assertEqual(row["fill_qty"], 0.1)

    def test_cancelled_sets_cancelled_at(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            row = apply_transition(c, iid, "cancelled",
                                   event="operator_aborted")
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["cancelled_at"])

    def test_closed_manual_sets_cancelled_at(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted", broker_order_id="x")
            apply_transition(c, iid, "filled",
                             fill_price=10.0, fill_qty=1.0)
            row = apply_transition(c, iid, "closed_manual",
                                   allow_terminal_override=True)
        self.assertEqual(row["status"], "closed_manual")
        self.assertIsNotNone(row["cancelled_at"])

    def test_filled_to_closed_manual_without_override(self):
        # M13.5.B blocker-2 fix: the operator manual-close path
        # (filled -> closed_manual) is an explicitly-permitted terminal
        # transition and must NOT require allow_terminal_override=True.
        # This matches the runbook step 5 command (no override flag).
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted", broker_order_id="x")
            apply_transition(c, iid, "filled", fill_price=10.0, fill_qty=1.0)
            row = apply_transition(c, iid, "closed_manual")  # no override
        self.assertEqual(row["status"], "closed_manual")
        self.assertIsNotNone(row["cancelled_at"])

    def test_filled_to_cancelled_still_refused_without_override(self):
        # Protection preserved: other terminal-out transitions still
        # require the explicit override flag.
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted")
            apply_transition(c, iid, "filled", fill_price=1.0, fill_qty=1.0)
            with self.assertRaises(LifecycleError):
                apply_transition(c, iid, "cancelled")   # not in allowlist

    def test_broker_rejected_to_closed_manual_still_refused_without_override(self):
        # Only filled -> closed_manual is whitelisted; broker_rejected ->
        # closed_manual still requires the override.
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted")
            apply_transition(c, iid, "broker_rejected")
            with self.assertRaises(LifecycleError):
                apply_transition(c, iid, "closed_manual")  # not whitelisted

    def test_terminal_refuses_further_transition(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted")
            apply_transition(c, iid, "filled", fill_price=1.0, fill_qty=1.0)
            with self.assertRaises(LifecycleError):
                apply_transition(c, iid, "cancelled")

    def test_terminal_override_permitted(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted")
            apply_transition(c, iid, "broker_rejected")
            # Override allowed only with explicit flag.
            row = apply_transition(c, iid, "closed_manual",
                                   allow_terminal_override=True)
        self.assertEqual(row["status"], "closed_manual")

    def test_lifecycle_events_appended_with_prev_status(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "awaiting_confirm")
            apply_transition(c, iid, "submitted")
            lc = get_lifecycle(c, iid)
        events = lc.get("events", [])
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[-1]["status"], "submitted")
        self.assertEqual(events[-1]["prev_status"], "awaiting_confirm")
        self.assertEqual(lc["last_status"], "submitted")

    def test_extra_lifecycle_merged(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted",
                             extra_lifecycle={"x_request_id": "abc"})
            lc = get_lifecycle(c, iid)
        self.assertEqual(lc.get("x_request_id"), "abc")

    def test_extra_lifecycle_cannot_clobber_events(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            apply_transition(c, iid, "submitted",
                             extra_lifecycle={"events": "INJECTED"})
            lc = get_lifecycle(c, iid)
        self.assertIsInstance(lc["events"], list)
        self.assertNotEqual(lc["events"], "INJECTED")

    def test_invalid_intent_id_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(LifecycleError):
                apply_transition(c, 0, "submitted")
            with self.assertRaises(LifecycleError):
                apply_transition(c, -1, "submitted")
            with self.assertRaises(LifecycleError):
                apply_transition(c, 9999999, "submitted")


class TestAttachEvidence(_DBFixtureMixin, unittest.TestCase):
    def test_attach_simple_value(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            attach_evidence(c, iid, key="client_intent_id",
                            value="0000-1111-2222-3333")
            lc = get_lifecycle(c, iid)
        self.assertEqual(lc["client_intent_id"], "0000-1111-2222-3333")

    def test_attach_nested_dict(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            attach_evidence(c, iid, key="payload",
                            value={"InstrumentID": 1000, "Amount": 10.0})
            lc = get_lifecycle(c, iid)
        self.assertEqual(lc["payload"]["InstrumentID"], 1000)

    def test_empty_key_rejected(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            with self.assertRaises(LifecycleError):
                attach_evidence(c, iid, key="", value=1)


class TestClientIntentIdLookup(_DBFixtureMixin, unittest.TestCase):
    def test_find_by_client_intent_id(self):
        iid = self.fx.make_intent()
        with self.fx.conn() as c:
            attach_evidence(c, iid, key="client_intent_id",
                            value="ci-xyz-001")
            found = find_by_client_intent_id(c, "ci-xyz-001")
        self.assertEqual(found, iid)

    def test_find_missing_returns_none(self):
        with self.fx.conn() as c:
            found = find_by_client_intent_id(c, "no-such-id")
        self.assertIsNone(found)

    def test_empty_id_returns_none(self):
        with self.fx.conn() as c:
            self.assertIsNone(find_by_client_intent_id(c, ""))
            self.assertIsNone(find_by_client_intent_id(c, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)

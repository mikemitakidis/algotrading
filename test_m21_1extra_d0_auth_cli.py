#!/usr/bin/env python3
"""M21.1extra-D0 Commit 2 — CLI + audit tests.

Proves the operator authorization CLI:
  - enable requires confirmation; bounded duration cap enforced
  - enable authorizes ibkr_paper + global with a real UTC +00:00 expiry
  - enable NEVER enables ibkr_live and NEVER touches eToro/live flags
  - enable NEVER copies legacy ibkr.auto_trading_enabled into a lane
  - disable immediately clears global + ibkr_paper authorization
  - the real reader (determine_signal_only_reason) allows only after enable and
    blocks after disable (writer/enforcer separation)
  - audit rows are append-only, evidence only, and record every action
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import tools.paper_loop.m21_1extra_d0_auth_cli as CLI
from bot.broker_allocation import load_policy, _ensure_table, validate_policy
from bot.etoro.signal_only_broker import determine_signal_only_reason as R


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.audit = tempfile.mktemp(suffix=".db")
        self.conn = sqlite3.connect(self.db)
        _ensure_table(self.conn)

    def tearDown(self):
        self.conn.close()
        for p in (self.db, self.audit):
            if os.path.exists(p):
                os.remove(p)

    def _enable(self, hours=1.0, operator="op", confirm=True):
        return CLI.enable_ibkr_paper(
            self.conn, duration_hours=hours, operator=operator,
            confirm=confirm, audit_db_path=self.audit)


class TestEnableGates(_Base):
    def test_enable_requires_confirmation(self):
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(confirm=False)
        # refusal is audited
        actions = [r["action"] for r in CLI.read_audit(self.audit)]
        self.assertIn("refused", actions)

    def test_duration_cap_enforced(self):
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=CLI._MAX_DURATION_HOURS + 1)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=0)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=-1)

    def test_enable_writes_utc_expiry_and_authorizes_paper(self):
        res = self._enable(hours=1)
        self.assertEqual(res["action"], "enable")
        self.assertTrue(res["expiry_utc"].endswith("+00:00"))
        p = load_policy(self.conn)
        self.assertTrue(p["global"]["auto_trading_enabled"])
        self.assertTrue(p["ibkr_paper"]["auto_trading_enabled"])
        self.assertTrue(
            p["global"]["auto_trading_enabled_until_utc"].endswith("+00:00"))
        self.assertTrue(
            p["ibkr_paper"]["auto_trading_enabled_until_utc"].endswith("+00:00"))
        # persisted policy is valid per the schema validator
        self.assertTrue(validate_policy(p).ok)


class TestNeverLiveNeverLegacy(_Base):
    def test_enable_never_enables_ibkr_live(self):
        self._enable()
        p = load_policy(self.conn)
        self.assertFalse(p["ibkr_live"]["auto_trading_enabled"])
        self.assertIsNone(p["ibkr_live"]["auto_trading_enabled_until_utc"])
        # reader confirms live still blocked
        skip, _ = R(p, "ibkr_live")
        self.assertTrue(skip)

    def test_enable_never_touches_etoro(self):
        self._enable()
        p = load_policy(self.conn)
        self.assertFalse(p["etoro"]["auto_trading_enabled"])
        self.assertFalse(p["routing"]["etoro_live_enabled"])

    def test_enable_does_not_copy_legacy_ibkr_flag(self):
        # pre-set the legacy ibkr flag; enable must not propagate it and the
        # lanes must be authorized only via their own blocks.
        p0 = load_policy(self.conn)
        p0["ibkr"]["auto_trading_enabled"] = True
        from bot.broker_allocation import save_policy
        save_policy(self.conn, p0)
        self._enable()
        p = load_policy(self.conn)
        # legacy stays whatever it was; lanes authorized via lane blocks only
        self.assertFalse(p["ibkr_live"]["auto_trading_enabled"])
        # paper authorized, but via ibkr_paper block (reader proves it)
        self.assertTrue(p["ibkr_paper"]["auto_trading_enabled"])

    def test_refuses_if_live_already_enabled(self):
        # if someone enabled live out-of-band, the paper CLI refuses rather
        # than proceed alongside a live authorization.
        p0 = load_policy(self.conn)
        p0["ibkr_live"]["auto_trading_enabled"] = True
        p0["ibkr_live"]["auto_trading_enabled_until_utc"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        from bot.broker_allocation import save_policy
        save_policy(self.conn, p0)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable()


class TestDisable(_Base):
    def test_disable_clears_authorization_immediately(self):
        self._enable()
        CLI.disable_ibkr_paper(self.conn, operator="op", audit_db_path=self.audit)
        p = load_policy(self.conn)
        self.assertFalse(p["global"]["auto_trading_enabled"])
        self.assertIsNone(p["global"]["auto_trading_enabled_until_utc"])
        self.assertFalse(p["ibkr_paper"]["auto_trading_enabled"])
        self.assertIsNone(p["ibkr_paper"]["auto_trading_enabled_until_utc"])
        skip, _ = R(p, "ibkr_paper")
        self.assertTrue(skip)


class TestReaderIntegration(_Base):
    def test_enable_then_reader_allows_then_disable_blocks(self):
        # writer/enforcer separation: the CLI only writes; the reader enforces.
        self._enable(hours=1)
        p = load_policy(self.conn)
        skip, reason = R(p, "ibkr_paper")
        self.assertFalse(skip, msg=reason)

        CLI.disable_ibkr_paper(self.conn, operator="op", audit_db_path=self.audit)
        p2 = load_policy(self.conn)
        skip2, _ = R(p2, "ibkr_paper")
        self.assertTrue(skip2)


class TestAudit(_Base):
    def test_audit_records_all_actions(self):
        try:
            self._enable(confirm=False)
        except CLI.AuthorizationRefused:
            pass
        self._enable(hours=1)
        CLI.disable_ibkr_paper(self.conn, operator="op", audit_db_path=self.audit)
        actions = [r["action"] for r in CLI.read_audit(self.audit)]
        self.assertIn("refused", actions)
        self.assertIn("enable", actions)
        self.assertIn("disable", actions)

    def test_audit_rows_have_utc_created_at(self):
        self._enable(hours=1)
        rows = CLI.read_audit(self.audit)
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(r["created_at_utc"].endswith("+00:00"))

    def test_audit_append_only_identical_replay_dedup(self):
        # writing the identical audit action content twice does not duplicate
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
        a = CLI._audit_write(self.audit, action="enable", lane="ibkr_paper",
                             operator="op", expiry_utc="2026-07-03T13:00:00+00:00",
                             duration_hours=1.0, reason="x",
                             created_at_utc=now.isoformat())
        b = CLI._audit_write(self.audit, action="enable", lane="ibkr_paper",
                             operator="op", expiry_utc="2026-07-03T13:00:00+00:00",
                             duration_hours=1.0, reason="x",
                             created_at_utc=now.isoformat())
        self.assertEqual(a["audit_id"], b["audit_id"])
        self.assertEqual(len(CLI.read_audit(self.audit)), 1)

    def test_audit_is_not_read_for_enforcement(self):
        # sanity: the CLI module must not import the reader to gate on audit.
        import ast
        with open("tools/paper_loop/m21_1extra_d0_auth_cli.py") as fh:
            src = fh.read()
        tree = ast.parse(src)
        # audit funcs must never call determine_signal_only_reason
        called = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                called.add(n.func.id)
        self.assertNotIn("determine_signal_only_reason", called)


if __name__ == "__main__":
    unittest.main(verbosity=2)

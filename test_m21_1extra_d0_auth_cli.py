#!/usr/bin/env python3
"""M21.1extra-D0 Commit 2 — CLI + audit tests (atomic, old-policy-safe)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import tools.paper_loop.m21_1extra_d0_auth_cli as CLI
from bot.broker_allocation import (
    load_policy, save_policy, _ensure_table, validate_policy, POLICY_KEY)
from bot.etoro.signal_only_broker import determine_signal_only_reason as R


def _write_raw_policy(conn, policy_dict):
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_risk_state (key, value, updated_at) "
        "VALUES (?, ?, ?)",
        (POLICY_KEY, json.dumps(policy_dict),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def _old_pre_d0_policy(legacy_ibkr_enabled=False):
    """A pre-D0 policy row: NO ibkr_paper / ibkr_live, NO global expiry."""
    return {
        "version": 1,
        "global": {"auto_trading_enabled": True,
                   "max_auto_trading_capital": 1000.0, "kill_switch": False},
        "ibkr": {"auto_trading_enabled": legacy_ibkr_enabled,
                 "max_auto_trading_capital": 500.0,
                 "max_single_trade_amount": 100.0, "max_daily_loss": 200.0,
                 "max_open_positions": 3, "kill_switch": False},
        "etoro": {"auto_trading_enabled": False,
                  "max_auto_trading_capital": 0.0,
                  "max_single_trade_amount": 0.0, "max_daily_loss": 0.0,
                  "max_open_positions": 0, "kill_switch": False},
        "routing": {"default_broker": "paper",
                    "route_overrides": {"IBKR": "ibkr_live",
                                        "ETORO": "etoro_paper"},
                    "allowed_brokers": ["paper", "ibkr_paper", "ibkr_live",
                                        "etoro_paper"],
                    "etoro_live_enabled": False},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        _ensure_table(self.conn)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db):
            os.remove(self.db)

    def _enable(self, hours=1.0, operator="op", confirm=True):
        return CLI.enable_ibkr_paper(
            self.conn, duration_hours=hours, operator=operator, confirm=confirm)


class TestEnableGates(_Base):
    def test_enable_requires_confirmation(self):
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(confirm=False)
        self.assertIn("refused",
                      [r["action"] for r in CLI.read_audit(self.conn)])

    def test_duration_cap_enforced(self):
        for h in (CLI._MAX_DURATION_HOURS + 1, 0, -1):
            with self.assertRaises(CLI.AuthorizationRefused):
                self._enable(hours=h)

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
        self.assertTrue(validate_policy(p).ok)


class TestNeverLiveNeverLegacy(_Base):
    def test_enable_never_enables_ibkr_live(self):
        self._enable()
        p = load_policy(self.conn)
        self.assertFalse(p["ibkr_live"]["auto_trading_enabled"])
        self.assertIsNone(p["ibkr_live"]["auto_trading_enabled_until_utc"])
        self.assertTrue(R(p, "ibkr_live")[0])

    def test_enable_never_touches_etoro(self):
        self._enable()
        p = load_policy(self.conn)
        self.assertFalse(p["etoro"]["auto_trading_enabled"])
        self.assertFalse(p["routing"]["etoro_live_enabled"])

    def test_refuses_if_live_already_enabled(self):
        p0 = load_policy(self.conn)
        p0["ibkr_live"]["auto_trading_enabled"] = True
        p0["ibkr_live"]["auto_trading_enabled_until_utc"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        save_policy(self.conn, p0)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable()


class TestOldPolicyNormalisation(_Base):
    """Blocker 1: the CLI must work on old pre-D0 signals.db rows."""

    def test_enable_on_old_pre_d0_policy(self):
        _write_raw_policy(self.conn, _old_pre_d0_policy())
        res = self._enable(hours=1)  # must NOT KeyError
        self.assertEqual(res["action"], "enable")
        p = load_policy(self.conn)
        self.assertTrue(p["ibkr_paper"]["auto_trading_enabled"])
        self.assertFalse(p["ibkr_live"]["auto_trading_enabled"])
        self.assertTrue(validate_policy(p).ok)

    def test_disable_on_old_pre_d0_policy(self):
        _write_raw_policy(self.conn, _old_pre_d0_policy())
        CLI.disable_ibkr_paper(self.conn, operator="op")  # must NOT KeyError
        p = load_policy(self.conn)
        self.assertFalse(p["global"]["auto_trading_enabled"])
        self.assertFalse(p["ibkr_paper"]["auto_trading_enabled"])
        self.assertTrue(validate_policy(p).ok)

    def test_legacy_ibkr_enabled_not_copied_to_paper(self):
        # _ensure_d0_fields adds missing lanes disabled; a legacy ibkr enable is
        # never propagated into a lane.
        _write_raw_policy(self.conn, _old_pre_d0_policy(legacy_ibkr_enabled=True))
        norm = CLI._ensure_d0_fields(load_policy(self.conn))
        self.assertFalse(norm["ibkr_paper"]["auto_trading_enabled"])

    def test_legacy_ibkr_enabled_not_copied_to_live(self):
        _write_raw_policy(self.conn, _old_pre_d0_policy(legacy_ibkr_enabled=True))
        norm = CLI._ensure_d0_fields(load_policy(self.conn))
        self.assertFalse(norm["ibkr_live"]["auto_trading_enabled"])

    def test_ensure_d0_fields_validates(self):
        _write_raw_policy(self.conn, _old_pre_d0_policy(legacy_ibkr_enabled=True))
        norm = CLI._ensure_d0_fields(load_policy(self.conn))
        self.assertTrue(validate_policy(norm).ok)

    def test_ensure_d0_fields_preserves_existing_values(self):
        # add-missing-only: it must NOT change an existing non-target value.
        pol = _old_pre_d0_policy()
        pol["etoro"]["auto_trading_enabled"] = True
        pol["routing"]["etoro_live_enabled"] = True
        norm = CLI._ensure_d0_fields(pol)
        self.assertTrue(norm["etoro"]["auto_trading_enabled"])
        self.assertTrue(norm["routing"]["etoro_live_enabled"])


class TestAtomicity(_Base):
    """Blocker 2: enable must be auditable by construction."""

    def test_audit_failure_leaves_policy_unchanged(self):
        before = load_policy(self.conn)
        self.assertFalse(before["ibkr_paper"]["auto_trading_enabled"])
        orig = CLI._audit_insert_conn

        def boom(*a, **k):
            raise RuntimeError("simulated audit failure")

        CLI._audit_insert_conn = boom
        try:
            with self.assertRaises(RuntimeError):
                self._enable(hours=1)
        finally:
            CLI._audit_insert_conn = orig
        after = load_policy(self.conn)
        self.assertFalse(after["ibkr_paper"]["auto_trading_enabled"],
                         "audit failure must NOT leave paper enabled")
        self.assertFalse(after["global"]["auto_trading_enabled"])
        self.assertNotIn("enable",
                         [r["action"] for r in CLI.read_audit(self.conn)])

    def test_successful_enable_writes_both_policy_and_audit(self):
        res = self._enable(hours=1)
        p = load_policy(self.conn)
        self.assertTrue(p["ibkr_paper"]["auto_trading_enabled"])
        rows = CLI.read_audit(self.conn)
        self.assertIn(res["audit_id"], [r["audit_id"] for r in rows])
        self.assertIn("enable", [r["action"] for r in rows])

    def test_disable_still_clears_after_normal_enable(self):
        self._enable(hours=1)
        CLI.disable_ibkr_paper(self.conn, operator="op")
        p = load_policy(self.conn)
        self.assertFalse(p["ibkr_paper"]["auto_trading_enabled"])
        self.assertTrue(R(p, "ibkr_paper")[0])


class TestReaderIntegration(_Base):
    def test_enable_then_reader_allows_then_disable_blocks(self):
        self._enable(hours=1)
        p = load_policy(self.conn)
        self.assertFalse(R(p, "ibkr_paper")[0])
        CLI.disable_ibkr_paper(self.conn, operator="op")
        p2 = load_policy(self.conn)
        self.assertTrue(R(p2, "ibkr_paper")[0])


class TestAudit(_Base):
    def test_audit_records_all_actions(self):
        try:
            self._enable(confirm=False)
        except CLI.AuthorizationRefused:
            pass
        self._enable(hours=1)
        CLI.disable_ibkr_paper(self.conn, operator="op")
        actions = [r["action"] for r in CLI.read_audit(self.conn)]
        for a in ("refused", "enable", "disable"):
            self.assertIn(a, actions)

    def test_audit_rows_have_utc_created_at(self):
        self._enable(hours=1)
        for r in CLI.read_audit(self.conn):
            self.assertTrue(r["created_at_utc"].endswith("+00:00"))

    def test_audit_append_only_identical_replay_dedup(self):
        now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).isoformat()
        for _ in range(2):
            CLI._audit_write_standalone(
                self.conn, action="refused", lane="ibkr_paper", operator="op",
                expiry_utc=None, duration_hours=1.0, reason="x",
                created_at_utc=now)
        self.assertEqual(len(CLI.read_audit(self.conn)), 1)

    def test_audit_is_not_read_for_enforcement(self):
        import ast
        with open("tools/paper_loop/m21_1extra_d0_auth_cli.py") as fh:
            tree = ast.parse(fh.read())
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn("determine_signal_only_reason", called)


class TestNonTargetRefusal(_Base):
    """D0 enable must refuse if ANY non-target authorization is active, because
    writing global=True could otherwise activate a pre-existing lane."""

    def _seed(self, **flags):
        pol = _old_pre_d0_policy()
        base = CLI._ensure_d0_fields(pol)  # add D0 fields, preserve values
        base["global"]["auto_trading_enabled"] = False  # isolate the flag under test
        for k, v in flags.items():
            d = base
            parts = k.split(".")
            for pp in parts[:-1]:
                d = d[pp]
            d[parts[-1]] = v
        _write_raw_policy(self.conn, base)

    def test_refuse_if_etoro_auto_trading_enabled(self):
        self._seed(**{"etoro.auto_trading_enabled": True})
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable()
        p = load_policy(self.conn)
        self.assertFalse(p["global"]["auto_trading_enabled"])
        self.assertIn("refused",
                      [r["action"] for r in CLI.read_audit(self.conn)])

    def test_refuse_if_etoro_live_enabled(self):
        self._seed(**{"routing.etoro_live_enabled": True})
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable()
        p = load_policy(self.conn)
        self.assertFalse(p["global"]["auto_trading_enabled"])
        self.assertIn("refused",
                      [r["action"] for r in CLI.read_audit(self.conn)])

    def test_refuse_if_ibkr_live_enabled(self):
        self._seed(**{"ibkr_live.auto_trading_enabled": True})
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable()
        self.assertFalse(
            load_policy(self.conn)["global"]["auto_trading_enabled"])

    def test_refused_non_target_leaves_policy_unchanged(self):
        self._seed(**{"etoro.auto_trading_enabled": True})
        before = load_policy(self.conn)
        try:
            self._enable()
        except CLI.AuthorizationRefused:
            pass
        after = load_policy(self.conn)
        self.assertEqual(before, after)


class TestAllowedBrokers(_Base):
    """No fake ENABLED: after enable the reader must actually allow ibkr_paper."""

    def test_old_policy_missing_ibkr_paper_in_allowed_still_works(self):
        pol = _old_pre_d0_policy()
        pol["routing"]["allowed_brokers"] = ["paper"]  # ibkr_paper NOT allowed
        _write_raw_policy(self.conn, pol)
        self._enable(hours=1)
        p = load_policy(self.conn)
        self.assertIn("ibkr_paper", p["routing"]["allowed_brokers"])
        self.assertTrue(validate_policy(p).ok)
        skip, reason = R(p, "ibkr_paper")
        self.assertFalse(skip, msg="reader must allow after ENABLED: %s" % reason)


class TestAllLanesAfterEnable(_Base):
    """Personally check EVERY non-target lane after a clean enable."""

    def test_every_lane_state_after_enable(self):
        self._enable(hours=1)
        p = load_policy(self.conn)
        self.assertFalse(R(p, "ibkr_paper")[0])   # allowed
        self.assertTrue(R(p, "ibkr_live")[0])      # blocked
        self.assertTrue(R(p, "etoro_paper")[0])    # blocked
        self.assertTrue(R(p, "etoro_real")[0])     # blocked
        self.assertTrue(R(p, "ibkr")[0])           # blocked/unsupported


class TestSafetyBooleanStrictness(_Base):
    """Safety flags use `is True`, never bool(): a truthy string like 'false'
    must never be read as an active/enabled flag."""

    def test_string_false_kill_not_treated_as_active_but_refused_as_malformed(self):
        # A raw kill_switch of the string "false" is NOT `is True`, so it is not
        # treated as an active kill switch — but it is a malformed boolean, so
        # enable refuses cleanly at validation rather than writing bad state.
        pol = _old_pre_d0_policy()
        pol["global"]["auto_trading_enabled"] = False
        pol["global"]["kill_switch"] = "false"
        _write_raw_policy(self.conn, pol)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=1)
        self.assertFalse(
            load_policy(self.conn)["global"]["auto_trading_enabled"])

    def test_string_true_ibkr_live_not_treated_as_enabled(self):
        # "true" (string) is not `is True`; the non-target refusal must use `is
        # True`, so a string does not trip it — but a real bool True does.
        pol = _old_pre_d0_policy()
        base = CLI._ensure_d0_fields(pol)
        base["ibkr_live"]["auto_trading_enabled"] = "true"  # string, not True
        _write_raw_policy(self.conn, base)
        # string "true" is not `is True` -> not treated as active; enable
        # proceeds past the refusal check, then validation rejects the non-bool.
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=1)

    def test_no_bool_calls_on_safety_fields_in_source(self):
        with open("tools/paper_loop/m21_1extra_d0_auth_cli.py") as fh:
            src = fh.read()
        self.assertNotIn("bool(", src)


class TestKillSwitchRefusal(_Base):
    """Blocker 1: enable must refuse if a kill switch is active (else the reader
    would block ibkr_paper → a fake ENABLED)."""

    def test_refuse_if_global_kill_switch(self):
        pol = CLI._ensure_d0_fields(_old_pre_d0_policy())
        pol["global"]["auto_trading_enabled"] = False
        pol["global"]["kill_switch"] = True
        _write_raw_policy(self.conn, pol)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=1)
        p = load_policy(self.conn)
        self.assertFalse(p["global"]["auto_trading_enabled"])
        self.assertIn("refused",
                      [r["action"] for r in CLI.read_audit(self.conn)])

    def test_refuse_if_ibkr_paper_kill_switch(self):
        pol = CLI._ensure_d0_fields(_old_pre_d0_policy())
        pol["global"]["auto_trading_enabled"] = False
        pol["ibkr_paper"]["kill_switch"] = True
        _write_raw_policy(self.conn, pol)
        with self.assertRaises(CLI.AuthorizationRefused):
            self._enable(hours=1)
        self.assertFalse(
            load_policy(self.conn)["global"]["auto_trading_enabled"])

    def test_kill_switch_refusal_leaves_policy_unchanged(self):
        pol = CLI._ensure_d0_fields(_old_pre_d0_policy())
        pol["global"]["auto_trading_enabled"] = False
        pol["global"]["kill_switch"] = True
        _write_raw_policy(self.conn, pol)
        before = load_policy(self.conn)
        try:
            self._enable(hours=1)
        except CLI.AuthorizationRefused:
            pass
        self.assertEqual(before, load_policy(self.conn))


class TestDisablePreservesNonTarget(_Base):
    """Blocker 2: disable must clear only D0 fields, preserving non-target."""

    def _seed_all_on(self):
        pol = CLI._ensure_d0_fields(_old_pre_d0_policy())
        pol["etoro"]["auto_trading_enabled"] = True
        pol["routing"]["etoro_live_enabled"] = True
        pol["ibkr_live"]["auto_trading_enabled"] = True
        pol["global"]["auto_trading_enabled"] = True
        pol["ibkr_paper"]["auto_trading_enabled"] = True
        _write_raw_policy(self.conn, pol)

    def test_disable_preserves_etoro_enabled(self):
        self._seed_all_on()
        CLI.disable_ibkr_paper(self.conn, operator="op")
        self.assertTrue(
            load_policy(self.conn)["etoro"]["auto_trading_enabled"])

    def test_disable_preserves_etoro_live_enabled(self):
        self._seed_all_on()
        CLI.disable_ibkr_paper(self.conn, operator="op")
        self.assertTrue(
            load_policy(self.conn)["routing"]["etoro_live_enabled"])

    def test_disable_preserves_ibkr_live_enabled(self):
        self._seed_all_on()
        CLI.disable_ibkr_paper(self.conn, operator="op")
        self.assertTrue(
            load_policy(self.conn)["ibkr_live"]["auto_trading_enabled"])

    def test_disable_still_clears_global_and_paper(self):
        self._seed_all_on()
        CLI.disable_ibkr_paper(self.conn, operator="op")
        p = load_policy(self.conn)
        self.assertFalse(p["global"]["auto_trading_enabled"])
        self.assertFalse(p["ibkr_paper"]["auto_trading_enabled"])


class TestNoAssertForSafety(_Base):
    """Blocker 3: safety must not depend on `assert` (stripped under -O)."""

    def test_no_assert_statements_in_source(self):
        import ast
        with open("tools/paper_loop/m21_1extra_d0_auth_cli.py") as fh:
            tree = ast.parse(fh.read())
        asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
        self.assertEqual(asserts, [], "no assert may guard safety in D0 CLI")


if __name__ == "__main__":
    unittest.main(verbosity=2)

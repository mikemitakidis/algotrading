#!/usr/bin/env python3
"""M21.1extra-B2a — tests for IBKR paper gateway readiness (read-only).

All gateway interaction is mocked; no IB Gateway required. Proves the 15 gates:
  1. paper mode required
  2. live mode refused
  3. live port 4001 refused
  4. wrong account refused
  5. kill switch active blocks before broker path
  6. default readiness mode is read-only
  7. no submit/placeOrder/bracket/order creation (AST/source)
  8. no OrderIntent / OrderResult construction (AST/source)
  9. manual cancel requires exact id + explicit confirmation
 10. no id -> no cancel attempted
 11. cancel result recorded truthfully
 12. reconcile/connection output recorded truthfully
 13. writes only under /tmp
 14. no scheduler/eToro/Telegram/dashboard/main
 15. flatten capability reported explicitly as not_available_in_current_adapter
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tokenize
import unittest

import tools.paper_loop.m21_1extra_b2a_gateway_readiness as B2A

_MODULE_PATH = "tools/paper_loop/m21_1extra_b2a_gateway_readiness.py"


class MockBroker:
    """Stand-in for IBKRBroker exposing only the read-only + cancel methods."""

    def __init__(self, *, orders=None, positions=None, cancel_result=True,
                 has_flatten=False, connected=True, account_verified=True,
                 reconcile_fails=False, reconcile_fails_after_cancel=False):
        self._orders = orders if orders is not None else [
            {"order_id": 42, "symbol": "AAA", "action": "BUY",
             "qty": 1, "status": "PreSubmitted"}]
        self._positions = positions if positions is not None else []
        self._cancel_result = cancel_result
        self._connected = connected
        self._account_verified = account_verified
        self._reconcile_fails = reconcile_fails
        self._reconcile_fails_after_cancel = reconcile_fails_after_cancel
        self.cancelled = []
        if has_flatten:
            # simulate an adapter that DID grow a flatten primitive
            self.flatten_position = lambda *a, **k: True

    def connection_status(self):
        if not self._connected:
            return {"connected": False, "host": "127.0.0.1", "port": 4002,
                    "account": "DUP623346", "error": "gateway down",
                    "is_live": False}
        return {"connected": True, "host": "127.0.0.1", "port": 4002,
                "account": "DUP623346",
                "account_verified": self._account_verified,
                "account_msg": "ok" if self._account_verified else "mismatch",
                "server_version": 176, "is_live": False, "mode": "PAPER"}

    def reconcile(self):
        # fail mode triggered either always, or only after a cancel happened
        fail = self._reconcile_fails or (
            self._reconcile_fails_after_cancel and self.cancelled)
        if fail:
            # mirrors the real adapter: never raises; reports via warnings
            return {"open_orders": [], "positions": [],
                    "warnings": ["reconcile failed: connection lost"]}
        return {"open_orders": list(self._orders),
                "positions": list(self._positions), "warnings": []}

    def get_positions(self):
        return list(self._positions)

    def cancel(self, broker_order_id):
        self.cancelled.append(broker_order_id)
        return self._cancel_result


class TestPaperModeGates(unittest.TestCase):
    def test_1_6_default_readiness_is_read_only(self):
        mb = MockBroker()
        r = B2A.run_readiness(kill_switch_active=False, broker=mb)
        self.assertEqual(r.mode, "readiness")
        self.assertTrue(r.paper_mode_asserted)
        self.assertFalse(r.cancel_attempted)
        self.assertEqual(mb.cancelled, [])           # no mutation by default
        self.assertFalse(r.order_origination_attempted)
        self.assertFalse(r.broker_submit_attempted)
        self.assertFalse(r.order_result_created)

    def test_2_live_mode_refused(self):
        prev = os.environ.get("BROKER")
        os.environ["BROKER"] = "ibkr_live"
        try:
            with self.assertRaises(B2A.PaperModeRefused):
                B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        finally:
            if prev is None:
                os.environ.pop("BROKER", None)
            else:
                os.environ["BROKER"] = prev

    def test_3_live_port_refused(self):
        prev_b = os.environ.get("BROKER")
        prev_p = os.environ.get("IBKR_PORT")
        os.environ.pop("BROKER", None)
        os.environ["IBKR_PORT"] = "4001"
        try:
            with self.assertRaises(B2A.PaperModeRefused):
                B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        finally:
            if prev_b is not None:
                os.environ["BROKER"] = prev_b
            if prev_p is None:
                os.environ.pop("IBKR_PORT", None)
            else:
                os.environ["IBKR_PORT"] = prev_p

    def test_4_wrong_account_refused(self):
        prev = os.environ.get("IBKR_ACCOUNT")
        os.environ["IBKR_ACCOUNT"] = "SOME_OTHER_ACCOUNT"
        try:
            with self.assertRaises(B2A.PaperModeRefused):
                B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        finally:
            if prev is None:
                os.environ.pop("IBKR_ACCOUNT", None)
            else:
                os.environ["IBKR_ACCOUNT"] = prev

    def test_5_kill_switch_blocks_before_broker_path(self):
        mb = MockBroker()
        r = B2A.run_readiness(kill_switch_active=True, broker=mb)
        self.assertTrue(r.kill_switch_active)
        self.assertFalse(r.connected)            # never connected
        self.assertEqual(mb.cancelled, [])
        self.assertEqual(r.flatten_capability, "not_attempted")

    def test_5_kill_switch_default_reads_real_state(self):
        import bot.kill_switch as ks
        original = ks.is_kill_switch_active
        try:
            ks.is_kill_switch_active = lambda: True
            r = B2A.run_readiness(broker=MockBroker())  # kill_switch_active=None
            self.assertTrue(r.kill_switch_active)
            self.assertFalse(r.connected)
        finally:
            ks.is_kill_switch_active = original


class TestCancelRules(unittest.TestCase):
    def test_9_cancel_requires_id_and_confirmation(self):
        # id present but NO confirmation -> refused, not attempted
        mb = MockBroker()
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-123")
        self.assertTrue(r.cancel_requested)
        self.assertFalse(r.cancel_attempted)
        self.assertIsNone(r.cancel_confirmed)
        self.assertEqual(mb.cancelled, [])

    def test_9_cancel_with_confirmation_attempts_exact_id(self):
        mb = MockBroker(cancel_result=True)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-123",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.cancel_attempted)
        self.assertTrue(r.cancel_confirmed)
        self.assertEqual(r.cancelled_order_id, "IB-PERM-123")
        self.assertEqual(mb.cancelled, ["IB-PERM-123"])  # exactly one id

    def test_10_no_id_no_cancel(self):
        mb = MockBroker()
        r = B2A.run_readiness(kill_switch_active=False, broker=mb)
        self.assertFalse(r.cancel_requested)
        self.assertFalse(r.cancel_attempted)
        self.assertEqual(mb.cancelled, [])

    def test_confirmation_without_id_does_not_cancel(self):
        mb = MockBroker()
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_confirmed_flag=True)
        self.assertFalse(r.cancel_attempted)
        self.assertEqual(mb.cancelled, [])

    def test_11_cancel_failure_recorded_truthfully(self):
        mb = MockBroker(cancel_result=False)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-999",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.cancel_attempted)
        self.assertFalse(r.cancel_confirmed)         # truthful False


class TestReadinessTruthfulness(unittest.TestCase):
    def test_12_reconcile_output_recorded(self):
        mb = MockBroker(
            orders=[{"order_id": 7, "symbol": "BBB", "status": "Submitted"}],
            positions=[{"symbol": "CCC", "position": 5, "avg_cost": 10.0}])
        r = B2A.run_readiness(kill_switch_active=False, broker=mb)
        self.assertTrue(r.connected)
        self.assertEqual(len(r.open_orders), 1)
        self.assertEqual(r.open_orders[0]["symbol"], "BBB")
        self.assertEqual(len(r.positions), 1)
        self.assertEqual(r.positions[0]["symbol"], "CCC")
        self.assertTrue(r.account_verified)

    def test_15_flatten_capability_not_available(self):
        r = B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        self.assertEqual(r.flatten_capability,
                         "not_available_in_current_adapter")

    def test_15_flatten_method_present_is_not_proven(self):
        # If a method merely EXISTS, B2a reports available_but_not_proven —
        # never available_and_proven (it does not exercise the primitive).
        r = B2A.run_readiness(kill_switch_active=False,
                            broker=MockBroker(has_flatten=True))
        self.assertEqual(r.flatten_capability, "available_but_not_proven")
        self.assertNotEqual(r.flatten_capability, "available_and_proven")

    def test_connection_status_failure_blocks_readiness(self):
        # gateway down -> connected False, account_verified False, no cancel
        mb = MockBroker(connected=False)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-1",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.connection_status_checked)
        self.assertFalse(r.connected)
        self.assertFalse(r.account_verified)
        self.assertFalse(r.cancel_attempted)     # cancel never runs
        self.assertEqual(mb.cancelled, [])

    def test_account_not_verified_blocks_readiness(self):
        mb = MockBroker(account_verified=False)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-1",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.connected)
        self.assertFalse(r.account_verified)
        self.assertFalse(r.cancel_attempted)
        self.assertEqual(mb.cancelled, [])

    def test_reconcile_failure_cannot_pass_as_ready(self):
        # reconcile reports a 'reconcile failed:' warning -> reconcile_succeeded
        # False, cancel not run, even though connection_status was OK.
        mb = MockBroker(reconcile_fails=True)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-1",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.connected)
        self.assertFalse(r.reconcile_succeeded)
        self.assertFalse(r.cancel_attempted)
        self.assertEqual(mb.cancelled, [])
        self.assertTrue(any(str(w).startswith("reconcile failed:")
                            for w in r.warnings))

    def test_get_positions_empty_does_not_alone_prove_account(self):
        # account_verified comes from connection_status, NOT from get_positions
        # returning []. With account_verified False at the status level, an
        # empty positions list must not flip account_verified to True.
        mb = MockBroker(account_verified=False, positions=[])
        r = B2A.run_readiness(kill_switch_active=False, broker=mb)
        self.assertFalse(r.account_verified)

    def test_positions_field_is_attempt_not_success(self):
        # The report must not carry a success-looking positions field that could
        # be a false positive from the non-raising get_positions().
        r = B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        d = r.to_dict()
        self.assertNotIn("positions_read_succeeded", d)
        self.assertIn("positions_read_attempted", d)
        self.assertTrue(d["positions_read_attempted"])

    def test_post_cancel_reconcile_failure_recorded_truthfully(self):
        # Cancel succeeds, but the post-cancel reconcile fails (swallowed
        # warning). post_cancel_reconcile_succeeded must be False and the
        # remaining-state-not-verified warning must be present.
        mb = MockBroker(cancel_result=True, reconcile_fails_after_cancel=True)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-5",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.cancel_attempted)
        self.assertTrue(r.cancel_confirmed)
        self.assertFalse(r.post_cancel_reconcile_succeeded)
        self.assertTrue(any(str(w).startswith("reconcile failed:")
                            for w in r.warnings))
        self.assertTrue(any("NOT verified" in str(w) for w in r.warnings))

    def test_post_cancel_reconcile_success_recorded(self):
        mb = MockBroker(cancel_result=True)
        r = B2A.run_readiness(kill_switch_active=False, broker=mb,
                            cancel_manual_order_id="IB-PERM-5",
                            cancel_confirmed_flag=True)
        self.assertTrue(r.post_cancel_reconcile_succeeded)

    def test_docstring_does_not_list_available_and_proven_as_output(self):
        # The module docstring must not present available_and_proven as a B2a
        # OUTPUT value. It may mention it only as a reserved/future value.
        import tools.paper_loop.m21_1extra_b2a_gateway_readiness as mod
        doc = mod.__doc__ or ""
        # the enum line must not list it among B2a outputs
        self.assertIn("available_but_not_proven", doc)
        self.assertNotIn(
            '"available_and_proven" / "not_available_in_current_adapter"', doc)

    def test_provenance_flags_always_false(self):
        r = B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        d = r.to_dict()
        self.assertFalse(d["order_origination_attempted"])
        self.assertFalse(d["broker_submit_attempted"])
        self.assertFalse(d["order_result_created"])


class TestOutputPath(unittest.TestCase):
    def test_13_writes_only_tmp(self):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        r = subprocess.run(
            [sys.executable, "-m",
             "tools.paper_loop.m21_1extra_b2a_gateway_readiness",
             "--mode", "readiness", "--report", "reports/should_refuse.md"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/tmp/", (r.stderr + r.stdout))


class TestSourceGuard(unittest.TestCase):
    """Gates 7, 8, 14: ban order-origination/network tokens in EXECUTABLE code
    (comments/strings stripped). IBKRBroker + reconcile/get_positions/cancel are
    allowed; submit/bracket/order-construction are not."""

    def _executable_source(self):
        with open(_MODULE_PATH, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
        return " ".join(t.string for t in toks
                        if t.type not in (tokenize.COMMENT, tokenize.STRING))

    def _code_name_tokens(self):
        """Set of NAME tokens in executable code (comments/strings excluded)."""
        with open(_MODULE_PATH, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
        return {t.string for t in toks if t.type == tokenize.NAME}

    def test_7_8_14_no_forbidden_tokens_in_code(self):
        # Check forbidden NAME tokens (tokenization-robust): a method/identifier
        # like submit/placeOrder/OrderIntent cannot appear in executable code.
        names = self._code_name_tokens()
        forbidden_names = {
            "submit", "_make_bracket", "OrderIntent", "OrderResult",
            "placeOrder", "MarketOrder", "LimitOrder", "StopOrder",
            "ib_insync", "etoro", "telegram", "notifier", "scheduler",
            "apscheduler", "dashboard",
        }
        bad = names & forbidden_names
        self.assertEqual(bad, set(), "forbidden name tokens in code: %s" % bad)
        # numeric/string-literal bans verified on the joined executable text
        code = self._executable_source()
        for tok in ("4001", "ibkr_live"):
            self.assertNotIn(tok, code)

    def test_allowed_readonly_methods_present(self):
        # The allowed read-only/cancel methods appear as NAME tokens in code.
        names = self._code_name_tokens()
        self.assertIn("reconcile", names)
        self.assertIn("get_positions", names)
        self.assertIn("cancel", names)

    def test_imports_constrained(self):
        with open(_MODULE_PATH) as fh:
            tree = ast.parse(fh.read())
        mods = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                mods.append(n.module or "")
        for m in mods:
            self.assertNotIn("ib_insync", m)
            self.assertFalse(m.startswith("bot.etoro"))
            self.assertNotEqual(m, "main")
            self.assertFalse(m.startswith("dashboard"))

    def test_no_order_origination_calls(self):
        with open(_MODULE_PATH) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or \
                    getattr(node.func, "attr", None)
                self.assertNotEqual(name, "submit")
                self.assertNotEqual(name, "_make_bracket")
                self.assertNotEqual(name, "placeOrder")
                self.assertNotEqual(name, "OrderIntent")
                self.assertNotEqual(name, "OrderResult")


class TestReportProvenance(unittest.TestCase):
    """Fix 3: the committed report must declare it is a mock/structural proof,
    not a real gateway contact."""

    def test_mock_render_labels_itself_mock(self):
        r = B2A.run_readiness(kill_switch_active=False, broker=MockBroker())
        d = r.to_dict()
        md = B2A._render(d, data_source="mock_broker_structural_proof")
        self.assertIn("data_source: **mock_broker_structural_proof**", md)
        self.assertIn("real_ibkr_gateway_connected: **false**", md)
        self.assertIn("vps_gateway_proof_required: **true**", md)

    def test_committed_report_is_labelled_mock(self):
        # The committed report on disk must not look like a real gateway proof.
        with open("reports/m21_1extra_b2a_gateway_readiness.md") as fh:
            md = fh.read()
        self.assertIn("mock_broker_structural_proof", md)
        self.assertIn("real_ibkr_gateway_connected: **false**", md)
        self.assertIn("vps_gateway_proof_required: **true**", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)

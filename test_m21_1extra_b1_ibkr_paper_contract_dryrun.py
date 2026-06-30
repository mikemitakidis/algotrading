#!/usr/bin/env python3
"""M21.1extra-B1 — tests for the IBKR paper contract DRY-RUN builder.

Proves (ChatGPT's 15 gates):
  1. builds a real OrderIntent
  2. does NOT create an OrderResult
  3. dry_run_only=true
  4. real_broker_order_attempted=false
  5. broker_order_id absent / None
  6. preserves execution_eligible=False
  7. preserves hard_gate_passed=False
  8. expected paper port/account = 4002 / DUP623346
  9. live mode refused
 10. live port 4001 refused
 11. kill switch active blocks future-submit readiness
 12. duplicate candidate cannot produce duplicate dry-run records
 13. operator report/json written only to /tmp
 14. fixture proof deterministic
 15. AST/source guard bans real broker submit/network paths
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest

from bot.brokers.base import OrderIntent
import tools.paper_loop.m21_1extra_b1_ibkr_paper_contract_dryrun as B1

_MODULE_PATH = "tools/paper_loop/m21_1extra_b1_ibkr_paper_contract_dryrun.py"


class TestContractBuild(unittest.TestCase):
    def test_1_builds_real_order_intent(self):
        sig = B1.fixture_signals()[0]
        intent = B1.build_order_intent(sig, position_size=10.0, risk_usd=50.0)
        self.assertIsInstance(intent, OrderIntent)
        self.assertEqual(intent.route, "IBKR")
        self.assertEqual(intent.symbol, sig["symbol"])

    def test_2_does_not_create_order_result(self):
        # AST proof: OrderResult is neither imported nor constructed.
        with open(_MODULE_PATH) as fh:
            tree = ast.parse(fh.read())
        imported = []
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                imported += [a.name for a in n.names]
            elif isinstance(n, ast.Import):
                imported += [a.name for a in n.names]
        self.assertNotIn("OrderResult", imported)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or \
                    getattr(node.func, "attr", None)
                self.assertNotEqual(name, "OrderResult")

    def test_3_4_dry_run_flags(self):
        s = B1.fixture_summary().to_dict()
        self.assertTrue(s["dry_run_only"])
        self.assertFalse(s["real_broker_order_attempted"])
        self.assertFalse(s["ib_gateway_connection_attempted"])

    def test_5_no_broker_order_id(self):
        s = B1.fixture_summary().to_dict()
        for c in s["contracts"]:
            self.assertNotIn("broker_order_id", c)
            self.assertNotIn("filled_price", c)

    def test_6_7_preserves_m19_truth(self):
        s = B1.fixture_summary().to_dict()
        for c in s["contracts"]:
            self.assertFalse(c["execution_eligible"])
            self.assertFalse(c["hard_gate_passed"])

    def test_8_paper_port_and_account(self):
        s = B1.fixture_summary().to_dict()
        self.assertEqual(s["paper_port_expected"], 4002)
        self.assertEqual(s["paper_account_expected"], "DUP623346")
        for c in s["contracts"]:
            self.assertEqual(c["port"], 4002)
            self.assertEqual(c["account"], "DUP623346")
            self.assertFalse(c["would_transmit"])


class TestLiveModeRefused(unittest.TestCase):
    def test_9_live_mode_refused(self):
        prev = os.environ.get("BROKER")
        os.environ["BROKER"] = "ibkr_live"
        try:
            with self.assertRaises(B1.LiveModeRefused):
                B1._assert_paper_mode()
        finally:
            if prev is None:
                os.environ.pop("BROKER", None)
            else:
                os.environ["BROKER"] = prev

    def test_10_live_port_refused(self):
        # Force paper mode but a live port via env -> must refuse.
        prev_broker = os.environ.get("BROKER")
        prev_port = os.environ.get("IBKR_PORT")
        os.environ.pop("BROKER", None)       # paper mode
        os.environ["IBKR_PORT"] = "4001"     # but live port
        try:
            with self.assertRaises(B1.LiveModeRefused):
                B1._assert_paper_mode()
        finally:
            if prev_broker is not None:
                os.environ["BROKER"] = prev_broker
            if prev_port is None:
                os.environ.pop("IBKR_PORT", None)
            else:
                os.environ["IBKR_PORT"] = prev_port

    def test_expected_paper_account_accepted(self):
        prev = os.environ.get("IBKR_ACCOUNT")
        os.environ["IBKR_ACCOUNT"] = "DUP623346"
        try:
            host, port, account = B1._assert_paper_mode()
            self.assertEqual(account, "DUP623346")
            self.assertEqual(port, 4002)
        finally:
            if prev is None:
                os.environ.pop("IBKR_ACCOUNT", None)
            else:
                os.environ["IBKR_ACCOUNT"] = prev

    def test_wrong_paper_account_refused(self):
        prev = os.environ.get("IBKR_ACCOUNT")
        os.environ["IBKR_ACCOUNT"] = "SOME_OTHER_ACCOUNT"
        try:
            with self.assertRaises(B1.LiveModeRefused):
                B1._assert_paper_mode()
        finally:
            if prev is None:
                os.environ.pop("IBKR_ACCOUNT", None)
            else:
                os.environ["IBKR_ACCOUNT"] = prev

    def test_contract_account_cannot_diverge_from_expected(self):
        # Because _assert_paper_mode enforces the exact account, a built
        # contract's account always equals paper_account_expected.
        s = B1.fixture_summary().to_dict()
        for c in s["contracts"]:
            self.assertEqual(c["account"], s["paper_account_expected"])


class TestKillSwitchAndIdempotency(unittest.TestCase):
    def test_11_kill_switch_blocks_submit_readiness(self):
        s = B1.run_once(B1.fixture_signals(), kill_switch_active=True)
        d = s.to_dict()
        # contracts may still be BUILT (dry-run), but none is submit-ready
        self.assertEqual(d["submit_ready_count"], 0)
        for c in d["contracts"]:
            self.assertTrue(c["future_submit_blocked_by_kill_switch"])

    def test_kill_switch_clear_is_submit_ready(self):
        d = B1.run_once(B1.fixture_signals(), kill_switch_active=False).to_dict()
        self.assertEqual(d["submit_ready_count"], d["dry_run_contracts_built"])

    def test_12_duplicate_candidate_not_double_recorded(self):
        sigs = B1.fixture_signals()
        dup = sigs + [dict(sigs[0])]
        d = B1.run_once(dup, kill_switch_active=False).to_dict()
        self.assertEqual(d["candidates_in"], 3)
        self.assertEqual(d["dry_run_contracts_built"], 2)
        self.assertTrue(any(r.get("reason") == "duplicate_signal"
                            for r in d["rejected"]))

    def test_run_once_default_reads_real_kill_switch(self):
        import bot.kill_switch as ks
        original = ks.is_kill_switch_active
        try:
            ks.is_kill_switch_active = lambda: True
            d = B1.run_once(B1.fixture_signals()).to_dict()
            self.assertEqual(d["submit_ready_count"], 0)
        finally:
            ks.is_kill_switch_active = original


class TestOutputPathAndDeterminism(unittest.TestCase):
    def test_13_live_mode_writes_only_tmp(self):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        r = subprocess.run(
            [sys.executable, "-m",
             "tools.paper_loop.m21_1extra_b1_ibkr_paper_contract_dryrun",
             "--mode", "live", "--report", "reports/should_refuse.md"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/tmp/", (r.stderr + r.stdout))

    def test_14_fixture_is_deterministic(self):
        a = B1.fixture_summary().to_dict()
        b = B1.fixture_summary().to_dict()
        # drop nothing: the dry-run is pure, so dict equality must hold
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_fixture_cli_self_loads_env_before_building(self):
        # The operator CLI must load .env for BOTH modes, before building
        # contracts. Track ordering: env loader fires, then fixture_summary.
        order = []
        orig_loader = B1._load_env_for_live
        orig_fixture = B1.fixture_summary
        argv = sys.argv
        try:
            B1._load_env_for_live = lambda: order.append("env")
            def _tracked_fixture():
                order.append("build")
                return orig_fixture()
            B1.fixture_summary = _tracked_fixture
            sys.argv = ["prog", "--mode", "fixture",
                        "--report", "/tmp/b1_cli_env_test.md",
                        "--json-out", "/tmp/b1_cli_env_test.json"]
            B1.main()
        finally:
            B1._load_env_for_live = orig_loader
            B1.fixture_summary = orig_fixture
            sys.argv = argv
        self.assertEqual(order[0], "env")
        self.assertIn("build", order)
        self.assertLess(order.index("env"), order.index("build"))


class TestSourceGuard(unittest.TestCase):
    """Gate 15: ban real broker submit / network / gateway tokens in the
    EXECUTABLE source. We strip comments and string/docstring literals first,
    so a token appearing only inside a docstring that says 'we never call
    placeOrder' does not falsely trip the guard — and, conversely, any such
    token in actual code is caught."""

    _BANNED = (
        ".submit(", "IBKRBroker(", "placeOrder", "ib.connect", "_connect(",
        "_gateway_available", "reconcile(", "get_positions(", "cancel(",
        "ib_insync", "4001", "ibkr_live",
        "etoro", "telegram", "notifier", "scheduler", "apscheduler",
        "dashboard",
    )

    def _executable_source(self):
        """Return the module source with comments and string literals removed,
        leaving only executable tokens (names, operators, numbers)."""
        import io
        import tokenize
        with open(_MODULE_PATH, "rb") as fh:
            tokens = list(tokenize.tokenize(fh.readline))
        kept = []
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(tok.string)
        return " ".join(kept)

    def test_15_executable_source_has_no_forbidden_tokens(self):
        code = self._executable_source()
        for tok in self._BANNED:
            # compare against executable code only (string/comment-free)
            self.assertNotIn(tok, code,
                             "forbidden token %r in B1 executable code" % tok)

    def test_raw_source_has_no_submit_or_placeorder_anywhere(self):
        # Belt-and-braces: these two must not appear even in comments/strings,
        # because there is no legitimate reason to name them at all.
        with open(_MODULE_PATH) as fh:
            src = fh.read()
        self.assertNotIn(".submit(", src)
        self.assertNotIn("ib.placeOrder", src)

    def test_imports_are_constrained(self):
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

    def test_module_does_not_instantiate_broker(self):
        # Static: no IBKRBroker(...) call node anywhere.
        with open(_MODULE_PATH) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                self.assertNotEqual(name, "IBKRBroker")
                self.assertNotEqual(name, "submit")


if __name__ == "__main__":
    unittest.main(verbosity=2)

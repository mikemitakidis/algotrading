#!/usr/bin/env python3
"""M21.1extra-B2b — tests for the one-tiny-paper-lifecycle harness.

Adapter (submit/flatten/reconcile/connection_status) is mocked; no IB Gateway
required. Proves the required gates:
  - live mode / wrong account / wrong port refused
  - kill switch blocks before submit
  - missing confirmation blocks before submit
  - size (notional) cap enforced
  - existing position / existing open order blocks submit
  - exactly one submit call per run
  - broker order id captured truthfully
  - fill/position observation required
  - B2flat cleanup invoked
  - B2flat failure => lifecycle_confirmed=false
  - residual final position/order => lifecycle_confirmed=false
  - source guard bans scheduler/dashboard/Telegram/eToro/persistence/M21.2
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tokenize
import unittest

from bot.brokers.base import OrderResult
import tools.paper_loop.m21_1extra_b2b_single_submit as B2B

_MODULE_PATH = "tools/paper_loop/m21_1extra_b2b_single_submit.py"


class MockBroker:
    def __init__(self, *, account_verified=True, pre_positions=None,
                 pre_orders=None, submit_status="accepted",
                 submit_order_id="IB-PERM-7", filled_price=308.3,
                 post_submit_positions=None, flatten_result=None,
                 reconcile_fails=False, position_appears_on_attempt=1):
        self._account_verified = account_verified
        self._pre_positions = pre_positions or []
        self._pre_orders = pre_orders or []
        self._submit_status = submit_status
        self._submit_order_id = submit_order_id
        self._filled_price = filled_price
        self._post_submit_positions = (
            post_submit_positions if post_submit_positions is not None
            else [{"symbol": "AAPL", "position": 1.0}])
        self._flatten_result = flatten_result or {
            "flatten_confirmed": True, "close_order_placed": True,
            "remaining_positions": [], "remaining_open_orders": [],
            "warnings": []}
        self._reconcile_fails = reconcile_fails
        # position becomes visible only on the Nth POST-submit reconcile
        self._appears_on = position_appears_on_attempt
        self._post_submit_reconciles = 0
        self.submit_calls = []
        self.flatten_calls = []

    def connection_status(self):
        return {"connected": True,
                "account_verified": self._account_verified,
                "account": "DUP623346", "port": 4002,
                "account_msg": "ok" if self._account_verified else "mismatch"}

    def reconcile(self):
        if self._reconcile_fails:
            return {"open_orders": [], "positions": [],
                    "warnings": ["reconcile failed: lost conn"]}
        if self.submit_calls:
            self._post_submit_reconciles += 1
            pos = (list(self._post_submit_positions)
                   if self._post_submit_reconciles >= self._appears_on else [])
            return {"open_orders": [], "positions": pos, "warnings": []}
        return {"open_orders": list(self._pre_orders),
                "positions": list(self._pre_positions), "warnings": []}

    def submit(self, intent):
        self.submit_calls.append(intent)
        return OrderResult(intent=intent, status=self._submit_status,
                           broker_order_id=self._submit_order_id,
                           filled_price=self._filled_price)

    def flatten_paper_position(self, symbol, confirm=False):
        self.flatten_calls.append((symbol, confirm))
        return dict(self._flatten_result)


def _run(broker, **kw):
    params = dict(symbol="AAPL", confirm=True, entry_price=308.3,
                  stop_loss=300.0, target_price=320.0,
                  kill_switch_active=False, broker=broker)
    params.update(kw)
    return B2B.run_lifecycle(**params)


class TestPaperGates(unittest.TestCase):
    def test_live_mode_refused(self):
        prev = os.environ.get("BROKER")
        os.environ["BROKER"] = "ibkr_live"
        try:
            with self.assertRaises(B2B.PaperModeRefused):
                _run(MockBroker())
        finally:
            if prev is None: os.environ.pop("BROKER", None)
            else: os.environ["BROKER"] = prev

    def test_wrong_account_refused(self):
        prev = os.environ.get("IBKR_ACCOUNT")
        os.environ["IBKR_ACCOUNT"] = "OTHER"
        try:
            with self.assertRaises(B2B.PaperModeRefused):
                _run(MockBroker())
        finally:
            if prev is None: os.environ.pop("IBKR_ACCOUNT", None)
            else: os.environ["IBKR_ACCOUNT"] = prev

    def test_wrong_port_refused(self):
        prev = os.environ.get("IBKR_PORT")
        os.environ["IBKR_PORT"] = "4001"
        try:
            with self.assertRaises(B2B.PaperModeRefused):
                _run(MockBroker())
        finally:
            if prev is None: os.environ.pop("IBKR_PORT", None)
            else: os.environ["IBKR_PORT"] = prev

    def test_kill_switch_blocks_before_submit(self):
        mb = MockBroker()
        d = _run(mb, kill_switch_active=True)
        self.assertTrue(d["kill_switch_active"])
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])
        self.assertFalse(d["lifecycle_confirmed"])

    def test_missing_confirmation_blocks_before_submit(self):
        mb = MockBroker()
        d = _run(mb, confirm=False)
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])

    def test_notional_cap_enforced(self):
        mb = MockBroker()
        d = _run(mb, entry_price=999999.0)   # 1 * 999999 > 5000 ceiling
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])
        self.assertTrue(any("notional cap" in w for w in d["warnings"]))


class TestPreExistingStateBlocks(unittest.TestCase):
    def test_existing_position_blocks_submit(self):
        mb = MockBroker(pre_positions=[{"symbol": "AAPL", "position": 1.0}])
        d = _run(mb)
        self.assertTrue(d["pre_existing_position"])
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])

    def test_existing_open_order_blocks_submit(self):
        mb = MockBroker(pre_orders=[{"symbol": "AAPL", "order_id": 1}])
        d = _run(mb)
        self.assertTrue(d["pre_existing_open_orders"])
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])

    def test_open_order_other_symbol_blocks(self):
        mb = MockBroker(pre_orders=[{"symbol": "MSFT", "order_id": 2}])
        d = _run(mb)
        self.assertTrue(d["pre_existing_open_orders"])
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])

    def test_open_order_missing_or_unknown_symbol_blocks(self):
        # order with no symbol / '?' must still block (conservative slate)
        for order in ({"order_id": 3}, {"symbol": "?", "order_id": 4}):
            mb = MockBroker(pre_orders=[order])
            d = _run(mb)
            self.assertTrue(d["pre_existing_open_orders"],
                            "order %r should block" % order)
            self.assertFalse(d["entry_order_originated"])
            self.assertEqual(mb.submit_calls, [])

    def test_account_not_verified_blocks_submit(self):
        mb = MockBroker(account_verified=False)
        d = _run(mb)
        self.assertFalse(d["account_verified"])
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])

    def test_pre_submit_reconcile_failure_blocks(self):
        mb = MockBroker(reconcile_fails=True)
        d = _run(mb)
        self.assertFalse(d["entry_order_originated"])
        self.assertEqual(mb.submit_calls, [])


class TestLifecycleHappyPath(unittest.TestCase):
    def test_exactly_one_submit(self):
        mb = MockBroker()
        _run(mb)
        self.assertEqual(len(mb.submit_calls), 1)

    def test_order_id_captured_truthfully(self):
        mb = MockBroker(submit_order_id="IB-PERM-42")
        d = _run(mb)
        self.assertEqual(d["entry_order_id"], "IB-PERM-42")
        self.assertTrue(d["entry_result_recorded"])
        self.assertEqual(d["entry_result_status"], "accepted")

    def test_position_observed_and_filled(self):
        mb = MockBroker()
        d = _run(mb)
        self.assertTrue(d["position_observed"])
        self.assertTrue(d["entry_filled"])

    def test_flatten_invoked(self):
        mb = MockBroker()
        d = _run(mb)
        self.assertTrue(d["flatten_called"])
        self.assertEqual(mb.flatten_calls, [("AAPL", True)])

    def test_lifecycle_confirmed_true_on_clean_run(self):
        mb = MockBroker()
        d = _run(mb)
        self.assertTrue(d["lifecycle_confirmed"])
        self.assertEqual(d["remaining_positions"], [])
        self.assertEqual(d["remaining_open_orders"], [])


class TestLifecycleFailClosed(unittest.TestCase):
    def test_flatten_failure_makes_lifecycle_false(self):
        mb = MockBroker(flatten_result={
            "flatten_confirmed": False, "close_order_placed": True,
            "remaining_positions": [{"symbol": "AAPL", "position": 1.0}],
            "remaining_open_orders": [], "warnings": ["not confirmed"]})
        d = _run(mb)
        self.assertTrue(d["flatten_called"])
        self.assertFalse(d["flatten_confirmed"])
        self.assertFalse(d["lifecycle_confirmed"])

    def test_residual_position_makes_lifecycle_false(self):
        mb = MockBroker(flatten_result={
            "flatten_confirmed": True, "close_order_placed": True,
            "remaining_positions": [{"symbol": "AAPL", "position": 1.0}],
            "remaining_open_orders": [], "warnings": []})
        d = _run(mb)
        self.assertFalse(d["lifecycle_confirmed"])

    def test_no_fill_observed_makes_lifecycle_false(self):
        # entry accepted but no position ever appears -> position_observed False
        t, p = B2B._OBSERVE_TIMEOUT_S, B2B._OBSERVE_POLL_S
        B2B._OBSERVE_TIMEOUT_S, B2B._OBSERVE_POLL_S = 0.2, 0.01
        try:
            mb = MockBroker(post_submit_positions=[], filled_price=None,
                            position_appears_on_attempt=9999)
            d = _run(mb)
        finally:
            B2B._OBSERVE_TIMEOUT_S, B2B._OBSERVE_POLL_S = t, p
        self.assertTrue(d["entry_order_originated"])
        self.assertFalse(d["position_observed"])
        self.assertFalse(d["lifecycle_confirmed"])


class TestOutputAndGuard(unittest.TestCase):
    def test_writes_only_tmp(self):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        r = subprocess.run(
            [sys.executable, "-m",
             "tools.paper_loop.m21_1extra_b2b_single_submit",
             "--symbol", "AAPL", "--entry-price", "308.3",
             "--stop-loss", "300", "--target-price", "320",
             "--i-understand-this-places-a-real-paper-order",
             "--report", "reports/should_refuse.md"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/tmp/", (r.stderr + r.stdout))

    def test_source_guard_bans_unrelated_systems(self):
        with open(_MODULE_PATH, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
        names = {t.string for t in toks if t.type == tokenize.NAME}
        forbidden = {"scheduler", "apscheduler", "dashboard", "telegram",
                     "notifier", "etoro", "sqlite3"}
        self.assertEqual(names & forbidden, set())

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
            self.assertFalse(m.startswith("bot.etoro"))
            self.assertNotEqual(m, "main")
            self.assertFalse(m.startswith("dashboard"))
            self.assertFalse("scheduler" in m)

    def test_does_not_edit_adapter(self):
        # B2b must consume the adapter, never modify it. Prove the adapter file
        # is unchanged vs origin/main.
        r = subprocess.run(
            ["git", "diff", "--name-only", "origin/main", "HEAD", "--",
             "bot/brokers/ibkr_broker.py"],
            capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "",
                         "B2b must not modify bot/brokers/ibkr_broker.py")


class TestObservationLoop(unittest.TestCase):
    def setUp(self):
        # make the poll fast + short so tests don't actually wait 30s
        self._t, self._p = B2B._OBSERVE_TIMEOUT_S, B2B._OBSERVE_POLL_S
        B2B._OBSERVE_TIMEOUT_S = 0.2
        B2B._OBSERVE_POLL_S = 0.01

    def tearDown(self):
        B2B._OBSERVE_TIMEOUT_S, B2B._OBSERVE_POLL_S = self._t, self._p

    def test_position_appears_on_later_poll_confirms(self):
        # first post-submit reconcile shows nothing; 3rd shows the position.
        mb = MockBroker(position_appears_on_attempt=3, filled_price=None)
        d = _run(mb)
        self.assertGreaterEqual(d["observation_attempts"], 3)
        self.assertTrue(d["position_observed"])
        self.assertFalse(d["observation_timeout"])
        self.assertTrue(d["lifecycle_confirmed"])
        self.assertEqual(len(mb.submit_calls), 1)   # still exactly one submit

    def test_no_position_before_timeout_flattens_but_not_confirmed(self):
        # position never appears -> timeout, flatten still called, not confirmed
        mb = MockBroker(position_appears_on_attempt=9999, filled_price=None,
                        post_submit_positions=[])
        d = _run(mb)
        self.assertTrue(d["observation_timeout"])
        self.assertFalse(d["position_observed"])
        self.assertTrue(d["flatten_called"])         # cleanup still runs
        self.assertFalse(d["lifecycle_confirmed"])
        self.assertEqual(len(mb.submit_calls), 1)

    def test_submit_called_once_even_with_polling(self):
        mb = MockBroker(position_appears_on_attempt=4, filled_price=None)
        _run(mb)
        self.assertEqual(len(mb.submit_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

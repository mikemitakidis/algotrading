#!/usr/bin/env python3
"""C3 (M21 safety-test repair) — B2b kill_switch_active=None real-module branch.

Closes audit R2-6: the production wiring (D2 passes kill_switch_active=None into
B2b.run_lifecycle) resolves the kill switch from the REAL bot.kill_switch module,
but that None->real-module branch was untested. A regression that made None mean
"inactive" would silence the kill switch in the supervised loop with no red test.

These tests exercise run_lifecycle with kill_switch_active=None and monkeypatch
bot.kill_switch.is_kill_switch_active to control what the real branch resolves.
The kill-switch check happens BEFORE any broker is constructed, so when it
resolves active the lifecycle returns early with NO broker action — no real
IBKRBroker, no connection, no gateway, no order. Paper env is set so
_assert_paper_mode() passes; no live mode is ever used.
"""
from __future__ import annotations

import os
import unittest

import bot.kill_switch as ks
from tools.paper_loop import m21_1extra_b2b_single_submit as b2b

_PAPER_PORT = 4002
_PAPER_ACCT = "DUP623346"


class _PaperEnvKillSwitch(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("BROKER", "IBKR_PORT", "IBKR_ACCOUNT")}
        os.environ["BROKER"] = "ibkr_paper"
        os.environ["IBKR_PORT"] = str(_PAPER_PORT)
        os.environ["IBKR_ACCOUNT"] = _PAPER_ACCT
        self._saved_ks = ks.is_kill_switch_active

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ks.is_kill_switch_active = self._saved_ks

    def _run(self, kill_switch_active, ks_module_returns=None, broker="SENTINEL"):
        if ks_module_returns is not None:
            ks.is_kill_switch_active = lambda: ks_module_returns
        # broker sentinel: if run_lifecycle ever tries to USE the broker, this
        # object will fail loudly (no real IBKR). For the kill-switch-active
        # path the broker is never touched.
        if broker == "SENTINEL":
            broker = _ExplodingBroker()
        return b2b.run_lifecycle(
            "AAPL", confirm=True, entry_price=100.0, stop_loss=99.0,
            target_price=102.0, kill_switch_active=kill_switch_active,
            broker=broker)


class _ExplodingBroker:
    """Any broker method call fails the test — proves no broker action was
    taken on the kill-switch-active path."""
    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(
                "broker.%s was called — a broker action happened despite the "
                "kill switch being active" % name)
        return _boom


class TestKillSwitchNoneResolvesFromRealModule(_PaperEnvKillSwitch):
    def test_none_resolves_active_from_real_module_blocks(self):
        # kill_switch_active=None + real module says ACTIVE -> blocked, no broker.
        r = self._run(kill_switch_active=None, ks_module_returns=True)
        self.assertTrue(r["kill_switch_active"])
        self.assertIn("kill switch active: no broker action taken", r["warnings"])
        self.assertFalse(r["lifecycle_confirmed"])
        self.assertFalse(r["entry_order_originated"])

    def test_none_resolves_inactive_from_real_module_proceeds_to_broker(self):
        # None + real module says INACTIVE -> the kill-switch gate passes, so
        # execution proceeds to the broker. With the exploding broker that
        # surfaces as an AssertionError, proving the gate did NOT block. We use
        # a benign broker here that fails account verification instead, so the
        # lifecycle stops safely AFTER the kill-switch gate without a real order.
        r = self._run(kill_switch_active=None, ks_module_returns=False,
                      broker=_UnverifiedBroker())
        self.assertFalse(r["kill_switch_active"])
        # proceeded past kill-switch gate -> hit account verification (False).
        self.assertFalse(r["account_verified"])
        self.assertFalse(r["lifecycle_confirmed"])
        self.assertTrue(any("account not verified" in w for w in r["warnings"]))

    def test_explicit_true_overrides_without_consulting_module(self):
        # explicit True must block regardless of the module (module set to
        # False to prove the explicit value is honoured, not the module).
        ks.is_kill_switch_active = lambda: (_ for _ in ()).throw(
            AssertionError("real module must NOT be consulted when explicit"))
        r = self._run(kill_switch_active=True)
        self.assertTrue(r["kill_switch_active"])
        self.assertIn("kill switch active: no broker action taken", r["warnings"])

    def test_regression_none_must_not_silently_mean_inactive(self):
        # THE R2-6 GUARD: if a regression made None -> inactive without consulting
        # the module, a module that says ACTIVE would be ignored and the broker
        # would be touched -> the exploding broker raises. This test passing
        # proves None genuinely consults the real module.
        r = self._run(kill_switch_active=None, ks_module_returns=True)
        # blocked (no exploding-broker call happened).
        self.assertTrue(r["kill_switch_active"])
        self.assertFalse(r["entry_order_originated"])


class _UnverifiedBroker:
    """A safe fake broker whose account verification fails, so the lifecycle
    stops right after the kill-switch gate without any order or connection."""
    def connection_status(self):
        return {"account_verified": False, "error": "test: not verified"}

    def reconcile(self):
        return {"positions": [], "open_orders": [], "warnings": []}


if __name__ == "__main__":
    unittest.main(verbosity=2)

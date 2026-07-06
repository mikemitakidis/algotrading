#!/usr/bin/env python3
"""D2 (M21 runtime hardening) — paper submit port==4002 guard.

Closes audit R2-3 (IBKR paper submit() lacked an explicit port==4002 guard).

The guard: in paper mode, submit() refuses (status='paper_port_refused') if the
resolved port is not PAPER_PORT (4002), BEFORE any gateway probe, connection, or
order placement. This is defense-in-depth: account verification remains an
independent downstream backstop and is not weakened.

No network, no real IBKR connection, no real order. Kill-switch and runtime
policy are monkeypatched to isolate the port-guard behaviour; env vars control
the resolved port. Live mode is never enabled.
"""
from __future__ import annotations

import os
import unittest

import bot.brokers.ibkr_broker as ib_mod
from bot.brokers.ibkr_broker import IBKRBroker, PAPER_PORT, LIVE_PORT
from bot.brokers.base import OrderIntent


def _intent():
    return OrderIntent(
        signal_id=1, symbol="AAPL", direction="long", route="IBKR",
        entry_price=100.0, stop_loss=99.0, target_price=102.0,
        valid_count=3, strategy_version=1, position_size=1, risk_usd=5.0,
    )


class _PaperSubmitBase(unittest.TestCase):
    """Isolate the port guard: force paper mode, stub kill-switch (inactive) and
    runtime policy (allow), and control IBKR_PORT via env. Never enables live."""

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("BROKER", "IBKR_PORT", "IBKR_ACCOUNT")}
        # paper mode (BROKER != 'ibkr_live')
        os.environ["BROKER"] = "ibkr_paper"
        os.environ["IBKR_ACCOUNT"] = "DUP623346"

        # stub kill-switch -> inactive, and runtime policy -> allow, so neither
        # short-circuits before the port guard.
        import bot.kill_switch as ks
        import bot.runtime_policy as rp
        self._saved_ks = ks.is_kill_switch_active
        self._saved_rp = rp.get_signal_only_reason
        ks.is_kill_switch_active = lambda: False
        rp.get_signal_only_reason = lambda name: (False, "allowed")
        # also patch the names imported inside submit() (function-local imports
        # resolve from the modules above, so patching the module attr suffices).

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import bot.kill_switch as ks
        import bot.runtime_policy as rp
        ks.is_kill_switch_active = self._saved_ks
        rp.get_signal_only_reason = self._saved_rp


class TestPaperPortGuard(_PaperSubmitBase):
    def test_paper_port_4001_refused_before_any_order(self):
        # misconfigured: paper mode but live port -> must refuse.
        os.environ["IBKR_PORT"] = str(LIVE_PORT)  # 4001
        broker = IBKRBroker()
        self.assertFalse(broker.is_live, "must be paper mode for this test")
        result = broker.submit(_intent())
        self.assertEqual(result.status, "paper_port_refused")
        self.assertIn(str(PAPER_PORT), result.reason)
        self.assertIn(str(LIVE_PORT), result.reason)

    def test_paper_port_4002_passes_guard_reaches_gateway_stage(self):
        # correct paper port -> guard passes; downstream gateway probe fails in
        # the sandbox (no gateway) -> connection_failed. Reaching that status
        # proves the port guard did NOT block a correctly-configured paper port.
        os.environ["IBKR_PORT"] = str(PAPER_PORT)  # 4002
        broker = IBKRBroker()
        self.assertFalse(broker.is_live)
        result = broker.submit(_intent())
        self.assertNotEqual(result.status, "paper_port_refused")
        # sandbox has no gateway -> connection_failed (or connect error), never
        # a real order.
        self.assertIn(result.status, ("connection_failed", "error"))

    def test_default_paper_port_is_4002_and_passes_guard(self):
        # IBKR_PORT unset -> _get_connection_params defaults to PAPER_PORT.
        os.environ.pop("IBKR_PORT", None)
        broker = IBKRBroker()
        self.assertFalse(broker.is_live)
        result = broker.submit(_intent())
        self.assertNotEqual(result.status, "paper_port_refused")


class TestGuardDoesNotWeakenOtherChecks(_PaperSubmitBase):
    def test_kill_switch_still_blocks_first(self):
        # even with a bad port, an active kill switch must block FIRST
        # (kill-switch is checked before the port guard).
        import bot.kill_switch as ks
        ks.is_kill_switch_active = lambda: True
        os.environ["IBKR_PORT"] = str(LIVE_PORT)
        broker = IBKRBroker()
        result = broker.submit(_intent())
        self.assertEqual(result.status, "kill_switch_active")

    def test_runtime_policy_skip_still_blocks_before_port_guard(self):
        import bot.runtime_policy as rp
        rp.get_signal_only_reason = lambda name: (True, "policy_skip_test")
        os.environ["IBKR_PORT"] = str(LIVE_PORT)
        broker = IBKRBroker()
        result = broker.submit(_intent())
        self.assertEqual(result.status, "signal_only_skipped")


class TestLiveModeUnaffected(_PaperSubmitBase):
    def test_live_mode_not_enabled_by_guard(self):
        # the guard is in the paper (else) branch; live mode still routes through
        # the live safety gate. With BROKER=ibkr_live and no live config, submit
        # must return live_safety_blocked (NOT paper_port_refused).
        os.environ["BROKER"] = "ibkr_live"
        # ensure no live config leaks in from env
        for k in ("IBKR_LIVE_ACCOUNT", "IBKR_LIVE_CONFIRMED", "IBKR_LIVE_PORT"):
            os.environ.pop(k, None)
        broker = IBKRBroker()
        self.assertTrue(broker.is_live)
        result = broker.submit(_intent())
        self.assertEqual(result.status, "live_safety_blocked")
        self.assertNotEqual(result.status, "paper_port_refused")


if __name__ == "__main__":
    unittest.main(verbosity=2)

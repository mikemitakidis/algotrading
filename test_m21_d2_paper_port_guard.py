#!/usr/bin/env python3
"""D2 (M21 runtime hardening) — paper submit port==4002 guard.

Closes audit R2-3 (IBKR paper submit() lacked an explicit port==4002 guard).

The guard: in paper mode, submit() refuses (status='paper_port_refused') if the
resolved port is not PAPER_PORT (4002), BEFORE any gateway probe, connection, or
order placement. Defense-in-depth: account verification remains an independent
downstream backstop and is not weakened.

FULL ISOLATION (no real IBKR, ever): these tests do NOT rely on the environment
lacking a gateway. Every test patches:
  - bot.brokers.ibkr_broker._gateway_available -> a recording fake (the good-
    port test returns False so submit stops at connection_failed; the bad-port
    tests' fake asserts if ever called, proving the guard short-circuits first);
  - IBKRBroker._connect -> a guard that raises if called, so no test can ever
    open a real IBKR connection even if a live gateway is present on the host.
Kill-switch and runtime policy are stubbed to isolate the port-guard behaviour.
Live mode is never enabled with real live config. No network, no real order.
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
    """Force paper mode; stub kill-switch (inactive) and runtime policy (allow);
    and HARD-ISOLATE the network path: _connect always raises if reached, and
    _gateway_available is replaced per-test. Nothing here can touch a real
    gateway even if one is running on the host."""

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in
                           ("BROKER", "IBKR_PORT", "IBKR_ACCOUNT")}
        os.environ["BROKER"] = "ibkr_paper"
        os.environ["IBKR_ACCOUNT"] = "DUP623346"

        import bot.kill_switch as ks
        import bot.runtime_policy as rp
        self._saved_ks = ks.is_kill_switch_active
        self._saved_rp = rp.get_signal_only_reason
        ks.is_kill_switch_active = lambda: False
        rp.get_signal_only_reason = lambda name: (False, "allowed")

        # HARD network isolation.
        self._saved_gw = ib_mod._gateway_available
        self._saved_connect = IBKRBroker._connect
        self.gateway_calls = []

        def _connect_must_not_be_called(_self):
            raise AssertionError(
                "IBKRBroker._connect() was called — a test reached the real "
                "connection path. Port guard / gateway isolation failed.")
        IBKRBroker._connect = _connect_must_not_be_called

        # default: a gateway fake that RECORDS and refuses (returns False) —
        # individual tests override this via _set_gateway_asserts_if_called.
        def _gw_record_refuse(host, port, timeout=3.0):
            self.gateway_calls.append((host, port))
            return False
        ib_mod._gateway_available = _gw_record_refuse

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
        ib_mod._gateway_available = self._saved_gw
        IBKRBroker._connect = self._saved_connect

    def _set_gateway_asserts_if_called(self):
        def _gw_must_not_be_called(host, port, timeout=3.0):
            self.gateway_calls.append((host, port))
            raise AssertionError(
                "_gateway_available() was called — the port guard did NOT "
                "short-circuit before the gateway probe.")
        ib_mod._gateway_available = _gw_must_not_be_called


class TestPaperPortGuard(_PaperSubmitBase):
    def test_paper_port_4001_refused_before_gateway_or_connect(self):
        # a bad port must be refused BEFORE _gateway_available and _connect.
        self._set_gateway_asserts_if_called()
        os.environ["IBKR_PORT"] = str(LIVE_PORT)  # 4001
        broker = IBKRBroker()
        self.assertFalse(broker.is_live, "must be paper mode")
        result = broker.submit(_intent())
        self.assertEqual(result.status, "paper_port_refused")
        self.assertIn(str(PAPER_PORT), result.reason)
        self.assertIn(str(LIVE_PORT), result.reason)
        # proof: neither the gateway probe nor connect was reached.
        self.assertEqual(self.gateway_calls, [],
                         "_gateway_available must NOT be called for a bad port")

    def test_paper_port_4002_passes_guard_stops_at_gateway_sentinel(self):
        # correct port -> guard passes -> reaches the PATCHED gateway sentinel
        # (returns False) -> connection_failed. _connect is never called.
        os.environ["IBKR_PORT"] = str(PAPER_PORT)  # 4002
        broker = IBKRBroker()
        self.assertFalse(broker.is_live)
        result = broker.submit(_intent())
        self.assertNotEqual(result.status, "paper_port_refused")
        self.assertEqual(result.status, "connection_failed")
        # proof: the guard let us reach the gateway probe with the paper port...
        self.assertEqual(self.gateway_calls, [("127.0.0.1", PAPER_PORT)])
        # ...and _connect was never reached (it would have raised AssertionError).

    def test_default_paper_port_is_4002_and_passes_guard(self):
        os.environ.pop("IBKR_PORT", None)  # default -> PAPER_PORT
        broker = IBKRBroker()
        self.assertFalse(broker.is_live)
        result = broker.submit(_intent())
        self.assertNotEqual(result.status, "paper_port_refused")
        self.assertEqual(result.status, "connection_failed")
        self.assertEqual(self.gateway_calls, [("127.0.0.1", PAPER_PORT)])


class TestGuardDoesNotWeakenOtherChecks(_PaperSubmitBase):
    def test_kill_switch_still_blocks_first(self):
        # kill switch is checked before the port guard; a bad port must not be
        # reached, and neither gateway nor connect is touched.
        self._set_gateway_asserts_if_called()
        import bot.kill_switch as ks
        ks.is_kill_switch_active = lambda: True
        os.environ["IBKR_PORT"] = str(LIVE_PORT)
        broker = IBKRBroker()
        result = broker.submit(_intent())
        self.assertEqual(result.status, "kill_switch_active")
        self.assertEqual(self.gateway_calls, [])

    def test_runtime_policy_skip_still_blocks_before_port_guard(self):
        self._set_gateway_asserts_if_called()
        import bot.runtime_policy as rp
        rp.get_signal_only_reason = lambda name: (True, "policy_skip_test")
        os.environ["IBKR_PORT"] = str(LIVE_PORT)
        broker = IBKRBroker()
        result = broker.submit(_intent())
        self.assertEqual(result.status, "signal_only_skipped")
        self.assertEqual(self.gateway_calls, [])


class TestLiveModeUnaffected(_PaperSubmitBase):
    def test_live_mode_not_enabled_by_guard(self):
        # live mode with no live config -> live_safety_blocked (before the paper
        # guard, before gateway/connect). Never paper_port_refused.
        self._set_gateway_asserts_if_called()
        os.environ["BROKER"] = "ibkr_live"
        for k in ("IBKR_LIVE_ACCOUNT", "IBKR_LIVE_CONFIRMED", "IBKR_LIVE_PORT"):
            os.environ.pop(k, None)
        broker = IBKRBroker()
        self.assertTrue(broker.is_live)
        result = broker.submit(_intent())
        self.assertEqual(result.status, "live_safety_blocked")
        self.assertNotEqual(result.status, "paper_port_refused")
        self.assertEqual(self.gateway_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

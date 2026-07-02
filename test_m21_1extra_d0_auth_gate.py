#!/usr/bin/env python3
"""M21.1extra-D0 Commit 1 — reader authorization tests.

Proves determine_signal_only_reason() enforces the two-layer, time-boxed,
per-lane authorization model:
  - global.auto_trading_enabled + valid unexpired global expiry are prerequisites
  - ibkr_paper / ibkr_live resolve to their OWN lean lane blocks (NOT the legacy
    shared `ibkr` block), each with its own enable + valid unexpired expiry
  - global OR lane kill switch overrides
  - the legacy `ibkr.auto_trading_enabled` can never authorize a lane
  - bare broker_name "ibkr" is unsupported/denied
Fail-closed on every missing/malformed/expired/ambiguous case.
"""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone, timedelta

from bot.broker_allocation import DEFAULT_POLICY
from bot.etoro.signal_only_broker import (
    determine_signal_only_reason as R,
    REASON_GLOBAL_KILL_SWITCH, REASON_GLOBAL_DISABLED,
    REASON_BROKER_DISABLED, REASON_BROKER_KILL_SWITCH,
    REASON_GLOBAL_EXPIRY_MISSING, REASON_GLOBAL_EXPIRY_MALFORMED,
    REASON_GLOBAL_EXPIRED, REASON_LANE_EXPIRY_MISSING,
    REASON_LANE_EXPIRY_MALFORMED, REASON_LANE_EXPIRED,
)


def _future(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _policy(*, global_enabled=True, global_until="future",
            lane="ibkr_paper", lane_enabled=True, lane_until="future",
            global_kill=False, lane_kill=False):
    p = copy.deepcopy(DEFAULT_POLICY)
    p["global"]["auto_trading_enabled"] = global_enabled
    p["global"]["kill_switch"] = global_kill
    p["global"]["auto_trading_enabled_until_utc"] = {
        "future": _future(), "past": _past(), "missing": None,
        "malformed": "not-a-date"}[global_until]
    if lane:
        p[lane]["auto_trading_enabled"] = lane_enabled
        p[lane]["kill_switch"] = lane_kill
        p[lane]["auto_trading_enabled_until_utc"] = {
            "future": _future(), "past": _past(), "missing": None,
            "malformed": "not-a-date"}[lane_until]
    return p


class TestGlobalTimeBox(unittest.TestCase):
    def test_1_both_active_allows_paper(self):
        skip, reason = R(_policy(), "ibkr_paper")
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    def test_2_global_expiry_missing_blocks(self):
        skip, reason = R(_policy(global_until="missing"), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_EXPIRY_MISSING)

    def test_3_global_expiry_malformed_blocks(self):
        skip, reason = R(_policy(global_until="malformed"), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_EXPIRY_MALFORMED)

    def test_4_global_expired_blocks(self):
        skip, reason = R(_policy(global_until="past"), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_EXPIRED)

    def test_global_disabled_blocks(self):
        skip, reason = R(_policy(global_enabled=False), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_DISABLED)


class TestLaneTimeBox(unittest.TestCase):
    def test_5_lane_expiry_missing_blocks(self):
        skip, reason = R(_policy(lane_until="missing"), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_LANE_EXPIRY_MISSING)

    def test_6_lane_expiry_malformed_blocks(self):
        skip, reason = R(_policy(lane_until="malformed"), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_LANE_EXPIRY_MALFORMED)

    def test_7_lane_expired_blocks(self):
        skip, reason = R(_policy(lane_until="past"), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_LANE_EXPIRED)

    def test_lane_disabled_blocks(self):
        skip, reason = R(_policy(lane_enabled=False), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_DISABLED)


class TestKillSwitchOverrides(unittest.TestCase):
    def test_8_global_kill_overrides_active_lane(self):
        skip, reason = R(_policy(global_kill=True), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_KILL_SWITCH)

    def test_9_lane_kill_overrides_active_global(self):
        skip, reason = R(_policy(lane_kill=True), "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_KILL_SWITCH)


class TestLaneSeparation(unittest.TestCase):
    def test_10_ibkr_live_disabled_by_default(self):
        # paper enabled, live left at default (disabled) -> live blocked
        skip, reason = R(_policy(lane="ibkr_paper"), "ibkr_live")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_DISABLED)

    def test_10b_ibkr_live_allowed_only_with_own_valid_expiry(self):
        p = _policy(lane="ibkr_live")  # enables the live lane with valid window
        skip, reason = R(p, "ibkr_live")
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    def test_11_legacy_ibkr_flag_cannot_authorize_paper(self):
        # legacy ibkr.auto_trading_enabled True, lanes left default disabled
        p = _policy(lane="ibkr_paper", lane_enabled=False)
        p["ibkr"]["auto_trading_enabled"] = True
        skip, reason = R(p, "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_DISABLED)

    def test_11b_legacy_ibkr_flag_cannot_authorize_live(self):
        p = _policy(lane="ibkr_paper")   # paper enabled; live default disabled
        p["ibkr"]["auto_trading_enabled"] = True
        skip, reason = R(p, "ibkr_live")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_DISABLED)

    def test_12_bare_ibkr_broker_name_denied(self):
        # bare "ibkr" is not an authorization lane; must be denied
        skip, reason = R(_policy(), "ibkr")
        self.assertTrue(skip)
        self.assertNotEqual(reason, "")

    def test_paper_broker_only_needs_global(self):
        # 'paper' has no lane; global gates suffice
        skip, reason = R(_policy(), "paper")
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    def test_naive_expiry_treated_as_malformed(self):
        # a tz-naive expiry string is ambiguous -> refuse (malformed)
        p = _policy()
        p["global"]["auto_trading_enabled_until_utc"] = \
            datetime.now().replace(tzinfo=None).isoformat()  # naive
        skip, reason = R(p, "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_EXPIRY_MALFORMED)


if __name__ == "__main__":
    unittest.main(verbosity=2)

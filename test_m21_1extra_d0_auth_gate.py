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


class TestSignalOnlyBrokerAcceptsD0Reasons(unittest.TestCase):
    """Blocker 1: every D0 reason the reader can return must be accepted by
    SignalOnlyBroker so the factory safely wraps instead of crashing."""

    def test_all_d0_reasons_in_valid_reasons(self):
        from bot.etoro.signal_only_broker import (
            VALID_REASONS, REASON_LANE_UNSUPPORTED)
        d0 = {REASON_GLOBAL_EXPIRY_MISSING, REASON_GLOBAL_EXPIRY_MALFORMED,
              REASON_GLOBAL_EXPIRED, REASON_LANE_EXPIRY_MISSING,
              REASON_LANE_EXPIRY_MALFORMED, REASON_LANE_EXPIRED,
              REASON_LANE_UNSUPPORTED}
        self.assertTrue(d0.issubset(VALID_REASONS))

    def test_signalonly_constructs_with_each_d0_reason(self):
        from bot.etoro.signal_only_broker import (
            SignalOnlyBroker, REASON_LANE_UNSUPPORTED)

        class _Dummy:
            name = "ibkr_paper"

            def submit(self, intent):
                raise AssertionError("must not be called")

        for reason in (REASON_GLOBAL_EXPIRY_MISSING,
                       REASON_GLOBAL_EXPIRY_MALFORMED, REASON_GLOBAL_EXPIRED,
                       REASON_LANE_EXPIRY_MISSING, REASON_LANE_EXPIRY_MALFORMED,
                       REASON_LANE_EXPIRED, REASON_LANE_UNSUPPORTED):
            # must NOT raise
            w = SignalOnlyBroker(_Dummy(), reason=reason)
            self.assertIsNotNone(w)

    def test_expired_policy_wraps_safely_end_to_end(self):
        # reader returns an expiry reason; that reason must construct a wrapper.
        from bot.etoro.signal_only_broker import SignalOnlyBroker

        class _Dummy:
            name = "ibkr_paper"

            def submit(self, intent):
                raise AssertionError("must not be called")

        skip, reason = R(_policy(global_until="missing"), "ibkr_paper")
        self.assertTrue(skip)
        # the factory-style path: wrap with the reader's reason — no crash
        w = SignalOnlyBroker(_Dummy(), reason=reason)
        self.assertIsNotNone(w)


class TestIsAutoTradingAllowedD0(unittest.TestCase):
    """Blocker 2: is_auto_trading_allowed() must enforce the same D0 contract."""

    def _pol(self, **over):
        import copy
        p = copy.deepcopy(DEFAULT_POLICY)
        p["global"]["auto_trading_enabled"] = True
        p["global"]["auto_trading_enabled_until_utc"] = _future()
        for k, v in over.items():
            d = p
            parts = k.split(".")
            for pp in parts[:-1]:
                d = d[pp]
            d[parts[-1]] = v
        return p

    def test_1_legacy_ibkr_cannot_authorize_paper(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol()
        p["ibkr"]["auto_trading_enabled"] = True   # legacy flag on
        ok, reason = A(p, "ibkr_paper")
        self.assertFalse(ok)

    def test_2_legacy_ibkr_cannot_authorize_live(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol()
        p["ibkr"]["auto_trading_enabled"] = True
        ok, reason = A(p, "ibkr_live")
        self.assertFalse(ok)

    def test_3_paper_valid_global_and_lane_allows(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol()
        p["ibkr_paper"]["auto_trading_enabled"] = True
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = _future()
        ok, reason = A(p, "ibkr_paper")
        self.assertTrue(ok, msg=reason)

    def test_4_ibkr_live_disabled_blocks(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol()
        p["ibkr_paper"]["auto_trading_enabled"] = True
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = _future()
        ok, reason = A(p, "ibkr_live")
        self.assertFalse(ok)

    def test_5_global_expiry_expired_blocks(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol(**{"global.auto_trading_enabled_until_utc": _past()})
        p["ibkr_paper"]["auto_trading_enabled"] = True
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = _future()
        ok, reason = A(p, "ibkr_paper")
        self.assertFalse(ok)

    def test_5b_global_expiry_missing_blocks(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol(**{"global.auto_trading_enabled_until_utc": None})
        p["ibkr_paper"]["auto_trading_enabled"] = True
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = _future()
        ok, reason = A(p, "ibkr_paper")
        self.assertFalse(ok)

    def test_6_lane_expiry_expired_blocks(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol()
        p["ibkr_paper"]["auto_trading_enabled"] = True
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = _past()
        ok, reason = A(p, "ibkr_paper")
        self.assertFalse(ok)

    def test_6b_lane_expiry_missing_blocks(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        p = self._pol()
        p["ibkr_paper"]["auto_trading_enabled"] = True
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = None
        ok, reason = A(p, "ibkr_paper")
        self.assertFalse(ok)

    def test_7_bare_ibkr_denied(self):
        from bot.broker_allocation import is_auto_trading_allowed as A
        ok, reason = A(self._pol(), "ibkr")
        self.assertFalse(ok)


class TestUntilUtcStrictness(unittest.TestCase):
    """UTC honesty: a field named *_utc must reject naive and non-UTC offsets."""

    def test_validator_accepts_utc_and_z(self):
        from bot.broker_allocation import _is_valid_until_utc
        self.assertTrue(_is_valid_until_utc(None))
        self.assertTrue(_is_valid_until_utc("2026-07-03T10:00:00+00:00"))
        self.assertTrue(_is_valid_until_utc("2026-07-03T10:00:00Z"))

    def test_validator_rejects_naive_and_nonutc(self):
        from bot.broker_allocation import _is_valid_until_utc
        self.assertFalse(_is_valid_until_utc("2026-07-03T10:00:00"))       # naive
        self.assertFalse(_is_valid_until_utc("2026-07-03T10:00:00+02:00"))  # +02
        self.assertFalse(_is_valid_until_utc("nope"))

    def test_reader_rejects_nonutc_offset_lane(self):
        # a future +02:00 lane expiry must be treated as malformed, not accepted
        p = _policy()
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = (
            (datetime.now(timezone.utc) + timedelta(hours=1))
            .astimezone(timezone(timedelta(hours=2))).isoformat())
        skip, reason = R(p, "ibkr_paper")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_LANE_EXPIRY_MALFORMED)

    def test_reader_accepts_z_suffix(self):
        p = _policy()
        z = ((datetime.now(timezone.utc) + timedelta(hours=1))
             .strftime("%Y-%m-%dT%H:%M:%S") + "Z")
        p["ibkr_paper"]["auto_trading_enabled_until_utc"] = z
        p["global"]["auto_trading_enabled_until_utc"] = z
        skip, reason = R(p, "ibkr_paper")
        self.assertFalse(skip)


if __name__ == "__main__":
    unittest.main(verbosity=2)

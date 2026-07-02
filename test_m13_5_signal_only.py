"""M13.5.B — SignalOnlyBroker + determine_signal_only_reason tests.

Invariant under test: when policy disables auto-trading, the wrapper
records the intent without ever calling the wrapped broker. Telegram
alerting (which runs in main.py after broker.submit()) is therefore
unaffected because submit() still returns a normal OrderResult.
"""
from __future__ import annotations

import copy
import unittest

from bot.broker_allocation import DEFAULT_POLICY
from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult
from bot.etoro.signal_only_broker import (
    SignalOnlyBroker,
    determine_signal_only_reason,
    REASON_BROKER_DISABLED,
    REASON_BROKER_KILL_SWITCH,
    REASON_BROKER_NOT_ALLOWED,
    REASON_ETORO_LIVE_DISABLED,
    REASON_GLOBAL_DISABLED,
    REASON_GLOBAL_KILL_SWITCH,
    REASON_POLICY_MISSING,
)


class _SpyBroker(BrokerAdapter):
    def __init__(self):
        self.submit_calls = 0
    @property
    def name(self):
        return "spy"
    def submit(self, intent):
        self.submit_calls += 1
        return OrderResult(intent=intent, status="accepted")
    def get_positions(self):
        return [{"symbol": "SPY"}]


def _intent():
    return OrderIntent(
        signal_id=1, symbol="SPY", direction="long", route="ETORO",
        entry_price=100.0, stop_loss=95.0, target_price=110.0,
        valid_count=4, strategy_version=1,
    )


def _future_utc(hours=1):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _enabled_policy(broker="etoro_real"):
    p = copy.deepcopy(DEFAULT_POLICY)
    p["global"]["auto_trading_enabled"] = True
    p["global"]["auto_trading_enabled_until_utc"] = _future_utc()
    p["etoro"]["auto_trading_enabled"] = True
    # D0: enable the ibkr lanes explicitly with valid unexpired windows (this
    # helper intentionally builds an ALLOW policy). Never relies on the legacy
    # ibkr block for lane authorization.
    p["ibkr"]["auto_trading_enabled"] = True
    p["ibkr_paper"]["auto_trading_enabled"] = True
    p["ibkr_paper"]["auto_trading_enabled_until_utc"] = _future_utc()
    p["ibkr_live"]["auto_trading_enabled"] = True
    p["ibkr_live"]["auto_trading_enabled_until_utc"] = _future_utc()
    p["routing"]["allowed_brokers"] = ["paper", "ibkr_paper", "ibkr_live",
                                       "etoro_paper", "etoro_real"]
    p["routing"]["etoro_live_enabled"] = True
    return p


class TestSignalOnlyWrapper(unittest.TestCase):
    def test_never_calls_wrapped(self):
        spy = _SpyBroker()
        w = SignalOnlyBroker(spy, reason=REASON_GLOBAL_DISABLED)
        result = w.submit(_intent())
        self.assertEqual(spy.submit_calls, 0)
        self.assertEqual(result.status, "signal_only_skipped")
        self.assertEqual(result.reason, REASON_GLOBAL_DISABLED)

    def test_name_includes_wrapped(self):
        w = SignalOnlyBroker(_SpyBroker(), reason=REASON_GLOBAL_DISABLED)
        self.assertEqual(w.name, "signal_only:spy")

    def test_is_live_false(self):
        w = SignalOnlyBroker(_SpyBroker(), reason=REASON_GLOBAL_DISABLED)
        self.assertFalse(w.is_live)

    def test_get_positions_passes_through(self):
        w = SignalOnlyBroker(_SpyBroker(), reason=REASON_GLOBAL_DISABLED)
        self.assertEqual(w.get_positions(), [{"symbol": "SPY"}])

    def test_unknown_reason_rejected(self):
        with self.assertRaises(ValueError):
            SignalOnlyBroker(_SpyBroker(), reason="made_up_reason")

    def test_requires_wrapped(self):
        with self.assertRaises(ValueError):
            SignalOnlyBroker(None, reason=REASON_GLOBAL_DISABLED)


class TestDetermineReason(unittest.TestCase):
    def test_enabled_etoro_real_not_skipped(self):
        skip, reason = determine_signal_only_reason(_enabled_policy(),
                                                    "etoro_real")
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    def test_policy_missing(self):
        skip, reason = determine_signal_only_reason(None, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_POLICY_MISSING)

    def test_global_kill_switch(self):
        p = _enabled_policy(); p["global"]["kill_switch"] = True
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_KILL_SWITCH)

    def test_global_disabled(self):
        p = _enabled_policy(); p["global"]["auto_trading_enabled"] = False
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_DISABLED)

    def test_broker_not_allowed(self):
        p = _enabled_policy()
        p["routing"]["allowed_brokers"] = ["paper"]
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_NOT_ALLOWED)

    def test_etoro_live_disabled_strict_identity(self):
        # etoro_live_enabled truthy-but-not-True must still skip.
        p = _enabled_policy()
        p["routing"]["etoro_live_enabled"] = 1   # truthy, not True
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_ETORO_LIVE_DISABLED)

    def test_etoro_live_disabled_false(self):
        p = _enabled_policy()
        p["routing"]["etoro_live_enabled"] = False
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_ETORO_LIVE_DISABLED)

    def test_broker_kill_switch(self):
        p = _enabled_policy(); p["etoro"]["kill_switch"] = True
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_KILL_SWITCH)

    def test_broker_disabled(self):
        p = _enabled_policy(); p["etoro"]["auto_trading_enabled"] = False
        skip, reason = determine_signal_only_reason(p, "etoro_real")
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_DISABLED)

    def test_paper_only_global_gates(self):
        # paper has no broker block — only global gates apply.
        p = _enabled_policy()
        p["routing"]["allowed_brokers"].append("paper")
        skip, reason = determine_signal_only_reason(p, "paper")
        self.assertFalse(skip)

    def test_ibkr_live_enabled(self):
        p = _enabled_policy()
        skip, reason = determine_signal_only_reason(p, "ibkr_live")
        self.assertFalse(skip)


if __name__ == "__main__":
    unittest.main(verbosity=2)

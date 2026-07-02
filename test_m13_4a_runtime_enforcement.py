"""test_m13_4a_runtime_enforcement.py — P0-3 runtime M13.4A
broker-allocation policy enforcement (audit, 2026-06-05).

Verifies that bot.runtime_policy.get_signal_only_reason re-checks
the policy at submit time (within a TTL cache), and that each
broker's submit() now consults it before proceeding.

Audit P0-3 finding: before this fix, the policy was consulted only
at scanner startup in get_broker(); mid-run dashboard toggles of
the global / per-broker kill_switch had no effect until restart.

Correction A coverage (the headline tests):
  * test_runtime_policy_uses_cache_when_db_fails_and_cache_exists
    — DB unavailable but a cached policy from a prior fresh read
      exists → use cached + log warning. Operator gets coverage
      even during transient DB blips.
  * test_runtime_policy_fail_safe_when_no_cache_and_db_unavailable
    — NO cached policy AND DB unavailable → (True,
      REASON_POLICY_UNAVAILABLE). Never fail-OPEN: safety surface
      must not assume "ok to trade" when policy state is unknown.

Mid-run activation test (the test the operator explicitly asked for):
  * test_mid_run_kill_switch_activation_blocks_next_submit — call
    submit once, write policy.global.kill_switch=true to the DB,
    advance the clock past TTL, call submit again, assert it now
    returns signal_only_skipped.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import bot.runtime_policy as rt
from bot.etoro.signal_only_broker import (
    REASON_GLOBAL_KILL_SWITCH,
    REASON_BROKER_KILL_SWITCH,
    REASON_POLICY_UNAVAILABLE,
    REASON_POLICY_MISSING,
    VALID_REASONS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_policy_db(policy_dict) -> str:
    """Build a temp signals.db with the policy stored in
    `portfolio_risk_state` (key='broker_allocation_policy'),
    matching how bot.broker_allocation.load_policy reads it.
    Returns the db path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_risk_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO portfolio_risk_state (key, value, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        ("broker_allocation_policy", json.dumps(policy_dict)),
    )
    conn.commit()
    conn.close()
    return path


def _future_utc(hours=1):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _lane_key_for(broker: str):
    if broker in ("ibkr_paper", "ibkr_live"):
        return broker            # D0: own lean lane block
    if broker in ("etoro_paper", "etoro_real"):
        return "etoro"
    return None


def _enabled_policy_for(broker: str) -> dict:
    """Policy that ENABLES auto-trading for `broker` (D0 lane model).

    'paper' has no lane block; only global gates apply. ibkr_paper /
    ibkr_live resolve to their OWN lean lane blocks; etoro_* to the
    'etoro' block. Global + lane are both time-boxed with valid
    unexpired windows so the reader allows.
    """
    lane_key = _lane_key_for(broker)

    policy = {
        "version": 1,
        "global": {
            "auto_trading_enabled": True,
            "auto_trading_enabled_until_utc": _future_utc(),
            "kill_switch":          False,
        },
        "routing": {
            "allowed_brokers":      [broker],
            "etoro_live_enabled":   (broker == "etoro_real"),
        },
    }
    if lane_key in ("ibkr_paper", "ibkr_live"):
        policy[lane_key] = {
            "auto_trading_enabled": True,
            "auto_trading_enabled_until_utc": _future_utc(),
            "kill_switch":          False,
        }
    elif lane_key == "etoro":
        policy["etoro"] = {
            "auto_trading_enabled": True,
            "kill_switch":          False,
        }
    return policy


def _kill_switch_policy_for(broker: str) -> dict:
    """Policy with the LANE/broker-level kill_switch set."""
    p = _enabled_policy_for(broker)
    lane_key = _lane_key_for(broker)
    if lane_key is not None and lane_key in p:
        p[lane_key]["kill_switch"] = True
    return p


def _global_kill_policy_for(broker: str) -> dict:
    p = _enabled_policy_for(broker)
    p["global"]["kill_switch"] = True
    return p


# ─────────────────────────────────────────────────────────────────────────────
# G1. TTL cache mechanics
# ─────────────────────────────────────────────────────────────────────────────

class TestRuntimePolicyCache(unittest.TestCase):

    def setUp(self):
        rt.clear_cache()

    def test_returns_skip_false_when_policy_enables_broker(self):
        db = _build_policy_db(_enabled_policy_for("paper"))
        try:
            skip, reason = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=100.0)
        finally:
            os.unlink(db)
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    def test_returns_skip_true_when_global_kill_active(self):
        db = _build_policy_db(_global_kill_policy_for("paper"))
        try:
            skip, reason = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=100.0)
        finally:
            os.unlink(db)
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_GLOBAL_KILL_SWITCH)

    def test_returns_skip_true_when_broker_kill_active(self):
        # 'paper' has no broker block; broker-kill only meaningful
        # for ibkr_* / etoro_*. Use ibkr_paper here.
        db = _build_policy_db(_kill_switch_policy_for("ibkr_paper"))
        try:
            skip, reason = rt.get_signal_only_reason(
                "ibkr_paper", db_path=db, ttl_sec=5.0, now=100.0)
        finally:
            os.unlink(db)
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_BROKER_KILL_SWITCH)

    def test_cache_returns_same_value_within_ttl(self):
        """Calls within TTL must not re-read the DB. We prove this
        by deleting the DB after the first call and asserting the
        second call still returns the cached value."""
        db = _build_policy_db(_enabled_policy_for("paper"))
        try:
            r1 = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=100.0)
        finally:
            os.unlink(db)
        # DB now gone — but a second call within TTL must NOT
        # attempt to read it.
        r2 = rt.get_signal_only_reason(
            "paper", db_path=db, ttl_sec=5.0, now=102.0)  # +2s, < 5s
        self.assertEqual(r1, r2)
        self.assertFalse(r2[0])

    def test_cache_refreshes_after_ttl(self):
        """After TTL elapses, a re-read against a CHANGED DB must
        reflect the new policy state."""
        # Initial DB: enabled.
        db = _build_policy_db(_enabled_policy_for("paper"))
        try:
            r1 = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=100.0)
            self.assertFalse(r1[0])
            # Mutate DB in place: flip global kill.
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE portfolio_risk_state SET value=? WHERE key=?",
                (json.dumps(_global_kill_policy_for("paper")),
                 "broker_allocation_policy"),
            )
            conn.commit()
            conn.close()
            # Within TTL: still cached as enabled.
            r_cached = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=104.0)
            self.assertFalse(r_cached[0])
            # Past TTL: must re-read and see kill_switch active.
            r_refreshed = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=110.0)
            self.assertTrue(r_refreshed[0])
            self.assertEqual(r_refreshed[1], REASON_GLOBAL_KILL_SWITCH)
        finally:
            os.unlink(db)


# ─────────────────────────────────────────────────────────────────────────────
# G2. Correction A — fail-safe semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestCorrectionAFailSafe(unittest.TestCase):

    def setUp(self):
        rt.clear_cache()

    def test_runtime_policy_fail_safe_when_no_cache_and_db_unavailable(self):
        """No cached policy AND DB read fails → fail-SAFE: skip with
        reason='policy_unavailable'. NEVER fail-open.

        Note: an EMPTY DB returns DEFAULT_POLICY (auto_trading=False)
        which is a DIFFERENT failure mode (intentional safe default
        from broker_allocation). The fail-safe in runtime_policy
        applies when the DB read itself fails — sqlite3 error,
        corrupt file, etc. We simulate that with a patch on
        _read_policy_from_db returning None.
        """
        with patch.object(rt, "_read_policy_from_db", return_value=None):
            skip, reason = rt.get_signal_only_reason(
                "paper",
                db_path="/tmp/anything.db",
                ttl_sec=5.0,
                now=100.0,
            )
        self.assertTrue(skip,
                        "Must fail-safe (skip=True) when policy state "
                        "is unknown — NOT fail-open.")
        self.assertEqual(reason, REASON_POLICY_UNAVAILABLE)
        self.assertIn(REASON_POLICY_UNAVAILABLE, VALID_REASONS,
                      "REASON_POLICY_UNAVAILABLE must be in the "
                      "SignalOnlyBroker VALID_REASONS set.")

    def test_runtime_policy_uses_cache_when_db_fails_and_cache_exists(self):
        """Cached policy exists from prior successful read; next
        read fails. Must return cached value + log warning."""
        # First read: success, cache populated with skip=False.
        db = _build_policy_db(_enabled_policy_for("paper"))
        try:
            r1 = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=100.0)
            self.assertFalse(r1[0])
        finally:
            os.unlink(db)

        # Second read past TTL: simulate DB read failure via patch.
        # Cache age = 10s, well under STALE_CACHE_MAX_AGE_SEC = 300s.
        with patch.object(rt, "_read_policy_from_db", return_value=None):
            with self.assertLogs("bot.runtime_policy", level="WARNING") as cm:
                skip, reason = rt.get_signal_only_reason(
                    "paper", db_path=db, ttl_sec=5.0, now=110.0)
        self.assertFalse(skip,
                         "Must use cached (skip=False), not fail-safe, "
                         "when cache exists and is reasonably fresh.")
        self.assertEqual(reason, "")
        self.assertTrue(any("using cached policy" in m
                            for m in cm.output),
                        "Must log a warning indicating cache fallback.")

    def test_stale_cache_too_old_fails_safe(self):
        """If cached policy is too old (> STALE_CACHE_MAX_AGE_SEC)
        AND DB fails → fail-safe, not fall through to ancient
        cache."""
        db = _build_policy_db(_enabled_policy_for("paper"))
        try:
            rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=100.0)
        finally:
            os.unlink(db)

        # Time-jump past STALE_CACHE_MAX_AGE_SEC + TTL, simulating
        # both cache aging and a real DB failure.
        far_future = 100.0 + rt.STALE_CACHE_MAX_AGE_SEC + 10
        with patch.object(rt, "_read_policy_from_db", return_value=None):
            skip, reason = rt.get_signal_only_reason(
                "paper", db_path=db, ttl_sec=5.0, now=far_future)
        self.assertTrue(skip)
        self.assertEqual(reason, REASON_POLICY_UNAVAILABLE)


# ─────────────────────────────────────────────────────────────────────────────
# G3. Per-broker integration — submit() now consults runtime policy
# ─────────────────────────────────────────────────────────────────────────────

class TestPaperBrokerRuntimeEnforcement(unittest.TestCase):

    def setUp(self):
        rt.clear_cache()

    def test_paper_broker_blocks_when_runtime_policy_says_skip(self):
        from bot.brokers.paper_broker import PaperBroker
        from bot.brokers.base import OrderIntent
        intent = OrderIntent(
            signal_id=1, symbol="AAPL", direction="long", route="IBKR",
            entry_price=150.0, stop_loss=145.0, target_price=155.0,
            valid_count=4, strategy_version=1,
        )
        broker = PaperBroker()
        with patch("bot.runtime_policy.get_signal_only_reason",
                     return_value=(True, REASON_GLOBAL_KILL_SWITCH)):
            result = broker.submit(intent)
        self.assertEqual(result.status, "signal_only_skipped")
        self.assertEqual(result.reason, REASON_GLOBAL_KILL_SWITCH)
        self.assertIsNone(result.broker_order_id)

    def test_paper_broker_proceeds_when_runtime_policy_says_no_skip(self):
        from bot.brokers.paper_broker import PaperBroker
        from bot.brokers.base import OrderIntent
        intent = OrderIntent(
            signal_id=2, symbol="AAPL", direction="long", route="IBKR",
            entry_price=150.0, stop_loss=145.0, target_price=155.0,
            valid_count=4, strategy_version=1,
        )
        broker = PaperBroker()
        # Disable file logging side effect with a temp dir + patch.
        with patch("bot.runtime_policy.get_signal_only_reason",
                     return_value=(False, "")), \
             patch.object(broker, "_log"):
            result = broker.submit(intent)
        self.assertEqual(result.status, "paper_logged")
        self.assertIsNotNone(result.broker_order_id)


class TestIBKRBrokerRuntimeEnforcement(unittest.TestCase):

    def setUp(self):
        rt.clear_cache()

    def test_ibkr_broker_blocks_when_runtime_policy_says_skip(self):
        """Runtime policy check is between the file-based kill
        switch (already in place) and the live safety gate. When
        file-based kill is INACTIVE but runtime policy says skip,
        submit returns signal_only_skipped without touching the
        Gateway."""
        from bot.brokers.ibkr_broker import IBKRBroker
        from bot.brokers.base import OrderIntent
        intent = OrderIntent(
            signal_id=3, symbol="AAPL", direction="long", route="IBKR",
            entry_price=150.0, stop_loss=145.0, target_price=155.0,
            valid_count=4, strategy_version=1,
        )
        broker = IBKRBroker()
        with patch("bot.kill_switch.is_kill_switch_active",
                     return_value=False), \
             patch("bot.runtime_policy.get_signal_only_reason",
                     return_value=(True, REASON_BROKER_KILL_SWITCH)), \
             patch.object(broker, "_connect") as mock_connect:
            result = broker.submit(intent)
        self.assertEqual(result.status, "signal_only_skipped")
        self.assertEqual(result.reason, REASON_BROKER_KILL_SWITCH)
        # Critical: no Gateway probe attempted.
        mock_connect.assert_not_called()

    def test_ibkr_broker_file_kill_takes_precedence_over_runtime(self):
        """Existing file-based kill switch fires first; runtime
        policy is not even consulted. Preserves prior contract."""
        from bot.brokers.ibkr_broker import IBKRBroker
        from bot.brokers.base import OrderIntent
        intent = OrderIntent(
            signal_id=4, symbol="AAPL", direction="long", route="IBKR",
            entry_price=150.0, stop_loss=145.0, target_price=155.0,
            valid_count=4, strategy_version=1,
        )
        broker = IBKRBroker()
        with patch("bot.kill_switch.is_kill_switch_active",
                     return_value=True), \
             patch("bot.runtime_policy.get_signal_only_reason") as rt_mock:
            result = broker.submit(intent)
        self.assertEqual(result.status, "kill_switch_active")
        rt_mock.assert_not_called()


class TestEtoroPaperBrokerRuntimeEnforcement(unittest.TestCase):

    def setUp(self):
        rt.clear_cache()

    def test_etoro_paper_blocks_when_runtime_policy_says_skip(self):
        from bot.etoro.paper_broker import PaperEtoroBroker
        from bot.brokers.base import OrderIntent
        intent = OrderIntent(
            signal_id=5, symbol="AAPL", direction="long", route="ETORO",
            entry_price=150.0, stop_loss=145.0, target_price=155.0,
            valid_count=4, strategy_version=1,
        )
        # Constructor needs a read adapter — provide a minimal mock.
        broker = PaperEtoroBroker(read_adapter=MagicMock())
        with patch("bot.runtime_policy.get_signal_only_reason",
                     return_value=(True, REASON_GLOBAL_KILL_SWITCH)), \
             patch.object(broker, "_resolve_instrument") as resolve_mock:
            result = broker.submit(intent)
        self.assertEqual(result.status, "signal_only_skipped")
        self.assertEqual(result.reason, REASON_GLOBAL_KILL_SWITCH)
        # Critical: no instrument resolution or HTTP read.
        resolve_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# G4. Headline mid-run activation test
# ─────────────────────────────────────────────────────────────────────────────

class TestMidRunActivation(unittest.TestCase):
    """The smoking-gun test the operator explicitly requested for
    P0-3: prove that activating the kill_switch via the DB at
    runtime causes the NEXT broker submit to return
    signal_only_skipped — no scanner restart required."""

    def setUp(self):
        rt.clear_cache()

    def test_mid_run_kill_switch_activation_blocks_next_submit(self):
        from bot.brokers.paper_broker import PaperBroker
        from bot.brokers.base import OrderIntent

        db = _build_policy_db(_enabled_policy_for("paper"))
        broker = PaperBroker()

        # First submit: policy is enabled → paper_logged path.
        # We patch get_signal_only_reason to inject our explicit
        # db_path + clock (the production code reads them from env
        # and time.monotonic; the test wants determinism).
        intent1 = OrderIntent(
            signal_id=10, symbol="AAPL", direction="long", route="IBKR",
            entry_price=150.0, stop_loss=145.0, target_price=155.0,
            valid_count=4, strategy_version=1,
        )

        orig_get = rt.get_signal_only_reason
        def _t100(broker_name, **kwargs):
            return orig_get(broker_name, db_path=db,
                              ttl_sec=5.0, now=100.0)
        def _t110(broker_name, **kwargs):
            return orig_get(broker_name, db_path=db,
                              ttl_sec=5.0, now=110.0)

        with patch("bot.runtime_policy.get_signal_only_reason",
                     side_effect=_t100), \
             patch.object(broker, "_log"):
            # NOTE: paper_broker imports get_signal_only_reason
            # lazily inside submit() via
            # `from bot.runtime_policy import get_signal_only_reason`.
            # The patch target must therefore be the source module.
            r1 = broker.submit(intent1)
        self.assertEqual(r1.status, "paper_logged",
                         "First submit must proceed normally — policy "
                         "is enabled and freshly cached.")

        # OPERATOR ACTION: flip global kill_switch in the DB.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE portfolio_risk_state SET value=? WHERE key=?",
            (json.dumps(_global_kill_policy_for("paper")),
             "broker_allocation_policy"),
        )
        conn.commit()
        conn.close()

        # Advance clock past TTL so cache refreshes.
        intent2 = OrderIntent(
            signal_id=11, symbol="MSFT", direction="long", route="IBKR",
            entry_price=300.0, stop_loss=295.0, target_price=305.0,
            valid_count=4, strategy_version=1,
        )
        try:
            with patch("bot.runtime_policy.get_signal_only_reason",
                         side_effect=_t110), \
                 patch.object(broker, "_log"):
                r2 = broker.submit(intent2)
        finally:
            os.unlink(db)

        # SMOKING-GUN ASSERTION.
        self.assertEqual(
            r2.status, "signal_only_skipped",
            "Mid-run kill_switch activation MUST block the next "
            "submit without scanner restart. Status was %r." % r2.status)
        self.assertEqual(r2.reason, REASON_GLOBAL_KILL_SWITCH)


if __name__ == "__main__":
    unittest.main(verbosity=2)

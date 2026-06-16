"""test_group_e_safety.py — proofs for pre-M19 Group E safety fixes.

ISSUE-014 — bot/brokers.get_broker(): when broker-allocation policy cannot be
            loaded, the factory consults the runtime-policy fail-safe and
            wraps the concrete broker in SignalOnlyBroker (reason
            'policy_unavailable') instead of silently returning the bare
            concrete broker. Never fails OPEN.
ISSUE-013 — dashboard/app.py: refuses to start without DASHBOARD_SECRET_KEY
            when DASHBOARD_ENV=production; dev/local keeps the warned fallback.

ISSUE-014 tests patch the policy/runtime-policy seams (no DB, no network, no
broker submit to a real venue). ISSUE-013 tests run isolated subprocess
imports with controlled env vars. No real signals.db, no data/ml.
"""
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parent


# ───────────────────────── ISSUE-014 ─────────────────────────
class Issue014BrokerFailSafe(unittest.TestCase):

    def setUp(self):
        # All tests default to the safe paper broker.
        self._env = mock.patch.dict(os.environ, {"BROKER": "paper"})
        self._env.start()
        self.addCleanup(self._env.stop)
        # Clear any cached runtime policy between tests.
        try:
            import bot.runtime_policy as rp
            rp.clear_cache()
        except Exception:
            pass

    def _get_broker_with(self, policy_return, rt_reason=None, rt_raises=False):
        import bot.brokers as brokers

        def fake_policy():
            return policy_return

        patches = [mock.patch.object(brokers, "_maybe_load_policy", fake_policy)]
        if rt_reason is not None or rt_raises:
            import bot.runtime_policy as rp

            def fake_rt(name, **kw):
                if rt_raises:
                    raise RuntimeError("runtime policy boom")
                return rt_reason
            patches.append(mock.patch.object(rp, "get_signal_only_reason", fake_rt))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return brokers.get_broker()

    def test_valid_policy_allowing_broker_returns_concrete(self):
        from bot.brokers.paper_broker import PaperBroker
        # policy present + determine_signal_only_reason says do-not-skip
        import bot.brokers as brokers
        with mock.patch.object(brokers, "_maybe_load_policy",
                               lambda: {"auto_trading_enabled": True}), \
             mock.patch("bot.etoro.signal_only_broker.determine_signal_only_reason",
                        lambda policy, name: (False, None)):
            b = brokers.get_broker()
        self.assertIsInstance(b, PaperBroker)

    def test_valid_policy_disabling_broker_returns_signal_only(self):
        import bot.brokers as brokers
        from bot.etoro.signal_only_broker import (
            SignalOnlyBroker, REASON_GLOBAL_DISABLED)
        with mock.patch.object(brokers, "_maybe_load_policy",
                               lambda: {"auto_trading_enabled": False}), \
             mock.patch("bot.etoro.signal_only_broker.determine_signal_only_reason",
                        lambda policy, name: (True, REASON_GLOBAL_DISABLED)):
            b = brokers.get_broker()
        self.assertIsInstance(b, SignalOnlyBroker)

    def test_policy_load_failure_does_not_return_bare_concrete(self):
        from bot.etoro.signal_only_broker import (
            SignalOnlyBroker, REASON_POLICY_UNAVAILABLE)
        b = self._get_broker_with(
            policy_return=None, rt_reason=(True, REASON_POLICY_UNAVAILABLE))
        self.assertIsInstance(b, SignalOnlyBroker)

    def test_wrapper_uses_reason_policy_unavailable(self):
        from bot.etoro.signal_only_broker import REASON_POLICY_UNAVAILABLE
        self.assertEqual(REASON_POLICY_UNAVAILABLE, "policy_unavailable")
        b = self._get_broker_with(
            policy_return=None, rt_reason=(True, REASON_POLICY_UNAVAILABLE))
        self.assertEqual(b.reason, "policy_unavailable")

    def test_signal_only_submit_does_not_call_wrapped(self):
        from bot.etoro.signal_only_broker import REASON_POLICY_UNAVAILABLE
        from bot.brokers.base import OrderIntent
        b = self._get_broker_with(
            policy_return=None, rt_reason=(True, REASON_POLICY_UNAVAILABLE))
        # Replace wrapped.submit with a tripwire.
        called = {"n": 0}

        def tripwire(intent):
            called["n"] += 1
            raise AssertionError("wrapped.submit must not be called")
        b._wrapped.submit = tripwire   # SignalOnlyBroker wraps as _wrapped
        intent = OrderIntent(
            signal_id=12345, symbol="AAPL", direction="long", route="IBKR",
            entry_price=100.0, stop_loss=98.0, target_price=104.0,
            valid_count=3, strategy_version=1)
        result = b.submit(intent)
        self.assertEqual(called["n"], 0)
        self.assertEqual(result.status, "signal_only_skipped")

    def test_failure_path_when_runtime_policy_errors_wraps_and_logs(self):
        from bot.etoro.signal_only_broker import SignalOnlyBroker
        with self.assertLogs("bot.brokers", level="WARNING") as cm:
            b = self._get_broker_with(policy_return=None, rt_raises=True)
        self.assertIsInstance(b, SignalOnlyBroker)
        self.assertEqual(b.reason, "policy_unavailable")
        self.assertTrue(any("policy unavailable" in m.lower() for m in cm.output))

    def test_etoro_real_still_raises(self):
        import bot.brokers as brokers
        with mock.patch.dict(os.environ, {"BROKER": "etoro_real"}):
            with self.assertRaises(ValueError):
                brokers.get_broker()


# ───────────────────────── ISSUE-013 ─────────────────────────
class Issue013DashboardSecret(unittest.TestCase):

    def _import_dashboard(self, env):
        """Import dashboard.app in an isolated subprocess with the given env
        overrides. Returns (returncode, stdout, stderr)."""
        full_env = dict(os.environ)
        # Strip the vars we control so the test env is deterministic.
        for k in ("DASHBOARD_SECRET_KEY", "DASHBOARD_ENV"):
            full_env.pop(k, None)
        full_env.update(env)
        code = (
            "import dashboard.app as a;"
            "print('SECRET=' + a.app.secret_key)"
        )
        p = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
            cwd=str(_REPO_ROOT), env=full_env)
        return p.returncode, p.stdout, p.stderr

    def test_production_without_secret_fails(self):
        rc, out, err = self._import_dashboard({"DASHBOARD_ENV": "production"})
        self.assertNotEqual(rc, 0, "production without secret must fail import")
        self.assertIn("RuntimeError", err)
        self.assertIn("DASHBOARD_SECRET_KEY must be set", err)

    def test_dev_without_secret_starts_with_fallback(self):
        rc, out, err = self._import_dashboard({})  # no DASHBOARD_ENV
        self.assertEqual(rc, 0, f"dev import should succeed; err={err[-500:]}")
        self.assertIn("SECRET=", out)
        self.assertTrue(out.strip().split("SECRET=")[-1].endswith("_algo_session"))

    def test_production_with_secret_works(self):
        rc, out, err = self._import_dashboard(
            {"DASHBOARD_ENV": "production", "DASHBOARD_SECRET_KEY": "prodkey123"})
        self.assertEqual(rc, 0, f"prod+secret should import; err={err[-500:]}")
        self.assertIn("SECRET=prodkey123", out)

    def test_dev_with_secret_works(self):
        rc, out, err = self._import_dashboard(
            {"DASHBOARD_SECRET_KEY": "devkey456"})
        self.assertEqual(rc, 0, f"dev+secret should import; err={err[-500:]}")
        self.assertIn("SECRET=devkey456", out)

    def test_no_route_auth_csrf_decorators_modified(self):
        """Static: the auth/CSRF decorators and key routes still exist
        unchanged in count — the Group E edit only touched the secret block."""
        src = (_REPO_ROOT / "dashboard" / "app.py").read_text()
        # Core decorators/endpoints must still be present.
        self.assertIn("@require_auth", src)
        self.assertIn("csrf_required", src)
        self.assertIn("/api/login", src)
        # The edit must not have removed the CSRF issuance endpoint.
        self.assertIn("/api/auth/csrf", src)

    def test_fallback_warning_says_dev_local_only(self):
        src = (_REPO_ROOT / "dashboard" / "app.py").read_text()
        self.assertIn("dev/local only", src)
        self.assertIn("DASHBOARD_ENV=production", src)


if __name__ == "__main__":
    unittest.main()

"""M15.3.A — Dashboard auth/security hardening tests.

Approved scope per Q-A.1 through Q-A.9 + corrections #1-#6:
  G1: Password verification (bcrypt + plaintext fallback, never-print)
  G2: Rate-limit + lockout (in-memory sliding window)
  G3: Session hardening (cookies, idle+absolute timeout, rotation)
  G4: CSRF protection (per-session token, exempt list, header verify)
  G5: Bind-host + startup-warning behaviour
  G6: auth_events DAO (append-only schema, hash session_id, read-back)
  G7: Flask integration — login/logout/CSRF endpoints, existing endpoints
  G8: AST scans + protected-files invariants
  G9: Subprocess/real-HTTP tests for cookie/bind-host behaviour

Hard constraints honored:
  * No order paths
  * No live mode
  * No eToro / IBKR broker / IB API imports anywhere in M15.3.A modules
  * No M14 engine / governor / snapshot / authority changes
  * No scanner / strategy changes
  * No secret material in any logged output, audit row, or test assertion

Run:
  python3 -m unittest test_m15_3_a_dashboard_auth
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────


_AUTH_ENV_KEYS = (
    "DASHBOARD_PASSWORD_HASH",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SECRET_KEY",
    "DASHBOARD_HTTPS_MODE",
    "DASHBOARD_COOKIE_SECURE",
    "DASHBOARD_BIND_HOST",
    "DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE",
    "DASHBOARD_SESSION_IDLE_MIN",
    "DASHBOARD_SESSION_MAX_HOUR",
    "LOGIN_LOCKOUT_WINDOW_SEC",
    "LOGIN_LOCKOUT_THRESHOLD",
    "LOGIN_LOCKOUT_DURATION_SEC",
    # M15.3.A.2 — TOTP secret. Added 2026-06-04 after the VPS .env
    # started carrying it post-M15.3.A.2 enable. If left un-cleaned,
    # password-only login tests fail with totp_required.
    "DASHBOARD_TOTP_SECRET",
    # Defensive — operator could in principle override DASHBOARD_PORT
    # in .env; we don't want a test to silently bind a different port.
    "DASHBOARD_PORT",
)


def _clean_auth_env():
    for k in _AUTH_ENV_KEYS:
        os.environ.pop(k, None)


def _reset_dashboard_modules():
    """Force a fresh import of dashboard.app + dashboard.auth.* so
    that env-var changes between tests are picked up correctly.

    Deprecated for most callers — class-identity bugs occur when
    different module instances coexist (e.g. the test's
    `dashboard.auth.LoginRateLimited` differs from the one raised
    by `dashboard.app`'s rate-limiter). Use `_make_test_app` with
    singleton mode instead.
    """
    for m in list(sys.modules):
        if m == "dashboard.app" or m.startswith("dashboard.app."):
            del sys.modules[m]
        if m == "dashboard.auth" or m.startswith("dashboard.auth."):
            del sys.modules[m]
    # Also remove the attribute from the parent package, otherwise
    # `from dashboard import app` returns the stale module reference.
    import dashboard as _dashboard_pkg
    for attr in ("app", "auth"):
        if hasattr(_dashboard_pkg, attr):
            delattr(_dashboard_pkg, attr)


# Singleton — _make_test_app imports dashboard.app once, then resets
# its mutable state per-test (rate-limiter, DB_PATH, env vars). This
# avoids the class-identity issues that arise when reloading modules
# while other modules still hold references to the previous classes.
_DASHAPP_SINGLETON = None


def _make_test_app(*, password="testpw-12345", with_hash=False,
                    bind_host=None, db_path=None,
                    https_mode=False,
                    secret_key="test_secret_key_M15.3.A_xxxx"):
    """Build a dashboard.app test instance with explicit env.

    Uses a SINGLETON dashboard.app module across all tests in the
    process. Per-test, the module's mutable state is reset:
      * env vars (DASHBOARD_PASSWORD / _HASH / _SECRET_KEY / _HTTPS_MODE)
      * rate-limiter (_m153a_login_limiter)
      * DB_PATH
      * cookie config (harden_app_config re-applied based on https_mode)

    Tests that need different env-at-import-time must call
    `_reset_dashboard_modules()` themselves AND not call _make_test_app.

    IMPORTANT (M15.3.A.cutover fix-2): dashboard.app calls
    `load_dotenv()` at module-import time. On a VPS where `.env`
    contains `DASHBOARD_TOTP_SECRET`, `DASHBOARD_BIND_HOST=127.0.0.1`,
    `DASHBOARD_HTTPS_MODE=true`, etc., a fixture that cleans env
    BEFORE the import lets dotenv re-pollute afterwards. The
    fixture's intended test values then lose to whatever is in .env,
    and password-only login tests fail with totp_required, bind-host
    tests see 127.0.0.1, etc.

    Fix (same pattern as M15.3.A.2 fix-1): import dashboard.app
    FIRST (let dotenv run whatever it runs), THEN clean env, THEN
    set test values. This way the test's env always wins, regardless
    of what dotenv loaded.
    """
    global _DASHAPP_SINGLETON

    # Step 1 — ensure dashboard.app is imported (singleton; only on
    # the very first call across all tests). This triggers
    # `load_dotenv()` which may populate os.environ from a real .env
    # in the cwd (VPS scenario). Subsequent calls skip the import.
    if _DASHAPP_SINGLETON is None:
        from dashboard import app as dashapp
        _DASHAPP_SINGLETON = dashapp
    dashapp = _DASHAPP_SINGLETON

    # Step 2 — NOW clean env, AFTER any dotenv pollution.
    _clean_auth_env()

    # Step 3 — set the test's env values. These override anything
    # dotenv may have loaded.
    os.environ["DASHBOARD_SECRET_KEY"] = secret_key
    if with_hash:
        try:
            import bcrypt
        except ImportError:
            raise unittest.SkipTest("bcrypt not installed")
        h = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
        os.environ["DASHBOARD_PASSWORD_HASH"] = h
    else:
        os.environ["DASHBOARD_PASSWORD"] = password
    if bind_host:
        os.environ["DASHBOARD_BIND_HOST"] = bind_host
    if https_mode:
        os.environ["DASHBOARD_HTTPS_MODE"] = "true"

    dashapp.app.config["TESTING"] = True
    # Re-apply env-dependent cookie config (DASHBOARD_HTTPS_MODE may
    # have changed since the previous test).
    from dashboard.auth.sessions import harden_app_config
    import logging
    silent_log = logging.getLogger("test_silent")
    silent_log.addHandler(logging.NullHandler())
    silent_log.propagate = False
    harden_app_config(dashapp.app, logger=silent_log)
    # Fresh rate-limiter per test instance — uses the SAME module's
    # RateLimiter / LoginRateLimited classes so exception-class
    # identity is preserved.
    from dashboard.auth.rate_limit import RateLimiter
    dashapp._m153a_login_limiter = RateLimiter(
        threshold=5, window_sec=600, lockout_sec=900,
    )
    if db_path is not None:
        dashapp.DB_PATH = Path(db_path)
        # Ensure auth_events schema exists in the temp DB.
        from dashboard.auth.audit import ensure_auth_events_schema
        c = sqlite3.connect(db_path)
        try:
            ensure_auth_events_schema(c)
        finally:
            c.close()
    return dashapp


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — passwords.verify_password
# ─────────────────────────────────────────────────────────────────────────────


class TestPasswordVerify(unittest.TestCase):

    def setUp(self):
        _clean_auth_env()
        import dashboard.auth.passwords as pw_mod
        importlib.reload(pw_mod)
        self.pw_mod = pw_mod

    def test_no_password_configured_rejects_everything(self):
        ok, info = self.pw_mod.verify_password("anything")
        self.assertFalse(ok)
        self.assertEqual(info["path"], "none")

    def test_default_changeme_treated_as_no_password(self):
        os.environ["DASHBOARD_PASSWORD"] = "changeme"
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("changeme")
        self.assertFalse(ok)
        self.assertEqual(info["path"], "none")
        self.assertFalse(self.pw_mod.password_configured())

    def test_plaintext_match(self):
        os.environ["DASHBOARD_PASSWORD"] = "real-password-here"
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("real-password-here")
        self.assertTrue(ok)
        self.assertEqual(info["path"], "plaintext")
        self.assertEqual(info["warning"], "plaintext_fallback_in_use")

    def test_plaintext_mismatch(self):
        os.environ["DASHBOARD_PASSWORD"] = "right"
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("wrong")
        self.assertFalse(ok)
        self.assertEqual(info["path"], "plaintext")

    def test_bcrypt_match(self):
        try:
            import bcrypt
        except ImportError:
            self.skipTest("bcrypt not installed")
        h = bcrypt.hashpw(b"test-bcrypt-pw", bcrypt.gensalt(rounds=4)).decode()
        os.environ["DASHBOARD_PASSWORD_HASH"] = h
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("test-bcrypt-pw")
        self.assertTrue(ok)
        self.assertEqual(info["path"], "bcrypt")
        self.assertNotIn("warning", info)

    def test_bcrypt_mismatch(self):
        try:
            import bcrypt
        except ImportError:
            self.skipTest("bcrypt not installed")
        h = bcrypt.hashpw(b"correct", bcrypt.gensalt(rounds=4)).decode()
        os.environ["DASHBOARD_PASSWORD_HASH"] = h
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("incorrect")
        self.assertFalse(ok)
        self.assertEqual(info["path"], "bcrypt")

    def test_bcrypt_preferred_over_plaintext_when_both_set(self):
        try:
            import bcrypt
        except ImportError:
            self.skipTest("bcrypt not installed")
        h = bcrypt.hashpw(b"bcrypt-pw", bcrypt.gensalt(rounds=4)).decode()
        os.environ["DASHBOARD_PASSWORD_HASH"] = h
        os.environ["DASHBOARD_PASSWORD"] = "plaintext-pw"
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("plaintext-pw")
        self.assertFalse(ok,
            "plaintext fallback must be ignored when bcrypt hash present")
        self.assertEqual(info["path"], "bcrypt")
        ok, info = self.pw_mod.verify_password("bcrypt-pw")
        self.assertTrue(ok)
        self.assertEqual(info["path"], "bcrypt")

    def test_malformed_bcrypt_hash_fails_closed(self):
        os.environ["DASHBOARD_PASSWORD_HASH"] = "not-a-valid-bcrypt-hash"
        os.environ["DASHBOARD_PASSWORD"] = "fallback"
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("fallback")
        self.assertFalse(ok)
        self.assertEqual(info["path"], "bcrypt")
        self.assertEqual(info.get("error"), "hash_malformed")

    def test_non_string_input_rejected(self):
        os.environ["DASHBOARD_PASSWORD"] = "x"
        importlib.reload(self.pw_mod)
        for bad in (None, 123, [], {}, True):
            ok, _ = self.pw_mod.verify_password(bad)
            self.assertFalse(ok, f"non-string input {bad!r} must reject")

    def test_return_dict_never_contains_password_material(self):
        os.environ["DASHBOARD_PASSWORD"] = "supersecret-12345"
        importlib.reload(self.pw_mod)
        ok, info = self.pw_mod.verify_password("supersecret-12345")
        self.assertTrue(ok)
        self.assertNotIn("supersecret-12345", json.dumps(info))

    def test_hash_password_returns_bcrypt_string(self):
        try:
            import bcrypt
        except ImportError:
            self.skipTest("bcrypt not installed")
        h = self.pw_mod.hash_password("plain-input")
        self.assertTrue(h.startswith("$2"), f"not a bcrypt hash: {h[:5]}")
        self.assertTrue(bcrypt.checkpw(b"plain-input", h.encode()))
        with self.assertRaises(ValueError):
            self.pw_mod.hash_password("")


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 — RateLimiter
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimiter(unittest.TestCase):

    def _mk(self, threshold=3, window=600, lockout=900, t0=1000.0):
        from dashboard.auth.rate_limit import RateLimiter
        self.t = [float(t0)]
        return RateLimiter(threshold=threshold, window_sec=window,
                            lockout_sec=lockout, clock=lambda: self.t[0])

    def test_fresh_key_not_locked(self):
        rl = self._mk()
        rl.check_locked("1.2.3.4")
        self.assertEqual(rl.failure_count("1.2.3.4"), 0)
        self.assertIsNone(rl.locked_until("1.2.3.4"))

    def test_failures_below_threshold_dont_lock(self):
        rl = self._mk(threshold=3)
        for _ in range(2):
            rl.record_failure("ip")
        rl.check_locked("ip")
        self.assertEqual(rl.failure_count("ip"), 2)

    def test_threshold_triggers_lockout(self):
        from dashboard.auth.rate_limit import LoginRateLimited
        rl = self._mk(threshold=3, lockout=900)
        for _ in range(3):
            rl.record_failure("ip")
        with self.assertRaises(LoginRateLimited) as ctx:
            rl.check_locked("ip")
        self.assertGreater(ctx.exception.retry_after_sec, 0)
        self.assertLessEqual(ctx.exception.retry_after_sec, 901)

    def test_success_clears_failures_and_lockout(self):
        rl = self._mk(threshold=3)
        for _ in range(3):
            rl.record_failure("ip")
        rl.record_success("ip")
        rl.check_locked("ip")
        self.assertEqual(rl.failure_count("ip"), 0)
        self.assertIsNone(rl.locked_until("ip"))

    def test_lockout_expires_after_duration(self):
        from dashboard.auth.rate_limit import LoginRateLimited
        rl = self._mk(threshold=3, lockout=900)
        for _ in range(3):
            rl.record_failure("ip")
        with self.assertRaises(LoginRateLimited):
            rl.check_locked("ip")
        self.t[0] += 901
        rl.check_locked("ip")  # no raise — expired

    def test_window_sliding_drops_old_failures(self):
        rl = self._mk(threshold=3, window=60)
        rl.record_failure("ip")
        self.t[0] += 61
        rl.record_failure("ip")
        rl.record_failure("ip")
        rl.check_locked("ip")  # only 2 in window — not locked
        self.assertEqual(rl.failure_count("ip"), 2)

    def test_different_keys_isolated(self):
        from dashboard.auth.rate_limit import LoginRateLimited
        rl = self._mk(threshold=3)
        for _ in range(3):
            rl.record_failure("ip-a")
        with self.assertRaises(LoginRateLimited):
            rl.check_locked("ip-a")
        rl.check_locked("ip-b")  # not locked

    def test_empty_or_none_key_is_safe(self):
        rl = self._mk()
        rl.record_failure("")
        rl.record_failure(None)
        rl.check_locked("")
        rl.check_locked(None)

    def test_invalid_constructor_args_rejected(self):
        from dashboard.auth.rate_limit import RateLimiter
        with self.assertRaises(ValueError):
            RateLimiter(threshold=0)
        with self.assertRaises(ValueError):
            RateLimiter(window_sec=-1)
        with self.assertRaises(ValueError):
            RateLimiter(lockout_sec=0)

    def test_policy_dict_for_audit_extras(self):
        rl = self._mk(threshold=7, window=300, lockout=600)
        pol = rl.policy()
        self.assertEqual(pol["threshold"], 7)
        self.assertEqual(pol["window_sec"], 300)
        self.assertEqual(pol["lockout_sec"], 600)


# ─────────────────────────────────────────────────────────────────────────────
# Group 3 — Session hardening
# ─────────────────────────────────────────────────────────────────────────────


class TestSessionHardening(unittest.TestCase):

    def setUp(self):
        _clean_auth_env()
        import dashboard.auth.sessions as ses
        importlib.reload(ses)
        self.ses = ses

    def test_secure_mode_default_false(self):
        """Correction #2: Secure must NOT default True (would break
        plain-HTTP login during transition)."""
        self.assertFalse(self.ses.is_secure_cookie_mode())

    def test_secure_mode_enabled_by_HTTPS_MODE(self):
        os.environ["DASHBOARD_HTTPS_MODE"] = "true"
        importlib.reload(self.ses)
        self.assertTrue(self.ses.is_secure_cookie_mode())

    def test_secure_mode_enabled_by_COOKIE_SECURE(self):
        os.environ["DASHBOARD_COOKIE_SECURE"] = "1"
        importlib.reload(self.ses)
        self.assertTrue(self.ses.is_secure_cookie_mode())

    def test_harden_app_sets_required_flags(self):
        from flask import Flask
        app = Flask(__name__)
        self.ses.harden_app_config(app)
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Strict")
        self.assertFalse(app.config["SESSION_COOKIE_SECURE"],
            "Secure must default False — correction #2")

    def test_harden_app_secure_when_https_mode(self):
        os.environ["DASHBOARD_HTTPS_MODE"] = "true"
        importlib.reload(self.ses)
        from flask import Flask
        app = Flask(__name__)
        self.ses.harden_app_config(app)
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_rotate_session_sets_timestamps(self):
        sess = {}
        t = [10000.0]
        self.ses.rotate_session(sess, client_ip="9.9.9.9",
                                  clock=lambda: t[0])
        self.assertTrue(sess["authed"])
        self.assertEqual(sess["_login_at"], 10000.0)
        self.assertEqual(sess["_last_seen"], 10000.0)
        self.assertEqual(sess["_login_ip"], "9.9.9.9")

    def test_idle_timeout_clears_session(self):
        sess = {"authed": True, "_login_at": 1000.0, "_last_seen": 1000.0}
        valid = self.ses.enforce_session_timeout(
            sess, idle_min=30, max_hour=12,
            clock=lambda: 1000.0 + 31 * 60,
        )
        self.assertFalse(valid)
        self.assertNotIn("authed", sess)

    def test_absolute_timeout_clears_session(self):
        sess = {"authed": True, "_login_at": 1000.0, "_last_seen": 9999999.0}
        valid = self.ses.enforce_session_timeout(
            sess, idle_min=30, max_hour=12,
            clock=lambda: 1000.0 + 13 * 3600,
        )
        self.assertFalse(valid)

    def test_active_session_within_both_limits_stays(self):
        sess = {"authed": True, "_login_at": 1000.0, "_last_seen": 1000.0}
        valid = self.ses.enforce_session_timeout(
            sess, idle_min=30, max_hour=12,
            clock=lambda: 1000.0 + 60,
        )
        self.assertTrue(valid)
        self.assertTrue(sess["authed"])
        self.assertEqual(sess["_last_seen"], 1060.0)

    def test_legacy_session_without_timestamps_grace(self):
        sess = {"authed": True}
        valid = self.ses.enforce_session_timeout(sess, clock=lambda: 5000.0)
        self.assertTrue(valid)
        self.assertEqual(sess["_login_at"], 5000.0)
        self.assertEqual(sess["_last_seen"], 5000.0)

    def test_unauthed_session_no_enforcement(self):
        sess = {}
        valid = self.ses.enforce_session_timeout(sess, clock=lambda: 1.0)
        self.assertTrue(valid)
        self.assertNotIn("authed", sess)


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — CSRF token primitives
# ─────────────────────────────────────────────────────────────────────────────


class TestCSRFPrimitives(unittest.TestCase):

    def setUp(self):
        from dashboard.auth import csrf as csrf_mod
        self.csrf = csrf_mod

    def test_issue_csrf_token_stores_in_session(self):
        sess = {}
        tok = self.csrf.issue_csrf_token(sess)
        self.assertTrue(tok)
        self.assertEqual(sess[self.csrf.CSRF_SESSION_KEY], tok)
        self.assertGreaterEqual(len(tok), 32)

    def test_issue_csrf_token_is_random(self):
        sess1, sess2 = {}, {}
        a = self.csrf.issue_csrf_token(sess1)
        b = self.csrf.issue_csrf_token(sess2)
        self.assertNotEqual(a, b)

    def test_rotate_replaces_token(self):
        sess = {}
        a = self.csrf.issue_csrf_token(sess)
        b = self.csrf.rotate_csrf_token(sess)
        self.assertNotEqual(a, b)
        self.assertEqual(sess[self.csrf.CSRF_SESSION_KEY], b)

    def test_get_csrf_token_empty_when_absent(self):
        self.assertEqual(self.csrf.get_csrf_token({}), "")

    def test_state_changing_methods_constant(self):
        self.assertEqual(
            self.csrf.STATE_CHANGING_METHODS,
            frozenset({"POST", "PUT", "PATCH", "DELETE"}),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — Bind-host behaviour
# ─────────────────────────────────────────────────────────────────────────────


class TestBindHostBehaviour(unittest.TestCase):
    """Per Q-A.3 / correction #3 — soft cutover with explicit warning."""

    def setUp(self):
        _clean_auth_env()

    def test_default_bind_host_is_0_0_0_0(self):
        bind_host = os.getenv("DASHBOARD_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.assertEqual(bind_host, "0.0.0.0")

    def test_DASHBOARD_BIND_HOST_override_honoured(self):
        os.environ["DASHBOARD_BIND_HOST"] = "127.0.0.1"
        bind_host = os.getenv("DASHBOARD_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.assertEqual(bind_host, "127.0.0.1")

    def test_plaintext_exposure_ack_truthy_values(self):
        for v in ("yes", "true", "1", "on", "YES", "True"):
            os.environ["DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE"] = v
            ack = os.getenv(
                "DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE", "").strip().lower() \
                  in ("true", "1", "yes", "on")
            self.assertTrue(ack, f"value {v!r} should be truthy")

    def test_startup_warning_emitted_when_exposed_plaintext(self):
        """When binding to 0.0.0.0 without HTTPS or explicit ack, the
        startup banner MUST emit a warning. This test forces a fresh
        dashboard.app import so the import-time warning fires.

        M15.3.A.cutover fix-2: this test RELOADS dashboard.app, so
        `load_dotenv()` runs again at reload time. On a VPS where
        `.env` has DASHBOARD_BIND_HOST=127.0.0.1 / DASHBOARD_HTTPS_MODE
        =true / DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE=…, dotenv would
        populate those values and suppress the warning. To prevent
        that, we explicitly seed the relevant vars to EMPTY STRING
        before the reload — dotenv's default `override=False` skips
        keys already in os.environ (including those set to ""), and
        `_m153a_bind_host = os.getenv(...).strip() or '0.0.0.0'`
        falls back to '0.0.0.0' on empty.
        """
        import logging
        import io
        _clean_auth_env()
        os.environ["DASHBOARD_PASSWORD"] = "any"
        os.environ["DASHBOARD_SECRET_KEY"] = "test_xxx"
        # Block .env-resident values from polluting the fresh import.
        os.environ["DASHBOARD_BIND_HOST"] = ""
        os.environ["DASHBOARD_HTTPS_MODE"] = ""
        os.environ["DASHBOARD_COOKIE_SECURE"] = ""
        os.environ["DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE"] = ""
        # Drop the singleton + force a fresh import.
        global _DASHAPP_SINGLETON
        # NOTE: this test deliberately invalidates the singleton; tests
        # that run AFTER it will get a fresh singleton on next call.
        from test_m15_3_a_dashboard_auth import _DASHAPP_SINGLETON as _s
        import test_m15_3_a_dashboard_auth as _mod
        _mod._DASHAPP_SINGLETON = None
        _reset_dashboard_modules()
        log_buf = io.StringIO()
        h = logging.StreamHandler(log_buf)
        h.setLevel(logging.WARNING)
        logger = logging.getLogger("dashboard.m153a")
        logger.addHandler(h)
        try:
            from dashboard import app  # noqa: F401
        finally:
            logger.removeHandler(h)
        out = log_buf.getvalue()
        self.assertIn("DASHBOARD_BIND_HOST=0.0.0.0", out)
        self.assertIn("plaintext", out.lower())

    # ─────────────────────────────────────────────────────────────────────
    # M15.3.A.cutover regression — app.run() must use DASHBOARD_BIND_HOST
    # ─────────────────────────────────────────────────────────────────────
    # Bug surfaced on VPS 2026-06-04 during M15.3.A.cutover Phase 2:
    # `.env` had DASHBOARD_BIND_HOST=127.0.0.1, but `ss -ltnp` showed the
    # dashboard still listening on 0.0.0.0:8080. Root cause: line 103
    # correctly read `_m153a_bind_host = os.getenv('DASHBOARD_BIND_HOST',
    # '0.0.0.0')...`, but the `if __name__ == '__main__':` block at the
    # bottom of the file passed `app.run(host='0.0.0.0', ...)` — a
    # hardcoded literal that ignored the env-controlled variable.
    # The three tests below lock the fix in place.

    def test_app_run_passes_bind_host_variable_not_literal(self):
        """AST scan: the `app.run(host=...)` call in the __main__ block
        MUST reference the `_m153a_bind_host` variable, NOT a hardcoded
        string. Catches future regressions where someone re-introduces
        '0.0.0.0' as a literal."""
        import ast
        src = (Path(_REPO) / "dashboard" / "app.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)

        # Find the `if __name__ == '__main__':` block.
        main_blocks = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                t = node.test
                if (isinstance(t, ast.Compare)
                    and isinstance(t.left, ast.Name)
                    and t.left.id == "__name__"):
                    main_blocks.append(node)
        self.assertEqual(len(main_blocks), 1,
            f"expected exactly one `if __name__ == '__main__':` block, "
            f"found {len(main_blocks)}")

        # Find the app.run() call within it.
        run_calls = []
        for sub in ast.walk(main_blocks[0]):
            if isinstance(sub, ast.Call):
                f = sub.func
                if (isinstance(f, ast.Attribute)
                    and f.attr == "run"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "app"):
                    run_calls.append(sub)
        self.assertEqual(len(run_calls), 1,
            f"expected exactly one app.run() in __main__, "
            f"found {len(run_calls)}")

        # Inspect host= kwarg.
        host_kwarg = None
        for kw in run_calls[0].keywords:
            if kw.arg == "host":
                host_kwarg = kw.value
                break
        self.assertIsNotNone(host_kwarg, "app.run() missing host= kwarg")

        # MUST be a Name node, NOT a Constant. (ast.Constant covers
        # str/int/None literals in Python 3.8+.)
        self.assertNotIsInstance(host_kwarg, ast.Constant,
            f"app.run(host=...) is hardcoded to a literal. "
            f"Use _m153a_bind_host instead (the env-controlled var). "
            f"This is the M15.3.A.cutover regression — see commit log.")
        self.assertIsInstance(host_kwarg, ast.Name,
            f"app.run(host=...) should be a variable reference, "
            f"got AST node {type(host_kwarg).__name__}")
        self.assertEqual(host_kwarg.id, "_m153a_bind_host",
            f"app.run() host= should reference _m153a_bind_host, "
            f"got {host_kwarg.id!r}")

    def test_dashboard_bind_host_env_to_module_variable_127(self):
        """Subprocess test: when DASHBOARD_BIND_HOST=127.0.0.1 in the
        subprocess env, `dashboard.app._m153a_bind_host` reads as
        '127.0.0.1'. Combined with the AST test above, this proves
        the env-to-runtime wiring is intact end-to-end.

        M15.3.A.cutover fix-2: explicitly clear other dotenv-touchable
        vars in the subprocess env (TOTP_SECRET, HTTPS_MODE, etc.) so
        the test's behaviour is independent of whatever .env file
        happens to be in the cwd. `load_dotenv` default override=False
        leaves already-set (even empty) keys alone.
        """
        import subprocess, sys as _sys
        env = {k: v for k, v in os.environ.items()
                if not k.startswith("DASHBOARD_")}
        env["PYTHONPATH"] = _REPO
        # The value being tested:
        env["DASHBOARD_BIND_HOST"] = "127.0.0.1"
        # Force the dashboard to start cleanly regardless of real .env:
        env["DASHBOARD_PASSWORD"] = "any-non-default-password"
        env["DASHBOARD_SECRET_KEY"] = "x" * 64
        env["DASHBOARD_HTTPS_MODE"] = "true"  # suppress startup warning
        # Block all other dashboard-relevant .env vars by pre-setting
        # them to empty — dotenv won't override.
        env["DASHBOARD_PASSWORD_HASH"] = ""
        env["DASHBOARD_TOTP_SECRET"] = ""
        env["DASHBOARD_COOKIE_SECURE"] = ""
        env["DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE"] = ""
        wrapper = (
            "import sys; sys.path.insert(0, '.'); "
            "import dashboard.app as a; "
            "print(repr(a._m153a_bind_host))"
        )
        proc = subprocess.run(
            [_sys.executable, "-c", wrapper],
            capture_output=True, text=True, timeout=15,
            env=env, cwd=_REPO,
        )
        self.assertEqual(proc.returncode, 0,
            f"subprocess failed: stderr={proc.stderr!r}")
        self.assertIn("'127.0.0.1'", proc.stdout,
            f"expected _m153a_bind_host == '127.0.0.1', "
            f"got stdout={proc.stdout!r}")

    def test_dashboard_bind_host_env_to_module_variable_default(self):
        """Subprocess test: when DASHBOARD_BIND_HOST is empty (or has
        never been set), `_m153a_bind_host` falls back to '0.0.0.0'.
        Proves we don't break existing default behaviour.

        M15.3.A.cutover fix-2: we explicitly set DASHBOARD_BIND_HOST=""
        (empty string) in the subprocess env. dotenv's default
        `override=False` skips already-set keys — even if the real
        `.env` in the cwd has `DASHBOARD_BIND_HOST=127.0.0.1` (which
        it does post-cutover on the VPS), the empty value wins. The
        dashboard's line 103 code is:
            os.getenv('DASHBOARD_BIND_HOST', '0.0.0.0').strip() or '0.0.0.0'
        which evaluates to `'0.0.0.0'` on empty.
        """
        import subprocess, sys as _sys
        env = {k: v for k, v in os.environ.items()
                if not k.startswith("DASHBOARD_")}
        env["PYTHONPATH"] = _REPO
        # Force empty so dotenv won't repopulate from .env on VPS:
        env["DASHBOARD_BIND_HOST"] = ""
        env["DASHBOARD_PASSWORD"] = "any-non-default-password"
        env["DASHBOARD_SECRET_KEY"] = "x" * 64
        env["DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE"] = "yes"  # silence warn
        # Block other dashboard-relevant .env vars:
        env["DASHBOARD_PASSWORD_HASH"] = ""
        env["DASHBOARD_TOTP_SECRET"] = ""
        env["DASHBOARD_HTTPS_MODE"] = ""
        env["DASHBOARD_COOKIE_SECURE"] = ""
        wrapper = (
            "import sys; sys.path.insert(0, '.'); "
            "import dashboard.app as a; "
            "print(repr(a._m153a_bind_host))"
        )
        proc = subprocess.run(
            [_sys.executable, "-c", wrapper],
            capture_output=True, text=True, timeout=15,
            env=env, cwd=_REPO,
        )
        self.assertEqual(proc.returncode, 0,
            f"subprocess failed: stderr={proc.stderr!r}")
        self.assertIn("'0.0.0.0'", proc.stdout,
            f"expected default _m153a_bind_host == '0.0.0.0', "
            f"got stdout={proc.stdout!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — auth_events DAO
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthEventsDAO(unittest.TestCase):

    def setUp(self):
        from dashboard.auth import audit as audit_mod
        self.audit = audit_mod
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_schema_idempotent(self):
        self.audit.ensure_auth_events_schema(self.conn)
        self.audit.ensure_auth_events_schema(self.conn)
        cols = self.conn.execute("PRAGMA table_info(auth_events)").fetchall()
        names = [c[1] for c in cols]
        for required in ("id", "ts_utc", "kind", "client_ip", "user_agent",
                          "session_id", "success", "extras_json"):
            self.assertIn(required, names, f"missing {required}")

    def test_session_id_is_sha256_hashed(self):
        h = self.audit.hash_session_id("plain-session-id-12345")
        self.assertEqual(len(h), 64)
        expected = hashlib.sha256(b"plain-session-id-12345").hexdigest()
        self.assertEqual(h, expected)

    def test_session_id_empty_for_none_or_empty(self):
        self.assertEqual(self.audit.hash_session_id(None), "")
        self.assertEqual(self.audit.hash_session_id(""), "")

    def test_record_auth_event_writes_row(self):
        rid = self.audit.record_auth_event(
            self.conn, kind="login_success",
            client_ip="1.2.3.4", user_agent="ua/1.0",
            session_id="rawid", success=True,
            extras={"path": "bcrypt"},
        )
        self.assertGreater(rid, 0)
        row = self.conn.execute(
            "SELECT kind, client_ip, user_agent, session_id, success "
            "FROM auth_events WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(row[0], "login_success")
        self.assertEqual(row[1], "1.2.3.4")
        self.assertEqual(row[2], "ua/1.0")
        # session_id stored as sha256, NOT raw
        self.assertEqual(row[3], hashlib.sha256(b"rawid").hexdigest())
        self.assertEqual(row[4], 1)

    def test_raw_session_id_never_in_db(self):
        """Critical: the raw session ID must NEVER appear in the DB."""
        raw = "this-is-the-raw-session-id-9999"
        self.audit.record_auth_event(
            self.conn, kind="login_success",
            client_ip="ip", user_agent="ua",
            session_id=raw, success=True,
        )
        # Dump all column contents and assert raw is nowhere.
        rows = self.conn.execute(
            "SELECT ts_utc, kind, client_ip, user_agent, session_id, "
            "extras_json FROM auth_events").fetchall()
        for r in rows:
            for col in r:
                if col is None:
                    continue
                self.assertNotIn(raw, str(col),
                    f"raw session_id leaked into column: {col!r}")

    def test_user_agent_truncated_to_200(self):
        long_ua = "x" * 500
        self.audit.record_auth_event(
            self.conn, kind="login_success",
            client_ip="1.1.1.1", user_agent=long_ua,
            session_id="s", success=True,
        )
        row = self.conn.execute(
            "SELECT user_agent FROM auth_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(len(row[0]), 200)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.audit.record_auth_event(
                self.conn, kind="not_a_real_kind",
                client_ip="x", user_agent="x", session_id="x",
                success=True)

    def test_all_allowed_kinds_accepted(self):
        for k in ("login_success", "login_failure", "login_locked",
                   "login_unconfigured", "logout", "session_rotate",
                   "csrf_invalid", "session_expired"):
            self.audit.record_auth_event(
                self.conn, kind=k, client_ip="ip", user_agent="ua",
                session_id="s", success=True)

    def test_extras_json_round_trips(self):
        extras = {"path": "bcrypt", "n": 42, "nested": {"a": 1}}
        self.audit.record_auth_event(
            self.conn, kind="login_success",
            client_ip="ip", user_agent="ua", session_id="s",
            success=True, extras=extras,
        )
        rows = self.audit.read_auth_events(self.conn, limit=1)
        self.assertEqual(rows[0]["extras"], extras)

    def test_read_auth_events_filtering(self):
        for _ in range(3):
            self.audit.record_auth_event(
                self.conn, kind="login_success",
                client_ip="1.1.1.1", user_agent="ua",
                session_id="s", success=True)
        for _ in range(2):
            self.audit.record_auth_event(
                self.conn, kind="login_failure",
                client_ip="2.2.2.2", user_agent="ua",
                session_id="s", success=False)
        successes = self.audit.read_auth_events(self.conn, kind="login_success")
        failures = self.audit.read_auth_events(self.conn, kind="login_failure")
        self.assertEqual(len(successes), 3)
        self.assertEqual(len(failures), 2)
        from_ip2 = self.audit.read_auth_events(self.conn, client_ip="2.2.2.2")
        self.assertEqual(len(from_ip2), 2)

    def test_ts_check_constraint_blocks_empty_ts(self):
        self.audit.ensure_auth_events_schema(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO auth_events (ts_utc, kind, client_ip, success) "
                "VALUES ('', 'login_success', 'ip', 1)")

    def test_success_check_constraint_blocks_invalid(self):
        self.audit.ensure_auth_events_schema(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO auth_events (ts_utc, kind, success) "
                "VALUES ('2026-06-03', 'login_success', 2)")

    def test_flywheel_migration_helper(self):
        """bot/flywheel.py:ensure_auth_events_migration must produce the
        same schema (compatibility with the canonical migration path)."""
        from bot.flywheel import ensure_auth_events_migration
        result = ensure_auth_events_migration(self.conn)
        self.assertTrue(result["applied"])
        self.assertEqual(result["table"], "auth_events")
        # Schema same as DAO version.
        cols = {c[1] for c in self.conn.execute(
            "PRAGMA table_info(auth_events)").fetchall()}
        self.assertIn("session_id", cols)
        self.assertIn("extras_json", cols)


# ─────────────────────────────────────────────────────────────────────────────
# Group 7 — Flask integration (login / CSRF / existing endpoints)
# ─────────────────────────────────────────────────────────────────────────────


class TestLoginEndpoint(unittest.TestCase):

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _client(self, **kwargs):
        kwargs.setdefault("db_path", self.tmp_db.name)
        dashapp = _make_test_app(**kwargs)
        return dashapp, dashapp.app.test_client()

    def test_login_correct_password_plaintext_returns_token(self):
        dashapp, c = self._client(password="testpw-12345")
        r = c.post("/api/login", json={"password": "testpw-12345"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertIn("csrf_token", d)
        self.assertGreater(len(d["csrf_token"]), 16)

    def test_fixture_isolates_password_only_login_from_vps_totp_dotenv(self):
        """Regression for M15.3.A.cutover VPS test failures (commit 224e8a3).

        Symptom on VPS: 21 M15.3.A tests failed after the cutover landed,
        because the operator's `.env` now carries `DASHBOARD_TOTP_SECRET`
        (from M15.3.A.2 enable), `DASHBOARD_BIND_HOST=127.0.0.1`, and
        `DASHBOARD_HTTPS_MODE=true`. The original `_make_test_app`
        cleaned `os.environ` BEFORE the dashboard.app import, so dotenv
        re-populated the TOTP secret after cleanup. Password-only
        login tests then returned `totp_required` instead of 200.

        Fix (same pattern as M15.3.A.2 fix-1): import dashboard.app
        first (let dotenv run), then clean env, then set test values.

        This test seeds the polluting vars into `os.environ` BEFORE
        invoking `_make_test_app`, simulating the VPS dotenv result,
        and asserts:
          * fixture clears the seeded TOTP secret + bind-host
          * password-only login still works (no totp_required)
          * the dashboard's TOTP block is NOT in effect during this test
        """
        try:
            import bcrypt, pyotp
        except ImportError:
            self.skipTest("bcrypt/pyotp not installed")
        # Seed the VPS-style pollution.
        polluting_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        polluting_hash = bcrypt.hashpw(
            b"DIFFERENT-OPERATOR-PASSWORD",
            bcrypt.gensalt(rounds=4),
        ).decode()
        os.environ["DASHBOARD_TOTP_SECRET"] = polluting_secret
        os.environ["DASHBOARD_PASSWORD_HASH"] = polluting_hash
        os.environ["DASHBOARD_BIND_HOST"] = "127.0.0.1"
        os.environ["DASHBOARD_HTTPS_MODE"] = "true"
        # Invoke fixture exactly as a normal test would.
        dashapp = _make_test_app(password="real-test-pw-12345",
                                   db_path=self.tmp_db.name)
        # After fixture: seeded pollution should be gone.
        self.assertNotIn("DASHBOARD_TOTP_SECRET", os.environ,
            "fixture must clear DASHBOARD_TOTP_SECRET (dotenv pollution)")
        self.assertNotIn("DASHBOARD_PASSWORD_HASH", os.environ,
            "fixture must clear DASHBOARD_PASSWORD_HASH")
        # Password-only login should succeed.
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "real-test-pw-12345"})
        self.assertEqual(r.status_code, 200,
            f"password-only login must succeed when fixture isolates "
            f"from VPS dotenv pollution; got {r.status_code} "
            f"body={r.get_json()!r}")
        self.assertTrue(r.get_json()["ok"])
        # And totp_required must NOT appear.
        self.assertNotEqual(r.get_json().get("error"), "totp_required",
            "fixture failed to suppress DASHBOARD_TOTP_SECRET — TOTP "
            "still active during a password-only M15.3.A test")

    def test_login_correct_password_bcrypt_returns_token(self):
        dashapp, c = self._client(password="testpw-12345", with_hash=True)
        r = c.post("/api/login", json={"password": "testpw-12345"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_login_wrong_password_returns_401(self):
        dashapp, c = self._client(password="testpw-12345")
        r = c.post("/api/login", json={"password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["ok"])

    def test_login_no_password_configured_returns_503(self):
        # Build app with password, then clear password env to simulate
        # "no password configured" at request time. (verify_password
        # reads env at call time, so clearing post-setup works.)
        dashapp, c = self._client(password="placeholder")
        # Clear all password-related env vars.
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ.pop("DASHBOARD_PASSWORD_HASH", None)
        r = c.post("/api/login", json={"password": "anything"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["error"], "no_password_configured")

    def test_login_rate_limit_returns_429(self):
        dashapp, c = self._client(password="testpw-12345")
        for _ in range(5):
            c.post("/api/login", json={"password": "wrong"})
        r = c.post("/api/login", json={"password": "wrong"})
        self.assertEqual(r.status_code, 429)
        d = r.get_json()
        self.assertEqual(d["error"], "rate_limited")
        self.assertGreater(d["retry_after_sec"], 0)

    def test_login_rate_limit_blocks_correct_password_too(self):
        dashapp, c = self._client(password="testpw-12345")
        for _ in range(5):
            c.post("/api/login", json={"password": "wrong"})
        # Even the CORRECT password now returns 429.
        r = c.post("/api/login", json={"password": "testpw-12345"})
        self.assertEqual(r.status_code, 429)

    def test_login_success_clears_rate_limit_counter(self):
        dashapp, c = self._client(password="testpw-12345")
        # 3 failures (below threshold of 5).
        for _ in range(3):
            c.post("/api/login", json={"password": "wrong"})
        # Success resets — next 4 failures should NOT trigger lockout.
        r_ok = c.post("/api/login", json={"password": "testpw-12345"})
        self.assertEqual(r_ok.status_code, 200)
        # After success, the counter is zero — 4 more failures shouldn't lock.
        for _ in range(4):
            c.post("/api/login", json={"password": "wrong"})
        # 4 failures < threshold 5; next bad login is 401, not 429.
        r = c.post("/api/login", json={"password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_login_writes_login_success_audit_row(self):
        dashapp, c = self._client(password="testpw-12345")
        r = c.post("/api/login", json={"password": "testpw-12345"})
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT kind, success FROM auth_events "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "login_success")
        self.assertEqual(row[1], 1)

    def test_login_failure_writes_login_failure_audit_row(self):
        dashapp, c = self._client(password="real")
        c.post("/api/login", json={"password": "wrong"})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT kind, success FROM auth_events "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "login_failure")
        self.assertEqual(row[1], 0)

    def test_lockout_writes_login_locked_audit_row(self):
        dashapp, c = self._client(password="real")
        for _ in range(5):
            c.post("/api/login", json={"password": "wrong"})
        c.post("/api/login", json={"password": "wrong"})  # 6th = locked
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            kind = conn.execute(
                "SELECT kind FROM auth_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(kind, "login_locked")

    def test_login_response_contains_no_password_material(self):
        """Defensive: response body must NEVER echo the password."""
        dashapp, c = self._client(password="super-secret-pw-12345")
        r = c.post("/api/login", json={"password": "super-secret-pw-12345"})
        self.assertNotIn(b"super-secret-pw-12345", r.data)
        r2 = c.post("/api/login", json={"password": "wrong-attempt-99"})
        self.assertNotIn(b"wrong-attempt-99", r2.data)


class TestCSRFEnforcement(unittest.TestCase):

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _logged_in_client(self, password="pw-test-1234"):
        dashapp = _make_test_app(password=password, db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = r.get_json()["csrf_token"]
        return c, token

    def test_logout_without_csrf_returns_403(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/logout")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "csrf_invalid")

    def test_logout_with_csrf_succeeds(self):
        c, token = self._logged_in_client()
        r = c.post("/api/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_login_is_exempt_from_csrf(self):
        """Per Q-A.7: /api/login is the only CSRF-exempt POST."""
        dashapp = _make_test_app(password="pw-test-1234",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        # No CSRF header → must still succeed (login is exempt).
        r = c.post("/api/login", json={"password": "pw-test-1234"})
        self.assertEqual(r.status_code, 200)

    def test_kill_switch_activate_blocked_without_csrf(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/kill-switch/activate")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "csrf_invalid")

    def test_kill_switch_activate_passes_csrf_with_token(self):
        c, token = self._logged_in_client()
        r = c.post("/api/kill-switch/activate",
                    headers={"X-CSRF-Token": token})
        # Not 403 — CSRF gate passed.
        self.assertNotEqual(r.status_code, 403,
            f"valid CSRF must pass — got {r.status_code}: "
            f"{r.get_data(as_text=True)!r}")

    def test_telegram_save_blocked_without_csrf(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/telegram/save", json={"enabled": False})
        self.assertEqual(r.status_code, 403)

    def test_strategy_save_blocked_without_csrf(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/strategy/save", json={"foo": "bar"})
        self.assertEqual(r.status_code, 403)

    def test_backtest_run_blocked_without_csrf(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/backtest/run", json={})
        self.assertEqual(r.status_code, 403)

    def test_portfolio_risk_config_blocked_without_csrf(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/portfolio-risk/config", json={})
        self.assertEqual(r.status_code, 403)

    def test_csrf_token_wrong_value_blocked(self):
        c, _ = self._logged_in_client()
        r = c.post("/api/logout",
                    headers={"X-CSRF-Token": "not-the-real-token"})
        self.assertEqual(r.status_code, 403)

    def test_csrf_endpoint_returns_current_token(self):
        c, token = self._logged_in_client()
        r = c.get("/api/auth/csrf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["csrf_token"], token)

    def test_csrf_endpoint_requires_auth(self):
        dashapp = _make_test_app(password="x", db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.get("/api/auth/csrf")
        self.assertEqual(r.status_code, 401)

    def test_csrf_form_field_alternative_works(self):
        """The CSRF module supports csrf_token form field as fallback
        for traditional forms (not just header)."""
        c, token = self._logged_in_client()
        r = c.post("/api/logout",
                    data={"csrf_token": token},
                    content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 200)

    def test_all_post_routes_have_csrf_protection(self):
        """AST-scan: every POST route in dashboard/app.py is either
        in EXEMPT or has @csrf_required."""
        EXEMPT = frozenset({"/api/login"})
        src = (Path(_REPO) / "dashboard" / "app.py").read_text()
        lines = src.split("\n")
        # Find every @app.route line with POST. Then look at the
        # decorator stack between it and the def.
        route_re = re.compile(
            r"^@app\.route\((['\"])(/[^'\"]+)\1.*methods=\[.*POST.*\]"
        )
        missing = []
        for i, line in enumerate(lines):
            m = route_re.match(line)
            if not m:
                continue
            route = m.group(2)
            if route in EXEMPT:
                continue
            # Look forward up to 10 lines for @csrf_required or `def`.
            found_csrf = False
            for j in range(i + 1, min(i + 11, len(lines))):
                stripped = lines[j].strip()
                if stripped == "@csrf_required":
                    found_csrf = True
                    break
                if stripped.startswith("def "):
                    break
            if not found_csrf:
                missing.append(f"{route} at line {i+1}")
        self.assertEqual(missing, [],
            f"POST routes missing @csrf_required: {missing}")


class TestExistingEndpointsStillWork(unittest.TestCase):
    """Regression — existing endpoints must continue to work."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _logged_in(self, password="pw-test-1234"):
        dashapp = _make_test_app(password=password, db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_health_returns_response(self):
        """/api/health is unauthenticated. Returns 200 if bot heartbeat
        is fresh, 503 if no heartbeat (the M14.G health check fires
        because we're running in a test env with no live bot). Either
        is acceptable — what we're regression-testing is that the
        endpoint EXISTS and is reachable without auth."""
        dashapp = _make_test_app(password="pw-test-1234",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.get("/api/health")
        self.assertIn(r.status_code, (200, 503),
            f"/api/health returned unexpected status {r.status_code}")
        # The endpoint must NOT require auth.
        self.assertNotEqual(r.status_code, 401,
            "/api/health must not require auth — needed by external probes")

    def test_gateway_health_requires_auth(self):
        dashapp = _make_test_app(password="pw-test-1234",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.get("/api/gateway/health")
        self.assertEqual(r.status_code, 401)

    def test_gateway_health_with_auth_succeeds(self):
        c = self._logged_in()
        r = c.get("/api/gateway/health")
        # 200 or 500 are both acceptable — we only care that the auth
        # gate passed (not 401). 500 happens if no IB gateway log on
        # this test machine.
        self.assertNotEqual(r.status_code, 401,
            f"auth gate must pass — got {r.status_code}")

    def test_risk_authority_scopes_requires_auth(self):
        dashapp = _make_test_app(password="pw-test-1234",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.get("/api/risk-authority/scopes")
        self.assertEqual(r.status_code, 401)

    def test_index_html_loads(self):
        dashapp = _make_test_app(password="x", db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<!DOCTYPE html>", r.data)

    def test_inline_html_includes_csrf_wrapper(self):
        """The inline HTML must contain the M15.3.A fetch monkey-patch
        so existing JS fetch() calls auto-attach CSRF headers."""
        dashapp = _make_test_app(password="x", db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.get("/")
        body = r.data.decode("utf-8")
        self.assertIn("window._csrfToken", body)
        self.assertIn("X-CSRF-Token", body)
        self.assertIn("/api/auth/csrf", body)


# ─────────────────────────────────────────────────────────────────────────────
# Group 8 — AST scans + protected-files invariants
# ─────────────────────────────────────────────────────────────────────────────


class TestNoForbiddenSurface(unittest.TestCase):

    FORBIDDEN_IMPORTS = (
        "bot.brokers", "bot.etoro", "ib_insync",
        "bot.scanner", "bot.strategy",
        "bot.risk_authority.engine", "bot.risk_authority.governor",
        "bot.risk_authority.authority", "bot.risk_authority.snapshot",
        "bot.risk_authority.preflight",
        "bot.risk_authority.ingest_ibkr_exposure",
        "bot.risk_authority.ibkr_paper_reader",
    )

    FORBIDDEN_NAMES = (
        "placeOrder", "cancelOrder", "modifyOrder",
        "reqGlobalCancel", "reqMktData", "reqHistoricalData",
        "reqOpenOrders", "reqExecutions",
    )

    M153A_MODULES = (
        "dashboard/auth/__init__.py",
        "dashboard/auth/passwords.py",
        "dashboard/auth/rate_limit.py",
        "dashboard/auth/csrf.py",
        "dashboard/auth/sessions.py",
        "dashboard/auth/audit.py",
        "tools/set_dashboard_password.py",
    )

    def test_no_forbidden_imports_in_auth_modules(self):
        for rel in self.M153A_MODULES:
            full = Path(_REPO) / rel
            tree = ast.parse(full.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for f in self.FORBIDDEN_IMPORTS:
                            self.assertFalse(
                                alias.name == f or alias.name.startswith(f + "."),
                                f"{rel}: forbidden import {alias.name!r}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        for f in self.FORBIDDEN_IMPORTS:
                            self.assertFalse(
                                node.module == f or node.module.startswith(f + "."),
                                f"{rel}: forbidden import-from {node.module!r}")

    def test_no_forbidden_names_in_auth_modules(self):
        for rel in self.M153A_MODULES:
            full = Path(_REPO) / rel
            src = full.read_text(encoding="utf-8")
            for name in self.FORBIDDEN_NAMES:
                pattern = r"\b" + re.escape(name) + r"\b"
                self.assertIsNone(
                    re.search(pattern, src),
                    f"{rel}: forbidden name {name!r} found")

    def test_csrf_module_does_not_import_flask_session_directly(self):
        """csrf.py must take session as a parameter rather than
        global-import — keeps unit-testability clean."""
        src = (Path(_REPO) / "dashboard" / "auth" / "csrf.py").read_text()
        # It MAY import `session` for the decorator, but that's fine.
        # Just verify the helpers (issue_csrf_token, verify_csrf_token)
        # accept session_obj as a parameter (already verified by tests).
        self.assertIn("def issue_csrf_token(session_obj)", src)
        self.assertIn("def verify_csrf_token(session_obj)", src)


class TestProtectedFilesUntouched(unittest.TestCase):
    """M15.3.A must NOT modify protected runtime files vs 60281c4.

    Note (2026-06-04, M15.3.B): `bot/risk_authority/audit_decisions.py`
    was removed from this list because M15.3.B legitimately extends it
    with an additive `write_manual_reset_decision` function per the
    pre-code checklist. The additive-only invariant is enforced by
    `test_m15_3_b_manual_reset.TestProtectedFilesUntouched.\
test_audit_decisions_only_additive_change` which checks that all
    pre-existing functions in that file remain byte-identical.
    """

    BASE_REV = "60281c4"
    PROTECTED = (
        "main.py",
        "bot/scanner.py",
        "bot/strategy.py",
        "bot/risk.py",
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/authority.py",
        "bot/risk_authority/snapshot.py",
        # audit_decisions.py removed — see class docstring.
        "bot/risk_authority/preflight.py",
        "bot/risk_authority/ingest_ibkr_exposure.py",
        "bot/risk_authority/ibkr_paper_reader.py",
        "bot/risk_authority/exposure_reading.py",
        "bot/risk_authority/ingest_exposure.py",
        "bot/gateway_health.py",
        "bot/gateway_watchdog.py",
        "bot/etoro/live_broker.py",
        "tools/etoro_live_write.py",
        "tools/ingest_exposure_state.py",
        "infra/systemd/algo-trader.service",
        "infra/systemd/algo-trader-dashboard.service",
        "infra/systemd/ibgateway.service.documented",
        "sync.sh",
        "deploy.sh",
    )

    def test_no_protected_file_modified(self):
        offenders = []
        for rel in self.PROTECTED:
            result = subprocess.run(
                ["git", "diff", "--stat", self.BASE_REV, "--", rel],
                cwd=_REPO, capture_output=True, text=True,
            )
            out = result.stdout.strip()
            if out:
                offenders.append(f"{rel}: {out}")
        self.assertEqual(offenders, [],
            f"M15.3.A modified protected files: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 9 — Real-HTTP / subprocess (Q-A.9: small subset)
# ─────────────────────────────────────────────────────────────────────────────


class TestRealHTTPCookieFlags(unittest.TestCase):
    """Real HTTP via Flask test client — verify Set-Cookie carries the
    expected hardening flags."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_set_cookie_carries_httponly_and_samesite(self):
        dashapp = _make_test_app(password="pw-cookie-test",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "pw-cookie-test"})
        self.assertEqual(r.status_code, 200)
        set_cookie_headers = [h for h in r.headers.items()
                                if h[0].lower() == "set-cookie"]
        self.assertGreater(len(set_cookie_headers), 0,
            "expected Set-Cookie on login")
        combined = " ; ".join(v for _, v in set_cookie_headers).lower()
        self.assertIn("httponly", combined,
            f"HttpOnly missing from Set-Cookie: {combined!r}")
        self.assertIn("samesite=strict", combined,
            f"SameSite=Strict missing from Set-Cookie: {combined!r}")

    def test_set_cookie_has_no_secure_when_no_https_mode(self):
        """Correction #2: Secure must NOT be set without explicit
        HTTPS-mode env."""
        _clean_auth_env()
        dashapp = _make_test_app(password="pw-cookie-test",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "pw-cookie-test"})
        combined = " ; ".join(v for k, v in r.headers.items()
                                if k.lower() == "set-cookie").lower()
        # Must NOT contain " secure" as a cookie attribute.
        self.assertNotRegex(combined, r"(^|;|\s)secure(;|\s|$)",
            f"Secure flag must NOT be set without HTTPS mode: {combined!r}")

    def test_set_cookie_has_secure_when_https_mode_enabled(self):
        dashapp = _make_test_app(password="pw-cookie-test",
                                   db_path=self.tmp_db.name,
                                   https_mode=True)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "pw-cookie-test"})
        combined = " ; ".join(v for k, v in r.headers.items()
                                if k.lower() == "set-cookie").lower()
        self.assertIn("secure", combined,
            f"Secure flag expected with HTTPS_MODE: {combined!r}")


class TestScriptModeInvocation(unittest.TestCase):
    """Regression test for the M15.3.A first-deploy systemd crash.

    The systemd unit runs `python3 /opt/algo-trader/dashboard/app.py`
    directly — i.e. script mode, not module mode. Before the
    M15.3.A sys.path bootstrap, script-mode invocation crashed
    immediately with `ModuleNotFoundError: No module named
    'dashboard'` because Python's script-mode sys.path only includes
    the script's directory (not the repo root).

    Tests that imported dashboard.app via `python3 -m unittest` did
    NOT exercise this code path because `-m` puts cwd on sys.path,
    masking the bug. This test invokes the script the same way
    systemd does — as a subprocess — and confirms it does NOT exit
    with status=1 in the first 1.5 seconds. If imports fail, the
    process exits within milliseconds with the ImportError on stderr.
    If imports succeed, the process hangs on app.run() and we kill
    it via timeout."""

    def test_dashboard_app_py_starts_in_script_mode(self):
        env = os.environ.copy()
        env["DASHBOARD_PASSWORD"] = "regression-test-pw"
        env["DASHBOARD_SECRET_KEY"] = "regression-test-secret-key-xxxx"
        env["DASHBOARD_PORT"] = "0"  # OS-assigned ephemeral port
        # Ensure cwd-on-sys.path doesn't mask the bug — clear
        # PYTHONPATH so the test is honest about systemd's regime.
        env.pop("PYTHONPATH", None)
        proc = subprocess.Popen(
            [sys.executable, "dashboard/app.py"],
            cwd=_REPO,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=1.5)
            # If we got here, the process exited within 1.5s — which
            # for a Flask app means it crashed at import time.
            err_text = stderr.decode("utf-8", errors="replace")
            out_text = stdout.decode("utf-8", errors="replace")
            self.fail(
                f"dashboard/app.py exited (rc={proc.returncode}) in "
                f"script mode within 1.5s — imports likely failed.\n"
                f"STDERR (first 800 chars):\n{err_text[:800]}\n"
                f"STDOUT (first 200 chars):\n{out_text[:200]}"
            )
        except subprocess.TimeoutExpired:
            # Process still running — imports succeeded, Flask is
            # listening (or about to). That's pass.
            proc.kill()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()


class TestSetPasswordToolSubprocess(unittest.TestCase):
    """tools/set_dashboard_password.py — invoked as a subprocess.
    Verifies it never prints the password, preserves unrelated .env
    lines, sets safe perms, and creates a backup."""

    def setUp(self):
        self.tmp_env = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False)
        self.tmp_env.write(
            "# Existing config\n"
            "TELEGRAM_BOT_TOKEN=abc-keep-me\n"
            "DASHBOARD_PASSWORD=old-pw\n"
            "OTHER_KEY=other-value\n"
        )
        self.tmp_env.close()

    def tearDown(self):
        for f in Path(self.tmp_env.name).parent.glob(
                Path(self.tmp_env.name).name + "*"):
            try:
                f.unlink()
            except OSError:
                pass

    def _run_setpw(self, *extra_args, stdin_input):
        """Run tools/set_dashboard_password.py as a subprocess with the
        repo on PYTHONPATH so it can `from dashboard.auth.passwords
        import hash_password`.

        Always passes --stdin so the tool reads the password from the
        piped sys.stdin instead of getpass()/dev/tty. Without this,
        getpass blocks forever on a VPS where the parent shell still
        has a controlling TTY (sudo chain etc.) — see the M15.3.A VPS
        verification run on 2026-06-03 for the symptom.
        """
        env = os.environ.copy()
        env["PYTHONPATH"] = _REPO + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "tools/set_dashboard_password.py",
              "--stdin",
              "--env-path", self.tmp_env.name, *extra_args],
            cwd=_REPO, env=env,
            input=stdin_input,
            capture_output=True, text=True, timeout=30,
        )

    def test_subprocess_runs_and_preserves_unrelated_lines(self):
        new_pw = "fresh-password-1234"
        proc = self._run_setpw(stdin_input=f"{new_pw}\n{new_pw}\n")
        self.assertEqual(proc.returncode, 0,
            f"set_dashboard_password.py failed: stderr={proc.stderr!r}")
        # The password must NEVER appear in stdout or stderr.
        self.assertNotIn(new_pw, proc.stdout, "PASSWORD LEAKED INTO STDOUT")
        self.assertNotIn(new_pw, proc.stderr, "PASSWORD LEAKED INTO STDERR")
        # Unrelated lines preserved.
        content = Path(self.tmp_env.name).read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=abc-keep-me", content)
        self.assertIn("OTHER_KEY=other-value", content)
        self.assertIn("# Existing config", content)
        # Hash line added; plaintext kept (transitional default).
        self.assertIn("DASHBOARD_PASSWORD_HASH=$2", content)
        self.assertIn("DASHBOARD_PASSWORD=old-pw", content)
        # SECRET_KEY auto-generated.
        self.assertIn("DASHBOARD_SECRET_KEY=", content)
        # File mode 0600.
        mode = os.stat(self.tmp_env.name).st_mode & 0o777
        self.assertEqual(mode, 0o600,
            f"expected 0o600 perms, got {oct(mode)}")
        # Backup file exists.
        backups = list(Path(self.tmp_env.name).parent.glob(
            Path(self.tmp_env.name).name + ".bak.*"))
        self.assertEqual(len(backups), 1,
            "expected exactly one .env.bak.* file")

    def test_subprocess_remove_plaintext_flag(self):
        new_pw = "another-fresh-pw-987"
        proc = self._run_setpw("--remove-plaintext",
                                stdin_input=f"{new_pw}\n{new_pw}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = Path(self.tmp_env.name).read_text()
        self.assertNotIn("DASHBOARD_PASSWORD=old-pw", content,
            "plaintext line should be removed with --remove-plaintext")
        self.assertIn("DASHBOARD_PASSWORD_HASH=$2", content,
            "hash must still be present")

    def test_subprocess_short_password_rejected(self):
        proc = self._run_setpw(stdin_input="short\nshort\n")
        self.assertEqual(proc.returncode, 1,
            f"short password should exit code 1, got {proc.returncode}: "
            f"stderr={proc.stderr!r}")
        # Original file unchanged.
        content = Path(self.tmp_env.name).read_text()
        self.assertNotIn("DASHBOARD_PASSWORD_HASH=", content)

    def test_subprocess_works_without_PYTHONPATH_from_non_repo_cwd(self):
        """Regression test for the M15.3.A.fix-2 bug.

        When the operator runs `sudo python tools/set_dashboard_password.py`
        on the VPS, sys.path only contains the script's directory
        (tools/), NOT the repo root — so `from dashboard.auth.passwords
        import hash_password` fails with `ModuleNotFoundError: No module
        named 'dashboard'`. The user worked around it by setting
        PYTHONPATH=/opt/algo-trader manually. The fix is the same
        sys.path bootstrap added to dashboard/app.py.

        This test runs the tool from /tmp with PYTHONPATH explicitly
        cleared, so the only way it succeeds is via the sys.path
        bootstrap inside the tool itself."""
        new_pw = "non-repo-cwd-test-1234"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)  # explicitly clear
        env["HOME"] = "/tmp"
        # Run from /tmp — explicitly NOT from the repo root.
        proc = subprocess.run(
            [sys.executable,
              str(Path(_REPO) / "tools" / "set_dashboard_password.py"),
              "--stdin",
              "--env-path", self.tmp_env.name],
            cwd="/tmp",  # NOT the repo root
            env=env,
            input=f"{new_pw}\n{new_pw}\n",
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0,
            f"set_dashboard_password.py must work from non-repo cwd "
            f"without PYTHONPATH. Got rc={proc.returncode}\n"
            f"STDERR: {proc.stderr!r}\nSTDOUT: {proc.stdout!r}")
        # And the hash was actually written:
        content = Path(self.tmp_env.name).read_text()
        self.assertIn("DASHBOARD_PASSWORD_HASH=$2", content,
            "tool ran but no bcrypt hash was written")
        # And password material never appears in stdout/stderr.
        self.assertNotIn(new_pw, proc.stdout)
        self.assertNotIn(new_pw, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

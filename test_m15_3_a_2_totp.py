"""M15.3.A.2 — Dashboard TOTP / Google Authenticator 2FA tests.

Approved scope per Q-A.1..Q-A.11 + Corrections 1-9:
  G1: TOTP primitives (generate, verify, replay cache)
  G2: Disabled mode (env unset) — password-only login unchanged (HARD GUARANTEE)
  G3: Enabled mode — missing/wrong/right code paths
  G4: --enable-totp / --disable-totp tool flags + audit resilience
  G5: Login combinations (right/wrong pw × right/wrong/missing code)
  G6: Rate-limiter integration (5 wrong TOTP → lockout, same per-IP bucket)
  G7: auth_events kinds (5 new closed-set values, no secret material)
  G8: Replay prevention (same time-step blocked within TTL)
  G9: AST scan + protected files (no forbidden imports, 0/24 protected files)

Hard constraints honoured:
  * No orders, no broker writes, no live mode
  * No scanner/strategy/M14 engine/eToro/IBKR changes
  * No multi-user, no manual_reset
  * No secrets printed in test output (asserted in G7)
  * Existing password-only login preserved when DASHBOARD_TOTP_SECRET unset (G2)
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
    "DASHBOARD_TOTP_SECRET",
)


def _clean_env():
    for k in _AUTH_ENV_KEYS:
        os.environ.pop(k, None)


_DASHAPP_SINGLETON = None


def _make_test_app(*, password="testpw-12345",
                    totp_secret=None,
                    db_path=None,
                    secret_key="test_m15_3_a_2_secret_xxxxxxxx"):
    """Build (or reuse) the dashboard.app test instance with explicit env.

    Per-test state reset: env, rate-limiter, replay-cache, DB_PATH."""
    global _DASHAPP_SINGLETON
    _clean_env()
    os.environ["DASHBOARD_SECRET_KEY"] = secret_key
    os.environ["DASHBOARD_PASSWORD"] = password
    if totp_secret:
        os.environ["DASHBOARD_TOTP_SECRET"] = totp_secret

    if _DASHAPP_SINGLETON is None:
        from dashboard import app as dashapp
        _DASHAPP_SINGLETON = dashapp
    dashapp = _DASHAPP_SINGLETON
    dashapp.app.config["TESTING"] = True
    # Silence the cookie/bind-host warnings during tests.
    import logging
    silent = logging.getLogger("test_silent_m153a2")
    silent.addHandler(logging.NullHandler())
    silent.propagate = False
    from dashboard.auth.sessions import harden_app_config
    harden_app_config(dashapp.app, logger=silent)
    # Fresh rate-limiter.
    from dashboard.auth.rate_limit import RateLimiter
    dashapp._m153a_login_limiter = RateLimiter(
        threshold=5, window_sec=600, lockout_sec=900,
    )
    # Fresh replay cache.
    from dashboard.auth import totp as totp_mod
    totp_mod.reset_default_replay_cache()
    if db_path is not None:
        dashapp.DB_PATH = Path(db_path)
        from dashboard.auth.audit import ensure_auth_events_schema
        c = sqlite3.connect(db_path)
        try:
            ensure_auth_events_schema(c)
        finally:
            c.close()
    return dashapp


def _current_code(secret: str, clock=time.time) -> str:
    import pyotp
    return pyotp.TOTP(secret).at(int(clock()))


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — TOTP primitives
# ─────────────────────────────────────────────────────────────────────────────


class TestTOTPPrimitives(unittest.TestCase):

    def setUp(self):
        _clean_env()
        from dashboard.auth import totp as totp_mod
        importlib.reload(totp_mod)
        self.totp = totp_mod
        totp_mod.reset_default_replay_cache()

    def test_totp_enabled_false_when_env_unset(self):
        _clean_env()
        self.assertFalse(self.totp.totp_enabled())

    def test_totp_enabled_false_when_empty(self):
        os.environ["DASHBOARD_TOTP_SECRET"] = ""
        self.assertFalse(self.totp.totp_enabled())
        os.environ["DASHBOARD_TOTP_SECRET"] = "   "
        self.assertFalse(self.totp.totp_enabled())

    def test_totp_enabled_true_when_set(self):
        os.environ["DASHBOARD_TOTP_SECRET"] = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        self.assertTrue(self.totp.totp_enabled())

    def test_generate_secret_format(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        s = self.totp.generate_secret()
        self.assertEqual(len(s), 32)
        self.assertTrue(all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s))
        # Two calls produce different secrets.
        s2 = self.totp.generate_secret()
        self.assertNotEqual(s, s2)

    def test_otpauth_uri_format(self):
        s = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        uri = self.totp.build_otpauth_uri(s, account_name="me",
                                            issuer="MyApp")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(f"secret={s}", uri)
        self.assertIn("MyApp", uri)
        self.assertIn("period=30", uri)
        self.assertIn("digits=6", uri)

    def test_otpauth_uri_rejects_empty_secret(self):
        with self.assertRaises(ValueError):
            self.totp.build_otpauth_uri("")

    def test_render_qr_terminal_returns_string(self):
        try:
            import qrcode  # noqa: F401
        except ImportError:
            self.skipTest("qrcode not installed")
        uri = self.totp.build_otpauth_uri("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
        qr = self.totp.render_qr_terminal(uri)
        self.assertIsInstance(qr, str)
        # QR rendered as multi-line string.
        self.assertGreater(len(qr.split("\n")), 5)

    def test_verify_correct_code(self):
        try:
            import pyotp
        except ImportError:
            self.skipTest("pyotp not installed")
        s = self.totp.generate_secret()
        t = [1000000.0]
        clk = lambda: t[0]
        code = pyotp.TOTP(s).at(int(t[0]))
        ok, info = self.totp.verify_code(code, secret=s,
                                           replay_cache=self.totp.ReplayCache(clock=clk),
                                           clock=clk)
        self.assertTrue(ok)
        self.assertEqual(info["reason"], "ok")
        self.assertEqual(info["window"], 0)

    def test_verify_wrong_code(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        s = self.totp.generate_secret()
        ok, info = self.totp.verify_code("000000", secret=s)
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "wrong_code")

    def test_verify_malformed_code(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        s = self.totp.generate_secret()
        for bad in ("abc", "12345", "1234567", "12 456", "", None):
            ok, info = self.totp.verify_code(bad, secret=s)  # type: ignore
            self.assertFalse(ok, f"input {bad!r} must reject")
            self.assertEqual(info["reason"], "wrong_format")

    def test_verify_no_secret(self):
        _clean_env()
        ok, info = self.totp.verify_code("123456")  # no env secret
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "no_secret")


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 — Disabled mode (HARD GUARANTEE: M15.3.A behaviour preserved)
# ─────────────────────────────────────────────────────────────────────────────


class TestTOTPDisabledMode(unittest.TestCase):
    """When DASHBOARD_TOTP_SECRET is unset/empty, the dashboard MUST
    behave exactly as M15.3.A — password-only login. This is the
    explicit operator constraint: 'do not break current password login
    until TOTP is explicitly enabled'."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_password_only_login_works_when_totp_unset(self):
        dashapp = _make_test_app(password="testpw-1234", totp_secret=None,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "testpw-1234"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_totp_code_ignored_when_totp_unset(self):
        """Even if the client sends a totp_code, server ignores when env unset."""
        dashapp = _make_test_app(password="testpw-1234", totp_secret=None,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={
            "password": "testpw-1234",
            "totp_code": "anything-totally-ignored",
        })
        self.assertEqual(r.status_code, 200)

    def test_wrong_password_still_401_when_totp_unset(self):
        dashapp = _make_test_app(password="real-pw", totp_secret=None,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "wrong"})
        self.assertEqual(r.status_code, 401)
        # Generic 401 — no totp_required leak even if client sent a code.
        self.assertNotIn("totp", json.dumps(r.get_json()))

    def test_empty_totp_secret_treated_as_unset(self):
        # Operator wrote DASHBOARD_TOTP_SECRET= (empty value) — still treat as off.
        dashapp = _make_test_app(password="testpw-1234", totp_secret="",
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        r = c.post("/api/login", json={"password": "testpw-1234"})
        self.assertEqual(r.status_code, 200)

    def test_no_totp_audit_kinds_written_when_disabled(self):
        """When TOTP is off, no totp_* rows should appear in auth_events
        even after a successful login."""
        dashapp = _make_test_app(password="testpw", totp_secret=None,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        c.post("/api/login", json={"password": "testpw"})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            totp_rows = conn.execute(
                "SELECT COUNT(*) FROM auth_events WHERE kind LIKE 'totp%'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(totp_rows, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Group 3 — Enabled mode
# ─────────────────────────────────────────────────────────────────────────────


class TestTOTPEnabledMode(unittest.TestCase):

    def setUp(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _client(self):
        dashapp = _make_test_app(password="testpw-1234",
                                   totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        return dashapp.app.test_client()

    def test_missing_totp_returns_totp_required(self):
        c = self._client()
        r = c.post("/api/login", json={"password": "testpw-1234"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json().get("error"), "totp_required")

    def test_empty_totp_returns_totp_required(self):
        c = self._client()
        r = c.post("/api/login", json={"password": "testpw-1234",
                                          "totp_code": ""})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json().get("error"), "totp_required")

    def test_wrong_totp_returns_generic_401(self):
        c = self._client()
        r = c.post("/api/login", json={"password": "testpw-1234",
                                          "totp_code": "000000"})
        self.assertEqual(r.status_code, 401)
        # MUST NOT leak that TOTP was the failed factor.
        self.assertNotIn("totp", json.dumps(r.get_json()).lower())

    def test_malformed_totp_returns_generic_401(self):
        c = self._client()
        r = c.post("/api/login", json={"password": "testpw-1234",
                                          "totp_code": "abc"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("totp", json.dumps(r.get_json()).lower())

    def test_correct_totp_logs_in(self):
        c = self._client()
        code = _current_code(self.secret)
        r = c.post("/api/login", json={"password": "testpw-1234",
                                          "totp_code": code})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertIn("csrf_token", r.get_json())

    def test_wrong_pw_correct_totp_returns_401_generic(self):
        c = self._client()
        code = _current_code(self.secret)
        r = c.post("/api/login", json={"password": "WRONG",
                                          "totp_code": code})
        self.assertEqual(r.status_code, 401)
        # Wrong password short-circuits before TOTP — same as M15.3.A.
        self.assertNotIn("totp", json.dumps(r.get_json()).lower())

    def test_wrong_pw_missing_totp_returns_401_generic_not_totp_required(self):
        """The totp_required hint MUST appear only after password verifies."""
        c = self._client()
        r = c.post("/api/login", json={"password": "WRONG"})
        self.assertEqual(r.status_code, 401)
        self.assertNotEqual(r.get_json().get("error"), "totp_required")


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — --enable-totp / --disable-totp tool flags
# ─────────────────────────────────────────────────────────────────────────────


class TestSetpwTOTPFlags(unittest.TestCase):

    def setUp(self):
        try:
            import pyotp, qrcode  # noqa: F401
        except ImportError:
            self.skipTest("pyotp/qrcode not installed")
        self.tmp_env = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False)
        self.tmp_env.write(
            "TELEGRAM_TOKEN=keep-me\n"
            "DASHBOARD_PASSWORD=existing-pw-12345\n"
            "OTHER_KEY=other-value\n"
        )
        self.tmp_env.close()
        self.known_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def tearDown(self):
        for f in Path(self.tmp_env.name).parent.glob(
                Path(self.tmp_env.name).name + "*"):
            try:
                f.unlink()
            except OSError:
                pass

    def _run_enable_with_known_secret(self, code_str):
        """Invoke --enable-totp with generate_secret monkey-patched to
        return self.known_secret. Pipes code_str as the verify input."""
        wrapper = (
            'import sys; sys.path.insert(0, "."); '
            'import dashboard.auth.totp as t; '
            f't.generate_secret = lambda: "{self.known_secret}"; '
            'from tools.set_dashboard_password import main; '
            f'sys.exit(main(["--enable-totp", "--stdin", '
            f'"--env-path", "{self.tmp_env.name}"]))'
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = _REPO
        return subprocess.run(
            [sys.executable, "-c", wrapper],
            input=code_str + "\n",
            capture_output=True, text=True, timeout=30, env=env,
            cwd=_REPO,
        )

    def test_enable_totp_writes_secret_after_verify(self):
        import pyotp
        correct = pyotp.TOTP(self.known_secret).now()
        proc = self._run_enable_with_known_secret(correct)
        self.assertEqual(proc.returncode, 0,
            f"stderr: {proc.stderr!r}\nstdout tail: {proc.stdout[-300:]!r}")
        content = Path(self.tmp_env.name).read_text()
        self.assertIn(f"DASHBOARD_TOTP_SECRET={self.known_secret}", content)
        # Other lines preserved.
        self.assertIn("TELEGRAM_TOKEN=keep-me", content)
        self.assertIn("DASHBOARD_PASSWORD=existing-pw-12345", content)
        # Permissions 0600.
        mode = os.stat(self.tmp_env.name).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # Backup file exists.
        backups = list(Path(self.tmp_env.name).parent.glob(
            Path(self.tmp_env.name).name + ".bak.*"))
        self.assertEqual(len(backups), 1)

    def test_enable_totp_rejects_wrong_code(self):
        proc = self._run_enable_with_known_secret("000000")
        self.assertEqual(proc.returncode, 1)
        # .env was NOT modified (no DASHBOARD_TOTP_SECRET line).
        content = Path(self.tmp_env.name).read_text()
        self.assertNotIn("DASHBOARD_TOTP_SECRET=", content)
        self.assertIn("DASHBOARD_PASSWORD=existing-pw-12345", content)

    def test_enable_totp_refuses_overwrite(self):
        """If DASHBOARD_TOTP_SECRET is already set, --enable-totp must
        refuse to silently overwrite (lockout-prevention)."""
        # Pre-populate the file.
        with open(self.tmp_env.name, "a") as f:
            f.write("DASHBOARD_TOTP_SECRET=PRE-EXISTING-SECRET-VALUE\n")
        proc = self._run_enable_with_known_secret("000000")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("already set", proc.stderr.lower())
        # Pre-existing value untouched.
        content = Path(self.tmp_env.name).read_text()
        self.assertIn("DASHBOARD_TOTP_SECRET=PRE-EXISTING-SECRET-VALUE", content)

    def test_enable_totp_refuses_without_password(self):
        """No DASHBOARD_PASSWORD / _HASH → reject (TOTP needs first factor)."""
        with open(self.tmp_env.name, "w") as f:
            f.write("OTHER_KEY=lonely\n")  # no password
        # Run with env explicitly stripped of password-related vars.
        env = {k: v for k, v in os.environ.items()
                if k not in ("DASHBOARD_PASSWORD", "DASHBOARD_PASSWORD_HASH")}
        env["PYTHONPATH"] = _REPO
        wrapper = (
            'import sys\n'
            'sys.path.insert(0, ".")\n'
            'import dashboard.auth.totp as t\n'
            f't.generate_secret = lambda: "{self.known_secret}"\n'
            'from tools.set_dashboard_password import main\n'
            f'sys.exit(main(["--enable-totp", "--stdin", '
            f'"--env-path", "{self.tmp_env.name}"]))\n'
        )
        proc = subprocess.run(
            [sys.executable, "-c", wrapper],
            input="000000\n",
            capture_output=True, text=True, timeout=15, env=env, cwd=_REPO,
        )
        self.assertEqual(proc.returncode, 1,
            f"expected rc=1; got {proc.returncode}\n"
            f"stderr: {proc.stderr!r}\nstdout: {proc.stdout[-200:]!r}")
        self.assertIn("no password configured", proc.stderr.lower())

    def test_disable_totp_removes_secret_preserves_password(self):
        # Pre-populate.
        with open(self.tmp_env.name, "a") as f:
            f.write("DASHBOARD_TOTP_SECRET=PREVIOUSLY_SET_SECRET\n")
        proc = subprocess.run(
            [sys.executable, "tools/set_dashboard_password.py",
              "--disable-totp", "--env-path", self.tmp_env.name],
            capture_output=True, text=True, timeout=15, cwd=_REPO,
            env={**os.environ, "PYTHONPATH": _REPO},
        )
        self.assertEqual(proc.returncode, 0)
        content = Path(self.tmp_env.name).read_text()
        self.assertNotIn("DASHBOARD_TOTP_SECRET=", content)
        self.assertIn("DASHBOARD_PASSWORD=existing-pw-12345", content)
        self.assertIn("TELEGRAM_TOKEN=keep-me", content)
        # Permissions preserved.
        mode = os.stat(self.tmp_env.name).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_disable_totp_idempotent_when_not_set(self):
        proc = subprocess.run(
            [sys.executable, "tools/set_dashboard_password.py",
              "--disable-totp", "--env-path", self.tmp_env.name],
            capture_output=True, text=True, timeout=15, cwd=_REPO,
            env={**os.environ, "PYTHONPATH": _REPO},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not set", proc.stdout.lower())

    def test_enable_and_disable_are_mutually_exclusive(self):
        proc = subprocess.run(
            [sys.executable, "tools/set_dashboard_password.py",
              "--enable-totp", "--disable-totp",
              "--env-path", self.tmp_env.name],
            capture_output=True, text=True, timeout=15, cwd=_REPO,
            env={**os.environ, "PYTHONPATH": _REPO},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("mutually exclusive", proc.stderr.lower())

    def test_enable_totp_never_prints_secret_to_stderr(self):
        """Defence-in-depth: even though secret IS shown on stdout
        (operator's TTY), it must NEVER be on stderr (which could be
        redirected to a log)."""
        import pyotp
        correct = pyotp.TOTP(self.known_secret).now()
        proc = self._run_enable_with_known_secret(correct)
        self.assertNotIn(self.known_secret, proc.stderr,
            "SECRET LEAKED TO STDERR")

    def test_enable_totp_stdout_contains_qr_and_secret_only(self):
        """Stdout should contain the QR + secret (operator's terminal),
        but the secret must appear AT MOST in the labeled 'Secret:' line
        and as part of the otpauth URI in the QR encoding (which is
        binary in the QR, not literal in stdout). Verifies we don't
        accidentally log the otpauth URI in plaintext."""
        import pyotp
        correct = pyotp.TOTP(self.known_secret).now()
        proc = self._run_enable_with_known_secret(correct)
        # Secret should appear in stdout (intentional, operator must see it).
        self.assertIn(self.known_secret, proc.stdout)
        # otpauth:// URI should NOT appear literally — it's encoded into
        # the QR only.
        self.assertNotIn("otpauth://", proc.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — Rate-limit integration
# ─────────────────────────────────────────────────────────────────────────────


class TestTOTPRateLimitIntegration(unittest.TestCase):

    def setUp(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_five_wrong_totp_codes_lock_out_ip(self):
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        for _ in range(5):
            r = c.post("/api/login", json={"password": "pw",
                                              "totp_code": "000000"})
            self.assertEqual(r.status_code, 401)
        # 6th attempt should be locked out.
        r = c.post("/api/login", json={"password": "pw",
                                          "totp_code": "000000"})
        self.assertEqual(r.status_code, 429)

    def test_wrong_totp_counts_in_same_bucket_as_wrong_password(self):
        """Mixed wrong-pw + wrong-TOTP should still trigger lockout
        at 5 total failures (per Correction 3)."""
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        # 3 wrong passwords
        for _ in range(3):
            c.post("/api/login", json={"password": "wrong"})
        # 2 wrong TOTPs (correct password)
        for _ in range(2):
            c.post("/api/login", json={"password": "pw",
                                          "totp_code": "000000"})
        # 6th attempt — locked out.
        r = c.post("/api/login", json={"password": "pw",
                                          "totp_code": "000000"})
        self.assertEqual(r.status_code, 429)

    def test_missing_totp_does_not_increment_counter(self):
        """totp_required (right pw + missing code) does NOT count as a
        failure — the operator just forgot to enter the code."""
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        # 10 missing-code requests (would lock out if counted).
        for _ in range(10):
            r = c.post("/api/login", json={"password": "pw"})
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.get_json().get("error"), "totp_required")
        # Operator now provides correct credentials — should succeed.
        import pyotp
        code = pyotp.TOTP(self.secret).now()
        r2 = c.post("/api/login", json={"password": "pw",
                                          "totp_code": code})
        self.assertEqual(r2.status_code, 200)

    def test_successful_totp_clears_counter(self):
        import pyotp
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        # 4 wrong TOTPs (below threshold of 5).
        for _ in range(4):
            c.post("/api/login", json={"password": "pw",
                                          "totp_code": "000000"})
        # Now succeed — counter resets.
        code = pyotp.TOTP(self.secret).now()
        r = c.post("/api/login", json={"password": "pw",
                                          "totp_code": code})
        self.assertEqual(r.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — Replay prevention
# ─────────────────────────────────────────────────────────────────────────────


class TestReplayPrevention(unittest.TestCase):

    def setUp(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        from dashboard.auth import totp as totp_mod
        importlib.reload(totp_mod)
        self.totp = totp_mod
        self.secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def test_same_code_within_window_rejected(self):
        import pyotp
        t = [1_000_000.0]
        clk = lambda: t[0]
        cache = self.totp.ReplayCache(clock=clk)
        code = pyotp.TOTP(self.secret).at(int(t[0]))
        # First use: ok.
        ok1, _ = self.totp.verify_code(code, secret=self.secret,
                                         replay_cache=cache, clock=clk)
        # Same code again within same 30-sec window: rejected.
        ok2, info2 = self.totp.verify_code(code, secret=self.secret,
                                             replay_cache=cache, clock=clk)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(info2["reason"], "replay")

    def test_different_secret_uses_different_cache_namespace(self):
        """Replay cache is keyed by (secret_fp, time_step), so a code
        accepted for secret A doesn't block the same code for secret B."""
        import pyotp
        s1 = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        s2 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        t = [1_000_000.0]
        clk = lambda: t[0]
        cache = self.totp.ReplayCache(clock=clk)
        c1 = pyotp.TOTP(s1).at(int(t[0]))
        c2 = pyotp.TOTP(s2).at(int(t[0]))
        self.totp.verify_code(c1, secret=s1, replay_cache=cache, clock=clk)
        ok2, _ = self.totp.verify_code(c2, secret=s2,
                                          replay_cache=cache, clock=clk)
        self.assertTrue(ok2)

    def test_cache_entry_expires_after_ttl(self):
        import pyotp
        t = [1_000_000.0]
        clk = lambda: t[0]
        cache = self.totp.ReplayCache(ttl_sec=120, clock=clk)
        code = pyotp.TOTP(self.secret).at(int(t[0]))
        self.totp.verify_code(code, secret=self.secret,
                                replay_cache=cache, clock=clk)
        self.assertEqual(cache.size(), 1)
        # Advance past TTL.
        t[0] += 130
        # _prune() runs on next is_replay/size; observe size drops to 0.
        self.assertEqual(cache.size(), 0)

    def test_cache_key_is_fingerprint_not_secret(self):
        """Defence-in-depth: dump the cache's internal storage and
        assert the raw secret never appears."""
        import pyotp
        cache = self.totp.ReplayCache()
        code = pyotp.TOTP(self.secret).now()
        # Run a real verify so cache populates.
        self.totp.verify_code(code, secret=self.secret, replay_cache=cache)
        for (key_fp, step), _ts in cache._used.items():
            self.assertNotEqual(key_fp, self.secret,
                "raw secret leaked into cache key")
            self.assertEqual(len(key_fp), 16,
                "cache key should be 16-char fingerprint, not raw secret")

    def test_replay_cache_invalid_args_rejected(self):
        with self.assertRaises(ValueError):
            self.totp.ReplayCache(ttl_sec=0)

    def test_replay_cache_clear(self):
        cache = self.totp.ReplayCache()
        cache.record_accepted("xxx", 12345)
        self.assertEqual(cache.size(), 1)
        cache.clear()
        self.assertEqual(cache.size(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Group 7 — auth_events kinds + no secret material
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthEventsTOTPKinds(unittest.TestCase):

    def setUp(self):
        try:
            import pyotp  # noqa: F401
        except ImportError:
            self.skipTest("pyotp not installed")
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def test_all_five_new_kinds_in_allowed_set(self):
        from dashboard.auth.audit import ALLOWED_KINDS
        for k in ("totp_success", "totp_failure",
                   "totp_required_not_provided",
                   "totp_setup", "totp_disabled"):
            self.assertIn(k, ALLOWED_KINDS)

    def test_totp_success_audited(self):
        import pyotp
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        code = pyotp.TOTP(self.secret).now()
        c.post("/api/login", json={"password": "pw", "totp_code": code})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            rows = conn.execute(
                "SELECT kind FROM auth_events ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        kinds = [r[0] for r in rows]
        self.assertIn("totp_success", kinds)
        self.assertIn("login_success", kinds)

    def test_totp_failure_audited(self):
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        c.post("/api/login", json={"password": "pw", "totp_code": "000000"})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            kinds = [r[0] for r in conn.execute(
                "SELECT kind FROM auth_events").fetchall()]
        finally:
            conn.close()
        self.assertIn("totp_failure", kinds)
        # login_success NOT written.
        self.assertNotIn("login_success", kinds)

    def test_totp_required_not_provided_audited(self):
        dashapp = _make_test_app(password="pw", totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        c.post("/api/login", json={"password": "pw"})  # no code
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            kinds = [r[0] for r in conn.execute(
                "SELECT kind FROM auth_events").fetchall()]
        finally:
            conn.close()
        self.assertIn("totp_required_not_provided", kinds)

    def test_extras_json_never_contains_secret_material(self):
        """Critical: extras_json for totp_* rows must NEVER include
        the code, the secret, the otpauth URI, or password material."""
        import pyotp
        dashapp = _make_test_app(password="my-secret-password-1234",
                                   totp_secret=self.secret,
                                   db_path=self.tmp_db.name)
        c = dashapp.app.test_client()
        code = pyotp.TOTP(self.secret).now()
        # Mix of success + failure + missing.
        c.post("/api/login", json={"password": "my-secret-password-1234",
                                       "totp_code": code})
        c.post("/api/login", json={"password": "my-secret-password-1234",
                                       "totp_code": "000000"})
        c.post("/api/login", json={"password": "my-secret-password-1234"})

        conn = sqlite3.connect(self.tmp_db.name)
        try:
            rows = conn.execute(
                "SELECT kind, extras_json FROM auth_events "
                "WHERE kind LIKE 'totp%'").fetchall()
        finally:
            conn.close()
        self.assertGreater(len(rows), 0)
        forbidden_substrings = [
            self.secret,
            code,
            "my-secret-password-1234",
            "otpauth://",
            "JBSWY3D",  # first chars of the secret
        ]
        for kind, extras_json in rows:
            if extras_json is None:
                continue
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden, extras_json,
                    f"row kind={kind} extras_json leaked: {forbidden!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 8 — AST scan + protected files
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

    NEW_M153A2_MODULES = (
        "dashboard/auth/totp.py",
    )

    def test_no_forbidden_imports_in_totp_module(self):
        for rel in self.NEW_M153A2_MODULES:
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

    def test_no_forbidden_names_in_totp_module(self):
        for rel in self.NEW_M153A2_MODULES:
            full = Path(_REPO) / rel
            src = full.read_text(encoding="utf-8")
            for name in self.FORBIDDEN_NAMES:
                pattern = r"\b" + re.escape(name) + r"\b"
                self.assertIsNone(
                    re.search(pattern, src),
                    f"{rel}: forbidden name {name!r} found")

    def test_totp_module_imports_only_safe_libs(self):
        """The TOTP module must only depend on stdlib + pyotp + qrcode +
        dashboard.auth.*. No engine, no broker, no IB API."""
        src = (Path(_REPO) / "dashboard/auth/totp.py").read_text()
        tree = ast.parse(src)
        ALLOWED_PREFIXES = (
            "__future__",
            "hashlib", "logging", "os", "time", "typing", "urllib", "hmac",
            "pyotp", "qrcode",
            "dashboard.auth",
        )
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    if not any(mod == p or mod.startswith(p + ".")
                                for p in ALLOWED_PREFIXES):
                        self.fail(f"unexpected import: {mod}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mod = node.module
                    if not any(mod == p or mod.startswith(p + ".")
                                for p in ALLOWED_PREFIXES):
                        self.fail(f"unexpected from-import: {mod}")


class TestProtectedFilesUntouched(unittest.TestCase):

    BASE_REV = "648682c"  # M15.3.A docs closeout = pre-M15.3.A.2 baseline
    PROTECTED = (
        "main.py",
        "bot/scanner.py",
        "bot/strategy.py",
        "bot/risk.py",
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/authority.py",
        "bot/risk_authority/snapshot.py",
        "bot/risk_authority/audit_decisions.py",
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
            f"M15.3.A.2 modified protected files: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

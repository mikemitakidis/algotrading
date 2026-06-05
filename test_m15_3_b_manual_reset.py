"""test_m15_3_b_manual_reset.py — M15.3.B operator manual_reset test suite.

Per the M15.3.B pre-code checklist (approved 2026-06-04 with corrections
C1..C4), this file covers:

  G1  TestEndpointAuth                 auth + CSRF gates
  G2  TestPreviewEndpoint              GET /api/manual-reset/preview
  G3  TestConfirmString                server-side "RESET" check
  G4  TestStepUpTOTP                   fresh TOTP required at reset
  G5  TestReasonField                  10..500 char operator reason
  G6  TestKillSwitchClearing           the actual policy mutation
  G7  TestAuditWrites                  auth_events + risk_decisions
  G8  TestAtomicity                    transaction rollback on failure
  G9  TestRateLimit                    3 attempts / 60min lockout
  G10 TestNoBrokerImports              AST scan — no broker code on
                                       the manual_reset path
  G11 TestProtectedFilesUntouched      diff 0/24 vs ae8fb0d baseline
  G12 TestAuthEventsKindsRegistered    4 new kinds present in
                                       ALLOWED_KINDS

Plus the M15.3.A.cutover fix-2 fixture pattern (import-first-then-clean
against a VPS-style polluted .env).
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# ─────────────────────────────────────────────────────────────────────────────
# Common fixture
# ─────────────────────────────────────────────────────────────────────────────

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
    "DASHBOARD_PORT",
)


def _clean_auth_env():
    for k in _AUTH_ENV_KEYS:
        os.environ.pop(k, None)


_DASHAPP_SINGLETON = None


def _make_test_app(*, password="testpw-12345", db_path=None,
                    totp_secret=None):
    """Build a dashboard.app test instance. Same M15.3.A.cutover fix-2
    pattern: import FIRST (let dotenv run), clean env AFTER, then set
    test values. Resets login limiter + manual_reset limiter + preview
    token store + replay cache per call so each test starts clean.
    """
    global _DASHAPP_SINGLETON
    if _DASHAPP_SINGLETON is None:
        from dashboard import app as dashapp
        _DASHAPP_SINGLETON = dashapp
    dashapp = _DASHAPP_SINGLETON

    _clean_auth_env()
    os.environ["DASHBOARD_SECRET_KEY"] = "test_secret_key_M15.3.B_xxxx"
    os.environ["DASHBOARD_PASSWORD"] = password
    if totp_secret is not None:
        os.environ["DASHBOARD_TOTP_SECRET"] = totp_secret

    dashapp.app.config["TESTING"] = True

    from dashboard.auth.sessions import harden_app_config
    import logging
    silent = logging.getLogger("test_silent_m15_3_b")
    silent.addHandler(logging.NullHandler())
    silent.propagate = False
    harden_app_config(dashapp.app, logger=silent)

    from dashboard.auth.rate_limit import RateLimiter
    from dashboard.auth.manual_reset import (
        make_manual_reset_limiter, PreviewTokenStore, set_preview_token_store,
    )
    dashapp._m153a_login_limiter = RateLimiter(
        threshold=5, window_sec=600, lockout_sec=900,
    )
    dashapp._m153b_reset_limiter = make_manual_reset_limiter()
    set_preview_token_store(PreviewTokenStore())

    # Reset the TOTP replay cache so tests don't bleed into each other.
    from dashboard.auth.totp import reset_default_replay_cache
    reset_default_replay_cache()

    if db_path is not None:
        dashapp.DB_PATH = Path(db_path)
        from dashboard.auth.audit import ensure_auth_events_schema
        from bot.flywheel import ensure_daily_state_per_broker_migrations
        c = sqlite3.connect(db_path)
        try:
            ensure_auth_events_schema(c)
            ensure_daily_state_per_broker_migrations(c)
            c.execute(
                "CREATE TABLE IF NOT EXISTS portfolio_risk_state ("
                "  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
            )
            c.commit()
        finally:
            c.close()
    return dashapp


def _fresh_secret():
    """Build a fresh random TOTP secret."""
    import pyotp
    return pyotp.random_base32(length=32)


def _login(client, *, password="testpw-12345", totp_secret=None):
    """Login and return (csrf_token, login_code_used_or_None).

    If totp_secret is provided, generates a real TOTP code, logs in
    with it, and returns the code (so tests can verify replay behaviour).
    Also clears the replay cache AFTER login so the next TOTP call
    in the same test can succeed (mimics 30+ seconds of real time).
    """
    body = {"password": password}
    code = None
    if totp_secret is not None:
        import pyotp
        code = pyotp.TOTP(totp_secret).now()
        body["totp_code"] = code
    r = client.post("/api/login", json=body)
    if r.status_code != 200:
        return None, code
    csrf = (r.get_json() or {}).get("csrf_token", "")
    return csrf, code


def _seed_kill_switches(db_path, *, state):
    """Seed the M13.4A allocation policy with given kill_switch values.

    state is {scope_name: bool}, e.g. {"global": True, "ibkr": False}.
    """
    from bot.broker_allocation import load_policy, save_policy
    conn = sqlite3.connect(db_path)
    try:
        p = load_policy(conn)
        for scope, val in state.items():
            p.setdefault(scope, {})["kill_switch"] = bool(val)
        save_policy(conn, p)
    finally:
        conn.close()


def _csrf_headers(token):
    return {"X-CSRF-Token": token or ""}


# ─────────────────────────────────────────────────────────────────────────────
# G1 — endpoint auth gates
# ─────────────────────────────────────────────────────────────────────────────


class TestEndpointAuth(unittest.TestCase):
    """Both endpoints require auth; POST also requires CSRF."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _client(self):
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=_fresh_secret())
        return dashapp.app.test_client()

    def test_preview_unauthenticated_returns_401(self):
        r = self._client().get("/api/manual-reset/preview")
        self.assertEqual(r.status_code, 401)

    def test_execute_unauthenticated_returns_401(self):
        r = self._client().post("/api/manual-reset",
                                  json={"confirm": "RESET"})
        self.assertEqual(r.status_code, 401)

    def test_preview_method_post_returns_405(self):
        r = self._client().post("/api/manual-reset/preview")
        self.assertEqual(r.status_code, 405)

    def test_execute_authed_no_csrf_returns_403(self):
        """POST without an X-CSRF-Token header (after login) returns 403."""
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        self.assertIsNotNone(csrf)
        # Send POST without CSRF header.
        r = cli.post("/api/manual-reset",
                      json={"confirm": "RESET",
                            "preview_token": "irrelevant",
                            "reason": "no csrf header test",
                            "totp_code": "000000"})
        self.assertEqual(r.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────────
# G2 — preview endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestPreviewEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _logged_in(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        self.assertIsNotNone(csrf, "login should succeed")
        return dashapp, cli, csrf

    def test_preview_returns_current_kill_switch_state(self):
        _seed_kill_switches(self.tmp_db.name,
                              state={"global": True, "ibkr": False})
        _, cli, _ = self._logged_in()
        r = cli.get("/api/manual-reset/preview")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        state = d["kill_switch_state"]
        self.assertIs(state["global"], True)
        self.assertIs(state["ibkr"], False)

    def test_preview_issues_token_with_60s_ttl(self):
        _, cli, _ = self._logged_in()
        r = cli.get("/api/manual-reset/preview")
        d = r.get_json()
        self.assertIn("preview_token", d)
        self.assertIsInstance(d["preview_token"], str)
        self.assertGreater(len(d["preview_token"]), 20)
        self.assertEqual(d["preview_token_ttl_seconds"], 60)

    def test_preview_audit_row_written(self):
        _, cli, _ = self._logged_in()
        cli.get("/api/manual-reset/preview")
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT kind, success FROM auth_events "
                "WHERE kind='manual_reset_preview' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row,
            "manual_reset_preview audit row missing")
        self.assertEqual(row[0], "manual_reset_preview")
        self.assertEqual(row[1], 1)

    def test_preview_not_rate_limited(self):
        """Per Q-B.9: GET preview is NOT counted against the limit."""
        dashapp, cli, _ = self._logged_in()
        for _ in range(10):
            r = cli.get("/api/manual-reset/preview")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(
            dashapp._m153b_reset_limiter.failure_count("127.0.0.1"), 0)


# ─────────────────────────────────────────────────────────────────────────────
# G3 — server-side confirm == "RESET"
# ─────────────────────────────────────────────────────────────────────────────


class TestConfirmString(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        self.assertIsNotNone(csrf)
        prev = cli.get("/api/manual-reset/preview").get_json()
        return cli, csrf, prev["preview_token"]

    def _post(self, cli, csrf, body):
        return cli.post("/api/manual-reset", json=body,
                          headers=_csrf_headers(csrf))

    def test_confirm_lowercase_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": "reset", "preview_token": tok,
            "reason": "valid reason text here", "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "confirm_invalid")

    def test_confirm_missing_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "preview_token": tok,
            "reason": "valid reason text here", "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "confirm_invalid")

    def test_confirm_wrong_type_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": 1, "preview_token": tok,
            "reason": "valid reason text here", "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "confirm_invalid")

    def test_confirm_RESET_passes_confirm_check(self):
        """Verify confirm validation passes when value is exactly 'RESET'
        by submitting bad TOTP — the response must NOT be confirm_invalid."""
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid reason text here", "totp_code": "000000"})
        d = r.get_json()
        self.assertNotEqual(d.get("error"), "confirm_invalid")

    def test_validate_confirm_pure_function(self):
        from dashboard.auth.manual_reset import validate_confirm
        self.assertTrue(validate_confirm("RESET"))
        self.assertFalse(validate_confirm("reset"))
        self.assertFalse(validate_confirm("Reset"))
        self.assertFalse(validate_confirm(""))
        self.assertFalse(validate_confirm(None))
        self.assertFalse(validate_confirm(42))


# ─────────────────────────────────────────────────────────────────────────────
# G12 — ALLOWED_KINDS registered (cheap unit test; put it near the top)
# ─────────────────────────────────────────────────────────────────────────────


class TestAuthEventsKindsRegistered(unittest.TestCase):
    def test_four_new_kinds_present(self):
        from dashboard.auth.audit import ALLOWED_KINDS
        for k in ("manual_reset_preview", "manual_reset_attempt",
                   "manual_reset_success", "manual_reset_failure"):
            self.assertIn(k, ALLOWED_KINDS,
                f"{k!r} missing from dashboard.auth.audit.ALLOWED_KINDS")


# ─────────────────────────────────────────────────────────────────────────────
# G4 — step-up TOTP
# ─────────────────────────────────────────────────────────────────────────────


class TestStepUpTOTP(unittest.TestCase):
    """Per operator C1: API exposes ONLY `hint='recently_used'` for
    replay; everything else (wrong code, malformed, missing) returns
    the generic `{ok:false, error:'totp_invalid'}` with no hint."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup_logged_in(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, login_code = _login(cli, totp_secret=secret)
        self.assertIsNotNone(csrf)
        prev = cli.get("/api/manual-reset/preview").get_json()
        return cli, csrf, prev["preview_token"], secret, login_code

    def _post(self, cli, csrf, body):
        return cli.post("/api/manual-reset", json=body,
                          headers=_csrf_headers(csrf))

    def test_totp_missing_returns_401_no_hint(self):
        cli, csrf, tok, _, _ = self._setup_logged_in()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid reason text here"})
        self.assertEqual(r.status_code, 401)
        d = r.get_json()
        self.assertEqual(d["error"], "totp_invalid")
        self.assertNotIn("hint", d)

    def test_totp_wrong_code_returns_401_no_hint(self):
        """Per C1: wrong codes get GENERIC response, no hint."""
        cli, csrf, tok, _, _ = self._setup_logged_in()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid reason text here",
            "totp_code": "999999"})
        self.assertEqual(r.status_code, 401)
        d = r.get_json()
        self.assertEqual(d["error"], "totp_invalid")
        self.assertNotIn("hint", d)

    def test_totp_malformed_returns_401_no_hint(self):
        cli, csrf, tok, _, _ = self._setup_logged_in()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid reason text here",
            "totp_code": "abc"})
        self.assertEqual(r.status_code, 401)
        d = r.get_json()
        self.assertEqual(d["error"], "totp_invalid")
        self.assertNotIn("hint", d)

    def test_totp_empty_string_returns_401_no_hint(self):
        cli, csrf, tok, _, _ = self._setup_logged_in()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid reason text here",
            "totp_code": ""})
        self.assertEqual(r.status_code, 401)
        d = r.get_json()
        self.assertEqual(d["error"], "totp_invalid")
        self.assertNotIn("hint", d)

    def test_totp_replay_returns_recently_used_hint(self):
        """The ONLY hint the API ever exposes (per C1). The login code
        was consumed by the replay cache during /api/login; reusing it
        at step-up triggers the hint."""
        cli, csrf, tok, _, login_code = self._setup_logged_in()
        self.assertIsNotNone(login_code)
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid reason text here",
            "totp_code": login_code})
        self.assertEqual(r.status_code, 401)
        d = r.get_json()
        self.assertEqual(d["error"], "totp_invalid")
        self.assertEqual(d.get("hint"), "recently_used")

    def test_totp_valid_step_up_succeeds(self):
        """Login + clear replay cache + reuse same code (simulating
        30s+ real-time passage where the cache has aged out)."""
        cli, csrf, tok, secret, login_code = self._setup_logged_in()
        from dashboard.auth.totp import reset_default_replay_cache
        reset_default_replay_cache()
        import pyotp
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "valid step-up reset reason",
            "totp_code": pyotp.TOTP(secret).now()})
        self.assertEqual(r.status_code, 200,
                          f"step-up should succeed; got {r.get_json()}")
        self.assertTrue(r.get_json()["ok"])

    def test_verify_step_up_totp_no_secret_refuses(self):
        """If DASHBOARD_TOTP_SECRET is unset, verify_step_up_totp must
        refuse (hard fail). Tests the function directly so we don't
        have to set up the dashboard."""
        # Save and clear DASHBOARD_TOTP_SECRET.
        saved = os.environ.pop("DASHBOARD_TOTP_SECRET", None)
        try:
            from dashboard.auth.manual_reset import verify_step_up_totp
            ok, hint = verify_step_up_totp("123456")
            self.assertFalse(ok)
            self.assertEqual(hint, "")
        finally:
            if saved is not None:
                os.environ["DASHBOARD_TOTP_SECRET"] = saved


# ─────────────────────────────────────────────────────────────────────────────
# G5 — reason field
# ─────────────────────────────────────────────────────────────────────────────


class TestReasonField(unittest.TestCase):
    """Per operator C3: 10..500 chars, required, no server-side
    content filtering (operator trust + UI helper text)."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        return cli, csrf, prev["preview_token"]

    def _post(self, cli, csrf, body):
        return cli.post("/api/manual-reset", json=body,
                          headers=_csrf_headers(csrf))

    def test_reason_missing_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "reason_invalid")

    def test_reason_too_short_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "short", "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "reason_invalid")

    def test_reason_whitespace_only_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "           ", "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "reason_invalid")

    def test_reason_too_long_rejected(self):
        cli, csrf, tok = self._setup()
        r = self._post(cli, csrf, {
            "confirm": "RESET", "preview_token": tok,
            "reason": "x" * 600, "totp_code": "000000"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "reason_invalid")

    def test_validate_reason_pure_function(self):
        from dashboard.auth.manual_reset import validate_reason
        self.assertEqual(validate_reason("valid reason text")[0], True)
        self.assertEqual(validate_reason("x" * 9)[0], False)
        self.assertEqual(validate_reason("x" * 10)[0], True)
        self.assertEqual(validate_reason("x" * 500)[0], True)
        self.assertEqual(validate_reason("x" * 501)[0], False)
        self.assertEqual(validate_reason(None)[0], False)
        self.assertEqual(validate_reason(42)[0], False)
        self.assertEqual(validate_reason("")[0], False)


# ─────────────────────────────────────────────────────────────────────────────
# G6 — kill-switch clearing
# ─────────────────────────────────────────────────────────────────────────────


class TestKillSwitchClearing(unittest.TestCase):
    """The actual policy mutation. Includes idempotent path (operator C2)."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _execute(self, *, seeded_state):
        """Login + preview + execute the reset. Returns the response."""
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        if seeded_state:
            _seed_kill_switches(self.tmp_db.name, state=seeded_state)
        csrf, _ = _login(cli, totp_secret=secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        from dashboard.auth.totp import reset_default_replay_cache
        reset_default_replay_cache()
        import pyotp
        r = cli.post("/api/manual-reset",
                       json={"confirm": "RESET",
                             "preview_token": prev["preview_token"],
                             "reason": "test clearing flow reason",
                             "totp_code": pyotp.TOTP(secret).now()},
                       headers=_csrf_headers(csrf))
        return r, dashapp, cli, csrf

    def test_clears_global_kill_switch(self):
        r, _, _, _ = self._execute(seeded_state={"global": True})
        self.assertEqual(r.status_code, 200, f"failed: {r.get_json()}")
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertIn("global", d["switches_cleared"])
        self.assertFalse(d["noop"])
        from bot.broker_allocation import load_policy
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            p = load_policy(conn)
        finally:
            conn.close()
        self.assertFalse(p["global"]["kill_switch"])

    def test_clears_multiple_kill_switches(self):
        r, _, _, _ = self._execute(seeded_state={
            "global": True, "ibkr": True, "etoro": True})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(set(d["switches_cleared"]),
                          {"global", "ibkr", "etoro"})

    def test_idempotent_when_nothing_to_clear(self):
        """Per operator C2: noop=True, switches_cleared=[], audit rows
        still written."""
        r, _, _, _ = self._execute(seeded_state={
            "global": False, "ibkr": False, "etoro": False})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["switches_cleared"], [])
        self.assertTrue(d["noop"])
        # Audit rows still written.
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            n_success = conn.execute(
                "SELECT COUNT(*) FROM auth_events "
                "WHERE kind='manual_reset_success'").fetchone()[0]
            n_decision = conn.execute(
                "SELECT COUNT(*) FROM risk_decisions "
                "WHERE source='manual_reset'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_success, 1)
        self.assertEqual(n_decision, 1)

    def test_response_before_and_after_state_correct(self):
        r, _, _, _ = self._execute(seeded_state={
            "global": True, "ibkr": False})
        d = r.get_json()
        self.assertIn("before_state", d)
        self.assertIn("after_state", d)
        self.assertTrue(d["before_state"]["global"])
        self.assertFalse(d["after_state"]["global"])
        self.assertFalse(d["after_state"]["ibkr"])

    def test_response_includes_audit_ids(self):
        r, _, _, _ = self._execute(seeded_state={"global": True})
        d = r.get_json()
        self.assertIn("audit", d)
        self.assertGreater(d["audit"]["auth_event_id"], 0)
        self.assertTrue(d["audit"]["decision_id"].startswith("mr-"))

    def test_preview_token_is_single_use(self):
        """Per Q-B.5: tokens consumed on success."""
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        token = prev["preview_token"]
        from dashboard.auth.totp import reset_default_replay_cache
        import pyotp
        # First use — should succeed.
        reset_default_replay_cache()
        r1 = cli.post("/api/manual-reset",
                       json={"confirm": "RESET", "preview_token": token,
                             "reason": "first single-use attempt",
                             "totp_code": pyotp.TOTP(secret).now()},
                       headers=_csrf_headers(csrf))
        self.assertEqual(r1.status_code, 200,
                          f"first POST should succeed: {r1.get_json()}")
        # Second use with SAME token — must fail.
        reset_default_replay_cache()
        r2 = cli.post("/api/manual-reset",
                       json={"confirm": "RESET", "preview_token": token,
                             "reason": "second single-use attempt",
                             "totp_code": pyotp.TOTP(secret).now()},
                       headers=_csrf_headers(csrf))
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r2.get_json()["error"], "preview_token_invalid")


# ─────────────────────────────────────────────────────────────────────────────
# G7 — audit writes (auth_events + risk_decisions)
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditWrites(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _execute_reset(self, *, seeded=None,
                        reason="audit-test reason text here"):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        if seeded:
            _seed_kill_switches(self.tmp_db.name, state=seeded)
        csrf, _ = _login(cli, totp_secret=secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        from dashboard.auth.totp import reset_default_replay_cache
        reset_default_replay_cache()
        import pyotp
        return cli.post("/api/manual-reset",
                          json={"confirm": "RESET",
                                "preview_token": prev["preview_token"],
                                "reason": reason,
                                "totp_code": pyotp.TOTP(secret).now()},
                          headers=_csrf_headers(csrf))

    def test_attempt_audit_always_written(self):
        r = self._execute_reset(seeded={"global": True})
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM auth_events "
                "WHERE kind='manual_reset_attempt'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_success_audit_extras_well_formed(self):
        r = self._execute_reset(seeded={"global": True})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT extras_json FROM auth_events "
                "WHERE kind='manual_reset_success' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        extras = json.loads(row[0])
        self.assertIn("global", extras["switches_cleared"])
        self.assertFalse(extras["noop"])
        self.assertIn("before_state", extras)
        self.assertIn("after_state", extras)
        self.assertIn("reason", extras)

    def test_risk_decisions_row_well_formed(self):
        r = self._execute_reset(seeded={"global": True})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT source, broker_scope, requested_action, result, "
                "  authority_before, authority_after, reason_codes, "
                "  actor, explainer, snapshot_id "
                "FROM risk_decisions ORDER BY taken_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "manual_reset")
        self.assertEqual(row[1], "GLOBAL")
        self.assertEqual(row[2], "query_authority")
        self.assertEqual(row[3], "allow")
        self.assertIsNone(row[9])  # snapshot_id NULL
        codes = json.loads(row[6])
        self.assertIn("manual_reset", codes)

    def test_actor_is_operator_no_session_id(self):
        """Per audit-extras invariant: actor is short, no secret material."""
        self._execute_reset(seeded={"global": True})
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT actor FROM risk_decisions "
                "WHERE source='manual_reset' "
                "ORDER BY taken_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "operator")

    def test_failure_audit_on_validation_failure(self):
        """Missing reason triggers manual_reset_failure with reason
        code 'reason_missing'."""
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        cli.post("/api/manual-reset",
                  json={"confirm": "RESET",
                        "preview_token": prev["preview_token"],
                        "totp_code": "000000"},
                  headers=_csrf_headers(csrf))
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT extras_json FROM auth_events "
                "WHERE kind='manual_reset_failure' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        extras = json.loads(row[0])
        self.assertEqual(extras["reason"], "reason_missing")

    def test_extras_json_never_contains_secret_material(self):
        """Per operator audit-extras invariant + M15.3.A.2 invariant:
        no TOTP code, TOTP secret, otpauth URI, password, raw session ID,
        broker credentials in any extras_json field."""
        # Use a known TOTP code/secret so we can grep for them.
        import pyotp
        known_secret = pyotp.random_base32(length=32)
        os.environ["DASHBOARD_TOTP_SECRET"] = known_secret
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=known_secret)
        cli = dashapp.app.test_client()
        _seed_kill_switches(self.tmp_db.name, state={"global": True})
        login_code = pyotp.TOTP(known_secret).now()
        csrf, _ = _login(cli, totp_secret=known_secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        from dashboard.auth.totp import reset_default_replay_cache
        reset_default_replay_cache()
        step_code = pyotp.TOTP(known_secret).now()
        cli.post("/api/manual-reset",
                  json={"confirm": "RESET",
                        "preview_token": prev["preview_token"],
                        "reason": "secret-material-invariant test",
                        "totp_code": step_code},
                  headers=_csrf_headers(csrf))
        # Also drive a failure path:
        prev2 = cli.get("/api/manual-reset/preview").get_json()
        cli.post("/api/manual-reset",
                  json={"confirm": "wrong",  # triggers failure
                        "preview_token": prev2["preview_token"],
                        "reason": "secret-material-invariant test 2",
                        "totp_code": step_code},
                  headers=_csrf_headers(csrf))

        forbidden_substrings = [
            known_secret,            # TOTP secret
            login_code,              # TOTP code (login)
            step_code,               # TOTP code (step-up)
            "testpw-12345",          # password
            "otpauth://",            # otpauth URI prefix
        ]

        conn = sqlite3.connect(self.tmp_db.name)
        try:
            rows = conn.execute(
                "SELECT kind, extras_json FROM auth_events "
                "WHERE kind LIKE 'manual_reset_%' "
                "  AND extras_json IS NOT NULL"
            ).fetchall()
            # Also check risk_decisions.
            mr_rows = conn.execute(
                "SELECT explainer, request_json, recovery_paths "
                "FROM risk_decisions WHERE source='manual_reset'"
            ).fetchall()
        finally:
            conn.close()
        self.assertGreater(len(rows), 0)

        all_payloads = [(k, j) for k, j in rows]
        for explainer, req_json, recovery in mr_rows:
            all_payloads.append(("rd.explainer", explainer or ""))
            all_payloads.append(("rd.request_json", req_json or ""))
            all_payloads.append(("rd.recovery_paths", recovery or ""))

        for label, payload in all_payloads:
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden, payload,
                    f"{label}: contains forbidden substring "
                    f"{forbidden[:8]}...: payload={payload[:120]}...")


# ─────────────────────────────────────────────────────────────────────────────
# G8 — atomicity (transaction rollback)
# ─────────────────────────────────────────────────────────────────────────────


class TestAtomicity(unittest.TestCase):
    """Per Q-B.8: kill-switch + risk_decisions + success-audit are all
    atomic in one transaction. Attempt/failure audits are OUTSIDE so
    we keep evidence of failed attempts even on rollback."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup_for_atomic_test(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        _seed_kill_switches(self.tmp_db.name, state={"global": True})
        csrf, _ = _login(cli, totp_secret=secret)
        prev = cli.get("/api/manual-reset/preview").get_json()
        from dashboard.auth.totp import reset_default_replay_cache
        reset_default_replay_cache()
        import pyotp
        return cli, csrf, prev["preview_token"], secret

    def test_db_error_leaves_policy_unchanged(self):
        """Simulate a DB error during the M14 audit write — the
        transaction must roll back the policy write too."""
        cli, csrf, tok, secret = self._setup_for_atomic_test()
        import pyotp
        with patch("bot.risk_authority.audit_decisions."
                    "write_manual_reset_decision",
                    side_effect=sqlite3.OperationalError("simulated")):
            r = cli.post("/api/manual-reset",
                          json={"confirm": "RESET",
                                "preview_token": tok,
                                "reason": "atomic rollback test reason",
                                "totp_code": pyotp.TOTP(secret).now()},
                          headers=_csrf_headers(csrf))
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"], "db_error")
        # Policy unchanged.
        from bot.broker_allocation import load_policy
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            p = load_policy(conn)
        finally:
            conn.close()
        self.assertTrue(p["global"]["kill_switch"],
            "policy should be unchanged after rollback")

    def test_db_error_failure_audit_still_written(self):
        """The failure audit row is OUTSIDE the transaction (Q-B.8)."""
        cli, csrf, tok, secret = self._setup_for_atomic_test()
        import pyotp
        with patch("bot.risk_authority.audit_decisions."
                    "write_manual_reset_decision",
                    side_effect=sqlite3.OperationalError("simulated")):
            cli.post("/api/manual-reset",
                      json={"confirm": "RESET",
                            "preview_token": tok,
                            "reason": "atomic rollback audit test",
                            "totp_code": pyotp.TOTP(secret).now()},
                      headers=_csrf_headers(csrf))
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT extras_json FROM auth_events "
                "WHERE kind='manual_reset_failure' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        extras = json.loads(row[0])
        self.assertEqual(extras["reason"], "db_error")

    def test_db_error_no_success_audit(self):
        """Success audit is INSIDE the transaction so it must roll back too."""
        cli, csrf, tok, secret = self._setup_for_atomic_test()
        import pyotp
        with patch("bot.risk_authority.audit_decisions."
                    "write_manual_reset_decision",
                    side_effect=sqlite3.OperationalError("simulated")):
            cli.post("/api/manual-reset",
                      json={"confirm": "RESET",
                            "preview_token": tok,
                            "reason": "atomic no-success-audit test",
                            "totp_code": pyotp.TOTP(secret).now()},
                      headers=_csrf_headers(csrf))
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM auth_events "
                "WHERE kind='manual_reset_success'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, "no success audit row should be written "
                                "when the transaction rolls back")


# ─────────────────────────────────────────────────────────────────────────────
# G9 — rate limit
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimit(unittest.TestCase):
    """Per Q-B.9: 3 attempts / 60min / 60min lockout, per client IP.
    Preview GET does NOT count."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()

    def tearDown(self):
        try:
            os.unlink(self.tmp_db.name)
        except OSError:
            pass

    def _setup(self):
        secret = _fresh_secret()
        dashapp = _make_test_app(db_path=self.tmp_db.name,
                                   totp_secret=secret)
        cli = dashapp.app.test_client()
        csrf, _ = _login(cli, totp_secret=secret)
        return dashapp, cli, csrf, secret

    def test_three_failed_attempts_then_429(self):
        dashapp, cli, csrf, _ = self._setup()
        # 3 attempts with wrong confirm — each counts as a failure.
        for i in range(3):
            r = cli.post("/api/manual-reset",
                          json={"confirm": "wrong",
                                "preview_token": "irrelevant",
                                "reason": "rate limit test " + str(i),
                                "totp_code": "000000"},
                          headers=_csrf_headers(csrf))
            self.assertEqual(r.status_code, 400)
        # 4th must be 429.
        r = cli.post("/api/manual-reset",
                      json={"confirm": "wrong",
                            "preview_token": "x",
                            "reason": "rate limit test 4",
                            "totp_code": "000000"},
                      headers=_csrf_headers(csrf))
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["error"], "rate_limited")
        self.assertGreater(r.get_json()["retry_after_sec"], 0)

    def test_preview_does_not_count(self):
        dashapp, cli, _, _ = self._setup()
        for _ in range(10):
            r = cli.get("/api/manual-reset/preview")
            self.assertEqual(r.status_code, 200)
        # No POST failures recorded.
        self.assertEqual(
            dashapp._m153b_reset_limiter.failure_count("127.0.0.1"), 0)

    def test_rate_limit_failure_audit_row(self):
        """The 4th attempt that gets locked out also writes a
        manual_reset_failure audit row with reason='rate_limited'."""
        dashapp, cli, csrf, _ = self._setup()
        for i in range(3):
            cli.post("/api/manual-reset",
                      json={"confirm": "wrong",
                            "preview_token": "x",
                            "reason": "rate limit audit test " + str(i),
                            "totp_code": "000000"},
                      headers=_csrf_headers(csrf))
        cli.post("/api/manual-reset",
                  json={"confirm": "wrong",
                        "preview_token": "x",
                        "reason": "rate limit audit fourth",
                        "totp_code": "000000"},
                  headers=_csrf_headers(csrf))
        conn = sqlite3.connect(self.tmp_db.name)
        try:
            row = conn.execute(
                "SELECT extras_json FROM auth_events "
                "WHERE kind='manual_reset_failure' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        extras = json.loads(row[0])
        self.assertEqual(extras["reason"], "rate_limited")


# ─────────────────────────────────────────────────────────────────────────────
# G10 — AST scan: no broker imports or method names in manual_reset path
# ─────────────────────────────────────────────────────────────────────────────


class TestNoBrokerImports(unittest.TestCase):
    """Per operator hard constraint + Q-B.10: AST-verify that
    dashboard/auth/manual_reset.py contains no broker libraries, no
    broker method names, no order-placement code paths."""

    # Modules that MUST NEVER be imported by dashboard/auth/manual_reset.py
    # — directly or transitively in any function body. Transitive check
    # only catches imports inside the manual_reset module itself; deep
    # transitive coverage is achieved by combining this with the
    # M14-engine-untouched protected-files check (G11).
    _FORBIDDEN_IMPORT_MODULES = (
        "ib_insync",
        "ibapi",
        "bot.broker_ibkr",
        "bot.broker_etoro",
        "bot.broker_router",
        "bot.broker_paper",
        "bot.gateway_health",
        "bot.gateway_watchdog",
        "bot.scanner",
        "bot.strategy",
        "bot.risk_authority.engine",
        "bot.risk_authority.governor",
        "bot.risk_authority.snapshot",
        "bot.risk_authority.preflight",
        "bot.risk_authority.ibkr_paper_reader",
        "bot.etoro.live_broker",
    )
    # Substrings of method names that no part of the manual_reset code
    # path may call. (String-literal check; complements the AST import
    # check above. Defensive vs. someone sneaking in `getattr` tricks.)
    _FORBIDDEN_METHOD_NAMES = (
        "placeOrder", "place_order",
        "cancelOrder", "cancel_order",
        "modifyOrder", "modify_order",
        "closePosition", "close_position",
        "submitOrder", "submit_order",
    )

    def _read(self, relpath):
        return (Path(_REPO) / relpath).read_text(encoding="utf-8")

    def _imported_modules(self, src):
        """Return the set of fully-qualified module names imported
        anywhere in `src` (module top OR inside function bodies)."""
        out = set()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                out.add(mod)
                # Also accumulate "mod.name" for from-imports so we
                # catch `from bot.risk_authority import engine`.
                for alias in node.names:
                    if alias.name and mod:
                        out.add(f"{mod}.{alias.name}")
        return out

    def test_manual_reset_module_imports_no_broker_code(self):
        src = self._read("dashboard/auth/manual_reset.py")
        imported = self._imported_modules(src)
        for forbidden in self._FORBIDDEN_IMPORT_MODULES:
            for imp in imported:
                self.assertFalse(
                    imp == forbidden or imp.startswith(forbidden + "."),
                    f"dashboard/auth/manual_reset.py imports {imp!r} "
                    f"which matches forbidden module {forbidden!r}. "
                    f"Manual_reset must not touch broker / engine / "
                    f"scanner / strategy code.")

    def test_manual_reset_module_no_broker_method_strings(self):
        """No string literal in the module matches a broker order
        method name. Catches eval/getattr-style sneaks."""
        src = self._read("dashboard/auth/manual_reset.py")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for forbidden in self._FORBIDDEN_METHOD_NAMES:
                    self.assertNotIn(forbidden, node.value,
                        f"dashboard/auth/manual_reset.py string literal "
                        f"contains broker method name {forbidden!r}: "
                        f"{node.value!r}")

    def test_audit_decisions_writer_imports_no_broker_code(self):
        """Same check applied to the M14-side audit writer that
        manual_reset calls. The writer was added in this milestone
        and lives in bot/risk_authority/audit_decisions.py."""
        src = self._read("bot/risk_authority/audit_decisions.py")
        # Look at the function body of write_manual_reset_decision
        # specifically. (The rest of audit_decisions.py existed before
        # M15.3.B and is allowed to import RiskDecision/RiskSnapshot.)
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                and node.name == "write_manual_reset_decision"):
                target = node
                break
        self.assertIsNotNone(target,
            "write_manual_reset_decision should be defined in audit_decisions.py")
        # No nested imports inside this function.
        for inner in ast.walk(target):
            self.assertNotIsInstance(inner, ast.Import,
                "write_manual_reset_decision must not have nested imports")
            self.assertNotIsInstance(inner, ast.ImportFrom,
                "write_manual_reset_decision must not have nested imports")

    def test_manual_reset_endpoints_no_broker_imports(self):
        """The manual_reset endpoint bodies in dashboard/app.py must
        not import broker code. We scan the relevant function defs."""
        src = self._read("dashboard/app.py")
        tree = ast.parse(src)
        targets = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                and node.name in ("m153b_manual_reset_preview",
                                    "m153b_manual_reset_execute",
                                    "_m153b_get_limiter",
                                    "_m153b_session_hash")):
                targets.append(node)
        self.assertEqual(len(targets), 4,
            f"expected 4 manual_reset endpoint functions, found "
            f"{len(targets)}: {[t.name for t in targets]}")
        for fn in targets:
            for inner in ast.walk(fn):
                if isinstance(inner, ast.ImportFrom):
                    mod = inner.module or ""
                    for forbidden in self._FORBIDDEN_IMPORT_MODULES:
                        self.assertFalse(
                            mod == forbidden or mod.startswith(forbidden + "."),
                            f"function {fn.name} imports from {mod!r} "
                            f"(matches forbidden {forbidden!r})")
                if isinstance(inner, ast.Import):
                    for alias in inner.names:
                        for forbidden in self._FORBIDDEN_IMPORT_MODULES:
                            self.assertFalse(
                                alias.name == forbidden
                                or alias.name.startswith(forbidden + "."),
                                f"function {fn.name} imports {alias.name!r} "
                                f"(matches forbidden {forbidden!r})")


# ─────────────────────────────────────────────────────────────────────────────
# G11 — protected files unchanged vs ae8fb0d (pre-M15.3.B baseline)
# ─────────────────────────────────────────────────────────────────────────────


class TestProtectedFilesUntouched(unittest.TestCase):
    """Per operator hard constraint + post-M15.3.A.cutover discipline.

    The set is the same 24-file canonical list used in every M15.3.x
    milestone. M15.3.B may NOT modify any of them.
    """

    _BASELINE = "ae8fb0d"
    _PROTECTED = (
        "main.py",
        "bot/scanner.py",
        "bot/strategy.py",
        "bot/risk.py",
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/authority.py",
        "bot/risk_authority/snapshot.py",
        # Note: audit_decisions.py IS in the protected list normally,
        # but M15.3.B explicitly extends it with write_manual_reset_decision
        # per the pre-code checklist. The expected diff is "additive only"
        # — see test_audit_decisions_only_additive_change below.
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

    def _git_diff(self, path):
        try:
            r = subprocess.run(
                ["git", "diff", "--unified=0", self._BASELINE, "--", path],
                cwd=_REPO, capture_output=True, text=True, timeout=10,
            )
            return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self.skipTest("git not available")
            return ""

    def test_zero_protected_files_changed_vs_baseline(self):
        modified = []
        for f in self._PROTECTED:
            full = Path(_REPO) / f
            if not full.exists():
                continue
            d = self._git_diff(f)
            if d.strip():
                modified.append(f)
        self.assertEqual(modified, [],
            f"M15.3.B must not modify protected files; modified: {modified}")

    def test_audit_decisions_only_additive_change(self):
        """audit_decisions.py IS modified by M15.3.B but only via an
        additive new function `write_manual_reset_decision`. All
        pre-existing functions (decide_and_audit, write_snapshot,
        write_decision) must be byte-identical to the baseline."""
        try:
            current = (Path(_REPO) /
                       "bot/risk_authority/audit_decisions.py"
                       ).read_text(encoding="utf-8")
            baseline = subprocess.run(
                ["git", "show",
                  f"{self._BASELINE}:bot/risk_authority/audit_decisions.py"],
                cwd=_REPO, capture_output=True, text=True, timeout=10,
                check=False,
            ).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self.skipTest("git not available")
            return
        if not baseline:
            self.skipTest("baseline content not available")
            return

        def _func_src(src, name):
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if (isinstance(node, ast.FunctionDef)
                    and node.name == name):
                    return ast.get_source_segment(src, node)
            return None

        for fn_name in ("decide_and_audit", "write_snapshot",
                         "write_decision", "_redact"):
            b = _func_src(baseline, fn_name)
            c = _func_src(current, fn_name)
            self.assertIsNotNone(b, f"{fn_name} missing from baseline")
            self.assertIsNotNone(c, f"{fn_name} missing from current")
            self.assertEqual(b, c,
                f"existing function {fn_name!r} in audit_decisions.py "
                f"was modified; M15.3.B should be additive only")

    def test_no_broker_files_touched(self):
        """Belt-and-braces — the broker/IBKR/eToro module files have
        zero diff. Same as the main protected check but spelled out
        for emphasis."""
        broker_files = (
            "bot/risk_authority/ibkr_paper_reader.py",
            "bot/etoro/live_broker.py",
            "tools/etoro_live_write.py",
        )
        for f in broker_files:
            full = Path(_REPO) / f
            if not full.exists():
                continue
            d = self._git_diff(f)
            self.assertEqual(d.strip(), "",
                f"broker-related file {f} must not be touched in M15.3.B; "
                f"diff:\n{d[:400]}")

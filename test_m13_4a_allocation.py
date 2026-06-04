"""
M13.4A — Broker Allocation + Budget Controls tests.

Coverage:
- validator pass case
- each validation rule fail case
- unknown top-level keys rejected
- money fields non-negative
- boolean fields strictly typed
- max_open_positions integer >= 0
- max_single_trade_amount <= max_auto_trading_capital
- broker capital <= global capital when global > 0
- default_broker must be in allowed_brokers
- etoro_real rejected (in allowed_brokers and as default_broker)
- etoro_live_enabled=true rejected
- load_policy returns DEFAULT_POLICY if absent
- corrupt JSON returns DEFAULT_POLICY with warning
- save/load round-trip
- is_auto_trading_allowed false for global disabled / global kill / broker
  disabled / broker kill / broker not allowed / etoro_real w/ live disabled
- GET endpoint requires auth
- POST endpoint requires auth
- POST endpoint rejects invalid policy
- POST endpoint persists valid policy

Run:  python3 test_m13_4a_allocation.py
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.broker_allocation import (  # noqa: E402
    DEFAULT_POLICY,
    POLICY_KEY,
    POLICY_VERSION,
    FORBIDDEN_BROKERS,
    ValidationResult,
    validate_policy,
    load_policy,
    save_policy,
    is_broker_allowed,
    is_auto_trading_allowed,
)


def _good() -> dict:
    """A fresh deep copy of a passing policy (DEFAULT_POLICY)."""
    return copy.deepcopy(DEFAULT_POLICY)


def _codes(result: ValidationResult) -> set[str]:
    return {e["code"] for e in result.errors}


def _paths(result: ValidationResult) -> set[str]:
    return {e["path"] for e in result.errors}


# ─────────────────────────────────────────────────────────────────────────────
# Validator — pass case
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorPass(unittest.TestCase):
    def test_default_policy_passes(self):
        result = validate_policy(_good())
        self.assertTrue(result.ok, msg=f"errors={result.errors}")
        self.assertEqual(result.errors, [])

    def test_realistic_funded_policy_passes(self):
        p = _good()
        p["global"]["auto_trading_enabled"] = True
        p["global"]["max_auto_trading_capital"] = 10000.0
        p["ibkr"]["auto_trading_enabled"] = True
        p["ibkr"]["max_auto_trading_capital"] = 8000.0
        p["ibkr"]["max_single_trade_amount"] = 500.0
        p["ibkr"]["max_daily_loss"] = 200.0
        p["ibkr"]["max_open_positions"] = 10
        p["etoro"]["max_auto_trading_capital"] = 2000.0
        p["etoro"]["max_single_trade_amount"] = 100.0
        result = validate_policy(p)
        self.assertTrue(result.ok, msg=f"errors={result.errors}")


# ─────────────────────────────────────────────────────────────────────────────
# Validator — type / structure failures
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorTypeStructure(unittest.TestCase):
    def test_non_object_policy_rejected(self):
        for bad in ([], "x", 1, None, True):
            result = validate_policy(bad)
            self.assertFalse(result.ok)

    def test_unknown_top_level_key_rejected(self):
        p = _good()
        p["surprise"] = 1
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("unknown_key", _codes(result))

    def test_missing_top_level_key_rejected(self):
        p = _good()
        del p["routing"]
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("missing_key", _codes(result))

    def test_version_must_match(self):
        p = _good()
        p["version"] = 99
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("version_mismatch", _codes(result))

    def test_version_bool_rejected(self):
        p = _good()
        p["version"] = True
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("type_error", _codes(result))

    def test_unknown_key_in_global_rejected(self):
        p = _good()
        p["global"]["surprise"] = 1
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("unknown_key", _codes(result))

    def test_unknown_key_in_broker_rejected(self):
        p = _good()
        p["ibkr"]["surprise"] = 1
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("unknown_key", _codes(result))

    def test_unknown_key_in_routing_rejected(self):
        p = _good()
        p["routing"]["surprise"] = 1
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("unknown_key", _codes(result))


# ─────────────────────────────────────────────────────────────────────────────
# Validator — money & integer rules
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorNumbers(unittest.TestCase):
    def test_negative_global_capital_rejected(self):
        p = _good()
        p["global"]["max_auto_trading_capital"] = -1
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("value_error", _codes(result))

    def test_negative_broker_capital_rejected(self):
        p = _good()
        p["ibkr"]["max_auto_trading_capital"] = -5
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("value_error", _codes(result))

    def test_negative_max_single_trade_rejected(self):
        p = _good()
        p["etoro"]["max_single_trade_amount"] = -1.5
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_negative_max_daily_loss_rejected(self):
        p = _good()
        p["ibkr"]["max_daily_loss"] = -0.01
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_string_money_rejected(self):
        p = _good()
        p["ibkr"]["max_auto_trading_capital"] = "1000"
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_max_open_positions_must_be_int(self):
        p = _good()
        p["ibkr"]["max_open_positions"] = 3.5
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_max_open_positions_negative_rejected(self):
        p = _good()
        p["ibkr"]["max_open_positions"] = -1
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_max_open_positions_bool_rejected(self):
        p = _good()
        p["ibkr"]["max_open_positions"] = True
        result = validate_policy(p)
        self.assertFalse(result.ok)


# ─────────────────────────────────────────────────────────────────────────────
# Validator — boolean strictness
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorBooleans(unittest.TestCase):
    def test_global_auto_trading_string_rejected(self):
        p = _good()
        p["global"]["auto_trading_enabled"] = "true"
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("type_error", _codes(result))

    def test_global_kill_switch_int_rejected(self):
        p = _good()
        p["global"]["kill_switch"] = 1
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_broker_kill_switch_string_rejected(self):
        p = _good()
        p["ibkr"]["kill_switch"] = "false"
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_etoro_live_enabled_string_rejected(self):
        p = _good()
        p["routing"]["etoro_live_enabled"] = "false"
        result = validate_policy(p)
        self.assertFalse(result.ok)


# ─────────────────────────────────────────────────────────────────────────────
# Validator — cross-field rules
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorCrossField(unittest.TestCase):
    def test_single_trade_exceeds_broker_capital_rejected(self):
        p = _good()
        p["ibkr"]["max_auto_trading_capital"] = 100.0
        p["ibkr"]["max_single_trade_amount"] = 200.0
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("single_trade_exceeds_capital", _codes(result))

    def test_single_trade_equal_to_capital_ok(self):
        p = _good()
        p["global"]["max_auto_trading_capital"] = 1000.0
        p["ibkr"]["max_auto_trading_capital"] = 1000.0
        p["ibkr"]["max_single_trade_amount"] = 1000.0
        result = validate_policy(p)
        self.assertTrue(result.ok, msg=f"errors={result.errors}")

    def test_broker_capital_exceeds_global_rejected(self):
        p = _good()
        p["global"]["max_auto_trading_capital"] = 1000.0
        p["ibkr"]["max_auto_trading_capital"] = 5000.0
        p["ibkr"]["max_single_trade_amount"] = 100.0
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("exceeds_global_capital", _codes(result))

    def test_broker_capital_when_global_zero_unconstrained(self):
        p = _good()
        # global cap 0 -> per-broker not constrained by global rule
        p["global"]["max_auto_trading_capital"] = 0.0
        p["ibkr"]["max_auto_trading_capital"] = 5000.0
        p["ibkr"]["max_single_trade_amount"] = 100.0
        result = validate_policy(p)
        self.assertTrue(result.ok, msg=f"errors={result.errors}")


# ─────────────────────────────────────────────────────────────────────────────
# Validator — routing rules
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorRouting(unittest.TestCase):
    def test_default_broker_not_in_allowed_rejected(self):
        p = _good()
        p["routing"]["default_broker"] = "ibkr_live"
        p["routing"]["allowed_brokers"] = ["paper", "ibkr_paper"]
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("not_in_allowed", _codes(result))

    def test_etoro_real_in_allowed_brokers_accepted_m13_5_b(self):
        # M13.5.B: etoro_real is now in ALLOWED_BROKER_WHITELIST. Validation
        # accepts it. Runtime gating (is_auto_trading_allowed + .env flag +
        # EtoroLiveBroker preflight + operator nonce) blocks actual writes.
        p = _good()
        p["routing"]["allowed_brokers"].append("etoro_real")
        result = validate_policy(p)
        self.assertTrue(result.ok, msg=f"errors={result.errors}")
        # And policy alone is still insufficient to enable live trading:
        # without etoro_live_enabled=True, is_auto_trading_allowed says no.
        p["global"]["auto_trading_enabled"] = True
        p["etoro"]["auto_trading_enabled"] = True
        ok, reason = is_auto_trading_allowed(p, "etoro_real")
        self.assertFalse(ok)
        self.assertEqual(reason, "etoro_live_disabled")

    def test_etoro_real_as_default_broker_accepted_when_in_allowed(self):
        # M13.5.B: now permitted at validation, provided it's in allowed_brokers.
        p = _good()
        p["routing"]["allowed_brokers"].append("etoro_real")
        p["routing"]["default_broker"] = "etoro_real"
        result = validate_policy(p)
        self.assertTrue(result.ok, msg=f"errors={result.errors}")

    def test_etoro_live_enabled_true_now_accepted_at_validation(self):
        # M13.5.B: the M13.4A 'etoro_live_forbidden' rejection is lifted.
        # The flag is permitted at validation; runtime gating decides.
        p = _good()
        p["routing"]["etoro_live_enabled"] = True
        result = validate_policy(p)
        self.assertTrue(result.ok, msg=f"errors={result.errors}")
        # etoro_live_forbidden must no longer appear among error codes.
        self.assertNotIn("etoro_live_forbidden", _codes(result))

    def test_unknown_broker_in_allowed_rejected(self):
        p = _good()
        p["routing"]["allowed_brokers"].append("ibkr_mythical")
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("unknown_broker", _codes(result))

    def test_route_override_must_be_in_allowed(self):
        p = _good()
        p["routing"]["allowed_brokers"] = ["paper", "etoro_paper"]
        p["routing"]["default_broker"] = "paper"
        p["routing"]["route_overrides"]["IBKR"] = "ibkr_live"
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("not_in_allowed", _codes(result))

    def test_route_override_to_etoro_real_requires_in_allowed_m13_5_b(self):
        # M13.5.B: route override to etoro_real is now permitted if and only
        # if etoro_real is also in allowed_brokers.
        p = _good()
        # Without etoro_real in allowed_brokers -> rejected as not_in_allowed.
        p["routing"]["route_overrides"]["ETORO"] = "etoro_real"
        result = validate_policy(p)
        self.assertFalse(result.ok)
        self.assertIn("not_in_allowed", _codes(result))
        # With etoro_real in allowed_brokers -> accepted at validation.
        p["routing"]["allowed_brokers"].append("etoro_real")
        result2 = validate_policy(p)
        self.assertTrue(result2.ok, msg=f"errors={result2.errors}")

    def test_route_overrides_non_string_rejected(self):
        p = _good()
        p["routing"]["route_overrides"]["IBKR"] = 1
        result = validate_policy(p)
        self.assertFalse(result.ok)

    def test_allowed_brokers_non_list_rejected(self):
        p = _good()
        p["routing"]["allowed_brokers"] = "paper"
        result = validate_policy(p)
        self.assertFalse(result.ok)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence: load_policy / save_policy
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.db = f.name
        # Pre-create the existing portfolio_risk_state table (production DB
        # state — flywheel.ensure_schema() would create this).
        c = sqlite3.connect(self.db)
        c.execute(
            "CREATE TABLE portfolio_risk_state ("
            "  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        c.commit()
        c.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _conn(self):
        return sqlite3.connect(self.db)

    def test_load_returns_default_when_absent(self):
        with self._conn() as c:
            p = load_policy(c)
        self.assertEqual(p, DEFAULT_POLICY)
        # Must be a deep copy — mutating must not affect DEFAULT_POLICY.
        p["global"]["auto_trading_enabled"] = True
        self.assertFalse(DEFAULT_POLICY["global"]["auto_trading_enabled"])

    def test_save_then_load_round_trip(self):
        p = _good()
        p["ibkr"]["max_open_positions"] = 7
        p["etoro"]["max_auto_trading_capital"] = 1234.5
        with self._conn() as c:
            save_policy(c, p)
        with self._conn() as c:
            loaded = load_policy(c)
        self.assertEqual(loaded["ibkr"]["max_open_positions"], 7)
        self.assertEqual(loaded["etoro"]["max_auto_trading_capital"], 1234.5)

    def test_save_invalid_raises(self):
        # M13.5.B: etoro_live_enabled=True is no longer invalid. Use a
        # genuinely invalid value (negative money field) instead.
        bad = _good()
        bad["ibkr"]["max_daily_loss"] = -1.0
        with self._conn() as c:
            with self.assertRaises(ValueError):
                save_policy(c, bad)

    def test_corrupt_json_returns_default(self):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO portfolio_risk_state "
                "(key, value, updated_at) VALUES (?, ?, ?)",
                (POLICY_KEY, "{not json", "2026-01-01T00:00:00+00:00"),
            )
            c.commit()
        with self._conn() as c:
            p = load_policy(c)
        self.assertEqual(p, DEFAULT_POLICY)

    def test_non_object_stored_returns_default(self):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO portfolio_risk_state "
                "(key, value, updated_at) VALUES (?, ?, ?)",
                (POLICY_KEY, json.dumps([1, 2, 3]),
                 "2026-01-01T00:00:00+00:00"),
            )
            c.commit()
        with self._conn() as c:
            p = load_policy(c)
        self.assertEqual(p, DEFAULT_POLICY)

    def test_load_creates_table_if_missing(self):
        # Drop the table first to simulate a fresh DB.
        with self._conn() as c:
            c.execute("DROP TABLE portfolio_risk_state")
            c.commit()
        with self._conn() as c:
            p = load_policy(c)
        self.assertEqual(p, DEFAULT_POLICY)


# ─────────────────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestReadHelpers(unittest.TestCase):
    def _enable(self) -> dict:
        p = _good()
        p["global"]["auto_trading_enabled"] = True
        p["ibkr"]["auto_trading_enabled"] = True
        p["etoro"]["auto_trading_enabled"] = True
        return p

    def test_is_broker_allowed_paper(self):
        p = _good()
        self.assertTrue(is_broker_allowed(p, "paper"))

    def test_is_broker_allowed_etoro_real_follows_allowed_brokers_m13_5_b(self):
        # M13.5.B: is_broker_allowed now reflects allowed_brokers honestly.
        # The runtime gating against live writes lives in
        # is_auto_trading_allowed() + the .env flag + the live broker
        # preflight, not in is_broker_allowed().
        p = _good()
        # Default policy does not list etoro_real -> False.
        self.assertFalse(is_broker_allowed(p, "etoro_real"))
        # Operator opts in -> True at the registry level. Live writes
        # still require the additional gates.
        p["routing"]["allowed_brokers"].append("etoro_real")
        self.assertTrue(is_broker_allowed(p, "etoro_real"))

    def test_is_broker_allowed_unknown_false(self):
        p = _good()
        self.assertFalse(is_broker_allowed(p, "made_up_broker"))

    def test_auto_trading_global_disabled(self):
        p = _good()  # global.auto_trading_enabled defaults to False
        ok, reason = is_auto_trading_allowed(p, "ibkr_paper")
        self.assertFalse(ok)
        self.assertEqual(reason, "global_disabled")

    def test_auto_trading_global_kill_switch(self):
        p = self._enable()
        p["global"]["kill_switch"] = True
        ok, reason = is_auto_trading_allowed(p, "ibkr_paper")
        self.assertFalse(ok)
        self.assertEqual(reason, "global_kill_switch")

    def test_auto_trading_broker_not_allowed(self):
        p = self._enable()
        p["routing"]["allowed_brokers"] = ["paper"]
        ok, reason = is_auto_trading_allowed(p, "ibkr_paper")
        self.assertFalse(ok)
        self.assertEqual(reason, "broker_not_allowed")

    def test_auto_trading_broker_disabled(self):
        p = self._enable()
        p["ibkr"]["auto_trading_enabled"] = False
        ok, reason = is_auto_trading_allowed(p, "ibkr_paper")
        self.assertFalse(ok)
        self.assertEqual(reason, "broker_disabled")

    def test_auto_trading_broker_kill_switch(self):
        p = self._enable()
        p["ibkr"]["kill_switch"] = True
        ok, reason = is_auto_trading_allowed(p, "ibkr_paper")
        self.assertFalse(ok)
        self.assertEqual(reason, "broker_kill_switch")

    def test_auto_trading_etoro_real_blocked_when_live_disabled(self):
        p = self._enable()
        # etoro_live_enabled defaults to False -> etoro_real blocked.
        ok, reason = is_auto_trading_allowed(p, "etoro_real")
        self.assertFalse(ok)
        self.assertEqual(reason, "etoro_live_disabled")

    def test_auto_trading_etoro_paper_ok(self):
        p = self._enable()
        ok, reason = is_auto_trading_allowed(p, "etoro_paper")
        self.assertTrue(ok, msg=f"reason={reason}")

    def test_auto_trading_paper_ok_with_global_enabled(self):
        p = self._enable()
        ok, _ = is_auto_trading_allowed(p, "paper")
        self.assertTrue(ok)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard endpoints — auth, validation, persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardEndpoints(unittest.TestCase):
    """End-to-end tests against the Flask app's broker-allocation endpoints.

    Uses Flask's test_client(). Auth is enforced via DASHBOARD_PASSWORD; we
    set it before importing the dashboard module."""

    @classmethod
    def setUpClass(cls):
        # Isolated tempdir so we don't touch the real signals.db.
        cls.tmp = tempfile.mkdtemp(prefix="m13_4a_dash_")
        cls.repo_root = Path(__file__).resolve().parent
        cls.data_dir = Path(cls.tmp) / "data"
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.data_dir / "signals.db"
        # Seed an empty portfolio_risk_state table.
        c = sqlite3.connect(str(cls.db_path))
        c.execute(
            "CREATE TABLE portfolio_risk_state ("
            "  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        c.commit()
        c.close()

        # M15.3.A.cutover fix-2: dashboard.app calls `load_dotenv()` at
        # import time. On the VPS post-cutover, /opt/algo-trader/.env now
        # carries DASHBOARD_PASSWORD_HASH (real operator hash, would
        # short-circuit our test password), DASHBOARD_TOTP_SECRET (would
        # require a TOTP code our test doesn't have), DASHBOARD_HTTPS_MODE=
        # true (would set Secure cookies that Werkzeug's HTTP test_client
        # won't send back), and DASHBOARD_BIND_HOST=127.0.0.1. Setting
        # DASHBOARD_PASSWORD before the import is necessary but not
        # sufficient — dotenv's default override=False does skip our
        # pre-set DASHBOARD_PASSWORD (good), BUT it also LOADS all the
        # other vars from .env (since they weren't pre-set), which
        # poisons the test environment. We clean those out AFTER the
        # import, then re-apply harden_app_config so cookies are HTTP-safe.
        os.environ["DASHBOARD_PASSWORD"] = "testpw"

        # Import dashboard.app fresh and rebind its BASE_DIR / DB_PATH to
        # the temp tree so we don't mutate the real DB.
        if "dashboard.app" in sys.modules:
            del sys.modules["dashboard.app"]
        sys.path.insert(0, str(cls.repo_root))
        import dashboard.app as dash_app

        # Clear dotenv-loaded pollutants AFTER the import.
        for _k in ("DASHBOARD_PASSWORD_HASH", "DASHBOARD_TOTP_SECRET",
                    "DASHBOARD_HTTPS_MODE", "DASHBOARD_COOKIE_SECURE",
                    "DASHBOARD_BIND_HOST",
                    "DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE"):
            os.environ.pop(_k, None)
        # Re-assert the test password (in case dotenv override happened
        # somewhere; cheap defensive).
        os.environ["DASHBOARD_PASSWORD"] = "testpw"

        dash_app.BASE_DIR = Path(cls.tmp)
        dash_app.DB_PATH = cls.db_path
        dash_app.app.config["TESTING"] = True

        # Re-harden cookie config WITHOUT https_mode so Secure flag is off
        # — Werkzeug's HTTP test_client won't return Secure cookies.
        try:
            from dashboard.auth.sessions import harden_app_config
            import logging
            silent = logging.getLogger("test_silent_m13_4a")
            silent.addHandler(logging.NullHandler())
            silent.propagate = False
            harden_app_config(dash_app.app, logger=silent)
        except ImportError:
            pass

        cls.dash_app = dash_app

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # Reset the policy row before each test.
        c = sqlite3.connect(str(self.db_path))
        c.execute("DELETE FROM portfolio_risk_state WHERE key=?", (POLICY_KEY,))
        c.commit()
        c.close()
        self.client = self.dash_app.app.test_client()

    def _login(self):
        # M15.3.A: capture CSRF token from login response so subsequent
        # POSTs can attach the X-CSRF-Token header. /api/login itself
        # is CSRF-exempt (no session yet) but every other state-changing
        # endpoint requires the header.
        r = self.client.post(
            "/api/login",
            data=json.dumps({"password": "testpw"}),
            content_type="application/json",
        )
        try:
            self._csrf = (r.get_json() or {}).get("csrf_token", "")
        except Exception:
            self._csrf = ""
        return r

    def _csrf_headers(self):
        """M15.3.A: header to attach to state-changing requests after
        _login(). Empty string if login hasn't been called yet."""
        return {"X-CSRF-Token": getattr(self, "_csrf", "")}

    # --- auth ---------------------------------------------------------------

    def test_get_requires_auth(self):
        r = self.client.get("/api/broker-allocation")
        self.assertEqual(r.status_code, 401)

    def test_post_requires_auth(self):
        r = self.client.post(
            "/api/broker-allocation",
            data=json.dumps(_good()),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 401)

    # --- happy path ---------------------------------------------------------

    def test_get_returns_default_when_no_row(self):
        self._login()
        r = self.client.get("/api/broker-allocation")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("policy", body)
        self.assertEqual(body["policy"], DEFAULT_POLICY)

    def test_post_persists_valid_policy(self):
        self._login()
        p = _good()
        p["ibkr"]["max_open_positions"] = 5
        r = self.client.post(
            "/api/broker-allocation",
            data=json.dumps(p),
            content_type="application/json",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body.get("ok"))

        # Verify it round-trips.
        r2 = self.client.get("/api/broker-allocation")
        body2 = r2.get_json()
        self.assertEqual(body2["policy"]["ibkr"]["max_open_positions"], 5)

    # --- rejection paths ----------------------------------------------------

    def test_post_accepts_etoro_live_enabled_true_m13_5_b(self):
        # M13.5.B: validation no longer rejects etoro_live_enabled=true.
        self._login()
        p = _good()
        p["routing"]["etoro_live_enabled"] = True
        r = self.client.post(
            "/api/broker-allocation",
            data=json.dumps(p),
            content_type="application/json",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))

    def test_post_accepts_etoro_real_in_allowed_brokers_m13_5_b(self):
        # M13.5.B: etoro_real is now in ALLOWED_BROKER_WHITELIST.
        self._login()
        p = _good()
        p["routing"]["allowed_brokers"].append("etoro_real")
        r = self.client.post(
            "/api/broker-allocation",
            data=json.dumps(p),
            content_type="application/json",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r.status_code, 200, msg=r.get_data(as_text=True))

    def test_post_rejects_unknown_top_level_key(self):
        self._login()
        p = _good()
        p["surprise"] = 42
        r = self.client.post(
            "/api/broker-allocation",
            data=json.dumps(p),
            content_type="application/json",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        codes = {e["code"] for e in body.get("errors", [])}
        self.assertIn("unknown_key", codes)

    def test_post_rejects_negative_money(self):
        self._login()
        p = _good()
        p["ibkr"]["max_daily_loss"] = -1
        r = self.client.post(
            "/api/broker-allocation",
            data=json.dumps(p),
            content_type="application/json",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r.status_code, 400)

    def test_post_rejects_non_json(self):
        self._login()
        r = self.client.post(
            "/api/broker-allocation",
            data="not json",
            content_type="application/json",
            headers=self._csrf_headers(),
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)

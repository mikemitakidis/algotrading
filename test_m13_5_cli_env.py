"""M13.5.B blocker-1 — the live-write CLI must load <repo>/.env.

The runbook tells the operator to put eToro keys + ETORO_LIVE_ENABLED in
.env and then run `python3 tools/etoro_live_write.py oneshot ...`. The
CLI must therefore load .env itself (the operator does not source it).

These tests:
  * prove _load_env() reads values from a .env file into os.environ;
  * prove an already-exported env var is NOT overridden by .env;
  * prove _read_keys() then sees the loaded values;
  * confirm no secret value is printed to stdout/stderr.

No network, no eToro endpoint, no real keys.
"""
from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tools.etoro_live_write as cli


SENTINEL_API = "SENTINEL_API_KEY_DO_NOT_LOG_1234"
SENTINEL_USER = "SENTINEL_USER_KEY_DO_NOT_LOG_5678"


class _CleanEnv:
    """Save/restore the eToro env vars around a test."""
    KEYS = ["ETORO_REAL_API_KEY", "ETORO_REAL_USER_KEY",
            "ETORO_DEMO_API_KEY", "ETORO_DEMO_USER_KEY",
            "ETORO_LIVE_ENABLED"]

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_env(dirpath: Path, body: str) -> Path:
    p = dirpath / ".env"
    p.write_text(body, encoding="utf-8")
    return p


class TestLoadEnv(unittest.TestCase):
    def test_loads_values_from_dotenv(self):
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_REAL_API_KEY={SENTINEL_API}\n"
                       f"ETORO_REAL_USER_KEY={SENTINEL_USER}\n"
                       f"ETORO_LIVE_ENABLED=true\n")
            ok = cli._load_env(root)
            self.assertTrue(ok)
            self.assertEqual(os.environ.get("ETORO_REAL_API_KEY"), SENTINEL_API)
            self.assertEqual(os.environ.get("ETORO_REAL_USER_KEY"), SENTINEL_USER)
            self.assertEqual(os.environ.get("ETORO_LIVE_ENABLED"), "true")

    def test_missing_dotenv_returns_false(self):
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            ok = cli._load_env(Path(d))   # no .env written
            self.assertFalse(ok)

    def test_does_not_override_existing_env(self):
        # An explicitly-exported value must win over .env (load_dotenv
        # default override=False).
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            os.environ["ETORO_REAL_API_KEY"] = "EXPORTED_WINS"
            _write_env(Path(d), f"ETORO_REAL_API_KEY={SENTINEL_API}\n")
            cli._load_env(Path(d))
            self.assertEqual(os.environ.get("ETORO_REAL_API_KEY"),
                             "EXPORTED_WINS")

    def test_read_keys_sees_loaded_values(self):
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_REAL_API_KEY={SENTINEL_API}\n"
                       f"ETORO_REAL_USER_KEY={SENTINEL_USER}\n"
                       f"ETORO_LIVE_ENABLED=true\n")
            cli._load_env(root)
            api, user, env_live, base_url = cli._read_keys(demo=False)
            self.assertEqual(api, SENTINEL_API)
            self.assertEqual(user, SENTINEL_USER)
            self.assertTrue(env_live)
            self.assertEqual(base_url, "https://public-api.etoro.com")

    def test_load_env_does_not_print_secrets(self):
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_REAL_API_KEY={SENTINEL_API}\n"
                       f"ETORO_REAL_USER_KEY={SENTINEL_USER}\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                cli._load_env(root)
            combined = out.getvalue() + err.getvalue()
            self.assertNotIn(SENTINEL_API, combined)
            self.assertNotIn(SENTINEL_USER, combined)

    def test_demo_guard_fails_closed_when_disabled(self):
        # Demo mode is disabled in M13.5.B — the guard must raise.
        self.assertFalse(cli.DEMO_MODE_ENABLED)
        with self.assertRaises(SystemExit):
            cli._demo_guard(True)
        # Non-demo never trips the guard.
        cli._demo_guard(False)  # no raise

    def test_read_keys_demo_disabled_raises(self):
        # Even with demo keys present, demo mode must refuse while
        # DEMO_MODE_ENABLED is False — and must NEVER use real keys.
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_DEMO_API_KEY={SENTINEL_API}\n"
                       f"ETORO_DEMO_USER_KEY={SENTINEL_USER}\n"
                       f"ETORO_REAL_API_KEY=REAL_SHOULD_NEVER_BE_USED\n"
                       f"ETORO_REAL_USER_KEY=REAL_SHOULD_NEVER_BE_USED\n")
            cli._load_env(root)
            with self.assertRaises(SystemExit):
                cli._read_keys(demo=True)

    def test_demo_never_falls_back_to_real_keys(self):
        # Temporarily simulate demo being enabled to exercise the
        # no-fallback logic. Real keys present, demo keys absent → must
        # raise about missing DEMO keys, never silently use real.
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_REAL_API_KEY=REAL_SHOULD_NEVER_BE_USED\n"
                       f"ETORO_REAL_USER_KEY=REAL_SHOULD_NEVER_BE_USED\n")
            cli._load_env(root)
            orig = cli.DEMO_MODE_ENABLED
            cli.DEMO_MODE_ENABLED = True
            try:
                with self.assertRaises(SystemExit) as ctx:
                    cli._read_keys(demo=True)
                self.assertIn("ETORO_DEMO", str(ctx.exception))
            finally:
                cli.DEMO_MODE_ENABLED = orig

    def test_demo_requires_demo_base_url(self):
        # Demo enabled + demo keys present but no base URL → raise.
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_DEMO_API_KEY={SENTINEL_API}\n"
                       f"ETORO_DEMO_USER_KEY={SENTINEL_USER}\n")
            cli._load_env(root)
            orig = cli.DEMO_MODE_ENABLED
            cli.DEMO_MODE_ENABLED = True
            try:
                with self.assertRaises(SystemExit) as ctx:
                    cli._read_keys(demo=True)
                self.assertIn("ETORO_DEMO_BASE_URL", str(ctx.exception))
            finally:
                cli.DEMO_MODE_ENABLED = orig

    def test_demo_uses_sandbox_url_never_real(self):
        # Demo enabled + all three demo vars set → returns sandbox URL and
        # demo keys, never the real API base or real keys.
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_DEMO_API_KEY={SENTINEL_API}\n"
                       f"ETORO_DEMO_USER_KEY={SENTINEL_USER}\n"
                       f"ETORO_DEMO_BASE_URL=https://sandbox.example.invalid\n"
                       f"ETORO_REAL_API_KEY=REAL_SHOULD_NEVER_BE_USED\n"
                       f"ETORO_REAL_USER_KEY=REAL_SHOULD_NEVER_BE_USED\n")
            cli._load_env(root)
            orig = cli.DEMO_MODE_ENABLED
            cli.DEMO_MODE_ENABLED = True
            try:
                api, user, env_live, base_url = cli._read_keys(demo=True)
                self.assertEqual(api, SENTINEL_API)
                self.assertEqual(user, SENTINEL_USER)
                self.assertEqual(base_url, "https://sandbox.example.invalid")
                self.assertNotIn("public-api.etoro.com", base_url)
                self.assertNotEqual(api, "REAL_SHOULD_NEVER_BE_USED")
            finally:
                cli.DEMO_MODE_ENABLED = orig

    def test_read_keys_missing_raises(self):
        with _CleanEnv():
            # No keys present anywhere -> SystemExit (CLI aborts cleanly).
            with self.assertRaises(SystemExit):
                cli._read_keys(demo=False)


class TestDemoFailsClosedEndToEnd(unittest.TestCase):
    """Prove --demo aborts in cmd_oneshot BEFORE any credential read,
    broker construction, or POST — by sabotaging those functions so the
    test fails loudly if they are ever reached."""

    def _args(self):
        import argparse
        return argparse.Namespace(
            demo=True, db=":memory:", base_url=None,
            instrument_id=1000, amount=10.0, symbol="SPY", leverage=1,
            is_buy=True, no_stop_loss=True, no_take_profit=True,
            close_plan="manual", market_open=True, open_positions=0,
            realised_daily_loss=0.0, quote_age_sec=1.0,
            quote_max_age_sec=30.0, spread_bps=5.0, spread_max_bps=50.0,
            amount_min=10.0, confirm=None,
            poll_max_attempts=5, poll_interval_sec=2.0,
        )

    def test_oneshot_demo_aborts_before_side_effects(self):
        # Sabotage _read_keys and _import_runtime: if cmd_oneshot reaches
        # either, the test errors out. The demo guard must fire first.
        orig_read = cli._read_keys
        orig_import = cli._import_runtime
        cli._read_keys = lambda demo: (_ for _ in ()).throw(
            AssertionError("_read_keys reached — demo guard failed"))
        cli._import_runtime = lambda: (_ for _ in ()).throw(
            AssertionError("_import_runtime reached — demo guard failed"))
        try:
            rc = cli.cmd_oneshot(self._args())
            # cmd_oneshot raises SystemExit via _demo_guard; if it returns
            # instead, that's still acceptable only if non-zero — but we
            # expect SystemExit.
            self.fail(f"expected SystemExit, got return code {rc}")
        except SystemExit as e:
            msg = str(e)
            self.assertIn("Demo mode is disabled", msg)
        finally:
            cli._read_keys = orig_read
            cli._import_runtime = orig_import


if __name__ == "__main__":
    unittest.main(verbosity=2)

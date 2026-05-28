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
            api, user, env_live = cli._read_keys(demo=False)
            self.assertEqual(api, SENTINEL_API)
            self.assertEqual(user, SENTINEL_USER)
            self.assertTrue(env_live)

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

    def test_read_keys_demo_uses_demo_keys(self):
        with _CleanEnv(), tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_env(root,
                       f"ETORO_DEMO_API_KEY={SENTINEL_API}\n"
                       f"ETORO_DEMO_USER_KEY={SENTINEL_USER}\n")
            cli._load_env(root)
            api, user, env_live = cli._read_keys(demo=True)
            self.assertEqual(api, SENTINEL_API)
            self.assertEqual(user, SENTINEL_USER)
            # demo mode treats env_live as True for the broker constructor
            self.assertTrue(env_live)

    def test_read_keys_missing_raises(self):
        with _CleanEnv():
            # No keys present anywhere -> SystemExit (CLI aborts cleanly).
            with self.assertRaises(SystemExit):
                cli._read_keys(demo=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

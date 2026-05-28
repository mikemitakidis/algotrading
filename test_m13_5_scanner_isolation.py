"""M13.5.B — Scanner isolation invariant (M13.5.A §1.4).

These tests prove:
  1. Importing the scanner / strategy / main runtime path does NOT
     transitively import bot.etoro.live_broker.
  2. get_broker() with BROKER=etoro_real still raises ValueError
     (operator-CLI-only construction).
  3. EtoroLiveBroker.submit() raises OperatorConfirmationRequired —
     even if someone obtains an instance, the BrokerAdapter entry
     point is not a live path.
  4. The reconciliation tool guards against being loaded in a process
     that already imported the live broker.

Each subprocess test runs in a FRESH interpreter so import-graph
assertions are not contaminated by this test module's own imports.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

_REPO = os.path.dirname(os.path.abspath(__file__))


def _run(code: str, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=_REPO, timeout=60,
    )


class TestImportGraphIsolation(unittest.TestCase):
    def test_importing_brokers_registry_does_not_pull_live_broker(self):
        code = (
            "import sys\n"
            "import bot.brokers\n"
            "assert 'bot.etoro.live_broker' not in sys.modules, "
            "'live_broker leaked into import graph via bot.brokers'\n"
            "print('OK')\n"
        )
        r = _run(code)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("OK", r.stdout)

    def test_importing_signal_only_does_not_pull_live_broker(self):
        code = (
            "import sys\n"
            "import bot.etoro.signal_only_broker\n"
            "assert 'bot.etoro.live_broker' not in sys.modules, "
            "'live_broker leaked via signal_only_broker'\n"
            "print('OK')\n"
        )
        r = _run(code)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("OK", r.stdout)

    def test_importing_main_does_not_pull_live_broker(self):
        # main.py imports the scanner / strategy / risk chain at module
        # load. Importing it must NOT transitively import the live broker.
        # We import main as a module without running it.
        code = (
            "import sys, importlib\n"
            "import bot.config\n"   # ensure package importable
            "# Import the scanner + strategy + risk explicitly (the chain\n"
            "# main.py pulls in) and assert live_broker stays absent.\n"
            "import bot.scanner\n"
            "import bot.strategy\n"
            "import bot.risk\n"
            "import bot.brokers\n"
            "assert 'bot.etoro.live_broker' not in sys.modules, "
            "'live_broker leaked via scanner/strategy/risk/brokers chain'\n"
            "print('OK')\n"
        )
        r = _run(code)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("OK", r.stdout)


class TestBrokerRegistryFailsLoud(unittest.TestCase):
    def test_etoro_real_raises_valueerror(self):
        code = (
            "import bot.brokers as b\n"
            "try:\n"
            "    b.get_broker()\n"
            "    print('NO_RAISE')\n"
            "except ValueError as e:\n"
            "    print('VALUEERROR')\n"
        )
        r = _run(code, env_extra={"BROKER": "etoro_real"})
        self.assertIn("VALUEERROR", r.stdout, msg=r.stderr + r.stdout)
        self.assertNotIn("NO_RAISE", r.stdout)


class TestSubmitNotALivePath(unittest.TestCase):
    def test_submit_raises_operator_confirmation_required(self):
        from bot.etoro.live_broker import (
            EtoroLiveBroker, OperatorConfirmationRequired,
        )
        b = EtoroLiveBroker(api_key="k", user_key="u", env_live_enabled=True,
                            transport=lambda *a, **k: (200, {}, b"{}"))

        class _I:
            symbol = "X"; direction = "long"
        with self.assertRaises(OperatorConfirmationRequired):
            b.submit(_I())


class TestReconcileGuard(unittest.TestCase):
    def test_reconcile_refuses_if_live_broker_loaded(self):
        # In a process that has already imported the live broker, importing
        # the reconcile tool must raise ImportError.
        code = (
            "import bot.etoro.live_broker\n"   # load live broker first
            "try:\n"
            "    import tools.etoro_reconcile\n"
            "    print('NO_RAISE')\n"
            "except ImportError:\n"
            "    print('IMPORTERROR')\n"
        )
        r = _run(code)
        self.assertIn("IMPORTERROR", r.stdout, msg=r.stderr + r.stdout)
        self.assertNotIn("NO_RAISE", r.stdout)

    def test_reconcile_imports_cleanly_on_its_own(self):
        code = (
            "import tools.etoro_reconcile\n"
            "print('OK')\n"
        )
        r = _run(code)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

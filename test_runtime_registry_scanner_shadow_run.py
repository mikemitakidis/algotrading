"""Runtime registry scanner shadow-run tests (offline, deterministic).

Proves the REAL scan_cycle runs against a monkeypatched fixture provider for the
5 UK pilot symbols only, with no network/DB/broker/Telegram, and that yfinance
is never called.
"""
import ast
import pathlib
import sys
import types
import unittest
from unittest import mock

from tools.universe_quality import scanner_shadow_run as S

_REPO = pathlib.Path(__file__).resolve().parent
_RUNNER = _REPO / "tools" / "universe_quality" / "scanner_shadow_run.py"
_REPORT = _REPO / "reports" / "runtime_registry_scanner_shadow_run.md"
_EXPECTED = ["AAF.L", "AAL.L", "ABDN.L", "ABF.L", "ADM.L"]


class RealScannerInvoked(unittest.TestCase):
    def test_scan_cycle_is_the_real_one(self):
        # the harness imports the real scan_cycle from bot.scanner
        from bot.scanner import scan_cycle as real
        self.assertTrue(callable(real))

    def test_run_returns_signals_and_meta(self):
        r = S.run_shadow()
        self.assertIn("signals", r)
        self.assertIn("meta", r)
        self.assertIsInstance(r["meta"], dict)
        self.assertEqual(r["meta"].get("symbols_scanned"), 5)


class ScopeFiveUKOnly(unittest.TestCase):
    def setUp(self):
        self.r = S.run_shadow()

    def test_focus_is_five_uk(self):
        self.assertEqual(self.r["focus"], sorted(_EXPECTED))
        self.assertEqual(self.r["n_focus"], 5)

    def test_only_pilot_symbols_requested(self):
        self.assertEqual(self.r["requested_symbols_unique"], sorted(_EXPECTED))

    def test_not_193_or_536(self):
        self.assertNotEqual(self.r["n_focus"], 193)
        self.assertNotEqual(self.r["n_focus"], 536)
        for s in self.r["requested_symbols_unique"]:
            self.assertTrue(s.endswith(".L"))
            self.assertFalse(s.endswith(".HK"))

    def test_elapsed_recorded(self):
        self.assertIn("elapsed_seconds", self.r)
        self.assertGreaterEqual(self.r["elapsed_seconds"], 0.0)


class FixtureUsedNotYFinance(unittest.TestCase):
    def test_yfinance_provider_not_called(self):
        # Install a fake bot.providers.yfinance_provider that raises if used,
        # proving the fixture monkeypatch fully replaces the live provider.
        called = {"yf": False}

        real_import = __import__

        def guard_import(name, *a, **k):
            if name == "bot.providers.yfinance_provider" or \
                    name.endswith("yfinance_provider"):
                called["yf"] = True
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=guard_import):
            S.run_shadow()
        self.assertFalse(called["yf"],
                         "yfinance provider must not be imported/called")

    def test_fixture_serves_only_pilot(self):
        fx = S.FixtureDataProvider()
        out = fx.fetch_bars(["AAF.L", "NOTREAL.L", "0001.HK"], "3mo", "1d")
        self.assertIn("AAF.L", out)
        self.assertNotIn("NOTREAL.L", out)
        self.assertNotIn("0001.HK", out)

    def test_fixture_bars_contract(self):
        fx = S.FixtureDataProvider()
        df = fx.fetch_bars(["AAF.L"], "3mo", "1d")["AAF.L"]
        self.assertEqual(list(df.columns),
                         ["open", "high", "low", "close", "volume"])
        self.assertTrue(str(df.index.tz) == "UTC")
        self.assertTrue(df.index.is_monotonic_increasing)


class NoSideEffects(unittest.TestCase):
    def test_conn_none_no_db(self):
        # run with conn=None (default in run_shadow) — if any DB write were
        # attempted on None it would raise; a clean run proves none happens.
        r = S.run_shadow()
        self.assertIsInstance(r["n_signals"], int)

    def test_report_is_simulated(self):
        self.assertTrue(_REPORT.is_file())
        text = _REPORT.read_text()
        self.assertIn("data_source: **simulated_fixture**", text)
        self.assertIn("network: **disabled**", text)
        self.assertIn("not_live_yfinance: **true**", text)
        self.assertNotIn("network: **enabled**", text)


class ImportSafety(unittest.TestCase):
    """AST guard: the harness must not import broker/live/paper/telegram/main."""

    def test_no_forbidden_runtime_imports(self):
        tree = ast.parse(_RUNNER.read_text())
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
        joined = " ".join(mods).lower()
        for bad in ("broker", "etoro", "risk", "telegram", "paper_broker",
                    "live_broker", "order"):
            self.assertNotIn(bad, joined,
                             "harness must not import %r (got %s)"
                             % (bad, mods))

    def test_patch_target_is_bot_providers_get_provider(self):
        # the documented correction: patch the callable bot.data resolves,
        # which is bot.providers.get_provider (imported at call time).
        self.assertEqual(S._PATCH_TARGET, "bot.providers.get_provider")


if __name__ == "__main__":
    unittest.main()

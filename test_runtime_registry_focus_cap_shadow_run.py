"""Runtime registry FOCUS_SIZE shadow-run tests (offline, deterministic).

Proves focus is sourced from the US default scan-ready set (536) capped to
FOCUS_SIZE (default 150), the REAL scan_cycle runs against a fixture provider
for those symbols, no Yahoo/DB/Telegram/broker, and the global 193 / UK pilot
are not loaded by default.
"""
import ast
import pathlib
import unittest

from tools.universe_quality import focus_cap_shadow_run as F
from bot.universe.active_selection import get_scan_ready_symbols

_REPO = pathlib.Path(__file__).resolve().parent
_RUNNER = _REPO / "tools" / "universe_quality" / "focus_cap_shadow_run.py"
_REPORT = _REPO / "reports" / "runtime_registry_focus_cap_shadow_run.md"
_UK_PILOT = ["AAF.L", "AAL.L", "ABDN.L", "ABF.L", "ADM.L"]


class FocusSelection(unittest.TestCase):
    def test_default_focus_size_is_150(self):
        self.assertEqual(F._DEFAULT_FOCUS_SIZE, 150)
        self.assertEqual(len(F.select_focus()), 150)

    def test_focus_from_us_scan_ready(self):
        us = set(get_scan_ready_symbols())
        focus = F.select_focus()
        self.assertTrue(all(s in us for s in focus))

    def test_focus_excludes_uk_pilot_and_suffixed(self):
        focus = F.select_focus()
        for s in focus:
            self.assertNotIn(s, _UK_PILOT)
            self.assertFalse(s.endswith(".L"))
            self.assertFalse(s.endswith(".HK"))

    def test_parameterised_size(self):
        self.assertEqual(len(F.select_focus(focus_size=300)), 300)
        self.assertEqual(len(F.select_focus(focus_size=536)), 536)

    def test_uk_pilot_only_when_explicit(self):
        uk = F.select_focus(source="uk_pilot")
        self.assertEqual(sorted(uk), sorted(_UK_PILOT))

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            F.select_focus(source="global")


class RealScannerRun(unittest.TestCase):
    def setUp(self):
        self.r = F.run_shadow()  # default 150, us_default

    def test_exactly_150_scanned(self):
        self.assertEqual(self.r["n_focus"], 150)
        self.assertEqual(self.r["symbols_scanned"], 150)
        self.assertEqual(self.r["n_requested_unique"], 150)

    def test_not_193_not_536_by_default(self):
        self.assertNotEqual(self.r["n_focus"], 193)
        self.assertNotEqual(self.r["n_focus"], 536)

    def test_elapsed_and_provider_recorded(self):
        self.assertIn("elapsed_seconds", self.r)
        self.assertGreaterEqual(self.r["elapsed_seconds"], 0.0)
        self.assertIn("n_signals", self.r)

    def test_real_scan_cycle_used(self):
        from bot.scanner import scan_cycle as real
        self.assertTrue(callable(real))
        self.assertIsInstance(self.r["symbols_scanned"], int)


class FixtureNotYFinance(unittest.TestCase):
    def test_fixture_serves_arbitrary_symbols(self):
        fx = F.ArbitrarySymbolFixtureProvider()
        out = fx.fetch_bars(["AAPL", "MSFT", "ZZZZ"], "3mo", "1d")
        self.assertEqual(set(out), {"AAPL", "MSFT", "ZZZZ"})

    def test_fixture_bars_contract(self):
        fx = F.ArbitrarySymbolFixtureProvider()
        df = fx.fetch_bars(["AAPL"], "3mo", "1d")["AAPL"]
        self.assertEqual(list(df.columns),
                         ["open", "high", "low", "close", "volume"])
        self.assertEqual(str(df.index.tz), "UTC")
        self.assertTrue(df.index.is_monotonic_increasing)

    def test_patch_target_is_bot_providers_get_provider(self):
        self.assertEqual(F._PATCH_TARGET, "bot.providers.get_provider")


class ReportProvenance(unittest.TestCase):
    def test_report_is_simulated(self):
        self.assertTrue(_REPORT.is_file())
        text = _REPORT.read_text()
        self.assertIn("data_source: **simulated_fixture**", text)
        self.assertIn("network: **disabled**", text)
        self.assertIn("not_live_yfinance: **true**", text)
        self.assertIn("focus_size: **150**", text)
        self.assertNotIn("network: **enabled**", text)


class ImportSafety(unittest.TestCase):
    def test_no_forbidden_runtime_imports(self):
        tree = ast.parse(_RUNNER.read_text())
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
        joined = " ".join(mods).lower()
        for bad in ("broker", "etoro", "risk", "telegram", "notifier",
                    "paper_broker", "live_broker", "order", "database",
                    "insert_signal"):
            self.assertNotIn(bad, joined,
                             "harness must not import %r (got %s)"
                             % (bad, mods))

    def test_no_main_import(self):
        tree = ast.parse(_RUNNER.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "main")


if __name__ == "__main__":
    unittest.main()

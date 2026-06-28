"""M21.UR UK-only pilot activation tests (read-only on the runtime default).

Proves the pilot is loadable explicitly, returns exactly the 5 approved UK .L
symbols, and is fully isolated from the US default scan-ready set (536). Does
not exercise any runtime/broker wiring (there is none).
"""
import json
import pathlib
import unittest

from bot.universe.active_selection import get_scan_ready_symbols, _DEFAULT_PATHS
from bot.universe.uk_pilot import get_uk_pilot_symbols, UK_PILOT_PATH

_REPO = pathlib.Path(__file__).resolve().parent
_PILOT = _REPO / "configs" / "universe" / "uk_pilot.json"
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"

_EXPECTED = ["AAF.L", "AAL.L", "ABDN.L", "ABF.L", "ADM.L"]
_EXPECTED_INTERNAL = ["LSE:AAF", "LSE:AAL", "LSE:ABDN", "LSE:ABF", "LSE:ADM"]


class PilotAccessor(unittest.TestCase):
    def test_returns_exactly_the_five(self):
        self.assertEqual(get_uk_pilot_symbols(), sorted(_EXPECTED))

    def test_all_suffixed_dot_L(self):
        for s in get_uk_pilot_symbols():
            self.assertTrue(s.endswith(".L"), "%s not .L-suffixed" % s)

    def test_no_hk_symbols(self):
        for s in get_uk_pilot_symbols():
            self.assertFalse(s.endswith(".HK"), "HK leaked into pilot: %s" % s)


class PilotFile(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_PILOT.is_file(), "uk_pilot.json missing")
        self.recs = json.loads(_PILOT.read_text())["symbols"]

    def test_five_records(self):
        self.assertEqual(len(self.recs), 5)
        self.assertEqual(sorted(r["internal_symbol"] for r in self.recs),
                         sorted(_EXPECTED_INTERNAL))

    def test_all_uk_lse_gbp_xlon(self):
        for r in self.recs:
            self.assertEqual(r["region"], "UK")
            self.assertEqual(r["exchange"], "LSE")
            self.assertEqual(r["currency"], "GBP")
            self.assertEqual(r["trading_calendar"], "XLON")

    def test_provider_symbol_present_and_suffixed(self):
        for r in self.recs:
            yf = (r.get("provider_symbols") or {}).get("yfinance")
            self.assertTrue(yf and yf.endswith(".L"))

    def test_scan_ready_true_only_here(self):
        for r in self.recs:
            self.assertTrue(r["scan_ready"], "pilot record must be scan_ready")

    def test_no_duplicate_provider_symbols(self):
        yfs = [r["provider_symbols"]["yfinance"] for r in self.recs]
        self.assertEqual(len(yfs), len(set(yfs)))


class Isolation(unittest.TestCase):
    def test_default_still_536(self):
        self.assertEqual(len(get_scan_ready_symbols()), 536)

    def test_default_excludes_pilot_symbols(self):
        default = set(get_scan_ready_symbols())
        for s in _EXPECTED:
            self.assertNotIn(s, default, "%s leaked into US default" % s)

    def test_default_disjoint_from_pilot(self):
        self.assertTrue(set(get_uk_pilot_symbols())
                        .isdisjoint(set(get_scan_ready_symbols())))

    def test_default_paths_unchanged(self):
        # neither global nor pilot is in the default runtime path
        joined = " ".join(str(p) for p in _DEFAULT_PATHS)
        self.assertNotIn("global_expanded", joined)
        self.assertNotIn("uk_pilot", joined)
        self.assertIn("us_seed", joined)
        self.assertIn("us_expanded", joined)


class GlobalUntouched(unittest.TestCase):
    def test_global_still_193_all_inactive(self):
        if not _GLOBAL.is_file():
            self.skipTest("global file missing")
        recs = json.loads(_GLOBAL.read_text())["symbols"]
        self.assertEqual(len(recs), 193)
        self.assertTrue(all(r.get("scan_ready") is False for r in recs))
        self.assertTrue(all(r.get("active") is False for r in recs))


class AccessorNotRuntimeWired(unittest.TestCase):
    """The pilot accessor must not be imported by any runtime entrypoint."""

    def test_not_imported_by_active_selection(self):
        src = (_REPO / "bot" / "universe" / "active_selection.py").read_text()
        self.assertNotIn("uk_pilot", src)

    def test_not_imported_by_main(self):
        main = _REPO / "main.py"
        if main.is_file():
            self.assertNotIn("uk_pilot", main.read_text())


if __name__ == "__main__":
    unittest.main()

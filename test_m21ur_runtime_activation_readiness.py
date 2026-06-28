"""M21.UR Runtime Registry Activation Readiness report tests (read-only).

Guards the readiness report's key facts AND asserts the live system state is
still inactive at evaluation time, so the report's non-activation claims are
verified against reality (not merely string-matched).
"""
import json
import pathlib
import unittest

_REPO = pathlib.Path(__file__).resolve().parent
_REPORT = _REPO / "reports" / "m21ur_runtime_activation_readiness.md"
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"


class ReadinessReport(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_REPORT.is_file(), "readiness report missing")
        self.text = _REPORT.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_label_and_head(self):
        self.assertIn("M21.UR", self.text)
        self.assertIn("Runtime Registry Activation Readiness", self.text)
        self.assertIn("2e826869e41b110dd922b4d391ec5456b5c2bd62", self.text)

    def test_scope_counts(self):
        for token in ("`global_symbols`: **193**", "UK: **100**",
                      "HK: **93**", "EU: **0**"):
            self.assertIn(token, self.text, "missing %r" % token)

    def test_runtime_flow_documented(self):
        for token in ("get_scan_ready_symbols", "_DEFAULT_PATHS",
                      "us_seed.json", "us_expanded.json",
                      "global_expanded.json"):
            self.assertIn(token, self.text)
        self.assertIn("**536**", self.text)
        self.assertIn("`global_in_default_paths`: **False**", self.text)

    def test_no_activation_performed(self):
        self.assertIn("NO activation is performed", self.text)
        self.assertIn("activation is NOT performed", self.text)

    def test_uk_pilot_is_future_option_only(self):
        self.assertIn("UK-only pilot", self.text)
        self.assertIn("FUTURE OPTION, not a current state", self.text)
        self.assertIn("5/5", self.text)

    def test_hk_blocked_by_rate_limit(self):
        self.assertIn("HK is blocked / deferred", self.text)
        self.assertIn("YFRateLimitError", self.text)
        self.assertIn("provider_rate_limited", self.text)

    def test_minimum_gates_listed(self):
        for token in ("provider symbol present", "suffix valid",
                      "duplicate-free", "provider-backed OHLCV",
                      "liquidity policy decided",
                      "market-hours compatibility",
                      "explicitly blocked"):
            self.assertIn(token, self.text, "missing gate %r" % token)

    def test_future_activation_files_and_rollback(self):
        self.assertIn("active_selection.py", self.text)
        self.assertIn("Rollback", self.text)
        self.assertIn("smallest safe", self.lower)

    def test_explicit_non_activation_statement(self):
        for token in ("no scan_ready change", "no default-path change",
                      "no runtime activation", "no config mutation",
                      "no symbols added", "no live reports committed",
                      "Explicit operator approval is REQUIRED"):
            self.assertIn(token, self.text, "missing %r" % token)

    def test_does_not_claim_active(self):
        self.assertNotIn("is now active", self.lower)
        self.assertNotIn("scan_ready=true on all", self.lower)


class LiveStateStillInactive(unittest.TestCase):
    """Verify the report's non-activation claims against the actual system."""

    def test_global_symbols_193_and_inactive(self):
        if not _GLOBAL.is_file():
            self.skipTest("global file missing")
        recs = json.loads(_GLOBAL.read_text())["symbols"]
        self.assertEqual(len(recs), 193)
        self.assertTrue(all(r.get("scan_ready") is False for r in recs))
        self.assertTrue(all(r.get("active") is False for r in recs))

    def test_scan_ready_still_536_and_global_excluded(self):
        try:
            from bot.universe.active_selection import (
                get_scan_ready_symbols, _DEFAULT_PATHS)
        except Exception:  # pragma: no cover
            self.skipTest("active_selection not importable")
        self.assertEqual(len(get_scan_ready_symbols()), 536)
        self.assertFalse(
            any("global_expanded" in str(p) for p in _DEFAULT_PATHS))


if __name__ == "__main__":
    unittest.main()

"""M21.U Global Universe Foundation closeout/status report tests (read-only).

Guards the status report against drift: it must record the accepted state, the
preserved M21 labels, Europe paused/source-blocked, deferrals, and must NOT
claim Europe succeeded or that full M21 is complete.
"""
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parent
_REPORT = _REPO / "reports" / "m21u_global_universe_foundation_status.md"


class StatusReport(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_REPORT.is_file(), "status report missing")
        self.text = _REPORT.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_preserves_m21_labels(self):
        for label in ("M21.U", "M21.U0", "M21.U0.H", "M21.U1", "M21.U2",
                      "M21.U3.HK", "M21.U4", "M21.UQ", "M21.UR",
                      "Runtime Registry Activation"):
            self.assertIn(label, self.text, "missing label %s" % label)

    def test_does_not_rename_to_numeric(self):
        # M21.U / M21.UQ / M21.UR must NOT be renamed to M21.1/M21.2/M21.3 as
        # sub-milestone replacements. (M21.1..M21.7 score-opt may be MENTIONED.)
        self.assertNotRegex(self.text, r"M21\.UQ\s*->\s*M21\.\d")
        self.assertNotRegex(self.text, r"renamed to M21\.\d")

    def test_records_core_counts(self):
        for token in ("global_symbols: **193**", "scan_ready: **536**",
                      "uk_count: **100**", "hk_count: **93**",
                      "eu_count: **0**"):
            self.assertIn(token, self.text, "missing %s" % token)

    def test_records_main_head(self):
        self.assertIn("18349c3ed5e5158b4621c08b82914f172af80e98", self.text)

    def test_europe_source_blocked_and_paused(self):
        self.assertIn("PAUSED", self.text)
        self.assertIn("SOURCE-BLOCKED", self.text.upper())
        self.assertIn("Europe count = 0", self.text)

    def test_japan_deferred(self):
        self.assertIn("Japan", self.text)
        self.assertIn("DEFERRED", self.text.upper())

    def test_china_adrs_deferred(self):
        self.assertRegex(self.text, r"China\s*/\s*ADRs")

    def test_no_runtime_activation_recorded(self):
        self.assertIn("no runtime activation", self.lower)
        self.assertIn("scan_ready unchanged", self.lower)

    def test_identifies_uq_as_next(self):
        # M21.UQ must be marked NEXT
        self.assertRegex(self.text, r"M21\.UQ[^\n]*NEXT")

    def test_does_not_claim_europe_succeeded(self):
        self.assertIn("did NOT succeed", self.text)
        self.assertNotIn("Europe accepted", self.text)
        self.assertNotIn("ACCEPT_FALLBACK_EXACT for", self.text)

    def test_does_not_claim_full_m21_complete(self):
        self.assertIn("Full M21 is NOT complete", self.text)
        self.assertNotIn("M21 complete", self.text.replace(
            "Full M21 is NOT complete", ""))


if __name__ == "__main__":
    unittest.main()

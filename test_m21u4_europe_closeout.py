"""M21.U4 Europe source-repair closeout test (read-only).

Verifies the committed closeout report records the required facts: no venue
accepted, no curation, the wrong-SMI-fund safety note, and the unchanged-state
confirmations. Guards against a closeout that silently claims success.
"""
import pathlib
import unittest

_REPO = pathlib.Path(__file__).resolve().parent
_REPORT = _REPO / "reports" / "m21u4_europe_closeout.md"


class CloseoutReport(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_REPORT.is_file(), "closeout report missing")
        self.text = _REPORT.read_text(encoding="utf-8")

    def test_states_no_venue_accepted(self):
        self.assertIn("No venue accepted", self.text)
        self.assertNotIn("ACCEPT_FALLBACK_EXACT (", self.text)

    def test_records_final_verification(self):
        for token in ("28 OK", "FINAL_LINK_EXTRACTION_VERIFY_RC=0",
                      "coverage: all repair venues present"):
            self.assertIn(token, self.text)

    def test_records_each_venue_outcome(self):
        for token in ("DAX", "SMI", "AEX", "CAC", "IBEX",
                      "FALLBACK_INCOMPLETE", "BLOCKED_NEEDS_MANUAL_SOURCE"):
            self.assertIn(token, self.text)

    def test_records_wrong_smi_fund_safety_note(self):
        # the concrete wrong-product evidence must be documented
        self.assertIn("SWDA_holdings", self.text)
        self.assertIn("MSCI World", self.text)
        self.assertIn("must NOT be used", self.text)

    def test_confirms_no_state_change(self):
        for token in ("no Europe symbols curated",
                      "global_expanded.json` change",
                      "source_registry.json` change",
                      "scan_ready` unchanged"):
            self.assertIn(token, self.text)

    def test_does_not_claim_success(self):
        lowered = self.text.lower()
        self.assertNotIn("source repair succeeded", lowered)
        self.assertIn("did not succeed", lowered)


if __name__ == "__main__":
    unittest.main()

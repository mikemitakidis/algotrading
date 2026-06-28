"""M21.U4 Europe source-repair report tests (read-only).

Validates the structured findings and that the report generator produces a
well-formed report. No network, no curation, no universe-data access.
"""
import unittest

from tools.eu_source_audit.repair_findings import FINDINGS
from tools.eu_source_audit import gen_repair_report as G

_VENUES = {"dax", "smi", "aex", "cac", "ibex"}
_CLASSES = {
    "ACCEPT_OFFICIAL", "ACCEPT_FALLBACK_EXACT",
    "FALLBACK_INCOMPLETE", "BLOCKED_NEEDS_MANUAL_SOURCE",
}
_ROLES = {"official_index", "official_exchange", "reputable_etf_fallback"}
_REQUIRED_KEYS = {
    "index", "exchange", "suffix", "expected", "audit_result",
    "current_source", "why_failed", "better_sources",
    "exact_count_possible", "fallback_exact_or_incomplete",
    "classification", "manual_file_needed",
}
_EXPECTED_COUNT = {"dax": 40, "smi": 20, "aex": 25, "cac": 40, "ibex": 35}


class FindingsShape(unittest.TestCase):
    def test_all_five_venues_present(self):
        self.assertEqual(set(FINDINGS), _VENUES)

    def test_required_keys(self):
        for v, f in FINDINGS.items():
            self.assertTrue(_REQUIRED_KEYS.issubset(f),
                            "%s missing keys: %s"
                            % (v, _REQUIRED_KEYS - set(f)))

    def test_expected_counts(self):
        for v, n in _EXPECTED_COUNT.items():
            self.assertEqual(FINDINGS[v]["expected"], n)

    def test_classifications_valid(self):
        for v, f in FINDINGS.items():
            self.assertIn(f["classification"], _CLASSES,
                          "%s invalid classification" % v)

    def test_better_sources_roles_valid(self):
        for v, f in FINDINGS.items():
            self.assertTrue(f["better_sources"], "%s no better_sources" % v)
            for role, name, note in f["better_sources"]:
                self.assertIn(role, _ROLES, "%s bad role %s" % (v, role))
                self.assertTrue(name and note)

    def test_dax_is_fallback_incomplete(self):
        # Documented current reality: iShares DAX ETF = 38/40.
        self.assertEqual(FINDINGS["dax"]["classification"],
                         "FALLBACK_INCOMPLETE")

    def test_no_venue_silently_accepted(self):
        # Until a source is verified exact, nothing should be ACCEPT_*.
        for v, f in FINDINGS.items():
            if f["classification"].startswith("ACCEPT"):
                # if ever ACCEPT, fallback flag must say EXACT or be official
                self.assertNotIn("INCOMPLETE",
                                 f["fallback_exact_or_incomplete"].upper())


class ReportGeneration(unittest.TestCase):
    def test_render_contains_all_venues_and_sections(self):
        md = G.render()
        for v in _VENUES:
            self.assertIn(v.upper(), md)
        for section in ("## Summary", "## Decision required",
                        "## Classification legend"):
            self.assertIn(section, md)

    def test_render_is_deterministic(self):
        # Two renders differ only by the timestamp line.
        a = [ln for ln in G.render().splitlines()
             if not ln.startswith("Generated:")]
        b = [ln for ln in G.render().splitlines()
             if not ln.startswith("Generated:")]
        self.assertEqual(a, b)

    def test_decision_section_lists_classifications(self):
        md = G.render()
        self.assertIn("FALLBACK_INCOMPLETE:", md)
        self.assertIn("BLOCKED_NEEDS_MANUAL_SOURCE:", md)


if __name__ == "__main__":
    unittest.main()

"""M21.U4 Europe endpoint-repair tooling tests (read-only).

Validates the corrected venue endpoints and the endpoint-repair report
generator. No network, no curation, no universe-data access.
"""
import unittest

from tools.eu_source_audit.venues import VENUES
from tools.eu_source_audit import gen_endpoint_repair_report as G

_REPAIRED = ["smi", "aex", "cac", "ibex"]


def _fallbacks(v):
    return [u for (role, u, note) in VENUES[v]["endpoints"]
            if role == "reputable_etf_fallback"]


class RepairedEndpoints(unittest.TestCase):
    def test_repaired_venues_have_multiple_fallback_candidates(self):
        # repair added >=2 candidate endpoints each, to beat single-id failures
        for v in _REPAIRED:
            self.assertGreaterEqual(len(_fallbacks(v)), 2,
                                    "%s should have >=2 fallback endpoints" % v)

    def test_repaired_endpoints_changed_from_old_failing_ids(self):
        # the old failing fragments must not be the sole endpoint anymore
        bad = {
            "smi": "291893/ishares-smi-ch-chf-acc",
            "aex": "ishares-aex-ucits-etf/1478358465952",
            "cac": "amundi-cac-40-ucits-etf-dist",
            "ibex": "ishares-ibex-35-ucits-etf/1478358465952",
        }
        for v, frag in bad.items():
            self.assertFalse(any(frag in u for u in _fallbacks(v)),
                             "%s still uses the old failing endpoint" % v)

    def test_each_repaired_endpoint_is_https(self):
        for v in _REPAIRED:
            for u in _fallbacks(v):
                self.assertTrue(u.startswith("https://"), "%s: %s" % (v, u))

    def test_dax_unchanged_single_fallback(self):
        # DAX intentionally not repaired (its ETF is structurally 38/40)
        self.assertEqual(len(_fallbacks("dax")), 1)
        self.assertIn("251464", _fallbacks("dax")[0])

    def test_official_index_endpoint_still_present_each_venue(self):
        for v in _REPAIRED:
            roles = [role for (role, u, n) in VENUES[v]["endpoints"]]
            self.assertIn("official_index", roles)


class RepairReportGeneration(unittest.TestCase):
    def test_plan_only_render_marks_pending(self):
        md = G.render(audit_path="")
        self.assertIn("PLAN ONLY", md)
        self.assertIn("PENDING_AUDIT", md)
        for v in _REPAIRED:
            self.assertIn(v.upper(), md)

    def test_render_has_generation_provenance_note(self):
        md = G.render(audit_path="")
        self.assertIn("generated_at_git_", md)
        self.assertIn("not the final committed-tree state", md)

    def test_render_merges_live_audit_json(self):
        # simulate a live audit json giving SMI exactly 20 -> EXACT
        import json
        import tempfile
        from pathlib import Path
        data = [{
            "venue": "smi",
            "attempts": [{
                "role": "reputable_etf_fallback",
                "inspection": {"included": [("X%d" % i, "n") for i in
                                            range(20)]},
            }],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        md = G.render(audit_path=str(p))
        self.assertIn("ACCEPT_FALLBACK_EXACT", md)
        self.assertIn("LIVE audit results merged", md)


if __name__ == "__main__":
    unittest.main()

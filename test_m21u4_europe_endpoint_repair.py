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
                "url": "http://smi-exact",
                "inspection": {"included": [("X%d" % i, "n") for i in
                                            range(20)],
                               "duplicate_tickers": []},
            }],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        md = G.render(audit_path=str(p))
        self.assertIn("ACCEPT_FALLBACK_EXACT", md)
        self.assertIn("LIVE audit results merged", md)

    def test_first_fallback_exact_not_overwritten_by_later_inexact(self):
        # SMI: attempt 1 exact (20), attempt 2 inexact (18). Must pick exact.
        import json
        import tempfile
        from pathlib import Path
        data = [{
            "venue": "smi",
            "attempts": [
                {"role": "reputable_etf_fallback", "url": "http://exact-20",
                 "inspection": {"included": [("X%d" % i, "n")
                                             for i in range(20)],
                                "duplicate_tickers": []}},
                {"role": "reputable_etf_fallback", "url": "http://inexact-18",
                 "inspection": {"included": [("Y%d" % i, "n")
                                             for i in range(18)],
                                "duplicate_tickers": []}},
            ],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        sel, _aud = G._load_audit(str(p))
        self.assertIn("smi", sel)
        self.assertEqual(sel["smi"]["url"], "http://exact-20")
        md = G.render(audit_path=str(p))
        # the SMI row must be exact + show the exact endpoint
        smi_line = [ln for ln in md.splitlines()
                    if ln.startswith("| SMI ")][0]
        self.assertIn("ACCEPT_FALLBACK_EXACT", smi_line)
        self.assertIn("exact-20", smi_line)
        self.assertNotIn("FALLBACK_INCOMPLETE", smi_line)

    def test_exact_after_inexact_order_also_picks_exact(self):
        # reverse order: inexact first, exact second -> still exact
        import json
        import tempfile
        from pathlib import Path
        data = [{
            "venue": "aex",
            "attempts": [
                {"role": "reputable_etf_fallback", "url": "http://inexact-22",
                 "inspection": {"included": [("Y%d" % i, "n")
                                             for i in range(22)],
                                "duplicate_tickers": []}},
                {"role": "reputable_etf_fallback", "url": "http://exact-25",
                 "inspection": {"included": [("X%d" % i, "n")
                                             for i in range(25)],
                                "duplicate_tickers": []}},
            ],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        sel, _aud = G._load_audit(str(p))
        self.assertEqual(sel["aex"]["url"], "http://exact-25")

    def test_exact_count_with_dups_is_not_exact(self):
        # 35 rows but a duplicate ticker -> not ACCEPT_FALLBACK_EXACT
        import json
        import tempfile
        from pathlib import Path
        data = [{
            "venue": "ibex",
            "attempts": [
                {"role": "reputable_etf_fallback", "url": "http://dup-35",
                 "inspection": {"included": [("X%d" % i, "n")
                                             for i in range(35)],
                                "duplicate_tickers": ["X1"]}},
            ],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        md = G.render(audit_path=str(p))
        ibex_line = [ln for ln in md.splitlines()
                     if ln.startswith("| IBEX ")][0]
        self.assertIn("FALLBACK_INCOMPLETE", ibex_line)
        self.assertNotIn("ACCEPT_FALLBACK_EXACT", ibex_line)

    def test_no_inspected_fallback_is_blocked(self):
        # CAC: attempts present but none have inspection -> venue omitted ->
        # report shows BLOCKED_NEEDS_MANUAL_SOURCE for CAC.
        import json
        import tempfile
        from pathlib import Path
        data = [{
            "venue": "cac",
            "attempts": [
                {"role": "official_index", "url": "http://dyn",
                 "inspection": None},
                {"role": "reputable_etf_fallback", "url": "http://404",
                 "inspection": None},
            ],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        sel, aud = G._load_audit(str(p))
        self.assertNotIn("cac", sel)
        self.assertIn("cac", aud)  # audited, just no usable fallback
        md = G.render(audit_path=str(p))
        cac_line = [ln for ln in md.splitlines()
                    if ln.startswith("| CAC ")][0]
        self.assertIn("BLOCKED_NEEDS_MANUAL_SOURCE", cac_line)

    def test_venue_missing_from_audit_json_is_not_audited_not_blocked(self):
        # audit json contains ONLY smi (exact). AEX/CAC/IBEX absent ->
        # must be NOT_AUDITED, never BLOCKED_NEEDS_MANUAL_SOURCE.
        import json
        import tempfile
        from pathlib import Path
        data = [{
            "venue": "smi",
            "attempts": [{
                "role": "reputable_etf_fallback", "url": "http://smi-20",
                "inspection": {"included": [("X%d" % i, "n")
                                            for i in range(20)],
                               "duplicate_tickers": []}},
            ],
        }]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        md = G.render(audit_path=str(p))
        smi_line = [ln for ln in md.splitlines()
                    if ln.startswith("| SMI ")][0]
        self.assertIn("ACCEPT_FALLBACK_EXACT", smi_line)
        for venue in ("AEX", "CAC", "IBEX"):
            line = [ln for ln in md.splitlines()
                    if ln.startswith("| %s " % venue)][0]
            self.assertIn("NOT_AUDITED", line,
                          "%s should be NOT_AUDITED" % venue)
            self.assertNotIn("BLOCKED_NEEDS_MANUAL_SOURCE", line,
                             "%s must NOT be silently blocked" % venue)
        self.assertIn("COVERAGE WARNING", md)

    def test_coverage_report_flags_incomplete_audit(self):
        import json
        import tempfile
        from pathlib import Path
        data = [{"venue": "smi", "attempts": [
            {"role": "reputable_etf_fallback", "url": "u",
             "inspection": {"included": [("X%d" % i, "n") for i in range(20)],
                            "duplicate_tickers": []}}]}]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        ok, missing = G.coverage_report(str(p))
        self.assertFalse(ok)
        self.assertEqual(set(missing), {"aex", "cac", "ibex"})

    def test_coverage_report_ok_when_all_present(self):
        import json
        import tempfile
        from pathlib import Path

        def rec(v, n):
            return {"venue": v, "attempts": [
                {"role": "reputable_etf_fallback", "url": "u",
                 "inspection": {"included": [("X%d" % i, "n")
                                             for i in range(n)],
                                "duplicate_tickers": []}}]}
        data = [rec("smi", 20), rec("aex", 25), rec("cac", 40),
                rec("ibex", 35)]
        p = Path(tempfile.mkdtemp()) / "audit.json"
        p.write_text(json.dumps(data))
        ok, missing = G.coverage_report(str(p))
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_coverage_report_no_json_is_incomplete(self):
        ok, missing = G.coverage_report("")
        self.assertFalse(ok)
        self.assertEqual(set(missing), {"smi", "aex", "cac", "ibex"})


if __name__ == "__main__":
    unittest.main()

"""M21.UQ closeout/status report tests (read-only).

Guards the closeout report against drift: it must record the scope, structural
result, provider availability semantics, safety confirmations, and that
M21.UR / Runtime Registry Activation are NOT started.
"""
import pathlib
import unittest

_REPO = pathlib.Path(__file__).resolve().parent
_REPORT = _REPO / "reports" / "m21uq_quality_collectors_closeout.md"


class CloseoutReport(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_REPORT.is_file(), "closeout report missing")
        self.text = _REPORT.read_text(encoding="utf-8")
        self.lower = self.text.lower()

    def test_has_m21uq_label_and_head(self):
        self.assertIn("M21.UQ", self.text)
        self.assertIn("5d893ea5fa06a72997567d027cde283d595ace5e", self.text)

    def test_records_scope_counts(self):
        for token in ("total global candidates: **193**", "UK: **100**",
                      "HK: **93**", "EU: **0**"):
            self.assertIn(token, self.text, "missing %r" % token)

    def test_records_structural_result(self):
        for token in ("attempted: **193**", "passed: **193**",
                      "failed: **0**"):
            self.assertIn(token, self.text)
        self.assertIn("liquidity_unknown", self.text)
        self.assertIn("non-fatal", self.lower)

    def test_provider_mode_explicit_only(self):
        self.assertIn("explicit-only", self.lower)
        self.assertIn("--provider none", self.text)
        self.assertIn("no runtime path imports it", self.lower)

    def test_live_smoke_result(self):
        self.assertIn("passed 5/5", self.text)
        self.assertIn("YFRateLimitError", self.text)
        self.assertIn("provider availability", self.lower)

    def test_provider_availability_semantics(self):
        for token in ("provider_rate_limited", "provider_fetch_error",
                      "ohlcv_empty"):
            self.assertIn(token, self.text)
        # rate-limit must NOT be equated with empty/volume failure
        self.assertIn("not `ohlcv_empty`", self.text)
        self.assertIn("volume_missing_or_zero", self.text)

    def test_safety_confirmations(self):
        for token in ("no `configs/universe/global_expanded.json` change",
                      "no `configs/universe/source_registry.json` change",
                      "no scan_ready change",
                      "no runtime activation",
                      "no symbols added",
                      "no live reports committed"):
            self.assertIn(token, self.text, "missing %r" % token)

    def test_no_runtime_or_europe_etc(self):
        self.assertIn("no Europe / Japan / China / ADR", self.text)

    def test_m21ur_not_started(self):
        self.assertIn("M21.UR", self.text)
        self.assertIn("NOT started", self.text)
        self.assertIn("Runtime Registry Activation", self.text)
        self.assertIn("explicit operator approval", self.lower)

    def test_closed_as_readonly_foundation(self):
        self.assertIn("closed as a read-only quality foundation", self.lower)

    def test_does_not_claim_activation(self):
        self.assertNotIn("scan_ready=true", self.lower)
        self.assertNotIn("activated the global universe", self.lower)


if __name__ == "__main__":
    unittest.main()

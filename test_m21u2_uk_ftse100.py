"""M21.U2 — UK FTSE 100 inactive-candidate tests.

Validates the committed global_expanded.json (100 UK records built from the
HL snapshot via the curated CSV) and the provenance chain in
source_registry.json. Read-only assertions; builds nothing.
"""
import json
import pathlib
import unittest
from collections import Counter

from bot.universe.schema import SymbolRecord

_REPO = pathlib.Path(__file__).resolve().parent
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"
_REGISTRY = _REPO / "configs" / "universe" / "source_registry.json"

_RAW_SOURCE_ID = "UK__FTSE100__2026-06-27__001"
_CURATED_SOURCE_ID = "UK__FTSE100__2026-06-27__002"
_ASOF = "2026-06-27"

# The 9 punctuation EPICs and their deterministic curated locals.
_PUNCT = {
    "AV.": "AV", "BA.": "BA", "BP.": "BP", "BT.A": "BT-A", "JD.": "JD",
    "NG.": "NG", "RR.": "RR", "SN.": "SN", "UU.": "UU",
}


def _load_global():
    return json.loads(_GLOBAL.read_text(encoding="utf-8"))


def _uk_records():
    return [r for r in _load_global()["symbols"]
            if "region:uk" in r.get("universe_tags", [])]


class U2Counts(unittest.TestCase):
    def test_exactly_100_uk_records(self):
        self.assertEqual(len(_uk_records()), 100)

    def test_global_total_is_100(self):
        # M21.U2 is the first data batch; global file holds only the UK 100.
        self.assertEqual(len(_load_global()["symbols"]), 100)


class U2RecordShape(unittest.TestCase):
    def setUp(self):
        self.recs = _uk_records()

    def test_all_validate_via_schema(self):
        for r in self.recs:
            SymbolRecord.from_dict(r)  # raises if invalid

    def test_all_inactive_unverified_not_scanready(self):
        for r in self.recs:
            self.assertFalse(r["active"])
            self.assertFalse(r["scan_ready"])
            self.assertEqual(r["data_quality_status"], "unverified")

    def test_liquidity_fields_null(self):
        for r in self.recs:
            for f in ("avg_volume_20d", "avg_dollar_volume_20d",
                      "median_spread_bps", "min_liquidity_tier"):
                self.assertIn(f, r)
                self.assertIsNone(r[f])

    def test_no_execution_or_paper_keys(self):
        for r in self.recs:
            self.assertNotIn("execution_eligible", r)
            self.assertNotIn("paper_routing_eligible", r)

    def test_adapter_and_suffix(self):
        for r in self.recs:
            self.assertEqual(r["exchange"], "LSE")
            self.assertEqual(r["region"], "UK")
            self.assertTrue(r["internal_symbol"].startswith("LSE:"))
            self.assertTrue(
                r["provider_symbols"]["yfinance"].endswith(".L"))


class U2Provenance(unittest.TestCase):
    def test_every_record_source_is_curated_id(self):
        for r in _uk_records():
            self.assertEqual(r["source"], _CURATED_SOURCE_ID)
            self.assertEqual(r["as_of_date"], _ASOF)

    def test_registry_has_raw_and_curated_entries(self):
        reg = json.loads(_REGISTRY.read_text(encoding="utf-8"))
        ids = {s["source_id"]: s for s in reg["sources"]}
        self.assertIn(_RAW_SOURCE_ID, ids)
        self.assertIn(_CURATED_SOURCE_ID, ids)

    def test_curated_notes_link_to_raw(self):
        reg = json.loads(_REGISTRY.read_text(encoding="utf-8"))
        cur = next(s for s in reg["sources"]
                   if s["source_id"] == _CURATED_SOURCE_ID)
        notes = cur.get("notes") or ""
        self.assertIn("curated_from=%s" % _RAW_SOURCE_ID, notes)
        self.assertIn("curation_method=manual_verified_ftse100_csv", notes)
        self.assertIn("curated_schema=m21u2_ftse100_v1", notes)


class U2PunctuationMappings(unittest.TestCase):
    def setUp(self):
        self.by_internal = {r["internal_symbol"]: r for r in _uk_records()}

    def test_nine_punctuation_locals_present(self):
        for raw, local in _PUNCT.items():
            internal = "LSE:%s" % local
            self.assertIn(internal, self.by_internal,
                          "missing curated local for %s -> %s" % (raw, local))
            r = self.by_internal[internal]
            self.assertEqual(r["provider_symbols"]["yfinance"],
                             "%s.L" % local)

    def test_punctuation_rows_have_deterministic_notes(self):
        note_rows = [r for r in _uk_records()
                     if r.get("notes") and "curation_rule=" in r["notes"]]
        self.assertEqual(len(note_rows), 9)
        for r in note_rows:
            self.assertRegex(
                r["notes"],
                r"raw_epic=[^;]+;curated_local=[^;]+;curation_rule=")


class U2NoFakeRowOrDuplicates(unittest.TestCase):
    def test_no_epic_name_header_artifact(self):
        for r in _uk_records():
            self.assertNotEqual(r["internal_symbol"], "LSE:EPIC")
            self.assertNotEqual(r["name"], "Name")

    def test_no_duplicate_internal_or_yfinance(self):
        recs = _uk_records()
        internals = [r["internal_symbol"] for r in recs]
        yfs = [r["provider_symbols"]["yfinance"] for r in recs]
        self.assertEqual(
            sorted(t for t, c in Counter(internals).items() if c > 1), [])
        self.assertEqual(
            sorted(t for t, c in Counter(yfs).items() if c > 1), [])

    def test_no_collision_with_us_registry(self):
        us_internals = set()
        us_yfs = set()
        for p in ("us_seed.json", "us_expanded.json"):
            doc = json.loads(
                (_REPO / "configs" / "universe" / p).read_text("utf-8"))
            for r in doc.get("symbols", []):
                us_internals.add(r["internal_symbol"])
                yf = (r.get("provider_symbols") or {}).get("yfinance")
                if yf:
                    us_yfs.add(yf)
        for r in _uk_records():
            self.assertNotIn(r["internal_symbol"], us_internals)
            self.assertNotIn(r["provider_symbols"]["yfinance"], us_yfs)


class U2RuntimeSafety(unittest.TestCase):
    def test_scan_ready_remains_536(self):
        from bot.universe.active_selection import get_scan_ready_symbols
        self.assertEqual(len(get_scan_ready_symbols()), 536)

    def test_global_not_in_default_paths(self):
        from bot.universe import active_selection as a
        self.assertTrue(
            all("global_expanded" not in str(p) for p in a._DEFAULT_PATHS))


if __name__ == "__main__":
    unittest.main()

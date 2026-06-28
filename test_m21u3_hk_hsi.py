"""M21.U3.HK — Hong Kong Hang Seng inactive-candidate tests.

Validates the committed global_expanded.json (UK 100 + HK 93 = 193 records) and
the HK provenance chain in source_registry.json. Read-only; builds nothing.
"""
import json
import pathlib
import unittest
from collections import Counter

from bot.universe.schema import SymbolRecord

_REPO = pathlib.Path(__file__).resolve().parent
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"
_REGISTRY = _REPO / "configs" / "universe" / "source_registry.json"

_RAW_SOURCE_ID = "HK__HSI__2026-06-26__001"
_CURATED_SOURCE_ID = "HK__HSI__2026-06-26__002"
_ASOF = "2026-06-26"

# major HK names expected in the batch (internal -> yfinance)
_EXAMPLES = {
    "HKEX:0005": "0005.HK", "HKEX:0700": "0700.HK", "HKEX:9988": "9988.HK",
    "HKEX:3690": "3690.HK", "HKEX:9618": "9618.HK", "HKEX:9888": "9888.HK",
}

# cash/bank-account rows that must never appear as equity candidates
_FORBIDDEN_NAMES = (
    "UNITED OVERSEAS BANK LIMITED HK BRANCH",
    "ANZ BANK HONG KONG",
    "HSBC HK (CURRENT ACCOUNT)",
)


def _load_global():
    return json.loads(_GLOBAL.read_text(encoding="utf-8"))


def _hk_records():
    return [r for r in _load_global()["symbols"]
            if "region:hk" in r.get("universe_tags", [])]


class U3HKCounts(unittest.TestCase):
    def test_global_total_is_193(self):
        self.assertEqual(len(_load_global()["symbols"]), 193)

    def test_hk_count_is_93(self):
        self.assertEqual(len(_hk_records()), 93)


class U3HKRecordShape(unittest.TestCase):
    def setUp(self):
        self.recs = _hk_records()

    def test_all_validate_via_schema(self):
        for r in self.recs:
            SymbolRecord.from_dict(r)

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

    def test_exchange_and_region(self):
        for r in self.recs:
            self.assertEqual(r["exchange"], "HKEX")
            self.assertEqual(r["region"], "HK")

    def test_yfinance_suffix_and_4digit_local(self):
        for r in self.recs:
            yf = r["provider_symbols"]["yfinance"]
            self.assertTrue(yf.endswith(".HK"))
            local = r["internal_symbol"].split(":")[1]
            self.assertTrue(local.isdigit() and len(local) == 4,
                            "non-4-digit HK local: %s" % local)


class U3HKProvenance(unittest.TestCase):
    def test_every_record_source_is_curated_id(self):
        for r in _hk_records():
            self.assertEqual(r["source"], _CURATED_SOURCE_ID)
            self.assertEqual(r["as_of_date"], _ASOF)

    def test_registry_has_raw_and_curated_entries(self):
        reg = json.loads(_REGISTRY.read_text(encoding="utf-8"))
        ids = {s["source_id"] for s in reg["sources"]}
        self.assertIn(_RAW_SOURCE_ID, ids)
        self.assertIn(_CURATED_SOURCE_ID, ids)

    def test_curated_notes_link_to_raw(self):
        reg = json.loads(_REGISTRY.read_text(encoding="utf-8"))
        cur = next(s for s in reg["sources"]
                   if s["source_id"] == _CURATED_SOURCE_ID)
        notes = cur.get("notes") or ""
        self.assertIn("curated_from=%s" % _RAW_SOURCE_ID, notes)
        self.assertIn("curation_method=manual_verified_hsi_csv", notes)
        self.assertIn("curated_schema=m21u3_hsi_v1", notes)


class U3HKExamplesAndExclusions(unittest.TestCase):
    def test_major_names_present(self):
        by = {r["internal_symbol"]: r["provider_symbols"]["yfinance"]
              for r in _hk_records()}
        for internal, yf in _EXAMPLES.items():
            self.assertIn(internal, by)
            self.assertEqual(by[internal], yf)

    def test_no_cash_or_bank_account_rows(self):
        names = {r["name"].upper() for r in _hk_records()}
        for bad in _FORBIDDEN_NAMES:
            self.assertNotIn(bad, names)


class U3HKNoDuplicates(unittest.TestCase):
    def test_no_duplicate_internal_or_yfinance_in_global(self):
        s = _load_global()["symbols"]
        internals = [r["internal_symbol"] for r in s]
        yfs = [r["provider_symbols"]["yfinance"] for r in s]
        self.assertEqual(
            sorted(t for t, c in Counter(internals).items() if c > 1), [])
        self.assertEqual(
            sorted(t for t, c in Counter(yfs).items() if c > 1), [])


class U3HKRuntimeSafety(unittest.TestCase):
    def test_scan_ready_remains_536(self):
        from bot.universe.active_selection import get_scan_ready_symbols
        self.assertEqual(len(get_scan_ready_symbols()), 536)

    def test_global_not_in_default_paths(self):
        from bot.universe import active_selection as a
        self.assertTrue(
            all("global_expanded" not in str(p) for p in a._DEFAULT_PATHS))


if __name__ == "__main__":
    unittest.main()

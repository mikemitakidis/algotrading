"""M20.UB — US universe expansion proof tests.

Validates configs/universe/us_expanded.json (S&P 500 + Nasdaq 100 + curated US
ETFs, deduped vs the seed) through the frozen SymbolRecord schema and the full
UniverseRegistry. Confirms status flags, US metadata, dedup/tag-merge, ETF/equity
classes, null liquidity, target count, and that universe CODE + paper + runtime
remain frozen. No bot/paper or runtime touch.
"""
import json
import pathlib
import subprocess
import unittest

from bot.universe.schema import SymbolRecord, AssetClass, DataQualityStatus
from bot.universe.registry import UniverseRegistry

_REPO = pathlib.Path(__file__).resolve().parent
_SEED = _REPO / "configs" / "universe" / "us_seed.json"
_EXPANDED = _REPO / "configs" / "universe" / "us_expanded.json"
_M20H_HEAD = "146759e4d454d0d851345eaf33bbd9f4dedcc50b"
_M20UA_HEAD = "97f02326e12d9e381d94544555524c2d87b2cf27"
_BASELINE = "e823fe6779deaccc7b8ff7859c17b4dab564b868"


def _load(path):
    return json.load(open(path))["symbols"]


class M20UBExpandedRecords(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.seed = _load(_SEED)
        cls.exp = _load(_EXPANDED)
        cls.all = cls.seed + cls.exp

    def test_expanded_file_exists_and_nonempty(self):
        self.assertGreater(len(self.exp), 0)

    def test_all_records_validate(self):
        for r in self.exp:
            SymbolRecord.from_dict(dict(r))

    def test_full_registry_loads(self):
        reg = UniverseRegistry.load([str(_SEED), str(_EXPANDED)])
        self.assertTrue(hasattr(reg, "_records"))

    def test_internal_symbols_unique_across_all(self):
        ids = [r["internal_symbol"] for r in self.all]
        self.assertEqual(len(ids), len(set(ids)))

    def test_yfinance_symbols_unique_across_all(self):
        yfs = [r["provider_symbols"]["yfinance"] for r in self.all]
        self.assertEqual(len(yfs), len(set(yfs)))

    def test_target_unique_count_in_range(self):
        self.assertTrue(550 <= len(self.all) <= 650,
                        f"total universe {len(self.all)} outside 550-650")

    def test_all_expanded_scan_ready_false(self):
        self.assertTrue(all(r["scan_ready"] is False for r in self.exp))

    def test_all_expanded_unverified(self):
        self.assertTrue(all(r["data_quality_status"] == "unverified"
                            for r in self.exp))

    def test_all_expanded_active_true(self):
        self.assertTrue(all(r["active"] is True for r in self.exp))

    def test_all_expanded_us_metadata(self):
        for r in self.exp:
            self.assertEqual(r["country"], "US")
            self.assertEqual(r["currency"], "USD")
            self.assertEqual(r["timezone"], "America/New_York")

    def test_all_expanded_supported_exchange(self):
        for r in self.exp:
            self.assertIn(r["exchange"], ("NASDAQ", "NYSE", "ARCA"))

    def test_liquidity_fields_null(self):
        for r in self.exp:
            self.assertIsNone(r["avg_volume_20d"])
            self.assertIsNone(r["avg_dollar_volume_20d"])
            self.assertIsNone(r["median_spread_bps"])
            self.assertIsNone(r["min_liquidity_tier"])

    def test_etfs_are_etf_class(self):
        etfs = [r for r in self.exp if "us_etf" in r["universe_tags"]]
        self.assertGreater(len(etfs), 0)
        for r in etfs:
            self.assertEqual(r["asset_class"], "ETF")

    def test_etf_count_in_range(self):
        etfs = [r for r in self.exp if r["asset_class"] == "ETF"]
        self.assertTrue(30 <= len(etfs) <= 60, f"ETF count {len(etfs)}")

    def test_equities_are_equity_class(self):
        eqs = [r for r in self.exp if r["asset_class"] == "EQUITY"]
        self.assertGreater(len(eqs), 0)
        for r in eqs:
            self.assertIn("sp500" in r["universe_tags"]
                          or "nasdaq100" in r["universe_tags"], (True,))

    def test_sp500_nasdaq_overlap_merged_single_record(self):
        # any symbol tagged both sp500 and nasdaq100 must be a single record
        both = [r for r in self.all if "sp500" in r["universe_tags"]
                and "nasdaq100" in r["universe_tags"]]
        self.assertGreater(len(both), 0)
        ids = [r["internal_symbol"] for r in both]
        self.assertEqual(len(ids), len(set(ids)))

    def test_seed_overlap_tags_merged(self):
        # AAPL is in the seed and is both S&P500 and Nasdaq100
        aapl = [r for r in self.seed
                if r["internal_symbol"] == "NASDAQ:AAPL"]
        self.assertEqual(len(aapl), 1)
        self.assertIn("sp500", aapl[0]["universe_tags"])
        self.assertIn("nasdaq100", aapl[0]["universe_tags"])

    def test_all_expanded_have_m20_ub_tag(self):
        self.assertTrue(all("m20_ub" in r["universe_tags"] for r in self.exp))

    def test_no_global_symbols(self):
        # every expanded record is US; no non-US country/currency
        for r in self.exp:
            self.assertEqual(r["region"], "US")
            self.assertNotIn(".", r["internal_symbol"].split(":")[0])

    def test_provenance_present(self):
        for r in self.exp:
            self.assertTrue(r["source"])
            self.assertTrue(r["as_of_date"])
            self.assertTrue(r["first_seen_utc"])


class M20UBFrozenChecks(unittest.TestCase):

    def _unchanged(self, baseline, *paths):
        r = subprocess.run(["git", "diff", "--name-only", baseline, "HEAD",
                            "--", *paths], capture_output=True, text=True,
                           timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", f"{paths} changed vs {baseline}")

    def test_paper_frozen_vs_m20h(self):
        self._unchanged(_M20H_HEAD, "bot/paper")

    def test_universe_code_frozen_vs_m20ua(self):
        self._unchanged(_M20UA_HEAD, "bot/universe/schema.py",
                        "bot/universe/registry.py", "bot/universe/suffixes.py")

    def test_protected_runtime_unchanged(self):
        self._unchanged(_BASELINE, "main.py", "bot/scanner.py", "bot/risk.py",
                        "bot/strategy.py", "dashboard/app.py", "bot/brokers",
                        "bot/flywheel.py", "bot/signal_scoring")

    def test_no_authoring_scripts_committed(self):
        r = subprocess.run(["git", "ls-files", "_authoring*"],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()

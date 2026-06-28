"""M21.UQ quality collectors/gates tests (read-only, deterministic, no network).

All OHLCV data is synthetic; the provider is a FixtureProvider. No live Yahoo.
"""
import datetime
import json
import pathlib
import unittest

from tools.universe_quality.evaluators import (
    check_liquidity, check_ohlcv, check_provider_symbol, check_suffix,
    check_volume, evaluate_candidate, find_duplicate_provider_symbols)
from tools.universe_quality.providers import (
    FixtureProvider, normalize_bars)
from tools.universe_quality.quality_model import (
    LIQUIDITY_UNKNOWN, OHLCV_EMPTY, OHLCV_NON_FINITE, OHLCV_STALE,
    OHLCV_TOO_FEW_BARS, OHLCVConfig, PROVIDER_SUFFIX_INVALID,
    PROVIDER_SYMBOL_DUPLICATE, PROVIDER_SYMBOL_MISSING,
    VOLUME_MISSING_OR_ZERO)

_REPO = pathlib.Path(__file__).resolve().parent
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"
_AS_OF = "2026-06-26"


def _rec(internal="LSE:HSBA", yf="HSBA.L", exch="LSE", region="UK",
         liquidity_null=True):
    r = {
        "internal_symbol": internal,
        "provider_symbols": {"yfinance": yf},
        "exchange": exch, "region": region,
        "avg_volume_20d": None, "avg_dollar_volume_20d": None,
        "median_spread_bps": None, "min_liquidity_tier": None,
    }
    if not liquidity_null:
        r["avg_volume_20d"] = 1000000
    return r


def _bars(n, last_date=_AS_OF, volume=1000.0, close=10.0):
    base = datetime.date.fromisoformat(last_date)
    out = []
    for i in range(n):
        d = base - datetime.timedelta(days=(n - 1 - i))
        out.append({"date": d.isoformat(), "open": close, "high": close,
                    "low": close, "close": close, "volume": volume})
    return out


class ProviderSymbolChecks(unittest.TestCase):
    def test_valid_symbol_passes(self):
        self.assertEqual(check_provider_symbol(_rec()), [])

    def test_missing_symbol_fails(self):
        r = _rec()
        r["provider_symbols"] = {}
        self.assertIn(PROVIDER_SYMBOL_MISSING, check_provider_symbol(r))

    def test_empty_symbol_fails(self):
        r = _rec(yf="")
        self.assertIn(PROVIDER_SYMBOL_MISSING, check_provider_symbol(r))


class SuffixChecks(unittest.TestCase):
    def test_uk_lse_suffix_ok(self):
        self.assertEqual(check_suffix(_rec(yf="HSBA.L", exch="LSE")), [])

    def test_hk_hkex_suffix_ok(self):
        self.assertEqual(
            check_suffix(_rec(internal="HKEX:0700", yf="0700.HK",
                              exch="HKEX", region="HK")), [])

    def test_bad_suffix_for_region_fails(self):
        # UK symbol with HK suffix
        self.assertIn(PROVIDER_SUFFIX_INVALID,
                      check_suffix(_rec(yf="HSBA.HK", exch="LSE")))

    def test_unknown_exchange_fails(self):
        self.assertIn(PROVIDER_SUFFIX_INVALID,
                      check_suffix(_rec(yf="X.ZZ", exch="NOPE")))


class OHLCVChecks(unittest.TestCase):
    def setUp(self):
        self.cfg = OHLCVConfig(min_bars=20, max_staleness_days=7)

    def test_valid_ohlcv_passes(self):
        bars = normalize_bars(_bars(25))
        self.assertEqual(check_ohlcv(bars, _AS_OF, self.cfg), [])

    def test_empty_ohlcv_fails(self):
        self.assertIn(OHLCV_EMPTY, check_ohlcv(normalize_bars([]), _AS_OF,
                                               self.cfg))

    def test_none_ohlcv_is_empty(self):
        self.assertIn(OHLCV_EMPTY, check_ohlcv(normalize_bars(None), _AS_OF,
                                               self.cfg))

    def test_too_few_bars_fails(self):
        bars = normalize_bars(_bars(5))
        self.assertIn(OHLCV_TOO_FEW_BARS, check_ohlcv(bars, _AS_OF, self.cfg))

    def test_stale_ohlcv_fails(self):
        bars = normalize_bars(_bars(25, last_date="2026-06-01"))
        self.assertIn(OHLCV_STALE, check_ohlcv(bars, _AS_OF, self.cfg))

    def test_non_finite_ohlcv_fails(self):
        raw = _bars(25)
        raw[-1]["close"] = float("nan")
        bars = normalize_bars(raw)
        self.assertIn(OHLCV_NON_FINITE, check_ohlcv(bars, _AS_OF, self.cfg))

    def test_inf_ohlcv_fails(self):
        raw = _bars(25)
        raw[0]["high"] = float("inf")
        bars = normalize_bars(raw)
        self.assertIn(OHLCV_NON_FINITE, check_ohlcv(bars, _AS_OF, self.cfg))


class VolumeChecks(unittest.TestCase):
    def test_valid_volume_passes(self):
        self.assertEqual(check_volume(normalize_bars(_bars(20))), [])

    def test_zero_volume_fails(self):
        self.assertIn(VOLUME_MISSING_OR_ZERO,
                      check_volume(normalize_bars(_bars(20, volume=0.0))))

    def test_negative_volume_fails(self):
        self.assertIn(VOLUME_MISSING_OR_ZERO,
                      check_volume(normalize_bars(_bars(20, volume=-5.0))))

    def test_nan_volume_fails(self):
        raw = _bars(20)
        raw[0]["volume"] = float("nan")
        # NaN volume -> not > 0 and total stays NaN; treated as missing/zero
        codes = check_volume(normalize_bars(raw))
        self.assertIn(VOLUME_MISSING_OR_ZERO, codes)

    def test_empty_volume_fails(self):
        self.assertIn(VOLUME_MISSING_OR_ZERO, check_volume([]))


class LiquidityChecks(unittest.TestCase):
    def test_null_liquidity_is_warning(self):
        self.assertIn(LIQUIDITY_UNKNOWN, check_liquidity(_rec()))

    def test_known_liquidity_ok(self):
        self.assertEqual(check_liquidity(_rec(liquidity_null=False)), [])


class DuplicateChecks(unittest.TestCase):
    def test_duplicate_provider_symbol_detected(self):
        recs = [_rec(internal="LSE:A", yf="DUP.L"),
                _rec(internal="LSE:B", yf="DUP.L"),
                _rec(internal="LSE:C", yf="UNIQ.L")]
        dups = find_duplicate_provider_symbols(recs)
        self.assertIn("DUP.L", dups)
        self.assertEqual(dups["DUP.L"], 2)
        self.assertNotIn("UNIQ.L", dups)


class EvaluateCandidate(unittest.TestCase):
    def test_structural_pass_without_provider(self):
        # no provider -> OHLCV skipped; valid symbol/suffix -> passes,
        # liquidity-unknown is a warning not a failure
        res = evaluate_candidate(_rec(), provider=None)
        self.assertTrue(res.passed)
        self.assertIn(LIQUIDITY_UNKNOWN, res.warnings)
        self.assertEqual(res.reason_codes, [])

    def test_full_pass_with_provider(self):
        prov = FixtureProvider({"HSBA.L": _bars(25)})
        res = evaluate_candidate(_rec(), provider=prov, as_of=_AS_OF,
                                 cfg=OHLCVConfig())
        self.assertTrue(res.passed)
        self.assertEqual(res.reason_codes, [])

    def test_missing_symbol_makes_fatal(self):
        r = _rec()
        r["provider_symbols"] = {}
        res = evaluate_candidate(r, provider=None)
        self.assertFalse(res.passed)
        self.assertIn(PROVIDER_SYMBOL_MISSING, res.reason_codes)

    def test_empty_ohlcv_makes_fatal(self):
        prov = FixtureProvider({"HSBA.L": []})
        res = evaluate_candidate(_rec(), provider=prov, as_of=_AS_OF)
        self.assertFalse(res.passed)
        self.assertIn(OHLCV_EMPTY, res.reason_codes)

    def test_unfetchable_symbol_ohlcv_empty(self):
        prov = FixtureProvider({})  # HSBA.L absent -> None -> empty
        res = evaluate_candidate(_rec(), provider=prov, as_of=_AS_OF)
        self.assertFalse(res.passed)
        self.assertIn(OHLCV_EMPTY, res.reason_codes)


class RealGlobalFileEvaluation(unittest.TestCase):
    """Run the offline structural evaluation over the actual 193 candidates.
    Proves the framework runs clean on real data and changes nothing."""

    def setUp(self):
        if not _GLOBAL.is_file():
            self.skipTest("global_expanded.json not present")
        self.records = json.loads(_GLOBAL.read_text())["symbols"]

    def test_193_candidates_structural_eval(self):
        from tools.universe_quality.run_quality_report import evaluate_all
        results = evaluate_all(self.records)  # offline, structural only
        self.assertEqual(len(results), 193)
        # all real candidates have valid provider symbol + suffix + no dup ->
        # structural pass (liquidity-unknown is only a warning)
        for r in results:
            self.assertTrue(
                r.passed,
                "%s failed structurally: %s"
                % (r.internal_symbol, r.reason_codes))

    def test_evaluation_does_not_mutate_records(self):
        before = json.dumps(self.records, sort_keys=True)
        from tools.universe_quality.run_quality_report import evaluate_all
        evaluate_all(self.records)
        after = json.dumps(self.records, sort_keys=True)
        self.assertEqual(before, after)

    def test_global_file_untouched_on_disk(self):
        before = _GLOBAL.read_bytes()
        from tools.universe_quality.run_quality_report import evaluate_all
        evaluate_all(self.records)
        self.assertEqual(_GLOBAL.read_bytes(), before)


class ReportStableMetadata(unittest.TestCase):
    """The committed dry-run report must NOT carry unstable per-commit git
    metadata, and must carry the stable wording instead."""

    def setUp(self):
        from tools.universe_quality.run_quality_report import (
            evaluate_all, render)
        if not _GLOBAL.is_file():
            self.skipTest("global_expanded.json not present")
        records = json.loads(_GLOBAL.read_text())["symbols"]
        self.md = render(records, evaluate_all(records))

    def test_no_unstable_git_metadata(self):
        for banned in ("generated_at_git_branch", "generated_at_git_head",
                       "generated_at_git_status", "dirty",
                       "8523a67", "run_environment", "Generated:"):
            self.assertNotIn(banned, self.md,
                             "report must not contain %r" % banned)

    def test_has_stable_wording(self):
        for token in ("report_type", "offline structural dry-run",
                      "source_file", "configs/universe/global_expanded.json",
                      "scope", "existing global candidates only",
                      "network", "disabled",
                      "provider_mode", "none / structural-only"):
            self.assertIn(token, self.md, "report missing %r" % token)

    def test_honest_counts_present(self):
        for token in ("total_candidates: **193**", "HK=93", "UK=100",
                      "passed (no fatal codes): **193**",
                      "failed (>=1 fatal code): **0**"):
            self.assertIn(token, self.md)
        self.assertIn("`liquidity_unknown` | 193", self.md)

    def test_committed_report_file_has_no_stale_metadata(self):
        rp = _REPO / "reports" / "m21uq_quality_collectors_plan_or_dryrun.md"
        if not rp.is_file():
            self.skipTest("committed report not present")
        text = rp.read_text()
        for banned in ("generated_at_git_status", "dirty",
                       "generated_at_git_head"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()

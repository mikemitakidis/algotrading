"""M21.UQ provider-backed dry-run tests (read-only, deterministic, no network).

The yfinance adapter is exercised via an injected _fetch_fn and via a
monkeypatched fake yfinance module. No real network calls.
"""
import datetime
import json
import pathlib
import sys
import types
import unittest

from tools.universe_quality.quality_model import (
    OHLCV_EMPTY, OHLCVConfig)
from tools.universe_quality.yfinance_provider import YFinanceProvider
from tools.universe_quality import run_quality_report as R

_REPO = pathlib.Path(__file__).resolve().parent
_GLOBAL = _REPO / "configs" / "universe" / "global_expanded.json"
_AS_OF = "2026-06-26"


def _bars(n, last_date=_AS_OF, volume=1000.0, close=10.0):
    base = datetime.date.fromisoformat(last_date)
    return [{"date": (base - datetime.timedelta(days=(n - 1 - i))).isoformat(),
             "open": close, "high": close, "low": close, "close": close,
             "volume": volume} for i in range(n)]


class AdapterInjectedFetch(unittest.TestCase):
    def test_injected_fetch_returns_bars(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        out = prov.fetch_ohlcv("HSBA.L")
        self.assertEqual(len(out), 25)

    def test_injected_fetch_none_is_passthrough(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: None)
        self.assertIsNone(prov.fetch_ohlcv("NOPE.L"))

    def test_injected_fetch_empty(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: [])
        self.assertEqual(prov.fetch_ohlcv("X.L"), [])


class AdapterFakeYFinanceModule(unittest.TestCase):
    """Monkeypatch a fake `yfinance` module to prove the lazy-import path maps a
    frame to list[dict] without any real network."""

    def setUp(self):
        self._saved = sys.modules.get("yfinance")

        class _Row(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)

        class _DF:
            def __init__(self, rows):
                self._rows = rows

            def __len__(self):
                return len(self._rows)

            def iterrows(self):
                for r in self._rows:
                    # idx is a date-like with .date()
                    idx = types.SimpleNamespace(
                        date=lambda d=r["date"]: d)
                    yield idx, _Row(r)

        class _Ticker:
            def __init__(self, sym):
                self.sym = sym

            def history(self, **kw):
                if self.sym == "EMPTY.L":
                    return _DF([])
                return _DF([
                    {"date": "2026-06-25", "Open": 1.0, "High": 2.0,
                     "Low": 0.5, "Close": 1.5, "Volume": 100.0},
                    {"date": "2026-06-26", "Open": 1.5, "High": 2.5,
                     "Low": 1.0, "Close": 2.0, "Volume": 200.0},
                ])

        fake = types.ModuleType("yfinance")
        fake.Ticker = _Ticker
        sys.modules["yfinance"] = fake

    def tearDown(self):
        if self._saved is not None:
            sys.modules["yfinance"] = self._saved
        else:
            sys.modules.pop("yfinance", None)

    def test_live_path_maps_frame_to_dicts(self):
        prov = YFinanceProvider()  # no _fetch_fn -> uses fake yfinance
        out = prov.fetch_ohlcv("HSBA.L")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["date"], "2026-06-25")
        self.assertEqual(out[1]["volume"], 200.0)

    def test_live_path_empty_frame(self):
        prov = YFinanceProvider()
        self.assertEqual(prov.fetch_ohlcv("EMPTY.L"), [])


class AdapterMissingYFinance(unittest.TestCase):
    def test_missing_yfinance_returns_none(self):
        saved = sys.modules.get("yfinance")
        sys.modules["yfinance"] = None  # force import to fail
        try:
            prov = YFinanceProvider()
            self.assertIsNone(prov.fetch_ohlcv("HSBA.L"))
        finally:
            if saved is not None:
                sys.modules["yfinance"] = saved
            else:
                sys.modules.pop("yfinance", None)


class SelectRecords(unittest.TestCase):
    def setUp(self):
        if not _GLOBAL.is_file():
            self.skipTest("global file missing")
        self.records = json.loads(_GLOBAL.read_text())["symbols"]

    def test_region_filter_uk(self):
        out = R.select_records(self.records, region="UK")
        self.assertEqual(len(out), 100)
        self.assertTrue(all(r["region"] == "UK" for r in out))

    def test_region_filter_hk_with_limit(self):
        out = R.select_records(self.records, region="HK", limit=5)
        self.assertEqual(len(out), 5)
        self.assertTrue(all(r["region"] == "HK" for r in out))

    def test_symbols_filter(self):
        sym = self.records[0]["internal_symbol"]
        out = R.select_records(self.records, symbols=[sym])
        self.assertEqual(len(out), 1)

    def test_select_does_not_mutate(self):
        before = json.dumps(self.records, sort_keys=True)
        R.select_records(self.records, region="UK", limit=3)
        self.assertEqual(json.dumps(self.records, sort_keys=True), before)


class BuildProvider(unittest.TestCase):
    def test_none_is_offline(self):
        self.assertIsNone(R.build_provider("none"))

    def test_yfinance_builds_adapter(self):
        prov = R.build_provider("yfinance")
        self.assertIsInstance(prov, YFinanceProvider)

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            R.build_provider("bogus")


class ProviderBackedEvaluation(unittest.TestCase):
    def _rec(self, internal="LSE:HSBA", yf="HSBA.L", exch="LSE", region="UK"):
        return {"internal_symbol": internal,
                "provider_symbols": {"yfinance": yf}, "exchange": exch,
                "region": region, "avg_volume_20d": None,
                "avg_dollar_volume_20d": None, "median_spread_bps": None,
                "min_liquidity_tier": None}

    def test_provider_failure_maps_to_ohlcv_empty(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: None)  # all unfetchable
        results = R.evaluate_all([self._rec()], provider=prov,
                                 as_of=_AS_OF)
        self.assertFalse(results[0].passed)
        self.assertIn(OHLCV_EMPTY, results[0].reason_codes)

    def test_provider_success_passes(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        results = R.evaluate_all([self._rec()], provider=prov, as_of=_AS_OF,
                                 cfg=OHLCVConfig())
        self.assertTrue(results[0].passed)

    def test_render_simulated_fixture_provenance(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        recs = [self._rec()]
        results = R.evaluate_all(recs, provider=prov, as_of=_AS_OF)
        md = R.render(recs, results, data_source="simulated_fixture",
                      attempted=len(recs))
        # simulated MUST NOT claim live provenance
        self.assertIn("data_source: **simulated_fixture**", md)
        self.assertIn("provider_mode: **fixture / simulated**", md)
        self.assertIn("network: **disabled**", md)
        self.assertIn("not_live_yfinance: **true**", md)
        self.assertIn("attempted: **1**", md)
        self.assertNotIn("network: **enabled**", md)
        self.assertNotIn("data_source: **live_yfinance**", md)

    def test_render_live_yfinance_provenance(self):
        recs = [self._rec()]
        results = R.evaluate_all(recs)
        md = R.render(recs, results, data_source="live_yfinance",
                      attempted=len(recs))
        self.assertIn("data_source: **live_yfinance**", md)
        self.assertIn("provider_mode: **yfinance**", md)
        self.assertIn("network: **enabled**", md)
        self.assertIn("not_live_yfinance: **false**", md)

    def test_default_render_is_structural_only(self):
        recs = [self._rec()]
        results = R.evaluate_all(recs)  # no provider
        md = R.render(recs, results)
        self.assertIn("data_source: **structural_only**", md)
        self.assertIn("provider_mode: **none / structural-only**", md)
        self.assertIn("network: **disabled**", md)

    def test_invalid_data_source_raises(self):
        recs = [self._rec()]
        with self.assertRaises(ValueError):
            R.render(recs, R.evaluate_all(recs), data_source="bogus")

    def test_simulated_report_cannot_claim_live(self):
        # explicit guard: a simulated fixture render never claims live
        # provenance. (Check the specific provenance lines, not the bare
        # substring, since 'not_live_yfinance' legitimately contains it.)
        prov = YFinanceProvider(_fetch_fn=lambda s: None)
        recs = [self._rec()]
        md = R.render(recs, R.evaluate_all(recs, provider=prov, as_of=_AS_OF),
                      data_source="simulated_fixture", attempted=1)
        self.assertNotIn("data_source: **live_yfinance**", md)
        self.assertNotIn("network: **enabled**", md)
        self.assertNotIn("not_live_yfinance: **false**", md)

    def test_provider_report_has_no_unstable_git_metadata(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        recs = [self._rec()]
        md = R.render(recs, R.evaluate_all(recs, provider=prov, as_of=_AS_OF),
                      data_source="simulated_fixture", attempted=1)
        for banned in ("generated_at_git_head", "generated_at_git_status",
                       "dirty", "run_environment"):
            self.assertNotIn(banned, md)


class NoMutation(unittest.TestCase):
    def test_provider_run_does_not_touch_global_file(self):
        if not _GLOBAL.is_file():
            self.skipTest("global file missing")
        before = _GLOBAL.read_bytes()
        records = json.loads(_GLOBAL.read_text())["symbols"]
        sel = R.select_records(records, region="UK", limit=5)
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        R.evaluate_all(sel, provider=prov, as_of=_AS_OF)
        self.assertEqual(_GLOBAL.read_bytes(), before)


class ProviderErrorClassification(unittest.TestCase):
    """Rate-limit / fetch-error must be classified distinctly, never reported
    as ohlcv_empty / volume_missing_or_zero. Deterministic, offline."""

    def _rec(self, internal="HKEX:0001", yf="0001.HK", exch="HKEX",
             region="HK"):
        return {"internal_symbol": internal,
                "provider_symbols": {"yfinance": yf}, "exchange": exch,
                "region": region, "avg_volume_20d": None,
                "avg_dollar_volume_20d": None, "median_spread_bps": None,
                "min_liquidity_tier": None}

    def test_classify_rate_limit_by_type_name(self):
        from tools.universe_quality.yfinance_provider import classify_exception

        class YFRateLimitError(Exception):
            pass
        self.assertEqual(
            classify_exception(YFRateLimitError("Too Many Requests. Rate "
                                                "limited. Try after a while.")),
            "rate_limited")

    def test_classify_rate_limit_by_message_only(self):
        from tools.universe_quality.yfinance_provider import classify_exception
        self.assertEqual(classify_exception(Exception("429 too many requests")),
                         "rate_limited")

    def test_classify_generic_is_fetch_error(self):
        from tools.universe_quality.yfinance_provider import classify_exception
        self.assertEqual(classify_exception(ValueError("connection reset")),
                         "fetch_error")

    def test_injected_fetch_raising_rate_limit_maps_to_code(self):
        class YFRateLimitError(Exception):
            pass

        def boom(sym):
            raise YFRateLimitError("Too Many Requests. Rate limited.")
        prov = YFinanceProvider(_fetch_fn=boom)
        results = R.evaluate_all([self._rec()], provider=prov, as_of=_AS_OF)
        codes = results[0].reason_codes
        self.assertIn("provider_rate_limited", codes)
        self.assertNotIn(OHLCV_EMPTY, codes)
        self.assertNotIn("volume_missing_or_zero", codes)
        self.assertFalse(results[0].passed)

    def test_injected_fetch_raising_generic_maps_to_fetch_error(self):
        def boom(sym):
            raise RuntimeError("dns failure")
        prov = YFinanceProvider(_fetch_fn=boom)
        results = R.evaluate_all([self._rec()], provider=prov, as_of=_AS_OF)
        codes = results[0].reason_codes
        self.assertIn("provider_fetch_error", codes)
        self.assertNotIn(OHLCV_EMPTY, codes)
        self.assertFalse(results[0].passed)

    def test_true_empty_still_ohlcv_empty(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: [])  # empty, no exception
        results = R.evaluate_all([self._rec()], provider=prov, as_of=_AS_OF)
        codes = results[0].reason_codes
        self.assertIn(OHLCV_EMPTY, codes)
        self.assertNotIn("provider_rate_limited", codes)
        self.assertNotIn("provider_fetch_error", codes)

    def test_success_still_passes(self):
        prov = YFinanceProvider(_fetch_fn=lambda s: _bars(25))
        results = R.evaluate_all([self._rec()], provider=prov, as_of=_AS_OF,
                                 cfg=OHLCVConfig())
        self.assertTrue(results[0].passed)
        self.assertNotIn("provider_rate_limited", results[0].reason_codes)

    def test_fake_yfinance_module_rate_limit(self):
        # exercise the live path with a fake yfinance whose history() raises a
        # rate-limit error -> structured result error_kind == rate_limited
        saved = sys.modules.get("yfinance")

        class YFRateLimitError(Exception):
            pass

        class _Ticker:
            def __init__(self, sym):
                pass

            def history(self, **kw):
                raise YFRateLimitError("Too Many Requests. Rate limited.")

        fake = types.ModuleType("yfinance")
        fake.Ticker = _Ticker
        sys.modules["yfinance"] = fake
        try:
            prov = YFinanceProvider()
            fr = prov.fetch_ohlcv_result("0001.HK")
            self.assertEqual(fr.error_kind, "rate_limited")
            self.assertIsNone(fr.bars)
        finally:
            if saved is not None:
                sys.modules["yfinance"] = saved
            else:
                sys.modules.pop("yfinance", None)

    def test_report_provider_availability_section(self):
        class YFRateLimitError(Exception):
            pass

        def boom(sym):
            raise YFRateLimitError("Rate limited")
        prov = YFinanceProvider(_fetch_fn=boom)
        recs = [self._rec()]
        md = R.render(recs, R.evaluate_all(recs, provider=prov, as_of=_AS_OF),
                      data_source="simulated_fixture", attempted=1)
        self.assertIn("Provider availability breakdown", md)
        self.assertIn("provider_rate_limited", md)


if __name__ == "__main__":
    unittest.main()

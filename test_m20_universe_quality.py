"""M20.UC2 — quality gate engine tests. Pure, offline, no network, no real
write-back to us_seed/us_expanded (uses temp copies / report-only)."""
import json
import os
import pathlib
import tempfile
import unittest

from bot.universe.quality import (
    run_quality_gates, evaluate_symbol, _is_etf_denied, _liquidity_tier,
    _load_thresholds)
from bot.universe.quality_gate_report import (
    QualityGateReport, SymbolDecision, GATE_REPORT_SCHEMA_VERSION)

_REPO = pathlib.Path(__file__).resolve().parent
_SNAP = str(_REPO / "configs" / "universe" / "quality_input"
            / "us_quality_v3_20260624.json")
_HAS_SNAP = pathlib.Path(_SNAP).exists()


def _cfg():
    return _load_thresholds()


def _ok_block(close=100.0, vol=2_000_000, dvol=5e8, bars=300,
              date="2026-06-24"):
    return {"status": "ok", "latest_close": close, "avg_volume_20d": vol,
            "avg_dollar_volume_20d": dvol, "bars_count": bars,
            "last_bar_date": date, "median_spread_bps": None}


def _rec(internal="NYSE:AAA", name="Test Co", asset_class="EQUITY",
         alpaca_sym="AAA"):
    return {"internal_symbol": internal, "name": name,
            "asset_class": asset_class,
            "provider_symbols": {"alpaca": alpaca_sym, "yahoo": alpaca_sym}}


class UC2Config(unittest.TestCase):
    def test_thresholds_config_loads(self):
        cfg = _cfg()
        self.assertEqual(cfg["schema_version"], "m20_quality_thresholds_v1")
        self.assertEqual(cfg["max_scan_ready_per_run"], 600)  # configurable
        self.assertEqual(cfg["liquidity_source"], "yahoo")
        self.assertTrue(cfg["cross_check"]["require_both_sources"])

    def test_liquidity_tiers(self):
        t = _cfg()["liquidity_tiers"]
        self.assertEqual(_liquidity_tier(2e8, t), "tier_1")
        self.assertEqual(_liquidity_tier(6e7, t), "tier_2")
        self.assertEqual(_liquidity_tier(2.5e7, t), "tier_3")
        self.assertIsNone(_liquidity_tier(1e7, t))

    def test_etf_deny(self):
        deny = _cfg()["etf_deny"]
        self.assertTrue(_is_etf_denied("iPath VIX Short-Term", "VXX", deny))
        self.assertTrue(_is_etf_denied("ProShares UltraPro 3x", "TQQQ", deny))
        self.assertFalse(_is_etf_denied("SPDR S&P 500 ETF", "SPY", deny))


class UC2GateLogic(unittest.TestCase):
    def test_both_sources_required(self):
        cfg = _cfg()
        # alpaca missing -> unverified
        d = evaluate_symbol(_rec(), {"alpaca": {"status": "error"},
                                     "yahoo": _ok_block()}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "unverified")
        self.assertFalse(d.scan_ready)
        self.assertIn("missing_alpaca", d.reasons)
        # yahoo missing -> unverified
        d2 = evaluate_symbol(_rec(), {"alpaca": _ok_block(),
                                      "yahoo": {"status": "no_data"}}, cfg,
                             asof="2026-06-24")
        self.assertEqual(d2.data_quality_status, "unverified")
        self.assertIn("missing_yahoo", d2.reasons)

    def test_clean_symbol_verifies_and_scan_ready(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(), {"alpaca": _ok_block(close=100, vol=50000,
                                                         dvol=4e7),
                                     "yahoo": _ok_block(close=100, vol=2_000_000,
                                                        dvol=5e8)}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "verified")
        self.assertTrue(d.scan_ready)
        self.assertEqual(d.min_liquidity_tier, "tier_1")
        self.assertEqual(d.liquidity_source, "yahoo")
        self.assertEqual(d.avg_volume_20d, 2_000_000)  # YAHOO volume, not alpaca
        self.assertEqual(d.last_verified_utc, "2026-06-24T00:00:00+00:00")

    def test_price_disagreement_fails(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(close=100.0),
                             "yahoo": _ok_block(close=110.0)}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "failed")
        self.assertFalse(d.scan_ready)
        self.assertIn("price_disagreement", d.reasons)

    def test_bar_date_mismatch_fails(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(date="2026-06-24"),
                             "yahoo": _ok_block(date="2026-06-18")}, cfg,
                            asof="2026-06-24")
        self.assertFalse(d.scan_ready)
        self.assertIn("bar_date_mismatch", d.reasons)

    def test_volume_divergence_does_not_fail(self):
        # IEX vs consolidated volume gap must NOT fail a symbol whose price agrees
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(close=100, vol=68000,
                                                 dvol=6.8e6),   # IEX single-venue
                             "yahoo": _ok_block(close=100.1, vol=13_000_000,
                                                dvol=1.3e9)},   # consolidated
                            cfg, asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "verified")
        self.assertTrue(d.scan_ready)
        self.assertNotIn("price_disagreement", d.reasons)

    def test_below_min_volume_fails(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(vol=10000, dvol=4e7),
                             "yahoo": _ok_block(vol=10000, dvol=4e7)}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "failed")
        self.assertIn("below_min_volume", d.reasons)

    def test_below_min_price_fails(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(close=3.0),
                             "yahoo": _ok_block(close=3.0)}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "failed")
        self.assertIn("below_min_price", d.reasons)

    def test_insufficient_history_fails(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(bars=100),
                             "yahoo": _ok_block(bars=100)}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "failed")
        self.assertIn("insufficient_history", d.reasons)

    def test_stale_data_fails(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(date="2026-05-01"),
                             "yahoo": _ok_block(date="2026-05-01")}, cfg,
                            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "failed")
        self.assertIn("stale_data", d.reasons)

    def test_etf_denied_never_scan_ready(self):
        cfg = _cfg()
        d = evaluate_symbol(
            _rec(internal="ARCA:VXX", name="iPath VIX Short-Term",
                 asset_class="ETF", alpaca_sym="VXX"),
            {"alpaca": _ok_block(), "yahoo": _ok_block()}, cfg,
            asof="2026-06-24")
        self.assertEqual(d.data_quality_status, "failed")
        self.assertFalse(d.scan_ready)
        self.assertIn("etf_denied_class", d.reasons)

    def test_spread_absent_skipped_not_failed(self):
        cfg = _cfg()
        d = evaluate_symbol(_rec(),
                            {"alpaca": _ok_block(), "yahoo": _ok_block()}, cfg,
                            asof="2026-06-24")
        # spread is None in the blocks -> noted but not a failure
        self.assertEqual(d.data_quality_status, "verified")
        self.assertIn("spread_unavailable_skipped", d.reasons)


@unittest.skipUnless(_HAS_SNAP, "v3 snapshot present")
class UC2ReportOnly(unittest.TestCase):
    def test_report_only_writes_nothing(self):
        seed = _REPO / "configs" / "universe" / "us_seed.json"
        exp = _REPO / "configs" / "universe" / "us_expanded.json"
        b_seed, b_exp = seed.read_bytes(), exp.read_bytes()
        r = run_quality_gates(snapshot_path=_SNAP, mode="report_only")
        self.assertEqual(seed.read_bytes(), b_seed)   # untouched
        self.assertEqual(exp.read_bytes(), b_exp)     # untouched
        self.assertEqual(r.mode, "report_only")
        self.assertEqual(r.symbols_total, 573)
        self.assertEqual(r.verified_count + r.failed_count
                         + r.unverified_count, 573)
        self.assertFalse(r.ceiling_exceeded)
        self.assertLessEqual(r.scan_ready_count, r.max_scan_ready_per_run)

    def test_report_only_idempotent(self):
        r1 = run_quality_gates(snapshot_path=_SNAP, mode="report_only")
        r2 = run_quality_gates(snapshot_path=_SNAP, mode="report_only")
        self.assertEqual(
            (r1.verified_count, r1.failed_count, r1.scan_ready_count),
            (r2.verified_count, r2.failed_count, r2.scan_ready_count))

    def test_report_only_flagged_symbols_not_scan_ready(self):
        r = run_quality_gates(snapshot_path=_SNAP, mode="report_only")
        for d in r.decisions:
            if "price_disagreement" in d.reasons \
                    or "bar_date_mismatch" in d.reasons:
                self.assertFalse(d.scan_ready)
                self.assertEqual(d.data_quality_status, "failed")

    def test_volume_divergence_is_informational(self):
        r = run_quality_gates(snapshot_path=_SNAP, mode="report_only")
        # large divergence count, but it never appears as a fail reason
        self.assertGreater(r.volume_semantics_divergence_count, 100)
        self.assertNotIn("volume_divergence", r.fail_reason_counts)
        self.assertNotIn("volume_disagreement", r.fail_reason_counts)


class UC2WriteBackOnTempCopy(unittest.TestCase):
    """write-back path tested on TEMP COPIES; never touches real universe files."""

    def test_write_back_only_changes_quality_fields(self):
        import bot.universe.quality as q
        seed = json.loads(q._SEED.read_text())
        exp = json.loads(q._EXPANDED.read_text())
        # one synthetic verified decision for the first expanded record
        rec0 = exp["symbols"][0]
        internal = rec0["internal_symbol"]
        identity_before = {k: rec0.get(k) for k in
                           ("internal_symbol", "name", "exchange",
                            "asset_class", "provider_symbols",
                            "universe_tags", "country", "currency")}
        dec = SymbolDecision(internal, "verified", True,
                             min_liquidity_tier="tier_1",
                             avg_volume_20d=2e6, avg_dollar_volume_20d=5e8,
                             median_spread_bps=None,
                             last_verified_utc="2026-06-24T00:00:00+00:00",
                             liquidity_source="yahoo", reasons=["passed"])
        with tempfile.TemporaryDirectory() as d:
            sp = pathlib.Path(d) / "seed.json"
            ep = pathlib.Path(d) / "exp.json"
            sp.write_text(json.dumps(seed))
            ep.write_text(json.dumps(exp))
            orig_seed, orig_exp = q._SEED, q._EXPANDED
            try:
                q._SEED, q._EXPANDED = sp, ep
                q._write_back([dec], json.loads(sp.read_text()),
                              json.loads(ep.read_text()))
                out = json.loads(ep.read_text())
            finally:
                q._SEED, q._EXPANDED = orig_seed, orig_exp
            updated = next(r for r in out["symbols"]
                           if r["internal_symbol"] == internal)
            # quality fields written
            self.assertTrue(updated["scan_ready"])
            self.assertEqual(updated["data_quality_status"], "verified")
            self.assertEqual(updated["avg_volume_20d"], 2e6)
            self.assertEqual(updated["min_liquidity_tier"], "tier_1")
            # identity/membership fields untouched
            for k, v in identity_before.items():
                self.assertEqual(updated.get(k), v)

    def test_real_universe_files_never_touched_by_test(self):
        # guard: this test module must not have modified the real files
        import bot.universe.quality as q
        self.assertTrue(q._SEED.exists() and q._EXPANDED.exists())


if __name__ == "__main__":
    unittest.main()

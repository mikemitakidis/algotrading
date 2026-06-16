"""test_group_f_price_adjustment.py — pre-M19 Group F2 (ISSUE-016 + ISSUE-017).

Price-adjustment provenance metadata + point-in-time leakage gate:
  * raw mode (default): manifest records price_adjustment_mode="raw" and the
    raw provenance fields; readiness/promotion not blocked by this gate.
  * adjusted mode: manifest records the synthetic-OHLC provenance fields;
    readiness/promotion BLOCKED with reason adjusted_price_pit_risk unless
    allow_adjusted_prices_for_ml=True.
  * dataset_hash changes when price_adjustment_mode changes.

Pure in-memory assembler builds (no I/O): no data/ml, no signals.db.
"""
import unittest

from test_m18_ml import (
    _multi_tf_for_assembler, ds_assembler, ds_anchors)
from bot.ml.readiness import assess_readiness


def _build(adjusted, allow=False, seed=21, n=2000):
    per_tf = _multi_tf_for_assembler(n_15m=n, seed=seed)
    cfg = ds_assembler.AssemblerConfig(
        symbol="X", anchor_tf="15m",
        anchor_set=ds_anchors.ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
        require_intraday=True, embargo_bars_override=10,
        adversarial_cv_folds=3, adversarial_threshold=1.0,
        adjusted=adjusted, allow_adjusted_prices_for_ml=allow)
    return ds_assembler.DatasetAssembler(cfg).build(per_tf_bars=per_tf)


def _report_with_blocked(manifest):
    """Minimal evaluation-report-like dict carrying the manifest's promotion
    fields, sufficient for assess_readiness's PIT gate."""
    return {
        "promotion_eligible": manifest.promotion_eligible,
        "promotion_blocked_reasons": list(manifest.promotion_blocked_reasons),
    }


class GroupF2PriceAdjustment(unittest.TestCase):

    # 1
    def test_raw_mode_manifest_mode(self):
        m = _build(adjusted=False).manifest
        self.assertEqual(m.price_adjustment_mode, "raw")

    # 2
    def test_adjusted_mode_manifest_mode(self):
        m = _build(adjusted=True).manifest
        self.assertEqual(m.price_adjustment_mode, "adjusted")

    # 3
    def test_adjusted_records_synthetic_true(self):
        m = _build(adjusted=True).manifest
        self.assertTrue(m.adjusted_ohlc_synthetic)

    # 4
    def test_adjusted_records_method_uniform_ratio(self):
        m = _build(adjusted=True).manifest
        self.assertEqual(m.adjusted_ohlc_method, "uniform_ratio")

    # 5
    def test_adjusted_records_ratio_source(self):
        m = _build(adjusted=True).manifest
        self.assertEqual(m.adjustment_ratio_source, "yfinance_adj_close")

    # 6
    def test_adjusted_records_close_real(self):
        m = _build(adjusted=True).manifest
        self.assertTrue(m.adjusted_close_real)

    def test_raw_records_inverse_metadata(self):
        m = _build(adjusted=False).manifest
        self.assertFalse(m.adjusted_ohlc_synthetic)
        self.assertEqual(m.adjusted_ohlc_method, "none")
        self.assertEqual(m.adjustment_ratio_source, "none")
        self.assertFalse(m.adjusted_close_real)

    # 7
    def test_raw_mode_not_blocked_by_pit_gate(self):
        m = _build(adjusted=False).manifest
        self.assertNotIn("adjusted_price_pit_risk",
                         m.promotion_blocked_reasons)
        rd = assess_readiness(_report_with_blocked(m))
        self.assertFalse(
            any("adjusted_price_pit_risk" in r for r in rd["reasons"]))

    # 8
    def test_adjusted_without_flag_blocked(self):
        m = _build(adjusted=True, allow=False).manifest
        self.assertIn("adjusted_price_pit_risk",
                      m.promotion_blocked_reasons)
        self.assertFalse(m.promotion_eligible)
        rd = assess_readiness(_report_with_blocked(m))
        self.assertFalse(rd["ready"])
        self.assertTrue(
            any("adjusted_price_pit_risk" in r for r in rd["reasons"]))

    # 9
    def test_adjusted_with_flag_not_blocked_by_this_gate(self):
        m = _build(adjusted=True, allow=True).manifest
        self.assertNotIn("adjusted_price_pit_risk",
                         m.promotion_blocked_reasons)
        self.assertTrue(m.adjusted_ohlc_synthetic)   # still records provenance
        self.assertTrue(m.allow_adjusted_prices_for_ml)
        rd = assess_readiness(_report_with_blocked(m))
        self.assertFalse(
            any("adjusted_price_pit_risk" in r for r in rd["reasons"]))

    # 10
    def test_dataset_hash_changes_with_mode(self):
        m_raw = _build(adjusted=False).manifest
        m_adj = _build(adjusted=True, allow=True).manifest
        self.assertNotEqual(m_raw.dataset_hash_sha256,
                            m_adj.dataset_hash_sha256)
        self.assertNotEqual(m_raw.dataset_id, m_adj.dataset_id)

    def test_manifest_roundtrips_with_new_fields(self):
        m = _build(adjusted=True, allow=True).manifest
        d = m.to_dict()
        m2 = type(m).from_dict(d)
        self.assertEqual(m2.price_adjustment_mode, "adjusted")
        self.assertTrue(m2.allow_adjusted_prices_for_ml)


class GroupF2RealBuildPathThreading(unittest.TestCase):
    """Prove the REAL CLI build path threads the SAME adjusted value into both
    m16_loader.load_bars(adjusted=...) and AssemblerConfig.adjusted, so the
    manifest can never silently disagree with how bars were actually loaded.

    We spy on the default provider's load_bars and on the assembler build, and
    drive the real bot.ml.cli build-dataset command (no _bars_provider
    injected, so the DEFAULT provider path is exercised). load_bars itself is
    patched to return deterministic fixture bars (so no real M16/network/IO).
    """

    def _run_build(self, extra_argv):
        import io
        import json as _json
        import tempfile as _tf
        from contextlib import redirect_stdout, redirect_stderr
        from unittest import mock
        import bot.ml.cli as cli

        per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
        calls = {}

        def fake_load_bars(symbol, tf, adjusted=True, **kw):
            # record the adjusted value the REAL provider passes in
            calls.setdefault("adjusted_values", []).append(adjusted)
            return per_tf[tf]

        out, err = io.StringIO(), io.StringIO()
        with _tf.TemporaryDirectory() as root:
            with mock.patch("bot.ml.dataset.m16_loader.load_bars",
                            side_effect=fake_load_bars):
                argv = ["--json", "build-dataset", "--symbol", "X",
                        "--anchor-set",
                        # Model B union anchors (same as other F2 builds)
                        __import__("bot.ml.dataset.anchors",
                                   fromlist=["ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES"])
                        .ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
                        "--output", root + "/ds"] + extra_argv
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.main(argv)   # NO _bars_provider -> default path
            manifest = None
            mpath = root + "/ds/manifest.json"
            import os
            if os.path.exists(mpath):
                with open(mpath) as _mf:
                    manifest = _json.load(_mf)
        return rc, calls, out.getvalue(), err.getvalue(), manifest

    def test_raw_config_loads_raw_and_manifest_says_raw(self):
        rc, calls, out, err, manifest = self._run_build([])  # default raw
        self.assertEqual(rc, 0, f"build failed: {err[-400:]}")
        # default provider must have called load_bars with adjusted=False
        self.assertTrue(calls["adjusted_values"])
        self.assertTrue(all(v is False for v in calls["adjusted_values"]),
                        f"loader adjusted values: {calls['adjusted_values']}")
        self.assertEqual(manifest["price_adjustment_mode"], "raw")

    def test_adjusted_config_loads_adjusted_and_manifest_says_adjusted(self):
        # adjusted + allow flag so the build is not blocked from writing.
        rc, calls, out, err, manifest = self._run_build(
            ["--adjusted", "--allow-adjusted-prices-for-ml"])
        self.assertEqual(rc, 0, f"build failed: {err[-400:]}")
        self.assertTrue(all(v is True for v in calls["adjusted_values"]),
                        f"loader adjusted values: {calls['adjusted_values']}")
        self.assertEqual(manifest["price_adjustment_mode"], "adjusted")
        self.assertTrue(manifest["adjusted_ohlc_synthetic"])

    def test_adjusted_without_allow_flag_blocks_promotion(self):
        rc, calls, out, err, manifest = self._run_build(["--adjusted"])
        # build still completes (writes manifest) but is promotion-blocked
        self.assertTrue(all(v is True for v in calls["adjusted_values"]))
        self.assertEqual(manifest["price_adjustment_mode"], "adjusted")
        self.assertIn("adjusted_price_pit_risk",
                      manifest["promotion_blocked_reasons"])
        self.assertFalse(manifest["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()

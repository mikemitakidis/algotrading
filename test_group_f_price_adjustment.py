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


if __name__ == "__main__":
    unittest.main()

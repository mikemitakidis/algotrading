"""test_group_f_advisories.py — pre-M19 Group F3 (ISSUE-018 + ISSUE-019).

Advisory-only provenance (NO behaviour change, NO rebucketing, NO short
execution):
  * 4H bars are UTC-fixed (00/04/08/12/16/20 UTC), NOT US-session aligned.
  * M17 scanner-replica validates the LONG path only; short side NOT validated.

Tests assert the manifest + readiness advisories record these truthfully and
that nothing overclaims session alignment or short-side validation. Pure
in-memory / static checks: no signals.db, no data/ml, no timeframes.py or
backtesting/strategy.py behaviour change.
"""
import pathlib
import unittest

from test_m18_ml import _multi_tf_for_assembler, ds_assembler, ds_anchors
from bot.ml.readiness import assess_readiness

_REPO_ROOT = pathlib.Path(__file__).resolve().parent


def _build_manifest():
    per_tf = _multi_tf_for_assembler(n_15m=2000, seed=21)
    cfg = ds_assembler.AssemblerConfig(
        symbol="X", anchor_tf="15m",
        anchor_set=ds_anchors.ANCHOR_SET_MODEL_B_1H_UNION_CANDIDATES,
        require_intraday=True, embargo_bars_override=10,
        adversarial_cv_folds=3, adversarial_threshold=1.0)
    return ds_assembler.DatasetAssembler(cfg).build(per_tf_bars=per_tf).manifest


def _readiness():
    # Minimal report; advisories are constant fields on the readiness output.
    return assess_readiness({"promotion_blocked_reasons": []})


class GroupF3Advisories(unittest.TestCase):

    # ── ISSUE-018: 4H alignment ─────────────────────────────────────
    def test_manifest_records_fourh_utc_fixed(self):
        m = _build_manifest()
        self.assertEqual(m.fourh_bucket_alignment, "utc_fixed")

    def test_readiness_says_4h_utc_fixed_not_session_aligned(self):
        rd = _readiness()
        self.assertEqual(rd["fourh_bucket_alignment"], "utc_fixed")
        self.assertFalse(rd["fourh_session_aligned"])
        self.assertIn("NOT aligned", rd["fourh_alignment_note"])

    def test_no_code_claims_4h_session_aligned(self):
        """Guard: no readiness output field claims session alignment."""
        rd = _readiness()
        self.assertNotEqual(rd.get("fourh_bucket_alignment"), "session_aligned")
        self.assertIs(rd.get("fourh_session_aligned"), False)

    # ── ISSUE-019: long/short validation ────────────────────────────
    def test_manifest_records_long_validated_true(self):
        m = _build_manifest()
        self.assertTrue(m.scanner_replica_long_side_validated)

    def test_manifest_records_short_validated_false(self):
        m = _build_manifest()
        self.assertFalse(m.scanner_replica_short_side_validated)

    def test_readiness_records_long_true_short_false(self):
        rd = _readiness()
        self.assertTrue(rd["scanner_replica_long_side_validated"])
        self.assertFalse(rd["scanner_replica_short_side_validated"])

    def test_guard_no_overclaim_shorts_equally_validated(self):
        """Guard: readiness must never assert shorts are validated, and the
        note must explicitly say the short side is NOT equally validated."""
        rd = _readiness()
        self.assertFalse(rd["scanner_replica_short_side_validated"])
        # The two flags must not both be True (would imply equal validation).
        self.assertNotEqual(
            (rd["scanner_replica_long_side_validated"],
             rd["scanner_replica_short_side_validated"]),
            (True, True))
        self.assertIn("NOT equally", rd["scanner_replica_short_side_note"])

    # ── No behaviour change guards ──────────────────────────────────
    def test_timeframes_4h_buckets_unchanged(self):
        """ISSUE-018 is advisory only: the 4H bucket constants must be
        unchanged (no rebucketing)."""
        import bot.historical.timeframes as tf
        self.assertEqual(tf.RESAMPLE_4H_BUCKETS_PER_DAY, 6)
        self.assertEqual(tf.RESAMPLE_4H_SOURCE, "1H")

    def test_backtesting_strategy_still_skips_shorts(self):
        """ISSUE-019 is advisory only: no short execution was implemented.
        The scanner-replica strategy source must still document long-only /
        short-skip (no allow_short flip)."""
        src = (_REPO_ROOT / "bot" / "backtesting" / "strategy.py").read_text()
        self.assertIn("long-only", src)
        self.assertIn("skip shorts", src)

    def test_manifest_roundtrips_with_advisory_fields(self):
        m = _build_manifest()
        m2 = type(m).from_dict(m.to_dict())
        self.assertEqual(m2.fourh_bucket_alignment, "utc_fixed")
        self.assertTrue(m2.scanner_replica_long_side_validated)
        self.assertFalse(m2.scanner_replica_short_side_validated)


if __name__ == "__main__":
    unittest.main()

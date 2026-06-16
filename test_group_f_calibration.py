"""test_group_f_calibration.py — proofs for pre-M19 Group F1 (ISSUE-007).

Predict-time isotonic calibration in bot.ml.registry.predictions:
  * applies the stored isotonic artifact (evaluation_report.json ->
    isotonic_calibration.artifact) to raw probabilities WHEN available;
  * always preserves raw probabilities;
  * never claims calibrated output unless the artifact was actually applied;
  * missing / unavailable / corrupt calibration falls back to raw and records
    truthful not-applied metadata (never crashes).

All tests use temporary registry/artifact directories (TemporaryDirectory).
No committed data/ml, no signals.db, no broker/live/network.
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np

# Reuse the heavy, already-validated registry/model fixture builders.
from test_m18_ml import _g8_build_clean_b2, Registry
from bot.ml.registry import storage as _store
from bot.ml.registry.predictions import predict_from_registry, PredictionResult


def _ap(root, model_id, name):
    return _store.artifact_path(Path(root), model_id, name)


# A simple monotone-increasing, NON-identity isotonic artifact: maps the
# [0,1] domain onto [0.2, 0.8]. Applying it must change typical raw probs.
_NONTRIVIAL_ARTIFACT = {
    "x_thresholds": [0.0, 0.5, 1.0],
    "y_thresholds": [0.2, 0.5, 0.8],
}


class GroupF1Calibration(unittest.TestCase):

    def _build_registered(self, root):
        res, out, rep = _g8_build_clean_b2()
        reg = Registry(root=root)
        entry = reg.register_candidate(out, rep, res)
        meta = _store.read_json(
            _ap(root, entry.model_id, _store.ARTIFACT_TRAINING_META))
        base_cols = meta["base_feature_columns"]
        X_in = res.dataset.iloc[:5][base_cols].reset_index(drop=True)
        return reg, entry, X_in

    def _set_eval_report(self, root, model_id, iso_block):
        """Overwrite evaluation_report.json's isotonic_calibration block."""
        ev_path = _ap(root, model_id, _store.ARTIFACT_EVAL_REPORT)
        report = _store.read_json(ev_path) if ev_path.exists() else {}
        report["isotonic_calibration"] = iso_block
        _store.atomic_write_json(ev_path, report)

    # 1. valid artifact changes the probability
    def test_valid_artifact_changes_probability(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": True, "method": "isotonic",
                "artifact": _NONTRIVIAL_ARTIFACT,
            })
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            df = r.predictions
            # calibrated differs from raw for at least one row
            self.assertFalse(
                np.allclose(df["prediction_raw"].to_numpy(),
                            df["prediction_calibrated"].to_numpy()),
                "calibrated probabilities should differ from raw")

    # 2. prediction / pred_proba use calibrated values when applied
    def test_prediction_uses_calibrated_when_applied(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": True, "artifact": _NONTRIVIAL_ARTIFACT})
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            df = r.predictions
            np.testing.assert_allclose(
                df["prediction"].to_numpy(),
                df["prediction_calibrated"].to_numpy())
            np.testing.assert_allclose(
                df["pred_proba"].to_numpy(),
                df["pred_proba_calibrated"].to_numpy())
            self.assertTrue(bool(df["prediction_calibration_applied"].iloc[0]))

    # 3. raw columns preserve raw values
    def test_raw_columns_preserved_when_applied(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": True, "artifact": _NONTRIVIAL_ARTIFACT})
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            df = r.predictions
            np.testing.assert_allclose(
                df["prediction_raw"].to_numpy(),
                df["pred_proba_raw"].to_numpy())
            # raw must NOT equal calibrated here (artifact is non-identity)
            self.assertFalse(np.allclose(
                df["prediction_raw"].to_numpy(),
                df["prediction"].to_numpy()))

    # 4. applied flag True + metadata when applied
    def test_metadata_when_applied(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": True, "artifact": _NONTRIVIAL_ARTIFACT})
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            self.assertTrue(r.predict_time_calibration_applied)
            self.assertEqual(
                r.calibration_source,
                "evaluation_report.isotonic_calibration.artifact")
            self.assertIsNone(r.calibration_unavailable_reason)

    # 5. missing evaluation report -> raw + not applied
    def test_missing_eval_report_falls_back_to_raw(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            ev_path = _ap(root, entry.model_id, _store.ARTIFACT_EVAL_REPORT)
            if ev_path.exists():
                ev_path.unlink()
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            df = r.predictions
            self.assertFalse(r.predict_time_calibration_applied)
            self.assertEqual(r.calibration_source, "none")
            self.assertEqual(r.calibration_unavailable_reason,
                             "evaluation_report_missing")
            np.testing.assert_allclose(
                df["prediction"].to_numpy(),
                df["prediction_raw"].to_numpy())
            # calibrated column equals raw (not null) for stable schema
            np.testing.assert_allclose(
                df["prediction_calibrated"].to_numpy(),
                df["prediction_raw"].to_numpy())

    # 6. available=False -> raw + not applied
    def test_unavailable_calibration_falls_back_to_raw(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": False, "unavailable_reason": "too_few_validation_rows"})
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            self.assertFalse(r.predict_time_calibration_applied)
            self.assertEqual(r.calibration_source, "none")
            self.assertEqual(r.calibration_unavailable_reason,
                             "too_few_validation_rows")
            df = r.predictions
            np.testing.assert_allclose(
                df["prediction"].to_numpy(),
                df["prediction_raw"].to_numpy())

    # 7. corrupt artifact -> raw + not applied (never crashes)
    def test_corrupt_artifact_falls_back_to_raw(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            # available True but artifact malformed (missing y_thresholds)
            self._set_eval_report(root, entry.model_id, {
                "available": True,
                "artifact": {"x_thresholds": [0.0, 1.0]}})
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            self.assertFalse(r.predict_time_calibration_applied)
            self.assertEqual(r.calibration_source, "none")
            self.assertIsNotNone(r.calibration_unavailable_reason)
            df = r.predictions
            np.testing.assert_allclose(
                df["prediction"].to_numpy(),
                df["prediction_raw"].to_numpy())

    # 8. PredictionResult flag matches row-level flag
    def test_result_flag_matches_row_flag(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": True, "artifact": _NONTRIVIAL_ARTIFACT})
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            row_flags = set(
                bool(x) for x in
                r.predictions["prediction_calibration_applied"].tolist())
            self.assertEqual(row_flags, {r.predict_time_calibration_applied})

    # 9. registry/artifact consistency still passes after our eval edits
    def test_registry_consistency_still_passes(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            self._set_eval_report(root, entry.model_id, {
                "available": True, "artifact": _NONTRIVIAL_ARTIFACT})
            c = reg.verify_artifact_consistency(entry.model_id)
            # evaluation_report.json is allowed to carry isotonic_calibration;
            # consistency (dataset hash / identity) must still hold.
            self.assertNotIn("metadata_dataset_hash!=entry_dataset_hash",
                             c.get("problems", []))

    # 10. Q20 required columns remain present
    def test_q20_required_columns_present(self):
        with tempfile.TemporaryDirectory() as root:
            reg, entry, X_in = self._build_registered(root)
            r = predict_from_registry(
                registry=reg, model_id=entry.model_id, X_input=X_in,
                write_output=False)
            cols = set(r.predictions.columns)
            for required in (
                "model_id", "prediction", "predicted_class",
                "feature_extrapolation_flags", "feature_extrapolation_count",
                "pred_proba", "pred_class", "feature_extrapolation_flag",
                "features_out_of_range",
                # F1 additions, always present
                "prediction_raw", "pred_proba_raw", "prediction_calibrated",
                "pred_proba_calibrated", "prediction_calibration_applied",
            ):
                self.assertIn(required, cols)


if __name__ == "__main__":
    unittest.main()

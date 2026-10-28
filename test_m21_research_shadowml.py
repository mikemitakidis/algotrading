"""test_m21_research_shadowml.py — M21 WP16 regime thresholds, WP17 meta-labelling, WP18 regime weights.

Shadow-only research on a strategy template's own entries. Pins the event
definition (template entries, barrier = the template's own exits), the
size-down-only meta policy, thresholds and weights learned only from past
RESOLVED folds, the modelled regime-detection lag, bounded weights with
reported drift, INCONCLUSIVE when evidence is thin, and that nothing here
can change a live decision. Synthetic markets; results describe the machinery.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from research import boundary
from research.genome.schema import seed as G
from research.genome.templates import signals as template_signals
from research.mllab import benchmarks as B
from research.shadowml import core as C

REPO = Path(__file__).resolve().parent
GENOME = G("mean_reversion", rsi_entry=35.0, bb_std=1.5)
N_SYM, N_SES = 12, 700


def build(name, seed=11):
    m = B.market(B.SCENARIOS[name], n_symbols=N_SYM, n_sessions=N_SES, seed=seed)
    ds = C.template_dataset(m.bars, GENOME, truth=m.truth)
    return m, ds


class S1_Events(unittest.TestCase):
    def test_events_are_the_templates_own_entries_with_its_exits(self):
        m, ds = build("null")
        s = sorted(m.bars)[0]
        entries = template_signals(GENOME, m.bars[s])["entry"].to_numpy()
        import pandas as pd
        ts = pd.to_datetime(m.bars[s]["ts_utc"], utc=True).to_numpy(dtype="datetime64[ns]").astype(np.int64)
        mine = set(ds.start[ds.symbol == s])
        self.assertTrue(mine <= set(ts[entries]))
        self.assertGreater(len(mine), 0)
        b = C.template_barrier(GENOME)
        self.assertEqual((b.tp_mult, b.sl_mult, b.horizon), (3.0, 2.0, 20))
        self.assertEqual(ds.meta["template"], "mean_reversion")


class S2_Policy(unittest.TestCase):
    def test_multipliers_never_size_up(self):
        p = np.array([0.1, 0.48, 0.52, 0.9, 0.7, 0.2])
        fold = np.array([0, 0, 0, 1, 1, -1])
        m = C.size_multipliers(p, [0.5, float("inf")], fold, band=0.05)
        self.assertEqual(m[:3].tolist(), [0.0, 0.5, 1.0])
        self.assertEqual(m[3:5].tolist(), [0.0, 0.0])                    # no profitable past threshold
        self.assertTrue(np.isnan(m[5]))
        self.assertLessEqual(np.nanmax(m), 1.0)
        self.assertEqual(C.MULTIPLIERS, (0.0, 0.5, 1.0))

    def test_past_rows_are_earlier_folds_resolved_before_the_fold_starts(self):
        class DS:
            start = np.array([0, 10, 20, 30, 40, 50])
            end = np.array([5, 35, 25, 45, 60, 70])
        fold = np.array([0, 0, 1, 1, 2, 2])
        self.assertEqual(C._past_resolved(DS, fold, 2).tolist(), [0, 1, 2])  # row 3 resolves at 45 > 40: unknowable
        self.assertEqual(C._past_resolved(DS, fold, 1).tolist(), [0])        # row 1 resolves at 35 > 20


class Lab(unittest.TestCase):
    name = "regime"

    @classmethod
    def setUpClass(cls):
        cls.m, cls.ds = build(cls.name)
        # fast linear stand-ins keep the suite practical; production runs use calibrated hgb
        cls.cfg = C.ShadowConfig(meta_model="logistic", baseline="prior")
        cls.res = C.lab(cls.ds, cls.cfg, models=("prior", "logistic", "regime_logistic"),
                        extra={"regime_logistic": C.regime_factory(cls.ds, "logistic")})


class S3_PlantedRegime(Lab):
    def test_meta_label_is_shadow_and_size_down_only(self):
        a = C.meta_label(self.ds, self.cfg, res=self.res)
        self.assertFalse(a["can_change_live"])
        self.assertTrue(a["shadow"])
        self.assertLessEqual(a["max_multiplier"], 1.0)
        self.assertTrue(set(a["multipliers_used"]) <= set(C.MULTIPLIERS))
        cmp = a["comparison_vs_take_all"]
        self.assertIn(cmp["verdict"], (C.IMPROVES, C.NO_EVIDENCE, C.WORSE, C.INCONCLUSIVE))
        self.assertEqual(a["evidence"]["holdout_status"], "NOT_RUN")
        self.assertEqual(a["evidence"]["wf_verdict"], {"IMPROVES": "PASS", "WORSE": "FAIL"}.get(cmp["verdict"],
                                                                                               "INCONCLUSIVE"))

    def test_regime_thresholds_use_lagged_regime_and_fall_back(self):
        b = C.regime_thresholds(self.ds, self.m.bars, self.cfg, res=self.res)
        self.assertEqual(b["lag_sessions"], 1)
        self.assertEqual(len(b["thresholds_by_fold"]), self.cfg.n_folds)
        for row in b["thresholds_by_fold"]:
            self.assertEqual(row["unknown"], row["global"])             # no regime: global threshold
        never = C.regime_thresholds(self.ds, self.m.bars, replace(self.cfg, min_regime_trades=10**9), res=self.res)
        for row in never["thresholds_by_fold"]:
            self.assertEqual({row["calm"], row["volatile"]}, {row["global"]})
        self.assertEqual(never["comparison_vs_global_threshold"].get("new_total"),
                         never["comparison_vs_global_threshold"].get("ref_total"))

    def test_detection_lag_is_a_shift(self):
        r0 = C.lagged_regime(self.ds, self.m.bars, 0)
        r1 = C.lagged_regime(self.ds, self.m.bars, 1)
        self.assertNotEqual(r0.tolist(), r1.tolist())
        self.assertTrue(set(r1) <= {"calm", "volatile", "unknown"})

    def test_regime_weights_bounded_past_only_with_drift(self):
        w = C.regime_weights(self.ds, self.cfg, res=self.res, global_model="logistic",
                             regime_model="regime_logistic")
        self.assertTrue(all(0.0 <= x <= 1.0 for x in w["weights_by_fold"]))
        self.assertEqual(w["weights_by_fold"][0], 0.0)                     # fold 0 has no resolved past
        self.assertGreaterEqual(w["max_fold_to_fold_drift"], 0.0)
        self.assertFalse(w["can_change_live"])
        flat = C.regime_weights(self.ds, replace(self.cfg, min_trades=10**9), res=self.res, global_model="logistic",
                                regime_model="regime_logistic")
        self.assertEqual(flat["weights_by_fold"], [0.0] * self.cfg.n_folds)
        p_global = self.res.runs["logistic"].p
        np.testing.assert_allclose(flat["_p"][self.res.oof], p_global[self.res.oof])

    def test_thin_evidence_is_inconclusive(self):
        a = C.meta_label(self.ds, replace(self.cfg, min_oof_events=10**9), res=self.res)
        self.assertEqual(a["comparison_vs_take_all"]["verdict"], C.INCONCLUSIVE)
        self.assertEqual(a["evidence"]["wf_verdict"], "INCONCLUSIVE")


class S4_Null(Lab):
    name = "null"

    def test_null_market_is_not_an_improvement(self):
        a = C.meta_label(self.ds, self.cfg, res=self.res)
        self.assertNotEqual(a["comparison_vs_take_all"]["verdict"], C.IMPROVES)
        w = C.regime_weights(self.ds, self.cfg, res=self.res, global_model="logistic",
                             regime_model="regime_logistic")
        self.assertNotEqual(w["comparison_log_likelihood_vs_global"]["verdict"], C.IMPROVES)


class S5_Boundary(unittest.TestCase):
    def test_research_boundary_and_no_production_import(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research" / "shadowml")], [])
        for top in ("bot", "tools", "dashboard"):
            for p in (REPO / top).rglob("*.py"):
                self.assertNotIn("research.shadowml", p.read_text(encoding="utf-8", errors="ignore"), str(p))


if __name__ == "__main__":
    unittest.main()

"""test_m21_research_mllab.py — lane C model laboratory (B14–B17, B19, D07).

Offline and deterministic. The tests prove that the laboratory:
- labels outcomes like production M18;
- never lets a model see the future (as-of audit, purged folds, past-only
  thresholds, purged calibration);
- measures uncertainty honestly (conformal and ACI coverage, ensemble
  dispersion);
- reaches the RIGHT verdict on known truth: no improvement under the null,
  trees beating logistic regression on a non-linear signal;
- charges every model to the ledger;
- emits model cards that cannot over-claim.

Run via ``python _test_isolation.py test_m21_research_mllab``.
"""
from __future__ import annotations

import unittest
import warnings

import numpy as np
import pandas as pd

from research.common.synthetic import MarketSpec, generate
from research.integrity.ledger import Ledger
from research.integrity.splits import walk_forward
from research.mllab import audits as AU
from research.mllab import benchmarks as BM
from research.mllab import evaluate as EV
from research.mllab import model_card as MC
from research.mllab import models as MD
from research.mllab import uncertainty as U
from research.mllab.datasets import (CTX_FEATURES, LEVEL_FEATURES, EventDataset, audit_feature_asof, build_dataset,
                                     every_k)
from research.mllab.labels import BarrierSpec, barrier_outcomes

DAY = int(86400e9)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)


def _market(n_symbols=4, n_sessions=300, seed=11, effects=()):
    return generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=seed, effects=effects))


def _event_ds(*, n_days=240, per_day=12, kind="linear", strength=2.0, seed=0, hold_days=2) -> EventDataset:
    """Direct event dataset with KNOWN truth. Column 4 is the row id (lets
    spies see which rows a model was given). r = +1 on a win, -1 on a loss."""
    rng = np.random.default_rng(seed)
    n = n_days * per_day
    X = rng.normal(size=(n, 5))
    X[:, 4] = np.arange(n)
    if kind == "linear":
        z = strength * X[:, 0]
    elif kind == "xor":
        z = strength * np.sign(X[:, 0] * X[:, 1])
    elif kind == "null":
        z = np.zeros(n)
    else:
        raise ValueError(kind)
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-z))).astype(float)
    day = np.repeat(np.arange(n_days), per_day)
    start = day * DAY + np.tile(np.arange(per_day), n_days) * 1000
    end = start + hold_days * DAY
    return EventDataset(X, ["x0", "x1", "x2", "x3", "row_id"], y, np.where(y == 1, 1.0, -1.0), start, end,
                        np.full(n, "S", dtype=object), np.array(["d%04d" % d for d in day], dtype=object),
                        np.full(n, "", dtype=object), {"kind": kind})


class _Spy:
    """Prior-like model that records which rows it was fitted on and asked about."""
    log = []

    def fit(self, X, y, *, start, end):
        self.p_ = float(np.clip(np.mean(y), 1e-6, 1 - 1e-6))
        _Spy.log.append(("fit", X[:, 4].astype(int).copy(), np.asarray(start), np.asarray(end)))
        return self

    def predict_proba(self, X):
        _Spy.log.append(("predict", X[:, 4].astype(int).copy()))
        return np.full(len(X), self.p_)


class _Noise:
    """Challenger with uninformative probabilities (negative control)."""

    def __init__(self, seed=0):
        self.rng = np.random.default_rng(seed)

    def fit(self, X, y, **kw):
        return self

    def predict_proba(self, X):
        return self.rng.uniform(0.05, 0.95, size=len(X))


# ─────────────────────────────────────────────────────────────────────────────

class C1_Labels(unittest.TestCase):
    def test_parity_with_m18_triple_barrier(self):
        m = _market(n_symbols=3, n_sessions=600)
        for s, b in m.bars.items():
            par = AU.parity_with_m18(b)
            self.assertGreater(par["compared"], 300, s)
            self.assertEqual(par["outcome_mismatch"], 0, s)
            self.assertEqual(par["resolution_bar_mismatch"], 0, s)
            self.assertEqual(par["gap_rows_same_resolution_bar"], par["gap_rows"], s)
            self.assertEqual(par["m18_resolved_research_unlabelled"], 0, s)

    def test_m18_same_bar_ties_are_flagged_not_booked(self):
        m = _market(n_symbols=1, n_sessions=400)
        b = next(iter(m.bars.values())).copy()
        for j in (150, 250):     # two bars whose range spans every barrier of the anchors before them
            o, c = b.loc[j, "open"], b.loc[j, "close"]
            b.loc[j, "high"] = max(o, c) * 1.8
            b.loc[j, "low"] = min(o, c) * 0.3
        par = AU.parity_with_m18(b)
        self.assertGreater(par["ambiguous"], 0)
        self.assertEqual(par["ambiguous_booked_as_stop_by_m18"], par["ambiguous"])
        self.assertEqual(par["outcome_mismatch"], 0)

    def _path(self, bars, i=0, spec=BarrierSpec(1.0, 1.0, 3)):
        o, h, l, c = (np.array([x[k] for x in bars], float) for k in range(4))
        out = barrier_outcomes(o, h, l, c, np.ones(len(o)), spec)
        return {k: v[i] for k, v in out.items()}

    def test_hand_built_paths(self):
        flat = (10.0, 10.2, 9.8, 10.0)
        # target first: entry 10, up 11, down 9
        r = self._path([flat, flat, (10, 11.2, 9.8, 11), flat, flat, flat])
        self.assertEqual((r["y"], r["r"], r["resolve_idx"], r["gap"]), (1.0, 1.0, 2, False))
        # stop on the entry bar itself (intrabar, not a gap)
        r = self._path([flat, (10, 10.2, 8.9, 9.5), flat, flat, flat])
        self.assertEqual((r["y"], r["r"], r["resolve_idx"], r["gap"]), (0.0, -1.0, 1, False))
        # gap through the stop at a later open resolves AT THE OPEN
        r = self._path([flat, flat, (8.5, 8.7, 8.2, 8.4), flat, flat])
        self.assertEqual((r["y"], r["gap"], r["resolve_idx"]), (0.0, True, 2))
        self.assertAlmostEqual(r["r"], -1.5)
        # gap through the target at a later open
        r = self._path([flat, flat, (11.6, 11.8, 11.5, 11.7), flat, flat])
        self.assertEqual((r["y"], r["gap"]), (1.0, True))
        self.assertAlmostEqual(r["r"], 1.6)
        # same bar holds both barriers: ambiguous, never booked
        r = self._path([flat, flat, (10, 11.2, 8.8, 10), flat, flat])
        self.assertTrue(r["ambiguous"])
        self.assertTrue(np.isnan(r["y"]))
        # timeout: R at the close of bar i + horizon
        r = self._path([flat, flat, flat, (10, 10.5, 9.5, 10.4), flat])
        self.assertEqual((r["y"], r["resolve_idx"]), (0.0, 3))
        self.assertAlmostEqual(r["r"], 0.4)
        # not enough bars and no hit: pending
        r = self._path([flat, flat, flat])
        self.assertTrue(r["pending"])
        self.assertTrue(np.isnan(r["y"]))
        # an invalid entry price is never labelled
        r = self._path([flat, (np.nan, 10.2, 9.8, 10), flat, flat, flat])
        self.assertTrue(np.isnan(r["y"]))
        self.assertFalse(r["pending"])

    def test_bad_spec_rejected(self):
        for kw in ({"tp_mult": 0}, {"sl_mult": -1}, {"horizon": 0}):
            with self.assertRaises(ValueError):
                BarrierSpec(**kw)


class C2_Datasets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _market(n_symbols=4, n_sessions=300)

    def test_asof_audit_is_clean_and_catches_a_leak(self):
        clean = audit_feature_asof(self.m.bars, probes=2)
        self.assertGreater(clean["cells_checked"], 50_000)
        self.assertEqual(clean["violations"], 0)
        leaky = audit_feature_asof(self.m.bars, probes=2, feature_fn=AU.leaky_symbol_features)
        self.assertGreater(leaky["violations"], 0)

    def test_dataset_invariants(self):
        ds = build_dataset(self.m.bars, sampler=every_k(3), truth=self.m.truth)
        self.assertTrue((ds.end > ds.start).all(), "a label must resolve strictly after its decision")
        self.assertTrue((np.diff(ds.start) >= 0).all())
        self.assertTrue(np.isfinite(ds.X).all())
        self.assertTrue(set(np.unique(ds.y)) <= {0.0, 1.0})
        for f in LEVEL_FEATURES:
            self.assertNotIn(f, ds.feature_names)
        self.assertFalse(any("regime" in f and not f.startswith(("ctx.", "vol_regime.")) for f in ds.feature_names))
        for f in CTX_FEATURES:
            self.assertIn(f, ds.feature_names)
        ind = [i for i, f in enumerate(ds.feature_names) if f.endswith("was_missing")]
        if ind:
            self.assertTrue(np.isin(ds.X[:, ind], (0.0, 1.0)).all())
        no_ctx = build_dataset(self.m.bars, sampler=every_k(3), include_context=False)
        self.assertFalse(any(f.startswith("ctx.") for f in no_ctx.feature_names))
        self.assertEqual(ds.digest, build_dataset(self.m.bars, sampler=every_k(3), truth=self.m.truth).digest)

    def test_digest_covers_every_field(self):
        """Second review P1 (#7): every field that drives training, purging, grouping or analysis is hashed."""
        import copy
        base = _event_ds(n_days=20, per_day=4)
        d0 = base.digest
        edits = {
            "X": lambda d: d.X.__setitem__((0, 0), d.X[0, 0] + 1),
            "y": lambda d: d.y.__setitem__(0, 1 - d.y[0]),
            "r": lambda d: d.r.__setitem__(0, d.r[0] + 0.5),
            "start": lambda d: d.start.__setitem__(0, d.start[0] + 1),
            "end": lambda d: d.end.__setitem__(0, d.end[0] + 1),
            "symbol": lambda d: d.symbol.__setitem__(0, "T"),
            "day": lambda d: d.day.__setitem__(0, "d9999"),
            "truth_regime": lambda d: d.truth_regime.__setitem__(0, "bull"),
            "feature_names": lambda d: d.feature_names.__setitem__(0, "renamed"),
            "meta": lambda d: d.meta.__setitem__("kind", "other"),
        }
        for name, f in edits.items():
            d = copy.deepcopy(base)
            f(d)
            self.assertNotEqual(d.digest, d0, "%s is not covered by the dataset digest" % name)


class C3_Harness(unittest.TestCase):
    def test_every_fit_sees_only_resolved_past(self):
        ds = _event_ds(n_days=120, per_day=6, hold_days=3)
        _Spy.log = []
        res = EV.run(ds, {"logistic": lambda: _Spy()}, EV.LabConfig(n_folds=4, embargo_ns=2 * DAY, min_trades=5))
        fits = [e for e in _Spy.log if e[0] == "fit"]
        preds = [e for e in _Spy.log if e[0] == "predict"]
        self.assertEqual(len(fits), 4)
        for (_, rows, st, en), (_, prow) in zip(fits, preds):
            self.assertLess(en.max(), ds.start[prow].min(), "a training label resolved inside the test window")
            self.assertEqual(len(np.intersect1d(rows, prow)), 0)
        self.assertTrue(all(f["purged"] > 0 for f in res.fold_sizes))
        self.assertTrue(AU.harness_leakage(ds, n_folds=4, embargo_ns=2 * DAY)["clean"])

    def test_thresholds_depend_only_on_past_folds(self):
        ds = _event_ds(n_days=200, per_day=10, strength=1.0)
        cfg = EV.LabConfig(n_folds=5, min_trades=10)
        fac = {"logistic": lambda: MD.make("logistic")}
        base = EV.run(ds, fac, cfg)
        folds = walk_forward(ds.start, ds.end, n_folds=5, min_train_groups=cfg.min_train_groups)
        for k in (4, 2):
            ds2 = ds.subset(np.arange(len(ds)))
            ds2.r = ds.r.copy()
            ds2.r[folds[k].test] *= -3.0          # rewrite the outcomes of one future fold
            alt = EV.run(ds2, fac, cfg)
            self.assertEqual(base.runs["logistic"].thresholds[:k + 1], alt.runs["logistic"].thresholds[:k + 1])
            np.testing.assert_array_equal(base.runs["logistic"].p, alt.runs["logistic"].p)

    def test_conformal_sets_are_calibrated_on_past_folds_only(self):
        ds = _event_ds(n_days=200, per_day=10, strength=1.0)
        ds.X[:, 4] = 0.0
        cfg = EV.LabConfig(n_folds=5, min_trades=10)
        fac = {"logistic": lambda: MD.make("logistic")}
        base = EV.run(ds, fac, cfg)
        last = base.fold_of == 4
        ds2 = ds.subset(np.arange(len(ds)))
        ds2.y = ds.y.copy()
        ds2.y[last] = 1.0 - ds2.y[last]               # flip the outcomes of the LAST fold only
        alt = EV.run(ds2, fac, cfg)
        np.testing.assert_array_equal(base.runs["logistic"].p, alt.runs["logistic"].p)
        np.testing.assert_array_equal(base.runs["logistic"].take_conformal, alt.runs["logistic"].take_conformal)
        self.assertTrue(base.runs["logistic"].take_conformal[base.fold_of >= 2].any())

    def test_paired_tests_use_daily_aggregates_and_holm(self):
        from research.integrity import multiple_testing as MTT
        ds = _event_ds(n_days=150, per_day=10, kind="null", seed=2)
        ds.X[:, 4] = 0.0
        res = EV.run(ds, {"logistic": lambda: MD.make("logistic"), "a": lambda: _Noise(1), "b": lambda: _Noise(2),
                          "c": lambda: _Noise(3)}, EV.LabConfig(n_folds=4, min_trades=20))
        oof = res.oof
        y, d = ds.y[oof], ds.day[oof]
        for r in res.comparisons:
            gain = EV._ll(y, res.runs["logistic"].p[oof]) - EV._ll(y, res.runs[r["model"]].p[oof])
            daily = pd.Series(gain).groupby(d).sum()
            self.assertAlmostEqual(r["logloss_gain_per_day"], float(daily.mean()), places=10)
        raw = [r["p_logloss"] for r in res.comparisons]
        np.testing.assert_allclose([r["holm_p_logloss"] for r in res.comparisons], MTT.adjust(raw, "holm"))
        # A comparison with an intermediate p-value, so that Holm visibly changes it (raw p = 1.0 cannot).
        ws = _event_ds(n_days=150, per_day=10, strength=0.35, seed=4)
        ws.X[:, 4] = 0.0
        r2 = EV.run(ws, {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic"),
                         "a": lambda: _Noise(1), "b": lambda: _Noise(2)},
                    EV.LabConfig(n_folds=4, min_trades=20, baseline="prior"))
        raw2 = [r["p_logloss"] for r in r2.comparisons]
        self.assertTrue(any(1e-12 < p < 0.3 for p in raw2), raw2)
        np.testing.assert_allclose([r["holm_p_logloss"] for r in r2.comparisons], MTT.adjust(raw2, "holm"))
        self.assertTrue(any(h > p for h, p in zip([r["holm_p_logloss"] for r in r2.comparisons], raw2)))
        raw_w = [r["p_logloss_worse"] for r in res.comparisons]
        np.testing.assert_allclose([r["holm_p_worse"] for r in res.comparisons], MTT.adjust(raw_w, "holm"))

    def test_conformal_needs_enough_past(self):
        ds = _event_ds(n_days=60, per_day=10)
        res = EV.run(ds, {"logistic": lambda: MD.make("logistic")}, EV.LabConfig(n_folds=5, min_trades=5))
        f0 = res.fold_of == 0
        self.assertFalse(res.runs["logistic"].take_conformal[f0].any())


class C4_Uncertainty(unittest.TestCase):
    def test_split_conformal_class_conditional_coverage(self):
        rng = np.random.default_rng(3)
        alpha, cw, cl = 0.2, [], []
        for _ in range(300):
            p = rng.uniform(0.05, 0.95, 800)
            y = (rng.uniform(size=800) < p).astype(int)
            cov = U.SplitConformal(alpha).fit(p[:400], y[:400]).coverage(p[400:], y[400:])
            cw.append(cov["coverage_win"])
            cl.append(cov["coverage_loss"])
        for c in (np.mean(cw), np.mean(cl)):
            self.assertGreaterEqual(c, 1 - alpha - 0.01)
            self.assertLessEqual(c, 1 - alpha + 0.03)

    def test_conformal_edge_cases_and_decisions(self):
        self.assertEqual(U._q(np.arange(1.0, 11.0), 0.2), 9.0)        # ceil((n + 1)(1 - alpha))-th smallest
        self.assertEqual(U._q(np.array([]), 0.2), 1.0)
        self.assertEqual(U._q(np.array([0.1, 0.2]), 0.2), 1.0)        # too few scores: full set, abstain
        cf = U.SplitConformal(0.2).fit(np.r_[np.full(50, 0.8), np.full(50, 0.2)], np.r_[np.ones(50), np.zeros(50)])
        d = cf.decision(np.array([0.85, 0.5, 0.15]))
        self.assertEqual(list(d), [1, 0, 0])
        self.assertEqual(cf.sets(np.array([0.5])).sum(), 0)              # neither class conforms at 0.5 here

    def test_aci_tracks_target_under_drift_static_does_not(self):
        rng = np.random.default_rng(0)
        p = np.r_[rng.uniform(0.5, 1, 1000), rng.uniform(0.2, 1, 3000)]
        t = np.arange(4000) * 10
        res = U.online_coverage(p, np.ones(4000), np.r_[np.zeros(1000), np.ones(3000)], t, t + 35, alpha=0.2)
        self.assertLess(res["static_coverage"], 0.6)
        self.assertLess(abs(res["aci_coverage"] - 0.8), 0.03)
        self.assertEqual(res["warmup_labels_pending"], 3)          # the last 3 warm-up labels resolve too late

    def test_aci_uses_only_labels_resolved_before_each_decision(self):
        """Second review P1 (#7): delayed, overlapping labels. The quantile used at decision t must not depend on
        any label resolving at or after t; changing those labels leaves every earlier quantile unchanged."""
        rng = np.random.default_rng(1)
        n = 1500
        p = rng.uniform(0.2, 1, n)
        y = (rng.uniform(size=n) < p).astype(int)
        block = np.r_[np.zeros(300), np.ones(n - 300)].astype(int)
        dts = np.arange(n) * 100
        rts = dts + rng.integers(1, 2000, n)                  # labels resolve 0-20 decisions later, overlapping
        base = U.online_coverage(p, y, block, dts, rts, alpha=0.2, return_q=True)["q_used"]
        for k in (400, 900, 1300):
            y2 = y.copy()
            y2[k:] = 1 - y2[k:]                               # rewrite every outcome from decision k on
            q2 = U.online_coverage(p, y2, block, dts, rts, alpha=0.2, return_q=True)["q_used"]
            unseen = (np.arange(n) >= 300) & (dts <= rts[k:].min())
            np.testing.assert_array_equal(base[unseen], q2[unseen])
            self.assertFalse(np.array_equal(base[300:], q2[300:]))
        # leaking control: label-instant feedback DOES change quantiles before the rewritten labels resolve
        aci = U.AdaptiveConformal(alpha=0.2)
        self.assertTrue(hasattr(aci, "resolve"))
        with self.assertRaises(ValueError):
            U.online_coverage(p, y, block, dts, dts - 1, alpha=0.2)
        with self.assertRaises(ValueError):                   # no warm-up label known before evaluation starts
            U.online_coverage(p, y, block, dts, dts + 10 ** 9, alpha=0.2)

    def test_risk_coverage_orders_by_confidence(self):
        rc = U.risk_coverage(np.array([0.9, 0.1, 0.8, 0.2]), np.array([1.0, -1.0, 1.0, -1.0]), grid=(0.5, 1.0))
        self.assertEqual(rc["curve"][0], (0.5, 1.0, 2))
        self.assertEqual(rc["curve"][1], (1.0, 0.0, 4))

    def test_dispersion_report_detects_informative_disagreement(self):
        rng = np.random.default_rng(5)
        n = 3000
        pt = rng.uniform(0.1, 0.9, n)
        y = (rng.uniform(size=n) < pt).astype(float)
        noise = rng.uniform(0.0, 0.35, n)
        M = np.clip(pt[:, None] + rng.normal(size=(n, 5)) * noise[:, None], 0.01, 0.99)
        M[:10] = np.nan
        rep = U.dispersion_report(M, M.mean(axis=1), y, np.where(y == 1, 1.0, -1.0), M.mean(axis=1) > 0.5)
        self.assertEqual(rep["n"], n - 10)
        self.assertGreater(rep["spearman_dispersion_vs_logloss"], 0.05)
        self.assertLess(rep["spearman_p"], 0.01)
        self.assertLess(rep["by_tercile"][0]["log_loss"], rep["by_tercile"][2]["log_loss"])


class C5_Models(unittest.TestCase):
    def test_calibrator_fits_on_purged_later_rows(self):
        ds = _event_ds(n_days=100, per_day=10)
        _Spy.log = []
        cal = MD.Calibrated(_Spy(), "isotonic", frac=0.25).fit(ds.X, ds.y, start=ds.start, end=ds.end)
        fit_rows = [e for e in _Spy.log if e[0] == "fit"][0][1]
        hold_rows = [e for e in _Spy.log if e[0] == "predict"][0][1]
        self.assertEqual(len(np.intersect1d(fit_rows, hold_rows)), 0)
        self.assertLess(ds.end[fit_rows].max(), ds.start[hold_rows].min())
        self.assertGreaterEqual(hold_rows.min(), int(0.74 * len(ds)))

    def test_calibrated_outputs_are_clipped(self):
        ds = _event_ds(n_days=150, per_day=10, strength=6.0)
        p = MD.Calibrated(MD.make("hgb"), "isotonic").fit(ds.X[:, :4], ds.y, start=ds.start,
                                                           end=ds.end).predict_proba(ds.X[:, :4])
        self.assertGreaterEqual(p.min(), MD.CAL_CLIP)
        self.assertLessEqual(p.max(), 1 - MD.CAL_CLIP)

    def test_single_class_training_degenerates_to_prior(self):
        m = MD.make("hgb").fit(np.zeros((40, 2)), np.ones(40))
        self.assertTrue(np.allclose(m.predict_proba(np.zeros((3, 2))), 1 - 1e-6))

    def test_noctx_variant_ignores_context_columns(self):
        ds = _event_ds(n_days=80, per_day=10)
        fac = MD.factories(["logistic_noctx"], seed=0, context_cols=[1, 2], n_features=5)["logistic_noctx"]
        m = fac().fit(ds.X, ds.y, start=ds.start, end=ds.end)
        X2 = ds.X.copy()
        X2[:, [1, 2]] = 1e6
        np.testing.assert_allclose(m.predict_proba(ds.X), m.predict_proba(X2))

    def test_models_are_deterministic_in_seed(self):
        ds = _event_ds(n_days=80, per_day=10, kind="xor")
        for name in [n for n in ("hgb", "xgb", "lgbm", "catboost", "rf", "extra_trees", "mlp") if MD.available(n)]:
            a = MD.make(name, seed=4).fit(ds.X, ds.y).predict_proba(ds.X)
            b = MD.make(name, seed=4).fit(ds.X, ds.y).predict_proba(ds.X)
            np.testing.assert_array_equal(a, b, err_msg=name)

    def test_models_write_no_files(self):
        import os
        import tempfile
        ds = _event_ds(n_days=40, per_day=10)
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                for name in [n for n in ("catboost", "lgbm", "xgb", "hgb") if MD.available(n)]:
                    MD.make(name).fit(ds.X, ds.y)
                self.assertEqual(os.listdir(d), [])
            finally:
                os.chdir(cwd)

    def test_regime_model_routes_rows_by_regime(self):
        rng = np.random.default_rng(1)
        X = rng.uniform(size=(2000, 2))
        hi = X[:, 1] > 0.7
        y = np.where(hi, rng.uniform(size=2000) < 0.9, rng.uniform(size=2000) < 0.1).astype(float)
        t = np.arange(2000)
        m = MD.RegimeModel(lambda: MD.Prior(), col=1, threshold=0.7, min_rows=100).fit(X, y, start=t, end=t)
        p = m.predict_proba(X)
        self.assertGreater(p[hi].min(), 0.8)
        self.assertLess(p[~hi].max(), 0.2)

    def test_ensembles_expose_members(self):
        ds = _event_ds(n_days=120, per_day=10)
        for name in ("mean_ens", "bag_hgb"):
            m = MD.factories([name], seed=0)[name]().fit(ds.X[:, :4], ds.y, start=ds.start, end=ds.end)
            P = m.member_proba(ds.X[:5, :4])
            self.assertEqual(P.shape[0], 5)
            self.assertGreaterEqual(P.shape[1], 3)
        with self.assertRaises(AttributeError):
            MD.Calibrated(MD.make("logistic")).member_proba(ds.X)


class C6_Verdicts(unittest.TestCase):
    def test_verdict_table(self):
        a = 0.05
        self.assertEqual(EV.verdict(0.01, 0.01, 1.0, a), "IMPROVES")
        self.assertEqual(EV.verdict(0.01, 0.5, 1.0, a), "PROBABILITIES_ONLY")
        self.assertEqual(EV.verdict(0.5, 0.01, 1.0, a), "DECISIONS_ONLY")
        self.assertEqual(EV.verdict(0.5, 0.5, 0.01, a), "WORSE")
        self.assertEqual(EV.verdict(0.5, 0.5, 0.5, a), "NO_EVIDENCE")
        self.assertEqual(EV.verdict(0.05, 0.05, 1.0, a), "IMPROVES")      # boundary is inclusive
        self.assertEqual(EV.verdict(0.051, 0.051, 0.051, a), "NO_EVIDENCE")

    def test_skill_gate_rule(self):
        row = {"verdict": "IMPROVES", "holm_p_logloss": 0.01, "holm_p_decision": 0.01, "holm_p_worse": 1.0}
        pr_none = {"holm_p_logloss": 0.4, "holm_p_decision": 0.4}
        pr_prob = {"holm_p_logloss": 0.01, "holm_p_decision": 0.4}
        self.assertEqual(EV.skill_gate(dict(row), pr_none, 0.05)["verdict"], "NO_EVIDENCE")
        self.assertEqual(EV.skill_gate(dict(row), pr_prob, 0.05)["verdict"], "PROBABILITIES_ONLY")
        g = EV.skill_gate(dict(row), {"holm_p_logloss": 0.01, "holm_p_decision": 0.01}, 0.05)
        self.assertEqual((g["verdict"], g["skill_gate"]), ("IMPROVES", ""))
        u = EV.skill_gate(dict(row), None, 0.05)
        self.assertEqual(u["verdict"], "IMPROVES")
        self.assertIn("ungated", u["skill_gate"])

    def test_beating_a_worse_than_prior_baseline_is_not_a_claim(self):
        class Half:
            def fit(self, X, y, **kw):
                return self

            def predict_proba(self, X):
                return np.full(len(X), 0.5)

        ds = _event_ds(n_days=200, per_day=12, kind="null", seed=9)
        ds.X[:, 4] = 0.0
        res = EV.run(ds, {"prior": lambda: MD.make("prior"), "noise": lambda: _Noise(3), "half": lambda: Half()},
                     EV.LabConfig(n_folds=5, min_trades=20, baseline="noise"))
        half = [r for r in res.comparisons if r["model"] == "half"][0]
        self.assertIn(half["verdict_raw"], ("IMPROVES", "PROBABILITIES_ONLY"))
        self.assertIn(half["verdict"], ("NO_EVIDENCE", "WORSE"))
        self.assertTrue(half["skill_gate"].startswith("downgraded"))

    def test_informative_model_improves_on_prior_and_noise_does_not_beat_it(self):
        ds = _event_ds(n_days=240, per_day=12, strength=2.0)
        fac = {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic"),
               "noise": lambda: _Noise(1)}
        res = EV.run(ds, fac, EV.LabConfig(n_folds=5, min_trades=20))
        vp = {r["model"]: r for r in res.vs_prior}
        self.assertEqual(vp["logistic"]["verdict"], "IMPROVES")
        noise = [r for r in res.comparisons if r["model"] == "noise"][0]
        self.assertEqual(noise["verdict"], "WORSE")
        self.assertGreater(res.runs["logistic"].metrics["auc"], 0.75)

    def test_nonlinear_signal_trees_beat_logistic(self):
        ds = _event_ds(n_days=240, per_day=12, kind="xor", strength=2.0)
        fac = {"logistic": lambda: MD.make("logistic"), "hgb": lambda: MD.make("hgb")}
        X = ds.X.copy()
        X[:, 4] = 0.0                                   # remove the row id so it cannot be used
        ds.X = X
        res = EV.run(ds, fac, EV.LabConfig(n_folds=5, min_trades=20))
        hgb = res.comparisons[0]
        self.assertIn(hgb["verdict"], ("IMPROVES", "PROBABILITIES_ONLY"))
        self.assertLess(hgb["holm_p_logloss"], 0.01)

    def test_null_events_never_improve(self):
        claims = 0
        for seed in range(4):
            ds = _event_ds(n_days=150, per_day=10, kind="null", seed=seed)
            ds.X[:, 4] = 0.0
            res = EV.run(ds, {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic"),
                              "hgb": lambda: MD.make("hgb")}, EV.LabConfig(n_folds=4, min_trades=20))
            claims += sum(r["verdict"] in ("IMPROVES", "PROBABILITIES_ONLY") for r in res.comparisons + res.vs_prior)
        self.assertEqual(claims, 0)


class C7_Provenance(unittest.TestCase):
    def test_card_without_ledger_is_a_local_diagnostic(self):
        ds = _event_ds(n_days=150, per_day=10, strength=1.5)
        res = EV.run(ds, {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic"),
                          "hgb": lambda: MD.make("hgb")}, EV.LabConfig(n_folds=4, min_trades=20))
        self.assertIsNotNone(res.card)
        self.assertNotEqual(res.card.verdict, "PASS")
        self.assertTrue(any("local diagnostic only" in r for r in res.card.reasons))
        self.assertEqual(res.trial_ids, {})

    def test_ledgered_runs_charge_every_family_trial(self):
        ds = _event_ds(n_days=150, per_day=10, strength=1.5)
        L = Ledger()
        fac = {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic"),
               "hgb": lambda: MD.make("hgb")}
        r1 = EV.run(ds, fac, EV.LabConfig(n_folds=4, min_trades=20), scenario="s", ledger=L)
        r2 = EV.run(ds, fac, EV.LabConfig(n_folds=4, min_trades=20), scenario="s", ledger=L)
        self.assertEqual(r1.meta["n_trials_charged"], 3)
        self.assertEqual(r2.meta["n_trials_charged"], 6)
        L.assert_ledgered(r2.trial_ids.values())
        self.assertEqual(r2.card.numbers["provenance_verified"], 1.0)
        self.assertEqual(r2.card.numbers["outcomes_verified"], 1.0)
        self.assertEqual(r2.card.numbers["n_trials"], 6.0)
        self.assertFalse(any("not reconciled" in r for r in r2.card.reasons))

    def test_trials_carry_real_code_identity_and_outcome_artifacts(self):
        """Second review P1 (#7): code_digest is a content hash of the lab sources, not a literal; trial artifacts
        bind the reported outcomes, so a substituted outcome stream is refused by the report card."""
        from research.integrity import report_card as RC
        from research.integrity.provenance import code_identity
        ds = _event_ds(n_days=150, per_day=10, strength=1.5)
        L = Ledger()
        fac = {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic")}
        res = EV.run(ds, fac, EV.LabConfig(n_folds=4, min_trades=20), scenario="s", ledger=L)
        rows = L.entries("trial")
        self.assertTrue(rows)
        want = code_identity(EV.CODE_PATHS)
        for e in rows:
            self.assertEqual(e.payload["code_digest"], want)
            self.assertEqual(set(e.payload["artifacts"]), {"gross", "family_series"})
        self.assertTrue(want.startswith("src-"))
        tid = res.trial_ids["logistic"]
        rec = [e.payload for e in rows if e.ref_id == tid][0]
        self.assertEqual(rec["data_digest"], ds.digest)
        # substituting the outcomes but keeping the genuine trial ids must not verify
        ids = list(res.trial_ids.values())
        rng = np.random.default_rng(0)
        card = RC.evaluate(candidate_id="s:logistic", family_id="mllab:s", gross=rng.normal(0.3, 1, 200),
                           cost_per_obs=0.0, n_trials=len(ids), rng=rng,
                           family_matrix=rng.normal(size=(60, len(ids))), ledger=L, trial_ids=ids,
                           candidate_trial_id=tid)
        self.assertEqual(card.numbers["outcomes_verified"], 0.0)
        self.assertNotEqual(card.verdict, "PASS")


class C8_ModelCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = _event_ds(n_days=240, per_day=12, strength=2.0)
        ds.X[:, 4] = 0.0
        cls.res = EV.run(ds, {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic"),
                              "noise": lambda: _Noise(2)}, EV.LabConfig(n_folds=5, min_trades=20),
                         scenario="card", ledger=Ledger())

    def test_card_contents(self):
        c = MC.build_card(self.res, "logistic", versions={"numpy": "x"})
        self.assertEqual(c.role, "baseline")
        self.assertEqual(c.recommendation, MC.RECOMMENDATIONS[0])
        self.assertTrue(c.trial_id.startswith("T"))
        self.assertEqual(c.dataset_digest, self.res.dataset_digest)
        text = MC.render(c)
        for s in ("Intended use", "Not approved for live or paper trading", c.dataset_digest[:12], c.trial_id,
                  "vs `prior`"):
            self.assertIn(s, text)
        self.assertNotIn("deploy", text.lower())

    def test_challenger_without_improvement_is_flagged(self):
        c = MC.build_card(self.res, "noise", versions={})
        self.assertEqual(c.role, "challenger")
        self.assertEqual(c.recommendation, "keep the baseline")
        self.assertTrue(any("do not prefer it" in x for x in c.limitations))

    def test_challenger_that_improves_is_only_a_forward_test_candidate(self):
        ds = _event_ds(n_days=240, per_day=12, strength=2.0)
        ds.X[:, 4] = 0.0
        res = EV.run(ds, {"prior": lambda: MD.make("prior"), "logistic": lambda: MD.make("logistic")},
                     EV.LabConfig(n_folds=5, min_trades=20, baseline="prior"))
        c = MC.build_card(res, "logistic", baseline="prior", versions={})
        self.assertEqual(c.recommendation, "candidate for pre-registered forward testing")
        self.assertNotIn("deploy", MC.render(c).lower())

    def test_unknown_model_refused(self):
        with self.assertRaises(KeyError):
            MC.build_card(self.res, "nope")


class C9_Benchmarks(unittest.TestCase):
    def test_null_market_scenario_finds_nothing(self):
        res = BM.run_scenario("null", n_symbols=12, n_sessions=400, models=("prior", "logistic", "hgb"), seed=3)
        rows = res.comparisons + res.vs_prior
        self.assertTrue(rows)
        self.assertFalse(any(r["verdict"] == "IMPROVES" for r in rows))
        self.assertFalse(any(r["verdict"] == "PROBABILITIES_ONLY" for r in res.comparisons))
        self.assertTrue(res.card is None or res.card.verdict != "PASS")

    def test_scaled_scenarios(self):
        z = BM.scaled("interaction", 0.0)
        self.assertEqual(z.name, "interaction_x0")
        self.assertTrue(all(e.strength == 0.0 for e in z.effects))
        h = BM.scaled("interaction", 0.5, "half")
        self.assertEqual([e.strength for e in h.effects], [-0.25, 0.5])
        self.assertEqual(BM.SCENARIOS["interaction"].effects[1].strength, 1.0)


class C10_EvidenceRun(unittest.TestCase):
    def test_tiny_evidence_run_is_complete_and_honest(self):
        import json
        import tempfile
        from pathlib import Path
        from research.mllab import evidence as EVD
        cfg = EVD.EvidenceConfig(bench_symbols=8, bench_sessions=300, bench_models=("prior", "logistic", "hgb"),
                                 parity_symbols=1, parity_sessions=300, asof_probes=1, battery_symbols=8,
                                 battery_sessions=300, battery_models=("prior", "logistic", "hgb"), n_nulls=2,
                                 effect_sizes=(1.0,), required_effects=(), n_per_effect=1, scenarios=("null",),
                                 profile="test")
        j = EVD.to_json(EVD.run_evidence(cfg))
        self.assertEqual(j["parity"]["outcome_mismatch"], 0)
        self.assertEqual(j["benchmarks"]["_asof"]["clean"]["violations"], 0)
        self.assertGreater(j["benchmarks"]["_asof"]["leaky_control"]["violations"], 0)
        self.assertTrue(j["benchmarks"]["null"]["leakage"]["clean"])
        self.assertEqual(j["battery"]["result"]["verdict"], "NOT_CALIBRATED")     # 2 nulls cannot bound anything
        self.assertTrue(j["ledger"]["verified"])
        self.assertNotEqual((j["calibrated_run"]["report_card"] or {}).get("verdict"), "PASS")
        self.assertTrue(j["code_identity"].startswith("src-"))
        online = j["benchmarks"]["null"]["uncertainty"]["online"]
        self.assertTrue(online and all(o["feedback"].startswith("causal") for o in online.values()))
        with tempfile.TemporaryDirectory() as d:
            jp, mp = EVD.write(j, Path(d) / "lane_c")
            self.assertEqual(json.loads(jp.read_text())["report_digest"], j["report_digest"])
            md = mp.read_text()
        for s in ("not** evidence of any edge", "## 3. Canary battery", "Model card", "What this does and does not show"):
            self.assertIn(s, md)
        self.assertNotIn("deploy", md.replace("never deployment", "").lower())


if __name__ == "__main__":
    unittest.main()

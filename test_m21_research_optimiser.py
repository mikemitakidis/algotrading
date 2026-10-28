"""test_m21_research_optimiser.py — M21 WP13 walk-forward optimiser driver (B11, B12, B08).

Pins fold geometry (train strictly before test, embargo, no leakage via the
independent ``leakage_report``), the one-shot sealed history, window
independence (future bars cannot change a fold), WP12 economics on every OOS
trade (unfillable trades are removed, never free), the fail-closed verdict
order, full ledgering (every grid point of every fold is a trial; DSR charged
for all of them), the holdout path on a declared planted-effect market, and
the canary battery being reported honestly when small. Synthetic only.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from research import boundary
from research.common.synthetic import MarketSpec, generate
from research.economics.model import EconomicsModel
from research.genome import engine as E
from research.genome.schema import seed as seed_genome
from research.integrity.ledger import Ledger, LedgerError
from research.integrity.splits import leakage_report
from research.optimiser import canary as C
from research.optimiser import walkforward as W

REPO = Path(__file__).resolve().parent
MR = ("mean_reversion",)


def small(seed=3, n_symbols=6, n_sessions=500):
    return generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, timeframe="1D", seed=seed)).bars


class O1_Folds(unittest.TestCase):
    def test_config_refusals(self):
        for kw in ({"n_folds": 1}, {"min_train_groups": 0}, {"embargo_bars": -1}, {"holdout_frac": 0.0},
                   {"holdout_frac": 0.6}, {"templates": ("martingale",)}):
            with self.assertRaises(ValueError, msg=kw):
                W.WFConfig(**kw)

    def test_train_precedes_test_with_embargo_and_blocks_cover(self):
        cfg = W.WFConfig(n_folds=4, min_train_groups=2, embargo_bars=5)
        fs = W.folds(600, cfg)
        self.assertEqual(len(fs), 4)
        for (tr, te) in fs:
            self.assertEqual(tr.lo, 0)                                  # expanding window
            self.assertGreaterEqual(te.lo - tr.hi - 1, 5)               # embargo gap
        self.assertEqual(fs[-1][1].hi, 599)
        for (a, b) in zip(fs, fs[1:]):
            self.assertEqual(b[0].hi, a[1].hi)                          # next train ends where the last test ended

    def test_no_training_trade_overlaps_its_test_window(self):
        bars = small()
        tl = W.timeline_of(bars)
        cfg = W.WFConfig(templates=MR)
        g = seed_genome("mean_reversion")
        for tr, te in W.folds(len(tl), cfg):
            sub, mask = W._slice(bars, tl, tr)
            res = E.run_genome(g, sub, cfg.engine, mask)
            pos = {t: i for i, t in enumerate(tl)}
            ev = sorted((pos[pd.Timestamp(t.decision_ts)], pos[pd.Timestamp(t.exit_bar_close_ts)])
                        for t in res.trades)
            self.assertTrue(ev)
            ev_all = sorted(ev + [(te.lo, te.hi)])                      # the test window as one event
            k_test = ev_all.index((te.lo, te.hi))
            starts, ends = [a for a, _ in ev_all], [b for _, b in ev_all]
            train_idx = np.array([i for i in range(len(ev_all)) if i != k_test])
            rep = leakage_report(train_idx, np.array([k_test]), starts, ends, embargo=cfg.embargo_bars)
            self.assertTrue(rep.clean, rep)
            self.assertTrue(all(e <= tr.hi and s >= tr.lo for s, e in ev))


class O2_SealedHistory(unittest.TestCase):
    def test_research_view_excludes_the_sealed_part_and_release_is_one_shot(self):
        bars = small()
        L = Ledger()
        tl = W.timeline_of(bars)
        seal = tl[400]
        sh = W.SealedHistory(L, "h", bars, seal)
        self.assertTrue(all((pd.to_datetime(b["ts_utc"], utc=True) < seal).all() for b in sh.research_bars.values()))
        full = sh.release("wf:x", "g1")
        self.assertEqual(sum(map(len, full.values())), sum(map(len, bars.values())))
        with self.assertRaises(LedgerError):
            sh.release("wf:x", "g2")
        sh.release("wf:y", "g1")                                        # another family has its own single use
        with self.assertRaises(LedgerError):
            W.SealedHistory(L, "h", bars, seal)
        self.assertEqual(len(L.entries("historical_holdout_release", "h")), 2)
        self.assertTrue(L.verify().ok)


class O3_WindowIndependence(unittest.TestCase):
    def test_future_bars_cannot_change_a_fold(self):
        bars = small()
        tl = W.timeline_of(bars)
        cfg = W.WFConfig(templates=MR)
        tr, te = W.folds(len(tl), cfg)[0]
        g = seed_genome("mean_reversion")
        a = W._oos(g, bars, tl, te, cfg, None, None)
        later = {s: b.copy() for s, b in bars.items()}
        for b in later.values():
            fut = pd.to_datetime(b["ts_utc"], utc=True) > tl[te.hi]
            b.loc[fut, ["open", "high", "low", "close"]] *= 3.0
        b_ = W._oos(g, later, tl, te, cfg, None, None)
        np.testing.assert_array_equal(a["returns"], b_["returns"])
        self.assertEqual(a["net_usd"], b_["net_usd"])


class O4_Economics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = small()
        cls.tl = W.timeline_of(cls.bars)
        cls.cfg = W.WFConfig(templates=MR)
        cls.te = W.folds(len(cls.tl), cls.cfg)[-1][1]
        cls.g = seed_genome("mean_reversion", rsi_entry=40.0, bb_std=1.0)

    def test_costs_lower_the_oos_result_and_every_trade_is_priced(self):
        free = replace(self.cfg, economics=EconomicsModel(cost_multiplier=0.0))
        a = W._oos(self.g, self.bars, self.tl, self.te, self.cfg, None, None)
        z = W._oos(self.g, self.bars, self.tl, self.te, free, None, None)
        self.assertGreater(a["n_trades"], 0)
        self.assertEqual(len(a["priced"]), len(a["res"].trades))
        self.assertLess(a["net_usd"], z["net_usd"])
        self.assertLess(W._sharpe_ann(a["returns"]), W._sharpe_ann(z["returns"]))

    def test_unfillable_trade_is_removed_not_free(self):
        a = W._oos(self.g, self.bars, self.tl, self.te, self.cfg, None, None)
        t0 = a["res"].trades[0]
        dead = {s: b.copy() for s, b in self.bars.items()}
        b = dead[t0.symbol]
        i = int(pd.DatetimeIndex(pd.to_datetime(b["ts_utc"], utc=True)).get_loc(pd.Timestamp(t0.decision_ts))) + 1
        b.loc[i, "volume"] = 0.0                                        # the entry bar traded nothing
        d = W._oos(self.g, dead, self.tl, self.te, self.cfg, None, None)
        self.assertEqual(d["n_unfilled"], a["n_unfilled"] + 1)
        self.assertEqual(d["n_trades"], a["n_trades"] - 1)
        te0 = [te for t, te in a["priced"] if t is t0][0]
        self.assertAlmostEqual(d["net_usd"], a["net_usd"] - te0.net_pnl, places=6)

    def test_size_sensitivity_grows_less_than_linearly(self):
        a = W._oos(self.g, self.bars, self.tl, self.te, self.cfg, None, None, size_mult=1.0)
        big = W._oos(self.g, self.bars, self.tl, self.te, self.cfg, None, None, size_mult=200.0)
        bps = lambda o: np.mean([te.cost_bps for _, te in o["priced"] if te.priced])
        self.assertGreater(bps(big), bps(a))                             # impact and caps bite at size
        self.assertGreater(big["capped"] + big["n_unfilled"], 0)


def summary(**kw):
    s = {"oos_trades": 50, "oos_sharpe_ann": 1.0, "oos_mean_net_r": 0.2, "stress_oos_sharpe_ann": 0.5,
         "stress_multiplier": 2.0, "pbo": {"pbo": 0.2}, "dsr": {"dsr": 0.99}, "n_trials_family": 36,
         "default_params_oos_sharpe_ann": 0.5}
    s.update(kw)
    return s


class O5_Verdicts(unittest.TestCase):
    def test_verdict_order_is_fail_closed(self):
        cfg = W.WFConfig()
        cases = [({}, W.PASS), ({"pbo": {"pbo": 0.8}, "oos_trades": 3}, W.FAIL),
                 ({"oos_trades": 10}, W.INCONCLUSIVE), ({"oos_sharpe_ann": -0.1}, W.FAIL),
                 ({"oos_mean_net_r": -0.01}, W.FAIL), ({"stress_oos_sharpe_ann": -0.2}, W.FAIL),
                 ({"dsr": {"dsr": 0.6}}, W.INCONCLUSIVE), ({"dsr": None}, W.INCONCLUSIVE),
                 ({"pbo": None}, W.INCONCLUSIVE), ({"oos_mean_net_r": float("nan")}, W.FAIL)]
        for over, want in cases:
            self.assertEqual(W._verdict(summary(**over), cfg)[0], want, over)
        v, r = W._verdict(summary(default_params_oos_sharpe_ann=2.0), cfg)
        self.assertEqual((v, r), (W.PASS, ["optimisation_adds_nothing_over_default_params"]))


class O6_EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        cls.out = W.run(small(), W.WFConfig(templates=MR), ledger=cls.L, data_seed=3)
        cls.r = cls.out["templates"]["mean_reversion"]

    def test_structure_ledger_and_labels(self):
        r, s = self.r, self.r["summary"]
        self.assertEqual(len(r["folds"]), 4)
        self.assertEqual(s["n_trials_family"], 9 * 4)                   # every grid point of every fold
        self.assertEqual(s["dsr"]["n_trials"], 36.0)
        self.assertEqual(len(self.L.trials("wf:mean_reversion")), 36)
        self.assertTrue(self.out["ledger_ok"])
        self.assertEqual(self.out["origin"], "synthetic")
        self.assertIn("SYNTHETIC", self.out["banner"])
        self.assertIn(r["verdict"], (W.PASS, W.FAIL, W.INCONCLUSIVE))
        if r["verdict"] != W.PASS:
            self.assertEqual(r["holdout"]["status"], W.HOLDOUT_NOT_RUN)
        for f in r["folds"]:
            self.assertGreaterEqual(f["chosen_plateau"], f["isolated_peak"]["plateau"])
        self.assertEqual(len(self.L.entries("wf_run", "wf:mean_reversion")), 1)

    def test_deterministic(self):
        again = W.run(small(), W.WFConfig(templates=MR), ledger=Ledger(), data_seed=3)
        self.assertEqual(again["templates"]["mean_reversion"]["summary"], self.r["summary"])
        self.assertEqual(again["templates"]["mean_reversion"]["folds"], self.r["folds"])

    def test_origin_must_be_declared(self):
        with self.assertRaises(ValueError):
            W.run_template("mean_reversion", small(n_sessions=200), W.WFConfig(templates=MR), ledger=Ledger(),
                           data_seed=1, origin="paper")


class O7_HoldoutOnPlantedEffect(unittest.TestCase):
    """Declared canary design (30 symbols x 750 sessions, rsi_oversold 0.5 sigma x 5 bars):
    the walk-forward PASSES, so the one-shot holdout runs exactly once."""

    @classmethod
    def setUpClass(cls):
        cls.L = Ledger()
        _, cls.bars = C.market(5004, n_symbols=30, n_sessions=750, template="mean_reversion", strength=0.5)
        cls.out = W.run(cls.bars, W.WFConfig(templates=MR), ledger=cls.L, data_seed=5004)
        cls.r = cls.out["templates"]["mean_reversion"]

    def test_pass_runs_the_holdout_once(self):
        self.assertEqual(self.r["verdict"], W.PASS, self.r["reasons"])
        h = self.r["holdout"]
        self.assertIn(h["status"], (W.PASS, W.FAIL, W.INCONCLUSIVE))
        self.assertIn("not provably out-of-sample", h["caveat"])
        self.assertEqual(len(self.L.entries("historical_holdout_release", "wf-holdout")), 1)
        self.assertEqual(self.r["summary"]["n_trials_family"], 36)       # the walk-forward verdict's charge
        self.assertEqual(h["n_trials_family_at_holdout"], 45)            # the final re-search is charged too
        self.assertEqual(self.L.family_trial_count("wf:mean_reversion"), 45)


class O8_Canary(unittest.TestCase):
    def test_small_battery_is_labelled_not_calibrated(self):
        L = Ledger()
        res = C.run_canary("mean_reversion", W.WFConfig(), ledger=L, name="unit", n_nulls=1, n_symbols=5,
                           n_sessions=400)
        self.assertEqual(res.verdict, "NOT_CALIBRATED")
        self.assertIn("need >= 59", res.notes)
        self.assertEqual(res.null_passes, 0)
        self.assertEqual(len(L.entries("control_battery_result")), 1)
        with self.assertRaises(LedgerError):
            C.run_canary("mean_reversion", W.WFConfig(), ledger=L, name="unit", n_nulls=1, n_symbols=5,
                         n_sessions=400)
        with self.assertRaises(ValueError):
            C.run_canary("trend_confluence", W.WFConfig(), ledger=L, name="u2", n_nulls=1, effect_sizes=(0.5,),
                         n_per_effect=1)


class O9_Boundary(unittest.TestCase):
    def test_research_boundary_and_no_production_import(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research" / "optimiser")], [])
        for top in ("bot", "tools", "dashboard"):
            for p in (REPO / top).rglob("*.py"):
                self.assertNotIn("research.optimiser", p.read_text(encoding="utf-8", errors="ignore"), str(p))


if __name__ == "__main__":
    unittest.main()

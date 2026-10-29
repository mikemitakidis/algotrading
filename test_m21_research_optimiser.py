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

import inspect
import unittest
from dataclasses import asdict, fields, replace
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from research import boundary
from research.common.synthetic import MarketSpec, generate
from research.economics.model import EconomicsModel
from research.genome import engine as E
from research.genome.schema import seed as seed_genome
from research.common.hashing import digest
from research.genome import search as SR
from research.integrity import report_card as RC
from research.integrity.controls import BatteryPlan, PipelineDecision, pipeline_identity, run_battery
from research.integrity.ledger import Ledger, LedgerError, TrialRecord
from research.integrity.provenance import code_identity
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
        ea = W.net_equity(a["res"], a["priced"], self.tl, self.te)
        ed = W.net_equity(d["res"], d["priced"], self.tl, self.te)
        self.assertAlmostEqual(ea.iloc[-1] - ed.iloc[-1], te0.net_pnl, places=4)   # its gross P&L is gone too

    def test_unknown_exit_is_inconclusive_and_kept_apart_from_no_fill(self):
        a = W._oos(self.g, self.bars, self.tl, self.te, self.cfg, None, None)
        t0 = next(t for t in a["res"].trades if t.exit_reason not in ("eod", "delisted"))
        dead = {s: b.copy() for s, b in self.bars.items()}
        b = dead[t0.symbol]
        x = int(pd.DatetimeIndex(pd.to_datetime(b["ts_utc"], utc=True)).get_loc(pd.Timestamp(t0.exit_bar_close_ts)))
        b.loc[x, "volume"] = 0.0                                        # the exit bar traded nothing
        d = W._oos(self.g, dead, self.tl, self.te, self.cfg, None, None)
        self.assertEqual((d["n_inconclusive"], d["n_no_fill"]), (a["n_inconclusive"] + 1, a["n_no_fill"]))
        self.assertEqual(d["n_trades"], a["n_trades"] - 1)
        self.assertEqual(d["n_unfilled"], d["n_no_fill"] + d["n_inconclusive"])

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
    def test_unknown_exits_block_a_pass_and_breaches_are_disclosed(self):
        v, why = W._verdict(summary(oos_inconclusive=2), W.WFConfig())
        self.assertEqual(v, W.INCONCLUSIVE)
        self.assertIn("oos_trades_with_unknown_exit_price_2", why)
        v, why = W._verdict(summary(oos_exit_capacity_breach=3), W.WFConfig())
        self.assertEqual(v, W.PASS)
        self.assertIn("exit_capacity_breach_trades_3", why)
        v, _ = W._verdict(summary(oos_inconclusive=2, oos_sharpe_ann=-1.0), W.WFConfig())
        self.assertEqual(v, W.FAIL)                                      # FAIL still takes precedence

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
            self.assertEqual(f["chosen_genome_id"], f["plateau_pick_genome_id"])      # the plateau, not the peak
        self.assertTrue(any(f["chosen_genome_id"] != f["isolated_peak"]["genome_id"] for f in r["folds"]))
        self.assertEqual(len(self.L.entries("wf_run", "wf:mean_reversion")), 1)

    def test_deterministic(self):
        again = W.run(small(), W.WFConfig(templates=MR), ledger=Ledger(), data_seed=3)
        self.assertEqual(again["templates"]["mean_reversion"]["summary"], self.r["summary"])
        self.assertEqual(again["templates"]["mean_reversion"]["folds"], self.r["folds"])

    def test_real_origin_requires_close_stamped_bars(self):
        bars = small(n_sessions=200)
        for b in (bars, {k: v.assign(bar_open_ts=pd.to_datetime(v["ts_utc"], utc=True)) for k, v in bars.items()}):
            for call in (lambda: W.run_template("mean_reversion", b, W.WFConfig(templates=MR), ledger=Ledger(),
                                                data_seed=1, origin="real"),
                         lambda: W.run(b, W.WFConfig(templates=MR), ledger=Ledger(), data_seed=1, origin="real")):
                with self.assertRaises(ValueError) as cm:
                    call()
                self.assertIn("bar_open_ts", str(cm.exception))
        ok = {k: v.assign(bar_open_ts=pd.to_datetime(v["ts_utc"], utc=True) - pd.Timedelta(hours=6, minutes=30))
              for k, v in bars.items()}
        W.require_close_stamped(ok)

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


class O8b_CanaryCLI(unittest.TestCase):
    def test_cli_writes_its_report(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc = C.main(["--template", "mean_reversion", "--nulls", "1", "--positives", "0", "--symbols", "4",
                         "--sessions", "300", "--name", "cli-test", "--out", tmp])
            self.assertEqual(rc, 0)
            rep = json.loads((Path(tmp) / "canary_mean_reversion.json").read_text())
            self.assertEqual((rep["origin"], rep["battery"]["n_nulls"], rep["ledger_ok"]), ("synthetic", 1, True))
            self.assertEqual(rep["plan"]["experiment_digest"], rep["experiment_digest"])
            self.assertEqual(rep["experiment_digest"], C.experiment_digest(
                "mean_reversion", W.WFConfig(), C.CanaryProtocol("cli-test", 1, (), 0, n_symbols=4, n_sessions=300)))
            self.assertEqual(rep["plan"]["plan_digest"], rep["battery"]["plan_digest"])
            self.assertTrue(Ledger.verify_jsonl(Path(tmp) / "canary_mean_reversion.ledger.jsonl").ok)
            self.assertEqual(set(rep["environment"]), {"python", "numpy", "pandas", "scipy"})


class O10_ExperimentIdentity(unittest.TestCase):
    """PR #24 review: calibration evidence is bound to the exact experiment and verified at consumption."""

    T = "mean_reversion"
    P = C.CanaryProtocol("id-test", 59, (0.5,), 20, n_symbols=30, n_sessions=750)

    def test_identity_is_deterministic_and_covers_every_input(self):
        from research.economics.model import EconomicsModel
        from research.genome import engine as E
        cfg = W.WFConfig()
        base = C.experiment_digest(self.T, cfg, self.P)
        self.assertEqual(base, C.experiment_digest(self.T, W.WFConfig(), C.CanaryProtocol(**vars(self.P))))
        cfgs = [replace(cfg, n_folds=5), replace(cfg, embargo_bars=6), replace(cfg, holdout_frac=0.25),
                replace(cfg, min_oos_trades=31), replace(cfg, dsr_min=0.9), replace(cfg, pbo_max=0.4),
                replace(cfg, pbo_blocks=10), replace(cfg, stress=3.0), replace(cfg, size_multipliers=(1.0, 10.0)),
                replace(cfg, economics=replace(EconomicsModel(), impact_coef=0.8)),
                replace(cfg, engine=replace(E.EngineConfig(), **{F.name: _bump(getattr(E.EngineConfig(), F.name))
                                                                 for F in fields(E.EngineConfig)[:1]})),
                replace(cfg, axes={self.T: {k: list(v)[:1] for k, v in cfg.axes_for(self.T).items()}})]
        protos = [replace(self.P, **{k: v}) for k, v in (
            ("name", "other"), ("n_nulls", 60), ("effect_sizes", (0.4,)), ("n_per_effect", 21), ("n_symbols", 31),
            ("n_sessions", 751), ("horizon", 6), ("base_seed", 5001), ("false_pass_target", 0.04),
            ("alpha", 0.01), ("min_power", 0.9))]
        ds = [C.experiment_digest(self.T, c, self.P) for c in cfgs] + \
             [C.experiment_digest(self.T, cfg, p) for p in protos] + \
             [C.experiment_digest("breakout_momentum", cfg, self.P)]
        real = C.code_identity
        with mock.patch.object(C, "code_identity",                      # the decision code alone changes
                               side_effect=lambda paths: "changed" if tuple(paths) == C.CODE_PATHS else real(paths)):
            ds.append(C.experiment_digest(self.T, cfg, self.P))
        with mock.patch.object(C, "MarketSpec", lambda **kw: MarketSpec(**dict(kw, tail_df=4.0))):
            ds.append(C.experiment_digest(self.T, cfg, self.P))           # a generator parameter changes
        real_pe = C.PlantedEffect
        with mock.patch.object(C, "PlantedEffect", lambda *a, **kw: real_pe(*a, **dict(kw, k=2.5))):
            ds.append(C.experiment_digest(self.T, cfg, self.P))           # a planted-effect default changes
        self.assertEqual(len(set(ds + [base])), len(ds) + 1)

    def test_plan_binds_the_identity_and_plans_without_one_hash_as_before(self):
        cfg = W.WFConfig()
        plan = C.canary_plan(self.T, cfg, self.P)
        exp = C.experiment_identity(self.T, cfg, self.P)
        self.assertEqual((plan.experiment, plan.experiment_digest), (exp, digest(exp)))
        self.assertEqual(plan.pipeline_id, pipeline_identity("wf:%s" % self.T, digest(exp["pipeline"])))
        self.assertNotEqual(plan.digest, C.canary_plan(self.T, replace(cfg, stress=3.0), self.P).digest)
        rec = plan.record()
        self.assertEqual(digest({k: v for k, v in rec.items() if k not in ("plan_digest", "experiment_digest")}),
                         plan.digest)
        legacy = BatteryPlan("b", "pipeline:f@c", n_nulls=60, effect_sizes=(1.0,), n_per_effect=20)
        old_fields = {f.name: getattr(legacy, f.name) for f in fields(legacy) if f.name != "experiment"}
        self.assertEqual(legacy.digest, digest(old_fields))
        self.assertNotIn("experiment", legacy.record())

    def test_the_protocol_horizon_reaches_the_planted_effect(self):
        with mock.patch.object(C, "run_battery") as rb:
            C.run_protocol(self.T, W.WFConfig(), replace(self.P, horizon=7), ledger=Ledger())
        make_positive = rb.call_args[0][3]
        with mock.patch.object(C, "market") as mk:
            make_positive(0.5, 1)
        self.assertEqual(mk.call_args.kwargs["horizon"], 7)

    def test_historical_v2_design_is_preserved_and_v3_differs_only_in_its_seeds(self):
        h, v3 = C.PROTOCOL_V2_HISTORICAL, C.PROTOCOL_V3
        self.assertEqual((h.name, h.n_nulls, h.effect_sizes, h.n_per_effect, h.n_symbols, h.n_sessions, h.horizon,
                          h.base_seed), ("wp13-canary-v2", 59, (0.5,), 20, 30, 750, 5, 5000))
        self.assertEqual(replace(v3, name=h.name), h)
        self.assertNotEqual(v3.name, h.name)


def _bump(v):
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v + 1
    return str(v) + "x"


class O11_ReportCardBinding(unittest.TestCase):
    """The report card accepts a WP13 battery only for the exact experiment the evaluation requires."""

    T = "mean_reversion"
    P = C.CanaryProtocol("rc-test", 59, (0.5,), 20, n_symbols=30, n_sessions=750)

    def battery(self, led, plan):
        return run_battery(plan, lambda d: PipelineDecision(d > 0), lambda s: 0.0, lambda e, s: e, ledger=led)

    def setUp(self):
        self.cfg = W.WFConfig()
        self.led = Ledger()
        self.trial = self.led.record_trial(TrialRecord(
            family_id="wf:%s" % self.T, kind="research", spec_digest="s", data_digest="d",
            code_digest=code_identity(SR.CODE_PATHS), seed=1, metrics={}))
        self.plan = C.canary_plan(self.T, self.cfg, self.P)
        self.res = self.battery(self.led, self.plan)
        self.want = C.experiment_digest(self.T, self.cfg, self.P)

    def status(self, res=None, family=None, trial=None, expected="want", led=None):
        return RC._canary_status(res or self.res, led or self.led, family or "wf:%s" % self.T,
                                 trial or self.trial, RC.GauntletConfig(),
                                 self.want if expected == "want" else expected)

    def test_matching_identity_is_verified(self):
        self.assertEqual(self.res.verdict, "CALIBRATED")
        self.assertEqual(self.status(), (True, "verified"))

    def test_mismatches_are_not_calibrated(self):
        other = C.experiment_digest(self.T, replace(self.cfg, stress=3.0), self.P)
        for kw, needle in ((dict(expected=None), "stated no required experiment identity"),
                           (dict(expected=other), "differs from the required experiment"),
                           (dict(family="wf:breakout_momentum"), "is for family")):
            ok, why = self.status(**kw)
            self.assertFalse(ok)
            self.assertIn(needle, why)
        t2 = self.led.record_trial(TrialRecord(family_id="wf:%s" % self.T, kind="research", spec_digest="s2",
                                               data_digest="d", code_digest="other-code", seed=1, metrics={}))
        ok, why = self.status(trial=t2)
        self.assertFalse(ok)
        self.assertIn("produced by other code", why)

    def test_legacy_or_inconsistent_batteries_are_refused(self):
        led = Ledger()
        t = led.record_trial(TrialRecord(family_id="wf:%s" % self.T, kind="research", spec_digest="s",
                                         data_digest="d", code_digest=code_identity(SR.CODE_PATHS), seed=1,
                                         metrics={}))
        legacy = BatteryPlan("legacy", pipeline_identity("wf:%s" % self.T, code_identity(SR.CODE_PATHS)), 59,
                             effect_sizes=(0.5,), n_per_effect=20)
        res = self.battery(led, legacy)
        for expected in ("want", None):                                  # wf: families always need one
            ok, why = self.status(res=res, trial=t, led=led, expected=expected)
            self.assertFalse(ok)
            self.assertIn("no experiment identity", why)
        bad = replace(self.plan, name="rc-test-2", n_nulls=60)            # plan disagrees with its declared design
        bad = replace(bad, experiment=dict(bad.experiment, design=dict(bad.experiment["design"], name="rc-test-2")))
        ok, why = self.status(res=self.battery(self.led, bad), expected=bad.experiment_digest)
        self.assertFalse(ok)
        self.assertIn("n_nulls differs from its declared experiment design", why)
        for i, (kw, needle) in enumerate(((dict(effect_sizes=(0.6,)), "effect_sizes differs"),
                                          (dict(pipeline_id="pipeline:wf:%s@x" % self.T), "not bound"))):
            name = "rc-test-%d" % (i + 3)
            b = replace(self.plan, name=name, **kw)
            b = replace(b, experiment=dict(b.experiment, design=dict(b.experiment["design"], name=name)))
            ok, why = self.status(res=self.battery(self.led, b), expected=b.experiment_digest)
            self.assertFalse(ok)
            self.assertIn(needle, why)

    def test_tampered_record_is_refused(self):
        led = Ledger()
        t = led.record_trial(TrialRecord(family_id="wf:%s" % self.T, kind="research", spec_digest="s",
                                         data_digest="d", code_digest=code_identity(SR.CODE_PATHS), seed=1,
                                         metrics={}))
        rec = self.plan.record()
        rec["experiment"] = dict(rec["experiment"], holdout_policy={"holdout_frac": 0.1})
        led.append("control_battery_plan", rec, ref_id=self.plan.digest)
        led.append("control_battery_result", asdict(self.res), ref_id=self.plan.digest)
        ok, why = self.status(trial=t, led=led)
        self.assertFalse(ok)
        self.assertIn("does not hash to the battery's plan digest", why)
        led2 = Ledger()
        t2 = led2.record_trial(TrialRecord(family_id="wf:%s" % self.T, kind="research", spec_digest="s",
                                           data_digest="d", code_digest=code_identity(SR.CODE_PATHS), seed=1,
                                           metrics={}))
        led2.append("control_battery_plan", dict(self.plan.record(), experiment_digest="0" * 64),
                    ref_id=self.plan.digest)
        led2.append("control_battery_result", asdict(self.res), ref_id=self.plan.digest)
        ok, why = self.status(trial=t2, led=led2)
        self.assertFalse(ok)
        self.assertIn("does not hash to its recorded digest", why)

    def test_evaluate_passes_the_required_identity_through(self):
        src = (REPO / "research/integrity/report_card.py").read_text()
        self.assertIn("_canary_status(canary, ledger, family_id, candidate_trial_id, cfg, canary_experiment)", src)
        self.assertIn("canary_experiment", str(inspect.signature(RC.evaluate)))


class O9_Boundary(unittest.TestCase):
    def test_research_boundary_and_no_production_import(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research" / "optimiser")], [])
        for top in ("bot", "tools", "dashboard"):
            for p in (REPO / top).rglob("*.py"):
                self.assertNotIn("research.optimiser", p.read_text(encoding="utf-8", errors="ignore"), str(p))


if __name__ == "__main__":
    unittest.main()

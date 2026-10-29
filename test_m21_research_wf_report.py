"""test_m21_research_wf_report.py — M21 E1: walk-forward results as research report cards.

Offline only: synthetic data, in-memory ledgers, trivially fast canary batteries
(their pipelines are lambdas; only the recorded counts matter to the card).
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import _test_isolation  # noqa: F401  (M21 C5a)

from research import boundary
from research.common.synthetic import MarketSpec, generate
from research.genome import search as SR
from research.integrity.controls import PipelineDecision, run_battery
from research.integrity.ledger import Ledger, TrialRecord, VerifyResult
from research.integrity.provenance import code_identity
from research.optimiser import canary as C
from research.optimiser import walkforward as W
from research.optimiser import wf_report as R

REPO = Path(__file__).resolve().parent
T = "mean_reversion"
MR = (T,)
PROTO = C.CanaryProtocol("e1-test", 59, (0.5,), 20, n_symbols=30, n_sessions=750)


def battery(led, cfg, proto=PROTO, null_pass=False):
    plan = C.canary_plan(T, cfg, proto)
    return run_battery(plan, lambda d: PipelineDecision(d > 0), lambda s: 1.0 if null_pass else 0.0,
                       lambda e, s: e, ledger=led)


class Fixture:
    """A walk-forward result recorded in a ledger exactly as ``walkforward.run_template`` records it."""

    def __init__(self, *, origin="real", verdict=W.PASS, holdout=W.PASS, code=None, template=T, cfg=None):
        self.cfg = cfg or W.WFConfig(templates=MR)
        self.led = Ledger()
        fam = "wf:%s" % template
        self.trial = self.led.record_trial(TrialRecord(
            family_id=fam, kind="research", spec_digest="g-chosen", data_digest="d",
            code_digest=code or code_identity(SR.CODE_PATHS), seed=1, metrics={}))
        self.result = {
            "template": template, "family_id": fam, "origin": origin,
            "folds": [{"fold": 0, "chosen_genome_id": "g-chosen"}],
            "summary": {"oos_days": 300, "oos_trades": 80, "oos_sharpe_ann": 1.2, "oos_mean_net_r": 0.2,
                        "default_params_oos_sharpe_ann": 0.4, "stress_oos_sharpe_ann": 0.7, "stress_multiplier": 2.0,
                        "n_trials_family": 36, "dsr": {"dsr": 0.97}, "pbo": {"pbo": 0.1},
                        "oos_no_fill": 2, "oos_inconclusive": 0, "oos_exit_capacity_breach": 0},
            "verdict": verdict, "reasons": [] if verdict == W.PASS else ["because"],
            "holdout": {"status": holdout, "trades": 20, "sharpe_ann": 0.9}}
        self.led.append("wf_run", dict({k: v for k, v in self.result.items() if k != "folds"}, n_folds=1),
                        ref_id=fam)
        self.digest = W._cfg_dict(self.cfg)["digest"]

    def card(self, canary="good", **kw):
        if canary == "good":
            canary = battery(self.led, self.cfg)
        args = dict(cfg=self.cfg, run_config_digest=self.digest, ledger=self.led, canary=canary, protocol=PROTO)
        args.update(kw)
        return R.template_card(self.result, **args)


class E1_Verdicts(unittest.TestCase):
    def test_pass_needs_real_data_wf_pass_holdout_pass_and_a_verified_canary(self):
        f = Fixture()
        c = f.card()
        self.assertEqual(c.verdict, R.PASS, c.reasons)
        self.assertEqual((c.numbers["canary_verified"], c.numbers["real_data"]), (1.0, 1.0))
        self.assertEqual(c.inputs["required_canary_experiment"], C.experiment_digest(T, f.cfg, PROTO))
        self.assertEqual(c.inputs["candidate_trial_id"], f.trial)
        self.assertEqual(c.inputs["evidence_class"], R.EVIDENCE_REAL)
        (e,) = f.led.entries("report_card", "wf:%s" % T)                 # the card is ledgered
        self.assertEqual(e.payload["verdict"], R.PASS)
        self.assertTrue(f.led.verify().ok)

    def test_synthetic_is_never_a_pass(self):
        c = Fixture(origin="synthetic").card()
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertIn(R.SYNTHETIC_REASON, c.reasons)
        self.assertEqual((c.inputs["wf_verdict"], c.inputs["evidence_class"]), (W.PASS, R.EVIDENCE_SYNTHETIC))

    def test_walk_forward_verdict_is_preserved_and_fail_takes_precedence(self):
        c = Fixture(verdict=W.FAIL, holdout=W.HOLDOUT_NOT_RUN).card(canary=None)
        self.assertEqual(c.verdict, R.FAIL)
        self.assertTrue(c.reasons[0].startswith("walk-forward FAIL: because"))
        c = Fixture(verdict=W.INCONCLUSIVE, holdout=W.HOLDOUT_NOT_RUN).card()
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertIn("walk-forward INCONCLUSIVE: because", c.reasons)
        self.assertEqual(c.inputs["wf_reasons"], ["because"])

    def test_holdout_must_pass(self):
        c = Fixture(holdout=W.FAIL).card()
        self.assertEqual(c.verdict, R.FAIL)
        self.assertIn("one-shot historical holdout FAILED", c.reasons)
        for h in (W.HOLDOUT_NOT_RUN, W.INCONCLUSIVE):
            c = Fixture(holdout=h).card()
            self.assertEqual(c.verdict, R.INCONCLUSIVE)
            self.assertIn("historical holdout %s" % h, c.reasons)

    def test_canary_must_be_calibrated_and_bound_to_this_experiment(self):
        f = Fixture()
        c = f.card(canary=None)
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertIn("walk-forward decision procedure has no CALIBRATED canary battery", c.reasons)
        f = Fixture()
        bad = battery(f.led, f.cfg, null_pass=True)                       # every null passes: not calibrated
        self.assertNotEqual(bad.verdict, "CALIBRATED")
        self.assertEqual(f.card(canary=bad).verdict, R.INCONCLUSIVE)
        f = Fixture()
        other = battery(f.led, replace(f.cfg, stress=3.0))                # calibrated, other experiment
        self.assertEqual(other.verdict, "CALIBRATED")
        c = f.card(canary=other)
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertTrue(any("not verified for this experiment" in r for r in c.reasons), c.reasons)
        f = Fixture()
        foreign = battery(Ledger(), f.cfg)                                 # recorded in another ledger
        self.assertEqual(f.card(canary=foreign).verdict, R.INCONCLUSIVE)

    def test_result_must_be_the_recorded_run_with_the_same_config(self):
        f = Fixture()
        f.result["summary"]["oos_sharpe_ann"] = 9.9                        # edited after recording
        c = f.card()
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertIn("result is not the walk-forward run recorded in this ledger", c.reasons)
        f = Fixture()
        c = f.card(run_config_digest="0" * 64)
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertIn("run config differs from the config the experiment identity is computed from", c.reasons)

    def test_trial_must_come_from_the_calibrated_code(self):
        f = Fixture(code="other-code")
        c = f.card()
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertTrue(any("produced by other code" in r for r in c.reasons), c.reasons)
        f = Fixture()
        f.result["folds"][-1]["chosen_genome_id"] = "never-ledgered"      # folds are not part of the record
        c = f.card()
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertIn("no ledgered trial of the chosen genome: the code that produced the result is unknown",
                      c.reasons)
        f = Fixture()                                                      # the ledger refuses UPDATEs; a
        f.led.verify = lambda *a: VerifyResult(False, 3, 1, "hash mismatch")  # copied, edited file would not verify
        c = f.card()
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertTrue(any(r.startswith("ledger chain does not verify") for r in c.reasons), c.reasons)

    def test_template_without_a_canary_design_is_inconclusive(self):
        f = Fixture(template="trend_confluence", cfg=W.WFConfig(templates=("trend_confluence",)))
        c = f.card(canary=None)
        self.assertEqual(c.verdict, R.INCONCLUSIVE)
        self.assertTrue(any("no canary experiment is defined" in r for r in c.reasons), c.reasons)

    def test_malformed_results_are_refused(self):
        f = Fixture()
        for bad in (dict(f.result, verdict="MAYBE"), dict(f.result, family_id="wf:other")):
            with self.assertRaises(ValueError):
                R.template_card(bad, cfg=f.cfg, run_config_digest=f.digest, ledger=f.led, canary=None,
                                protocol=PROTO)


class E1_RealDriver(unittest.TestCase):
    """The genuine driver output, end to end (synthetic market: never a PASS card)."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = W.WFConfig(templates=MR)
        cls.led = Ledger()
        bars = generate(MarketSpec(n_symbols=6, n_sessions=500, timeframe="1D", seed=3)).bars
        cls.out = W.run(bars, cls.cfg, ledger=cls.led, data_seed=3)

    def test_cards_from_a_driver_run(self):
        cards = R.run_cards(self.out, cfg=self.cfg, ledger=self.led, canaries={T: battery(self.led, self.cfg)},
                            protocol=PROTO)
        c = cards[T]
        r = self.out["templates"][T]
        self.assertEqual(c.inputs["wf_verdict"], r["verdict"])
        self.assertEqual(c.verdict, R.FAIL if r["verdict"] == W.FAIL else R.INCONCLUSIVE)
        self.assertIn(R.SYNTHETIC_REASON, c.reasons)
        self.assertEqual(c.numbers["ledger_recorded"], 1.0)
        self.assertIsNotNone(c.inputs["candidate_trial_id"])
        md = R.render_markdown(cards)
        self.assertIn("SYNTHETIC", md)
        self.assertIn("| %s | **%s** |" % (T, c.verdict), md)
        with self.assertRaises(ValueError):
            R.run_cards(self.out, cfg=replace(self.cfg, stress=3.0), ledger=self.led, canaries={})


class E1_Boundary(unittest.TestCase):
    def test_research_only(self):
        self.assertEqual([str(v) for v in boundary.scan_tree(REPO / "research" / "optimiser")], [])
        for top in ("bot", "tools", "dashboard"):
            for p in (REPO / top).rglob("*.py"):
                self.assertNotIn("wf_report", p.read_text(encoding="utf-8", errors="ignore"), str(p))


if __name__ == "__main__":
    unittest.main()

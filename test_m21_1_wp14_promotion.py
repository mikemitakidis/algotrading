"""test_m21_1_wp14_promotion.py — M21 WP14 Promotion & Approval Model (B13, B23 auto-revert; C-6).

Pins the tier classifier (incl. "a tighter stop is never GREEN"), approval
tokens that cannot be forged or reused for another proposal or tier, the
shadow comparison (INCONCLUSIVE when thin), the decay guard (never cancels
orders), and the persisted state machine: shadow first, one active change
and one apply per 7 days, RED cooling-off that survives a restart, apply
through the WP9 controls, auto-revert with an alert, and an audited refusal
for every rule. Offline only; no broker, no orders.
"""
from __future__ import annotations

import ast
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.promotion import evaluation as EV
from bot.promotion import machine as MC
from bot.promotion import tiers as TI
from bot.promotion.approvals import ApprovalService
from bot.templates import controls as K
from bot.templates import routing as RT

REPO = Path(__file__).resolve().parent
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
SVC = ApprovalService("operator-approvals", b"k" * 32)
FORGER = ApprovalService("operator-approvals", b"z" * 32)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def base_cfg(rsi=30.0, extra=()):
    return K.validate_config({"selected": ["scanner", "mean_reversion", *extra], "champion": "scanner",
                              "params": {"mean_reversion": {"rsi_entry": rsi}}})


PASS_EV = {"wf_verdict": "PASS", "holdout_status": "PASS"}


def prop(pid, cur, new, kind="tune", evidence=PASS_EV, **kw):
    return TI.Proposal(pid, kind, cur, new, dict(evidence), author="claude", **kw)


class Sink:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def notify(self, e):
        if self.fail:
            raise ConnectionError("down")
        self.events.append(e)


def shadow(n, cur=0.2, pro=0.3, pro_losses=None):
    out = []
    for i in range(n):
        p = -1.0 if pro_losses is not None and i < pro_losses else pro
        out.append(EV.ShadowObservation("s%03d" % i, cur, p))
    return out


class P1_Tiers(unittest.TestCase):
    def test_tiers(self):
        c = base_cfg()
        cases = [(base_cfg(31.0), "tune", TI.GREEN), (base_cfg(36.0), "tune", TI.AMBER),
                 (base_cfg(15.0), "tune", TI.AMBER), (base_cfg(10.0), "tune", TI.RED),
                 (base_cfg(extra=("breakout_momentum",)), "select", TI.AMBER),
                 (K.validate_config({**c, "champion": "mean_reversion"}), "champion", TI.AMBER),
                 (c, "promote", TI.RED)]
        for new, kind, want in cases:
            self.assertEqual(TI.classify(prop("p", c, new, kind)).tier, want, (kind, new["params"]))
        mode = K.validate_config({**c, "params": {"mean_reversion": {"rsi_entry": 30.0, "rsi_mode": "wilder"}}})
        self.assertEqual(TI.classify(prop("p", c, mode)).tier, TI.AMBER)

    def test_tighter_stop_is_never_green(self):
        d = TI.classify(prop("p", base_cfg(), base_cfg(), "exits", exits_current={"stop_atr": 2.0, "target_atr": 3.0},
                             exits_proposed={"stop_atr": 1.5, "target_atr": 3.0}))
        self.assertEqual(d.tier, TI.AMBER)
        self.assertIn("exit_policy_change_can_raise_losses", d.reasons)

    def test_deselect_is_risk_reducing_green(self):
        c = base_cfg(extra=("breakout_momentum",))
        d = TI.classify(prop("p", c, base_cfg(), "deselect", evidence={}))
        self.assertEqual((d.tier, d.eligible_evidence), (TI.GREEN, True))

    def test_thin_or_failed_evidence_is_not_eligible(self):
        c = base_cfg()
        inc = TI.classify(prop("p", c, base_cfg(31.0), evidence={"wf_verdict": "INCONCLUSIVE"}))
        self.assertFalse(inc.eligible_evidence)
        self.assertIn("walk_forward_INCONCLUSIVE", inc.evidence_reasons)
        fail = TI.classify(prop("p", c, base_cfg(31.0), evidence={"wf_verdict": "FAIL"}))
        self.assertEqual((fail.tier, fail.eligible_evidence), (TI.RED, False))
        promote = TI.classify(prop("p", c, c, "promote", evidence={"wf_verdict": "PASS"}))
        self.assertIn("holdout_missing", promote.evidence_reasons)
        self.assertIn("no_change", TI.classify(prop("p", c, c)).evidence_reasons)
        with self.assertRaises(ValueError):
            TI.Proposal("p", "yolo", c, c, author="x")
        with self.assertRaises(ValueError):
            TI.Proposal("p", "tune", c, c, author=" ")


class P2_Approvals(unittest.TestCase):
    def tok(self, svc=SVC, **kw):
        a = dict(proposal_id="p1", proposal_digest="d1", tier=TI.AMBER, approver="mike", issued=T0,
                 expires=T0 + timedelta(days=1))
        a.update(kw)
        return svc.issue(**a)

    def test_only_the_exact_proposal_tier_and_window_verify(self):
        v = SVC.verifier()
        chk = lambda t, **kw: v.verify(t, **{**dict(proposal_id="p1", proposal_digest="d1", tier=TI.AMBER,
                                                  now=T0 + timedelta(hours=1)), **kw})
        self.assertEqual(chk(self.tok()), (True, "ok"))
        self.assertEqual(chk(self.tok(tier=TI.RED)), (True, "ok"))                 # higher tier covers lower
        self.assertEqual(chk(self.tok(tier=TI.GREEN))[1], "token_tier_GREEN_below_AMBER")
        self.assertEqual(chk(self.tok(), proposal_id="p2")[1], "token_for_another_proposal")
        self.assertEqual(chk(self.tok(), proposal_digest="d2")[1], "token_for_another_proposal")
        self.assertEqual(chk(self.tok(), now=T0 + timedelta(days=2))[1], "token_outside_validity")
        self.assertEqual(chk(self.tok(svc=FORGER))[1], "bad_signature")
        self.assertEqual(chk(None)[1], "no_token")
        other = ApprovalService("someone-else", b"k" * 32)
        self.assertEqual(chk(self.tok(svc=other))[1], "wrong_service")
        t = self.tok()
        from dataclasses import replace
        self.assertEqual(chk(replace(t, approver="eve"))[1], "bad_signature")

    def test_automation_cannot_obtain_a_token(self):
        for who in ("claude", "system", "", "Scheduler"):
            with self.assertRaises(PermissionError):
                self.tok(approver=who)


class P3_ShadowAndDecay(unittest.TestCase):
    def test_shadow_comparison(self):
        self.assertEqual(EV.compare_shadow(shadow(10), min_signals=20).status, EV.INCONCLUSIVE)
        self.assertEqual(EV.compare_shadow(shadow(25), min_signals=20).status, EV.ELIGIBLE)
        worse = EV.compare_shadow(shadow(25, cur=0.3, pro=0.1), min_signals=20)
        self.assertEqual(worse.status, EV.INELIGIBLE)
        lossy = EV.compare_shadow(shadow(25, pro=0.6, pro_losses=5), min_signals=20)
        self.assertFalse(lossy.loss_rate_not_higher)
        self.assertEqual(lossy.status, EV.INELIGIBLE)
        with self.assertRaises(ValueError):
            EV.compare_shadow(shadow(3) + shadow(1), min_signals=1)
        with self.assertRaises(ValueError):
            EV.compare_shadow([EV.ShadowObservation("x", float("nan"), 0.1)], min_signals=1)

    def test_decay_guard(self):
        ok = EV.check_decay([0.3, -0.2, 0.5] * 5, baseline_mean_r=0.2)
        self.assertFalse(ok.revert)
        bad = EV.check_decay([-0.5, 0.1] * 6, baseline_mean_r=0.3)
        self.assertTrue(bad.revert)
        self.assertTrue(any(r.startswith("mean_r") for r in bad.reasons))
        dd = EV.check_decay([-2.0, -2.5, -2.0], baseline_mean_r=0.2)
        self.assertTrue(dd.revert)                                        # drawdown fires before min trades
        self.assertFalse(dd.cancel_orders)
        with self.assertRaises(TypeError):
            EV.RevertDecision(True, (), 1, 0.0, 0.0, cancel_orders=True)
        with self.assertRaises(ValueError):
            EV.check_decay([float("inf")], baseline_mean_r=0.0)


class Book(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.ctl = K.TemplateControls(os.path.join(self.tmp.name, "controls.db"), clock=self.clock)
        v = self.ctl.propose(base_cfg(), author="claude", reason="baseline")
        self.ctl.approve(v, approver="mike", reason="baseline")
        self.ctl.activate(v, actor="mike")
        self.path = os.path.join(self.tmp.name, "promotion.db")
        self.book = MC.PromotionBook(self.path, clock=self.clock)
        self.v = SVC.verifier()

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, pid, tier=None, svc=SVC):
        r = self.book._row(pid)
        return svc.issue(proposal_id=pid, proposal_digest=r["digest"], tier=tier or r["tier"], approver="mike",
                         issued=self.clock.t - timedelta(minutes=1), expires=self.clock.t + timedelta(days=30))

    def to_eligible(self, p, n=None):
        self.book.submit(p)
        self.book.start_shadow(p.proposal_id, actor="claude")
        need = TI.SHADOW_MIN_SIGNALS[self.book._row(p.proposal_id)["tier"]]
        self.assertEqual(self.book.record_shadow(p.proposal_id, EV.compare_shadow(shadow(n or need),
                                                                                  min_signals=need), actor="claude"),
                         MC.S_ELIGIBLE)

    def apply(self, pid):
        t = self.token(pid)
        self.book.approve(pid, t, self.v)
        return self.book.apply(pid, self.ctl, token=t, verifier=self.v)

    def refusals(self):
        return [e for e in self.book.events() if e["kind"] == "REFUSED"]


class P4_Lifecycle(Book):
    def test_green_change_end_to_end(self):
        p = prop("g1", K.read_active(self.ctl.path).config, base_cfg(31.0))
        self.assertEqual(self.book.submit(p), TI.GREEN)
        self.book.start_shadow("g1", actor="claude")
        self.assertEqual(self.book.record_shadow("g1", EV.compare_shadow(shadow(10), min_signals=20), actor="c"),
                         MC.SHADOW)                                       # thin: stays in shadow
        self.assertEqual(self.book.record_shadow("g1", EV.compare_shadow(shadow(25), min_signals=20), actor="c"),
                         MC.S_ELIGIBLE)
        v = self.apply("g1")
        a = K.read_active(self.ctl.path)
        self.assertEqual((a.version, a.config["params"]["mean_reversion"]["rsi_entry"]), (v, 31.0))
        self.assertEqual(self.book.state("g1"), MC.MONITORING)
        self.assertEqual(self.book.monitor("g1", [0.4, -0.1] * 15, self.ctl, sink=Sink()), MC.CONFIRMED)
        kinds = [(e["from_state"], e["to_state"]) for e in self.book.events("g1") if e["kind"] in
                 ("TRANSITION", "APPLIED")]
        self.assertEqual(kinds, [(None, "CLASSIFIED"), ("CLASSIFIED", "SHADOW"), ("SHADOW", "ELIGIBLE"),
                                 ("ELIGIBLE", "APPROVED"), ("APPROVED", "MONITORING"), ("MONITORING", "CONFIRMED")])
        self.assertEqual(K.read_active(self.ctl.path).version, v)

    def test_evidence_and_state_rules(self):
        cur = K.read_active(self.ctl.path).config
        self.book.submit(prop("i1", cur, base_cfg(31.0), evidence={"wf_verdict": "INCONCLUSIVE"}))
        with self.assertRaises(MC.PromotionRefused):
            self.book.start_shadow("i1", actor="claude")
        self.book.withdraw("i1", actor="mike", reason="thin")
        self.book.submit(prop("i2", cur, base_cfg(31.0)))
        with self.assertRaises(MC.PromotionRefused):
            self.book.approve("i2", self.token("i2"), self.v)             # not eligible yet
        with self.assertRaises(MC.PromotionRefused):
            self.book.submit(prop("i2", cur, base_cfg(32.0)))             # id reuse / one active change
        self.assertGreaterEqual(len(self.refusals()), 3)

    def test_forged_or_mismatched_tokens_are_refused(self):
        p = prop("f1", K.read_active(self.ctl.path).config, base_cfg(36.0))
        self.to_eligible(p)
        for bad in (self.token("f1", svc=FORGER), self.token("f1", tier=TI.GREEN)):
            with self.assertRaises(MC.PromotionRefused):
                self.book.approve("f1", bad, self.v)
        self.assertEqual(self.book.state("f1"), MC.S_ELIGIBLE)
        good = self.token("f1")
        self.book.approve("f1", good, self.v)
        with self.assertRaises(MC.PromotionRefused):
            self.book.apply("f1", self.ctl, token=self.token("f1", svc=FORGER), verifier=self.v)

    def test_exit_change_needs_loss_rate_not_higher(self):
        cur = K.read_active(self.ctl.path).config
        p = prop("x1", cur, cur, "exits", exits_current={"stop_atr": 2.0}, exits_proposed={"stop_atr": 1.5})
        self.book.submit(p)
        self.book.start_shadow("x1", actor="claude")
        verdict = EV.compare_shadow(shadow(60, pro=0.6, pro_losses=10), min_signals=50)
        self.assertEqual(self.book.record_shadow("x1", verdict, actor="claude"), MC.S_INELIGIBLE)


class P5_BudgetCoolingOffRevert(Book):
    def test_one_apply_per_seven_days(self):
        self.to_eligible(prop("a1", K.read_active(self.ctl.path).config, base_cfg(31.0)))
        self.apply("a1")
        self.book.monitor("a1", [0.3] * 30, self.ctl, sink=Sink())
        self.to_eligible(prop("a2", K.read_active(self.ctl.path).config, base_cfg(30.0)))
        t = self.token("a2")
        self.book.approve("a2", t, self.v)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("a2", self.ctl, token=t, verifier=self.v)
        self.assertIn("one_apply_per_7_days", str(cm.exception))
        self.clock.advance(days=8)
        self.book.apply("a2", self.ctl, token=t, verifier=self.v)
        self.assertEqual(self.book.state("a2"), MC.MONITORING)

    def test_red_cooling_off_survives_restart(self):
        cur = K.read_active(self.ctl.path).config
        self.to_eligible(prop("r1", cur, base_cfg(12.0)))                 # 60 % move: RED
        self.assertEqual(self.book._row("r1")["tier"], TI.RED)
        self.apply("r1")
        self.book.monitor("r1", [0.3] * 30, self.ctl, sink=Sink())
        restarted = MC.PromotionBook(self.path, clock=self.clock)
        self.clock.advance(days=10)
        with self.assertRaises(MC.PromotionRefused) as cm:
            restarted.submit(prop("r2", K.read_active(self.ctl.path).config, base_cfg(12.5)))
        self.assertIn("cooling_off", str(cm.exception))
        self.clock.advance(days=5)
        self.assertEqual(restarted.submit(prop("r3", K.read_active(self.ctl.path).config, base_cfg(12.5))), TI.GREEN)

    def test_auto_revert_rolls_back_alerts_and_never_cancels_orders(self):
        before = K.read_active(self.ctl.path)
        self.to_eligible(prop("d1", before.config, base_cfg(36.0)))
        self.apply("d1")
        sink = Sink()
        self.assertEqual(self.book.monitor("d1", [-0.6, 0.1] * 6, self.ctl, sink=sink), MC.REVERTED)
        after = K.read_active(self.ctl.path)
        self.assertEqual((after.version, after.config), (before.version, before.config))
        self.assertEqual(sink.events[0]["kind"], "PROMOTION_AUTO_REVERT")
        self.assertIs(sink.events[0]["cancel_orders"], False)
        self.assertIsNotNone(self.book.cooling_off_until())
        self.assertEqual(RT.LIVE_TEMPLATES, frozenset({"scanner"}))

    def test_failed_alert_does_not_undo_the_revert(self):
        self.to_eligible(prop("d2", K.read_active(self.ctl.path).config, base_cfg(36.0)))
        self.apply("d2")
        self.assertEqual(self.book.monitor("d2", [-3.0, -4.0], self.ctl, sink=Sink(fail=True)), MC.REVERTED)
        self.assertIn("ALERT_FAILED", [e["kind"] for e in self.book.events("d2")])

    def test_apply_needs_a_revertible_fresh_baseline(self):
        p = prop("s1", base_cfg(29.0), base_cfg(31.0))                   # built on a config that is not active
        self.to_eligible(p)
        t = self.token("s1")
        self.book.approve("s1", t, self.v)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("s1", self.ctl, token=t, verifier=self.v)
        self.assertIn("stale_config", str(cm.exception))
        empty = K.TemplateControls(os.path.join(self.tmp.name, "empty.db"), clock=self.clock)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("s1", empty, token=t, verifier=self.v)
        self.assertIn("no_intact_active_version", str(cm.exception))

    def test_withdraw_rules_and_determinism(self):
        self.book.submit(prop("w1", K.read_active(self.ctl.path).config, base_cfg(31.0)))
        self.book.start_shadow("w1", actor="claude")
        self.book.withdraw("w1", actor="mike", reason="changed my mind")
        with self.assertRaises(MC.PromotionRefused):
            self.book.withdraw("w1", actor="mike", reason="twice")
        self.assertEqual(MC.proposal_digest(prop("z", base_cfg(), base_cfg(31.0))),
                         MC.proposal_digest(prop("z", base_cfg(), base_cfg(31.0))))


class P6_Isolation(unittest.TestCase):
    FILES = ["bot/promotion/__init__.py", "bot/promotion/tiers.py", "bot/promotion/evaluation.py",
             "bot/promotion/approvals.py", "bot/promotion/machine.py"]

    def test_no_broker_order_messaging_or_routing_code(self):
        bad = ("bot.brokers", "bot.notifier", "bot.paper_loop_d2", "bot.kill_switch", "bot.templates.routing",
               "ib_insync", "alpaca", "requests", "socket", "telegram", "urllib.request", "subprocess", "research")
        for f in self.FILES:
            tree = ast.parse((REPO / f).read_text())
            mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            for m in mods:
                self.assertFalse(any(m == b or m.startswith(b + ".") for b in bad), (f, m))
            text = (REPO / f).read_text()
            for s in ("scoring_truth", "scanner_bridge", "candidate_ledger", "cancelOrder", "cancel_order(",
                      "placeOrder"):
                self.assertNotIn(s, text, (f, s))


if __name__ == "__main__":
    unittest.main()

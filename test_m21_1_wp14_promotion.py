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
import sqlite3
import tempfile
import threading
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
    def test_one_active_change_at_a_time(self):
        cur = K.read_active(self.ctl.path).config
        self.book.submit(prop("o1", cur, base_cfg(31.0)))
        self.book.start_shadow("o1", actor="claude")
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.submit(prop("o2", cur, base_cfg(32.0)))
        self.assertIn("one_active_change:o1", str(cm.exception))
        self.book.withdraw("o1", actor="mike", reason="make room")
        self.assertEqual(self.book.submit(prop("o3", cur, base_cfg(32.0))), TI.GREEN)

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


class Crash(BaseException):
    """A process death: nothing after it runs (no except-Exception handler sees it)."""


class FaultAt:
    def __init__(self, point, exc=None, times=1):
        self.point, self.exc, self.left = point, exc or Crash("crash at %s" % point), times

    def __call__(self, point):
        if point == self.point and self.left:
            self.left -= 1
            raise self.exc


class FailingControls(K.TemplateControls):
    def __init__(self, path, *, clock, fail_on=()):
        super().__init__(path, clock=clock)
        self.fail_on = set(fail_on)

    def _maybe(self, name):
        if name in self.fail_on:
            raise sqlite3.OperationalError("simulated controls write failure in %s" % name)

    def propose(self, *a, **k):
        self._maybe("propose")
        return super().propose(*a, **k)

    def activate(self, *a, **k):
        self._maybe("activate")
        return super().activate(*a, **k)

    def rollback(self, *a, **k):
        self._maybe("rollback")
        return super().rollback(*a, **k)


class CrossStore(Book):
    """Fault injection at every boundary between the promotion and controls stores (review 5686441635)."""

    def approved(self, pid, rsi=36.0):
        self.to_eligible(prop(pid, K.read_active(self.ctl.path).config, base_cfg(rsi)))
        t = self.token(pid)
        self.book.approve(pid, t, self.v)
        return t

    def restart(self):
        return MC.PromotionBook(self.path, clock=self.clock)

    def audits(self, action):
        return [a for a in self.ctl.audit() if a["action"] == action]

    def no_live_leftovers(self):
        """No version is left PROPOSED/APPROVED and exactly one is ACTIVE."""
        st = [v["status"] for v in self.ctl.versions()]
        self.assertEqual(st.count(K.ACTIVE), 1, st)
        self.assertFalse({K.PROPOSED, K.APPROVED} & set(st), st)

    def assert_shut(self, book, pid, needle):
        acts = [lambda: book.submit(prop("zz", K.read_active(self.ctl.path).config, base_cfg(31.0)))]
        if book.state(pid) != MC.MONITORING:
            acts.append(lambda: book.withdraw(pid, actor="mike", reason="x"))
        for act in acts:
            with self.assertRaises(MC.PromotionRefused) as cm:
                act()
            self.assertIn(needle, str(cm.exception))


class P7_CrashAndFaults(CrossStore):
    def test_01_crash_before_activation_aborts_and_never_reapplies(self):
        for point in ("apply:intent_recorded", "apply:version_proposed", "apply:version_approved"):
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                base = K.read_active(self.ctl.path)
                t = self.approved("c1")
                self.book.fault_hook = FaultAt(point)
                with self.assertRaises(Crash):
                    self.book.apply("c1", self.ctl, token=t, verifier=self.v)
                book = self.restart()
                self.assert_shut(book, "c1", "operation_open_reconcile_required")
                (op,) = book.ops("c1")
                self.assertEqual(book.reconcile(self.ctl)["settled"], {op["op_id"]: MC.OP_PENDING})  # lease held
                self.clock.advance(minutes=11)
                self.assertEqual(book.reconcile(self.ctl)["settled"], {op["op_id"]: MC.OP_ABORTED})
                now = K.read_active(self.ctl.path)
                self.assertEqual((now.version, now.config), (base.version, base.config))
                self.assertEqual(book.state("c1"), MC.APPROVED)
                self.assertEqual(len(self.audits("ACTIVATED")), 1)          # only the baseline ever activated
                self.no_live_leftovers()
                # 06: a repeated apply after recovery is a fresh, audited attempt and applies exactly once
                v2 = book.apply("c1", self.ctl, token=t, verifier=self.v)
                self.assertEqual((K.read_active(self.ctl.path).version, book.state("c1")), (v2, MC.MONITORING))
                n = len(self.ctl.versions())
                with self.assertRaises(MC.PromotionRefused):
                    book.apply("c1", self.ctl, token=t, verifier=self.v)
                self.assertEqual(len(self.ctl.versions()), n)
                self.no_live_leftovers()

    def test_02_crash_after_activation_before_promotion_commit_completes_the_audit(self):
        t = self.approved("c2")
        self.book.fault_hook = FaultAt("apply:activated")
        with self.assertRaises(Crash):
            self.book.apply("c2", self.ctl, token=t, verifier=self.v)
        live = K.read_active(self.ctl.path).version
        book = self.restart()
        self.assertEqual(book.state("c2"), MC.APPROVED)                     # the stores disagree right now ...
        self.assert_shut(book, "c2", "operation_open_reconcile_required")  # ... so nothing else may happen
        self.clock.advance(minutes=11)
        book.reconcile(self.ctl)
        self.assertEqual((book.state("c2"), book._row("c2")["controls_version"]), (MC.MONITORING, live))
        applied = [e for e in book.events("c2") if e["kind"] == "APPLIED"]
        self.assertEqual(len(applied), 1)
        self.assertTrue(MC.json.loads(applied[0]["detail"])["recovered"])
        before = len(book.events())
        self.assertEqual(book.reconcile(self.ctl), {"settled": {}, "blocked": [], "diverged": []})
        self.assertEqual(len(book.events()), before)                       # idempotent
        with self.assertRaises(MC.PromotionRefused):                       # 06: no second apply
            book.apply("c2", self.ctl, token=t, verifier=self.v)
        self.assertEqual(len(self.audits("ACTIVATED")), 2)
        self.no_live_leftovers()

    def test_03_promotion_store_write_failure_keeps_the_op_open_until_reconciled(self):
        t = self.approved("c3")
        self.book.fault_hook = FaultAt("apply:complete_tx", exc=sqlite3.OperationalError("disk I/O error"), times=2)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("c3", self.ctl, token=t, verifier=self.v)
        self.assertIn("UNSETTLED:OperationalError", str(cm.exception))
        self.assertEqual(self.book.state("c3"), MC.APPROVED)
        self.assertEqual([o["state"] for o in self.book.ops("c3")], [MC.OP_PENDING])
        self.assert_shut(self.book, "c3", "operation_open_reconcile_required")
        self.clock.advance(minutes=11)                                     # the store is healthy again
        self.book.reconcile(self.ctl)
        self.assertEqual(self.book.state("c3"), MC.MONITORING)
        self.assertEqual(self.book._row("c3")["controls_version"], K.read_active(self.ctl.path).version)
        self.no_live_leftovers()

    def test_04_controls_store_write_failure_aborts_cleanly(self):
        base = K.read_active(self.ctl.path)
        t = self.approved("c4")
        for step in ("activate", "propose"):
            bad = FailingControls(self.ctl.path, clock=self.clock, fail_on={step})
            with self.assertRaises(MC.PromotionRefused) as cm:
                self.book.apply("c4", bad, token=t, verifier=self.v)
            self.assertIn("ABORTED", str(cm.exception))
            self.assertEqual(K.read_active(self.ctl.path).version, base.version)
            self.assertEqual(self.book.state("c4"), MC.APPROVED)
            self.no_live_leftovers()
        self.assertEqual([o["state"] for o in self.book.ops("c4")], [MC.OP_ABORTED] * 2)
        v = self.book.apply("c4", self.ctl, token=t, verifier=self.v)
        self.assertEqual(K.read_active(self.ctl.path).version, v)

    def test_05_crash_during_auto_revert_is_completed_once(self):
        for point in ("revert:intent_recorded", "revert:rolled_back", "revert:complete_tx"):
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                base = K.read_active(self.ctl.path)
                self.apply_d = self.approved("d1")
                self.book.apply("d1", self.ctl, token=self.apply_d, verifier=self.v)
                self.book.fault_hook = FaultAt(point)
                with self.assertRaises(Crash):
                    self.book.monitor("d1", [-3.0, -4.0], self.ctl, sink=Sink())
                book = self.restart()
                self.assertIsNotNone(book.cooling_off_until())             # durable with the decision
                self.clock.advance(minutes=11)
                sink = Sink()
                book.reconcile(self.ctl, sink=sink)
                now = K.read_active(self.ctl.path)
                self.assertEqual((now.version, now.config), (base.version, base.config))
                self.assertEqual(book.state("d1"), MC.REVERTED)
                self.assertEqual(len(self.audits("ROLLED_BACK")), 1)       # never rolled back twice
                self.assertEqual([e["kind"] for e in sink.events], ["PROMOTION_AUTO_REVERT"])
                self.assertIs(sink.events[0]["cancel_orders"], False)
                self.assertEqual(sink.events[0]["reverted_to_version"], base.version)

    def test_06_reconcile_and_retries_are_idempotent(self):
        t = self.approved("i1")
        v = self.book.apply("i1", self.ctl, token=t, verifier=self.v)
        n = len(self.book.events())
        for _ in range(3):
            self.assertEqual(self.book.reconcile(self.ctl, sink=Sink())["blocked"], [])
        self.assertEqual(len(self.book.events()), n)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("i1", self.ctl, token=t, verifier=self.v)
        self.assertIn("not_approved:MONITORING", str(cm.exception))
        self.assertEqual(K.read_active(self.ctl.path).version, v)

    def test_09_restart_during_cooling_off(self):
        t = self.approved("k1")
        self.book.apply("k1", self.ctl, token=t, verifier=self.v)
        self.book.fault_hook = FaultAt("revert:intent_recorded")
        with self.assertRaises(Crash):
            self.book.monitor("k1", [-3.0, -4.0], self.ctl, sink=Sink())
        book = self.restart()
        self.clock.advance(minutes=11)
        book.reconcile(self.ctl)
        cur = K.read_active(self.ctl.path).config
        self.clock.advance(days=13)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.restart().submit(prop("k2", cur, base_cfg(31.0)))
        self.assertIn("cooling_off", str(cm.exception))
        self.clock.advance(days=2)
        self.assertEqual(self.restart().submit(prop("k3", cur, base_cfg(31.0))), TI.GREEN)

    def test_10_corrupt_or_unavailable_controls_block_safely(self):
        t = self.approved("b1")
        v = self.book.apply("b1", self.ctl, token=t, verifier=self.v)
        con = sqlite3.connect(self.ctl.path)
        good = con.execute("SELECT config_json FROM template_config_versions WHERE version=?", (v,)).fetchone()[0]
        con.execute("UPDATE template_config_versions SET config_json=? WHERE version=?",
                    (good.replace("36.0", "99.0"), v))
        con.commit()
        rep = self.book.reconcile(self.ctl, sink=Sink())
        self.assertEqual(rep["diverged"], ["b1"])
        (blk,) = self.book.blocked()
        self.assertIn("last_good", MC.json.loads(blk["detail"])["blocked_because"])
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.monitor("b1", [0.3] * 30, self.ctl, sink=Sink())
        self.assertIn("blocked:", str(cm.exception))
        self.assert_shut(self.book, "b1", "blocked:")
        with self.assertRaises(MC.PromotionRefused):                          # still corrupt: cannot resolve
            self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_FORWARD, reason="r")
        con.execute("UPDATE template_config_versions SET config_json=? WHERE version=?", (good, v))
        con.commit()
        con.close()
        self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_FORWARD, reason="restored")
        self.assertEqual((self.book.blocked(), self.book.state("b1")), ([], MC.MONITORING))
        # an unreadable controls store during an open apply also blocks
        self.tearDown()
        self.setUp()
        t = self.approved("b2")
        self.book.fault_hook = FaultAt("apply:intent_recorded")
        with self.assertRaises(Crash):
            self.book.apply("b2", self.ctl, token=t, verifier=self.v)
        Path(self.ctl.path).write_bytes(b"not a database" * 100)
        self.clock.advance(minutes=11)
        rep = self.restart().reconcile(self.ctl)
        self.assertEqual(list(rep["settled"].values()), [MC.OP_BLOCKED])
        self.assertIn("store_unreadable", MC.json.loads(self.book.blocked()[0]["detail"])["blocked_because"])

    def test_11_failed_alert_is_audited_and_retried(self):
        t = self.approved("a1")
        self.book.apply("a1", self.ctl, token=t, verifier=self.v)
        self.assertEqual(self.book.monitor("a1", [-3.0, -4.0], self.ctl, sink=Sink(fail=True)), MC.REVERTED)
        self.assertIn("ALERT_FAILED", [e["kind"] for e in self.book.events("a1")])
        self.assertEqual(self.book.unsent_alerts(), ["a1"])
        ok = Sink()
        self.assertEqual(self.book.retry_alerts(ok), ["a1"])
        self.assertEqual((self.book.unsent_alerts(), ok.events[0]["kind"]), ([], "PROMOTION_AUTO_REVERT"))
        self.assertEqual(self.book.retry_alerts(ok), [])

    def test_12_contradictory_stores_block_until_an_operator_resolves(self):
        t = self.approved("x1")
        self.book.fault_hook = FaultAt("apply:intent_recorded")
        with self.assertRaises(Crash):
            self.book.apply("x1", self.ctl, token=t, verifier=self.v)
        third = self.ctl.propose(base_cfg(25.0), author="mike", reason="manual change during the crash")
        self.ctl.approve(third, approver="mike", reason="manual")
        self.ctl.activate(third, actor="mike")
        self.clock.advance(minutes=11)
        book = self.restart()
        self.assertEqual(list(book.reconcile(self.ctl)["settled"].values()), [MC.OP_BLOCKED])
        (blk,) = book.blocked()
        self.assert_shut(book, "x1", "blocked:")                         # nothing else moves while blocked
        for who in ("claude", "Scheduler", " "):                         # a named human only
            with self.assertRaises(MC.PromotionRefused):
                book.resolve_block(blk["op_id"], self.ctl, actor=who, resolution=MC.ROLL_BACK, reason="r")
        with self.assertRaises(MC.PromotionRefused):                     # the promotion's config is not active
            book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_FORWARD, reason="r")
        self.assertEqual(len(book.blocked()), 1)
        # the promotion's version never went live: the operator closes it without undoing their own change
        book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_BACK, reason="stale baseline")
        self.assertEqual((book.blocked(), book.state("x1")), ([], MC.APPROVED))
        self.assertEqual(book.ops("x1")[0]["state"], MC.OP_ABORTED)
        self.assertEqual(K.read_active(self.ctl.path).version, third)
        res = [e for e in book.events("x1") if e["kind"] == "BLOCK_RESOLVED"]
        self.assertEqual((len(res), res[0]["actor"]), (1, "mike"))      # operator recovery is audited
        with self.assertRaises(MC.PromotionRefused) as cm:               # and the stale proposal cannot apply
            book.apply("x1", self.ctl, token=t, verifier=self.v)
        self.assertIn("stale_config", str(cm.exception))

    def test_12b_monitoring_divergence_and_revert_cannot_roll_forward(self):
        base = K.read_active(self.ctl.path)
        t = self.approved("y1")
        self.book.apply("y1", self.ctl, token=t, verifier=self.v)
        self.ctl.rollback(actor="mike", reason="manual rollback outside the book")
        rep = self.book.reconcile(self.ctl)
        self.assertEqual(rep["diverged"], ["y1"])
        (blk,) = self.book.blocked()
        with self.assertRaises(MC.PromotionRefused):
            self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_FORWARD, reason="r")
        self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_BACK, reason="agree")
        self.assertEqual(self.book.state("y1"), MC.REVERTED)
        self.assertIsNotNone(self.book.cooling_off_until())
        self.assertEqual(K.read_active(self.ctl.path).version, base.version)
        # a REVERT that cannot complete stays a revert: rolling forward is refused
        self.tearDown()
        self.setUp()
        t = self.approved("y2")
        self.book.apply("y2", self.ctl, token=t, verifier=self.v)
        bad = FailingControls(self.ctl.path, clock=self.clock, fail_on={"rollback"})
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.monitor("y2", [-3.0, -4.0], bad, sink=Sink())
        self.assertIn("auto_revert_not_completed:PENDING", str(cm.exception))
        self.assertIsNotNone(self.book.cooling_off_until())
        self.clock.advance(minutes=11)
        self.assertEqual(self.book.state("y2"), MC.MONITORING)
        self.book.reconcile(self.ctl)                                       # store healthy: revert completes
        self.assertEqual(self.book.state("y2"), MC.REVERTED)

    def test_12c_intended_version_activated_and_replaced_outside_the_book_blocks(self):
        t = self.approved("z1")
        self.book.fault_hook = FaultAt("apply:activated")
        with self.assertRaises(Crash):
            self.book.apply("z1", self.ctl, token=t, verifier=self.v)
        self.ctl.rollback(actor="mike", reason="someone undid it before reconciliation")
        self.clock.advance(minutes=11)
        self.assertEqual(list(self.restart().reconcile(self.ctl)["settled"].values()), [MC.OP_BLOCKED])
        (blk,) = self.book.blocked()
        self.assertIn("activated_and_replaced", MC.json.loads(blk["detail"])["blocked_because"])
        with self.assertRaises(MC.PromotionRefused):                     # it did go live: not "never active"
            self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_FORWARD, reason="r")
        self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_BACK, reason="undone")
        self.assertEqual(self.book.blocked(), [])

    def test_12d_active_config_not_the_intended_one_blocks(self):
        t = self.approved("z2")
        self.book.fault_hook = FaultAt("apply:activated")
        with self.assertRaises(Crash):
            self.book.apply("z2", self.ctl, token=t, verifier=self.v)
        v = K.read_active(self.ctl.path).version
        other = base_cfg(20.0)                                             # internally consistent, but not intended
        con = sqlite3.connect(self.ctl.path)
        con.execute("UPDATE template_config_versions SET config_json=?, config_sha256=? WHERE version=?",
                    (K.canonical(other), K.sha256(other), v))
        con.commit()
        con.close()
        self.assertEqual(K.read_active(self.ctl.path).source, K.SOURCE_STORE)
        self.clock.advance(minutes=11)
        self.assertEqual(list(self.restart().reconcile(self.ctl)["settled"].values()), [MC.OP_BLOCKED])
        self.assertIn("intended_sha256", MC.json.loads(self.book.blocked()[0]["detail"])["blocked_because"])
        (blk,) = self.book.blocked()
        with self.assertRaises(MC.PromotionRefused) as cm:             # the operator cannot "confirm" a config
            self.book.resolve_block(blk["op_id"], self.ctl, actor="mike",  # that is not the intended one
                                    resolution=MC.ROLL_FORWARD, reason="looks applied")
        self.assertIn("intended_version_is_not_active", str(cm.exception))
        self.assertEqual((len(self.book.blocked()), self.book.state("z2")), (1, MC.APPROVED))

    def test_12e_revert_that_lands_anywhere_but_the_pre_apply_version_blocks(self):
        base = K.read_active(self.ctl.path)
        t = self.approved("z3")
        v = self.book.apply("z3", self.ctl, token=t, verifier=self.v)
        x = self.ctl.propose(base_cfg(25.0), author="mike", reason="manual")
        self.ctl.approve(x, approver="mike", reason="manual")
        self.ctl.activate(x, actor="mike")
        self.ctl.rollback(actor="mike", reason="back to v")                # history: base, v, x, v
        self.assertEqual(K.read_active(self.ctl.path).version, v)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.monitor("z3", [-3.0, -4.0], self.ctl, sink=Sink())   # rollback would land on x, not base
        self.assertIn("auto_revert_not_completed:BLOCKED", str(cm.exception))
        (blk,) = self.book.blocked()
        self.assertNotEqual(K.read_active(self.ctl.path).version, base.version)
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_FORWARD, reason="r")
        self.assertIn("a_decay_revert_cannot_be_rolled_forward", str(cm.exception))
        self.assertEqual(self.book.state("z3"), MC.MONITORING)

    def test_config_changed_between_intent_and_activation_is_never_overwritten(self):
        t = self.approved("z5")
        ctl = self.ctl

        def operator_changes_controls(point):
            if point == "apply:version_approved":
                x = ctl.propose(base_cfg(25.0), author="mike", reason="concurrent manual change")
                ctl.approve(x, approver="mike", reason="manual")
                ctl.activate(x, actor="mike")
        self.book.fault_hook = operator_changes_controls
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("z5", self.ctl, token=t, verifier=self.v)
        self.assertIn("BLOCKED", str(cm.exception))
        self.assertEqual(K.read_active(self.ctl.path).config, base_cfg(25.0))  # the operator's change stands
        self.assertEqual(self.book.state("z5"), MC.APPROVED)

    def test_divergence_is_not_opened_for_a_proposal_that_moved_on(self):
        t = self.approved("z4")
        self.book.apply("z4", self.ctl, token=t, verifier=self.v)
        stale = self.book._row("z4")
        self.book.monitor("z4", [-3.0, -4.0], self.ctl, sink=Sink())       # REVERTED meanwhile
        self.book._open_divergence(stale, K.read_active(self.ctl.path))
        self.assertEqual(self.book.blocked(), [])

    def test_13_compare_and_swap_closes_the_check_then_activate_window(self):
        base = K.read_active(self.ctl.path)
        t = self.approved("w1")
        ctl = self.ctl

        def operator_changes_controls(point):                            # lands AFTER the pre-activate check
            if point == "apply:checked_before_activate":
                x = ctl.propose(base_cfg(25.0), author="mike", reason="concurrent manual change")
                ctl.approve(x, approver="mike", reason="manual")
                ctl.activate(x, actor="mike")
        self.book.fault_hook = operator_changes_controls
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("w1", self.ctl, token=t, verifier=self.v)
        self.assertIn("BLOCKED", str(cm.exception))
        self.assertEqual(K.read_active(self.ctl.path).config, base_cfg(25.0))  # never overwritten
        self.assertIn("ACTIVATE_REFUSED", [a["action"] for a in self.ctl.audit()])
        self.assertEqual(self.book.state("w1"), MC.APPROVED)
        self.assertNotEqual(K.read_active(self.ctl.path).version, base.version)

    def test_14_an_activated_config_must_match_its_recorded_digest(self):
        t = self.approved("w2")
        path = self.ctl.path

        def tamper(point):
            if point == "apply:activated":
                v = K.read_active(path).version
                other = base_cfg(20.0)
                con = sqlite3.connect(path)
                con.execute("UPDATE template_config_versions SET config_json=?, config_sha256=? WHERE version=?",
                            (K.canonical(other), K.sha256(other), v))
                con.commit()
                con.close()
        self.book.fault_hook = tamper
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.apply("w2", self.ctl, token=t, verifier=self.v)
        self.assertIn("BLOCKED", str(cm.exception))                      # never a valid-looking MONITORING
        self.assertEqual(self.book.state("w2"), MC.APPROVED)
        self.assertEqual([e for e in self.book.events("w2") if e["kind"] == "APPLIED"], [])

    def test_15_recovery_refuses_an_activation_over_an_unexpected_version(self):
        t = self.approved("w3")
        self.book.fault_hook = FaultAt("apply:version_approved")
        with self.assertRaises(Crash):
            self.book.apply("w3", self.ctl, token=t, verifier=self.v)
        x = self.ctl.propose(base_cfg(25.0), author="mike", reason="manual")
        self.ctl.approve(x, approver="mike", reason="manual")
        self.ctl.activate(x, actor="mike")
        ours = [v for v in self.ctl.versions() if v["reason"].startswith("promotion w3")][0]["version"]
        self.ctl.activate(ours, actor="mike")                            # out-of-band activation over x
        self.clock.advance(minutes=11)
        self.assertEqual(list(self.restart().reconcile(self.ctl)["settled"].values()), [MC.OP_BLOCKED])
        (blk,) = self.book.blocked()
        self.assertIn("not_pre_apply", MC.json.loads(blk["detail"])["blocked_because"])
        self.assertEqual(self.book.state("w3"), MC.APPROVED)
        with self.assertRaises(MC.PromotionRefused):                     # its version IS live: cannot just close
            self.book.resolve_block(blk["op_id"], self.ctl, actor="mike", resolution=MC.ROLL_BACK, reason="r")
        self.assertEqual(len(self.book.blocked()), 1)

    def test_17_revert_never_overwrites_a_change_landing_after_its_check(self):
        t = self.approved("w5")
        self.book.apply("w5", self.ctl, token=t, verifier=self.v)
        ctl = self.ctl

        def operator_changes_controls(point):
            if point == "revert:checked_before_rollback":
                x = ctl.propose(base_cfg(25.0), author="mike", reason="concurrent manual change")
                ctl.approve(x, approver="mike", reason="manual")
                ctl.activate(x, actor="mike")
        self.book.fault_hook = operator_changes_controls
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.monitor("w5", [-3.0, -4.0], self.ctl, sink=Sink())
        self.assertIn("BLOCKED", str(cm.exception))
        self.assertEqual(K.read_active(self.ctl.path).config, base_cfg(25.0))  # the operator's change stands
        self.assertIsNotNone(self.book.cooling_off_until())

    def test_16_recovery_is_idempotent_across_repeated_restarts(self):
        t = self.approved("w4")
        self.book.fault_hook = FaultAt("apply:activated")
        with self.assertRaises(Crash):
            self.book.apply("w4", self.ctl, token=t, verifier=self.v)
        self.clock.advance(minutes=11)
        counts = []
        for _ in range(4):                                               # four restarts
            book = self.restart()
            book.reconcile(self.ctl, sink=Sink())
            counts.append((book.state("w4"), len(book.events()), len(self.ctl.audit()),
                           len([e for e in book.events() if e["kind"] == "APPLIED"])))
        self.assertEqual(counts[0][0], MC.MONITORING)
        self.assertEqual(len(set(counts)), 1, counts)                    # no further writes after the first

    def test_record_shadow_checks_state_and_tokens_cannot_be_reused(self):
        cur = K.read_active(self.ctl.path).config
        self.book.submit(prop("s1", cur, base_cfg(31.0)))                  # CLASSIFIED, not SHADOW
        with self.assertRaises(MC.PromotionRefused) as cm:
            self.book.record_shadow("s1", EV.compare_shadow(shadow(3), min_signals=20), actor="c")
        self.assertIn("not_in_shadow:CLASSIFIED", str(cm.exception))
        self.assertNotIn("SHADOW_INCONCLUSIVE", [e["kind"] for e in self.book.events("s1")])
        self.book.withdraw("s1", actor="mike", reason="x")
        with self.assertRaises(MC.PromotionRefused):
            self.book.record_shadow("s1", EV.compare_shadow(shadow(3), min_signals=20), actor="c")
        t1 = self.approved("s2")
        self.book.apply("s2", self.ctl, token=t1, verifier=self.v)
        self.book.monitor("s2", [0.3] * 30, self.ctl, sink=Sink())
        self.clock.advance(days=8)
        self.to_eligible(prop("s3", K.read_active(self.ctl.path).config, base_cfg(30.0)))
        with self.assertRaises(MC.PromotionRefused):
            self.book.approve("s3", t1, self.v)                            # s2's token is bound to s2
        with self.assertRaises(MC.PromotionRefused):
            self.book.submit(prop("s2", K.read_active(self.ctl.path).config, base_cfg(30.0)))  # id reuse

    def test_database_level_guards(self):
        cur = K.read_active(self.ctl.path).config
        self.book.submit(prop("g1", cur, base_cfg(31.0)))
        con = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO promotion_proposals VALUES ('g2','t','a','tune','GREEN','SHADOW','d','{}','{}',"
                        "NULL,NULL)")
        con.execute("INSERT INTO promotion_ops VALUES ('o1','g1','APPLY',1,'PENDING',1,NULL,'s','r','m','t','t',"
                    "NULL,'{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO promotion_ops VALUES ('o2','g1','APPLY',2,'BLOCKED',1,NULL,'s','r','m','t','t',"
                        "NULL,'{}')")
        con.close()


class P8_Concurrency(CrossStore):
    def race(self, fns):
        barrier, out = threading.Barrier(len(fns)), [None] * len(fns)

        def run(i):
            barrier.wait()
            try:
                out[i] = ("ok", fns[i]())
            except MC.PromotionRefused as exc:
                out[i] = ("refused", str(exc))
        ts = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        return out

    def test_07_simultaneous_proposals_admit_exactly_one(self):
        cur = K.read_active(self.ctl.path).config
        fns = [(lambda i=i: MC.PromotionBook(self.path, clock=self.clock)
                .submit(prop("p%d" % i, cur, base_cfg(31.0 + i * 0.1)))) for i in range(6)]
        out = self.race(fns)
        self.assertEqual([o[0] for o in out].count("ok"), 1, out)
        self.assertEqual(len([e for e in self.book.events() if e["kind"] == "TRANSITION"]), 1)
        self.assertEqual(len(self.refusals()), 5)

    def test_08_simultaneous_approves_and_applies_change_once(self):
        self.to_eligible(prop("q1", K.read_active(self.ctl.path).config, base_cfg(36.0)))
        t = self.token("q1")
        out = self.race([lambda: MC.PromotionBook(self.path, clock=self.clock).approve("q1", t, self.v)] * 2)
        self.assertEqual([o[0] for o in out].count("ok"), 1, out)
        n = len(self.ctl.versions())
        out = self.race([lambda: MC.PromotionBook(self.path, clock=self.clock).apply(
            "q1", K.TemplateControls(self.ctl.path, clock=self.clock), token=t, verifier=self.v)] * 2)
        self.assertEqual([o[0] for o in out].count("ok"), 1, out)
        self.assertEqual(len(self.ctl.versions()), n + 1)
        self.assertEqual(len([e for e in self.book.events() if e["kind"] == "APPLIED"]), 1)
        self.assertEqual(self.book.state("q1"), MC.MONITORING)
        self.no_live_leftovers()


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

    def test_a_rollback_cannot_reach_exposure_or_protective_orders(self):
        """The controls store feeds only the template SHADOW runner: apply or
        revert changes shadow parameters, never routing, sizing, brackets or
        orders. Any new reader of the store must be reviewed here."""
        readers = set()
        for top in ("bot", "tools", "dashboard"):
            for p in (REPO / top).rglob("*.py"):
                t = p.read_text(encoding="utf-8", errors="ignore")
                if "bot.templates.controls" in t or "template_controls.db" in t:
                    readers.add(str(p.relative_to(REPO)))
        self.assertEqual({r for r in readers if not r.startswith(("bot/promotion/", "bot/templates/"))},
                         {"tools/paper_loop/m21_1extra_d2_3_scheduler.py"})
        sched = (REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py").read_text()
        i = sched.index("from bot.templates.controls import")
        self.assertIn("run_template_shadow", sched[i:i + 600])               # the shadow path, nothing else


if __name__ == "__main__":
    unittest.main()

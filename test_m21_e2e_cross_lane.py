"""test_m21_e2e_cross_lane.py — M21 cross-lane end-to-end test (integration tree only).

One deterministic chain across both lanes, on SYNTHETIC data (it describes
the machinery, not a market):

  R  start-stamped "store" bars -> WP11 detect + re-stamp (lookahead fix)
     -> WP13 walk-forward with WP12 economics -> PASS -> one-shot holdout
  L  WP8 bot templates reproduce the research signals on the same bars
     -> WP9 controls baseline -> WP14 proposal from the WP13 evidence
     -> shadow comparison on the same signals -> human token -> apply
     -> WP8 shadow params follow the applied controls -> monitoring confirm
  +  WP10 alarms on the paper journal; WP15 shadow context decision logged

Then 17 negative cases: every fail-closed point in the chain refuses the bad
input. No broker, no network, no production data; nothing is enabled.

E4 is the operational L chain on fixtures: scanner signal -> WP5 evidence ->
research score -> the REAL M14 gate on a temp signals DB -> WP7 ledger
(authenticated M14_LIVE) -> WS2 journal -> SIMULATED paper broker -> outcome,
with a forged decision id refused at the same point.

R1 (release candidate, E1-E7): IBKR-statement-proven paper trades flow from E5
(statement reconciliation) into E4 (score versus outcome) while journal-only,
unidentified-account and simulated trades never become evidence; every new
runtime path stays OFF and the E6 view has no write route.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot.bar_completion import select_completed
from bot.monitoring import checks as MCK
from bot.monitoring.alarms import MemorySink, Monitor
from bot.paper_loop_d2.candidates import LoopCandidate
from bot.promotion import evaluation as PEV
from bot.promotion import machine as PM
from bot.promotion import tiers as PT
from bot.promotion.approvals import ApprovalService
from bot.templates import controls as K
from bot.templates import core as TC
from bot.templates import routing as RT
from research.dataqa import readiness as RD
from research.dataqa import stamps as ST
from research.dataqa.calendar import synthetic_calendar
from research.economics.model import (CorporateActions, EconomicsModel, TradeSpec, liquidity_asof,
                                      missing_liquidity, price_trade, BorrowInfo)
from research.genome.schema import seed as seed_genome
from research.genome.templates import signals as research_signals
from research.integrity.ledger import Ledger, LedgerError
from research.optimiser import canary as CN
from research.optimiser import walkforward as W

REPO = Path(__file__).resolve().parent
UTC = timezone.utc
SVC = ApprovalService("operator-approvals", b"e2e-test-key-0123456789")
MR = ("mean_reversion",)


def to_start_stamped(bars):
    """What M16 stores: each daily bar stamped at the START of its date (00:00 here)."""
    return {s: b.assign(ts_utc=pd.to_datetime(b["ts_utc"], utc=True).dt.normalize()) for s, b in bars.items()}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class E1_PositiveChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        _, cls.truth_bars = CN.market(5004, n_symbols=30, n_sessions=750, template="mean_reversion", strength=0.5)
        days = [pd.Timestamp(b["ts_utc"].iloc[k]).date() for b in cls.truth_bars.values() for k in (0, -1)]
        cls.cal = synthetic_calendar(min(days), max(days))
        cls.store = to_start_stamped(cls.truth_bars)
        cls.conv, cls.conv_reports = {}, {}
        for s, b in cls.store.items():
            cls.conv[s], cls.conv_reports[s] = ST.to_close_stamped(b, "1D", cls.cal,
                                                                   ST.detect_convention(b["ts_utc"], "1D",
                                                                                        cls.cal).convention)
        cls.ledger = Ledger()
        cls.wf = W.run(cls.conv, W.WFConfig(templates=MR), ledger=cls.ledger, data_seed=5004)
        cls.r = cls.wf["templates"]["mean_reversion"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_1_readiness_restamps_store_bars_exactly(self):
        s = sorted(self.store)[0]
        self.assertEqual(ST.detect_convention(self.store[s]["ts_utc"], "1D", self.cal).convention, ST.START)
        pd.testing.assert_series_equal(self.conv[s]["ts_utc"].reset_index(drop=True),
                                       pd.to_datetime(self.truth_bars[s]["ts_utc"], utc=True).reset_index(drop=True),
                                       check_names=False)
        self.assertTrue((self.conv[s]["bar_open_ts"].dt.strftime("%H:%M") == "14:30").all())
        self.assertTrue(all(r.duplicate_close == 0 and r.dropped_inconsistent == 0 for r in self.conv_reports.values()))
        cov = RD.coverage_check(self.conv[s], "1D", self.cal)
        self.assertEqual(cov.verdict, RD.PASS)

    def test_2_walk_forward_passes_and_spends_the_holdout_once(self):
        self.assertEqual(self.r["verdict"], W.PASS, self.r["reasons"])
        self.assertIn(self.r["holdout"]["status"], (W.PASS, W.FAIL, W.INCONCLUSIVE))
        self.assertEqual(len(self.ledger.entries("historical_holdout_release", "wf-holdout")), 1)
        self.assertTrue(self.wf["ledger_ok"])
        self.assertEqual(self.wf["origin"], "synthetic")

    def test_3_bot_templates_reproduce_research_signals_on_the_converted_bars(self):
        g = seed_genome("mean_reversion", **{k: v for k, v in self.r["holdout"]["genome"].items()})
        for s in sorted(self.conv)[:5]:
            live = TC.signals("mean_reversion", self.conv[s], TC.validate_params("mean_reversion", g.p))
            ref = research_signals(g, self.conv[s])[list(TC.SIGNAL_COLUMNS)]
            pd.testing.assert_frame_equal(live, ref, check_exact=True)

    def test_4_promotion_applies_the_evidence_through_controls(self):
        clock = Clock(datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
        ctl = K.TemplateControls(os.path.join(self.tmp.name, "controls.db"), clock=clock)
        base = K.validate_config({"selected": ["scanner", "mean_reversion"], "champion": "scanner", "params": {}})
        v0 = ctl.propose(base, author="claude", reason="baseline")
        ctl.approve(v0, approver="mike", reason="baseline")
        ctl.activate(v0, actor="mike")
        chosen = TC.validate_params("mean_reversion", self.r["holdout"]["genome"])
        proposed = K.validate_config({**base, "params": {"mean_reversion": chosen}})
        evidence = {"wf_verdict": self.r["verdict"], "holdout_status": self.r["holdout"]["status"]}
        book = PM.PromotionBook(os.path.join(self.tmp.name, "promotion.db"), clock=clock)
        p = PT.Proposal("e2e-1", "tune", K.read_active(ctl.path).config, proposed, evidence, author="claude")
        tier = book.submit(p)
        dec = PT.classify(p)
        if not dec.eligible_evidence:                                    # e.g. a holdout that did not pass
            with self.assertRaises(PM.PromotionRefused):
                book.start_shadow("e2e-1", actor="claude")
            return
        book.start_shadow("e2e-1", actor="claude")
        # shadow: proposed vs current on the SAME signals (all OOS folds, WP12-priced)
        cfg, tl = W.WFConfig(templates=MR), W.timeline_of(self.conv)
        research_part = {s: b.loc[pd.to_datetime(b["ts_utc"], utc=True) < pd.Timestamp(self.wf["seal_ts_utc"])]
                         for s, b in self.conv.items()}
        rtl = W.timeline_of(research_part)
        cur_g, new_g = seed_genome("mean_reversion"), seed_genome("mean_reversion", **chosen)
        rs = {}
        for _, te in W.folds(len(rtl), cfg):
            for key, g in (("cur", cur_g), ("new", new_g)):
                for t, e in W._oos(g, research_part, rtl, te, cfg, None, None)["priced"]:
                    if e.priced:
                        rs.setdefault("%s@%s" % (t.symbol, t.decision_ts), {})[key] = e.net_r
        obs = [PEV.ShadowObservation(k, v.get("cur"), v.get("new")) for k, v in sorted(rs.items())]
        verdict = PEV.compare_shadow(obs, min_signals=PT.SHADOW_MIN_SIGNALS[tier])
        state = book.record_shadow("e2e-1", verdict, actor="claude")
        if state != PM.S_ELIGIBLE:
            self.assertIn(state, (PM.SHADOW, PM.S_INELIGIBLE))           # the gate held; nothing applied
            self.assertEqual(K.read_active(ctl.path).version, v0)
            return
        row = book._row("e2e-1")
        tok = SVC.issue(proposal_id="e2e-1", proposal_digest=row["digest"], tier=row["tier"], approver="mike",
                        issued=clock.t - timedelta(minutes=1), expires=clock.t + timedelta(days=7))
        book.approve("e2e-1", tok, SVC.verifier())
        v1 = book.apply("e2e-1", ctl, token=tok, verifier=SVC.verifier())
        self.assertEqual(K.read_active(ctl.path).config["params"]["mean_reversion"], chosen)
        self.assertEqual(K.shadow_params(K.read_active(ctl.path))["mean_reversion"], chosen)
        self.assertNotEqual(v1, v0)
        self.assertEqual(RT.LIVE_TEMPLATES, frozenset({"scanner"}))       # still shadow-only for orders
        self.assertEqual(book.monitor("e2e-1", [0.4, -0.2] * 15, ctl, sink=MemorySink()), PM.CONFIRMED)

    def test_6_shadow_ml_evidence_flows_into_the_promotion_gate(self):
        from research.shadowml import core as SM
        g = seed_genome("mean_reversion", **self.r["holdout"]["genome"])
        part = {s: self.conv[s] for s in sorted(self.conv)[:12]}
        ds = SM.template_dataset(part, g)
        cfg = SM.ShadowConfig(meta_model="logistic", baseline="prior")
        res = SM.lab(ds, cfg, models=("prior", "logistic"))
        meta = SM.meta_label(ds, cfg, res=res)
        self.assertFalse(meta["can_change_live"])
        self.assertLessEqual(meta["max_multiplier"], 1.0)
        base = K.validate_config({"selected": ["scanner", "mean_reversion"], "champion": "scanner", "params": {}})
        p = PT.Proposal("meta-1", "tune", base, K.validate_config({**base, "params": {"mean_reversion":
                        {"rsi_entry": 31.0}}}), meta["evidence"], author="claude")
        d = PT.classify(p)
        self.assertEqual(d.eligible_evidence, meta["evidence"]["wf_verdict"] == "PASS")

    def test_5_monitoring_and_context_shadow(self):
        now = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)
        mon = Monitor(os.path.join(self.tmp.name, "mon.db"), sinks=[MemorySink()], clock=lambda: now)
        ok = mon.evaluate([MCK.check_gateway({"as_of_utc": now.isoformat(), "expected_port": 4002,
                                              "status": MCK.GATEWAY_READY}, now)])
        self.assertEqual(ok, [])
        from bot.market_context import features as F
        from bot.market_context import throttle as TH
        from bot.market_context.store import ContextStore, Observation
        cs = ContextStore(os.path.join(self.tmp.name, "ctx.db"))
        for i, v in enumerate((18.0, 19.0, 21.0, 22.0, 26.0, 27.0)):
            cs.record(Observation("VIXCLS", date(2026, 10, 27) + timedelta(days=i), v,
                                  now - timedelta(days=6 - i), now, "FRED:VIXCLS", "FRED / CBOE terms", "assumed_lag"))
        spy = self.conv[sorted(self.conv)[0]].assign(
            ts_utc=pd.date_range(end=now - timedelta(hours=20), periods=len(self.conv[sorted(self.conv)[0]]),
                                 freq="B"))
        ctx = F.build_context(cs, now, index_bars={"SPY": spy}, sector_bars={}, universe_bars={}, events=[])
        d = TH.decide(ctx)
        self.assertIn("vix_high", d.reasons)
        self.assertLessEqual(d.multiplier, 0.75)
        cs.log_decision(d.to_json(), now)


class E2_NegativeCases(unittest.TestCase):
    """Every fail-closed point across the lanes refuses the bad input."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _, self.bars = CN.market(77, n_symbols=6, n_sessions=500)
        days = [pd.Timestamp(b["ts_utc"].iloc[k]).date() for b in self.bars.values() for k in (0, -1)]
        self.cal = synthetic_calendar(min(days), max(days))

    def tearDown(self):
        self.tmp.cleanup()

    def test_n01_unconverted_start_stamps_leak_and_are_caught(self):
        from bot.backtesting.mtf_context import MultiTimeframeContext
        b = to_start_stamped(self.bars)["S000"]
        t = pd.Timestamp(b["ts_utc"].iloc[300]) + pd.Timedelta(hours=16)        # mid-session
        q = pd.DataFrame({"ts_utc": [t - pd.Timedelta(minutes=15)]})
        leak = MultiTimeframeContext({"15m": q, "1D": b}, anchor_tf="15m").snapshot_at(t)["1D"]
        self.assertEqual(leak.idx, 300)                                  # today's bar visible: lookahead
        conv, _ = ST.to_close_stamped(b, "1D", self.cal, ST.START)
        safe = MultiTimeframeContext({"15m": q, "1D": conv}, anchor_tf="15m").snapshot_at(t)["1D"]
        self.assertEqual(safe.idx, 299)

    def test_n02_mixed_stamps_fail_readiness(self):
        b = self.bars["S000"]
        mixed = pd.concat([to_start_stamped({"x": b.iloc[:5]})["x"], b.iloc[10:15]])
        chk, use = RD.convention_check(mixed, "1D", self.cal, None)
        self.assertEqual((chk.verdict, use), (RD.FAIL, None))

    def test_n03_forming_bar_is_never_selected(self):
        b = self.bars["S000"].set_index("ts_utc")
        last = b.index[-1]
        sel, why = select_completed(b, "1D", lambda tf, st: ("forming", None) if st == last else ("complete", st))
        self.assertEqual(sel.index[-1], b.index[-2])
        none, why2 = select_completed(b, "1D", lambda tf, st: ("unknown", None))
        self.assertIsNone(none)

    def test_n04_missing_liquidity_is_never_free(self):
        t = price_trade(TradeSpec("S000", "long", 100, 50.0, 55.0, date(2018, 3, 1), date(2018, 3, 5)),
                        model=EconomicsModel(), entry_liq=missing_liquidity("S000"), actions=CorporateActions(True))
        self.assertEqual((t.status, t.priced, t.net_pnl), ("NO_FILL", False, 0.0))

    def test_n05_short_without_borrow_is_refused(self):
        liq = liquidity_asof(self.bars["S000"], self.bars["S000"]["ts_utc"].iloc[-1])
        t = price_trade(TradeSpec("S000", "short", 100, 50.0, 45.0, date(2018, 3, 1), date(2018, 3, 5)),
                        model=EconomicsModel(), entry_liq=liq, borrow=BorrowInfo("unknown"),
                        actions=CorporateActions(True))
        self.assertEqual(t.reasons, ("borrow_unknown",))

    def test_n06_raw_prices_without_corporate_actions_are_inconclusive(self):
        liq = liquidity_asof(self.bars["S000"], self.bars["S000"]["ts_utc"].iloc[-1])
        t = price_trade(TradeSpec("S000", "long", 10, 50.0, 55.0, date(2018, 3, 1), date(2018, 3, 5)),
                        model=EconomicsModel(), entry_liq=liq, actions=CorporateActions(False), basis="raw")
        self.assertEqual(t.status, "INCONCLUSIVE")

    def test_n07_templates_2_to_4_cannot_reach_a_broker(self):
        sig = TC.TemplateSignal("mean_reversion", 1, TC.strategy_id("mean_reversion"), "d" * 12, "S000", "long",
                                "t", True, True, "", 100.0, 96.0, 106.0, 2.0, 1.0)
        with self.assertRaises(RT.TemplateNotLive):
            RT.to_loop_candidate(sig, score_100=99.0)

    def test_n08_conflicting_candidates_are_both_dropped(self):
        mk = lambda d: LoopCandidate.build(symbol="AAA", direction=d, score=90.0, strategy_id="scanner_v3",
                                           entry_ref_price=100.0, stop_loss=95.0 if d == "long" else 105.0,
                                           take_profit=110.0 if d == "long" else 90.0)
        out, notes = RT.merge_tradable([mk("long"), mk("short")])
        self.assertEqual(out, [])
        self.assertTrue(notes[0].startswith("conflict_dropped:AAA"))

    def test_n09_automation_cannot_approve_controls(self):
        ctl = K.TemplateControls(os.path.join(self.tmp.name, "c.db"), clock=Clock(datetime(2026, 11, 2, tzinfo=UTC)))
        v = ctl.propose({"selected": ["scanner"], "champion": "scanner"}, author="claude", reason="r")
        with self.assertRaises(K.ControlError):
            ctl.approve(v, approver="claude", reason="self")

    def test_n10_thin_walk_forward_evidence_cannot_start_a_promotion(self):
        out = W.run(self.bars, W.WFConfig(templates=MR, min_oos_trades=10**6), ledger=Ledger(), data_seed=77)
        r = out["templates"]["mean_reversion"]
        self.assertIn(r["verdict"], (W.INCONCLUSIVE, W.FAIL))
        book = PM.PromotionBook(os.path.join(self.tmp.name, "p.db"), clock=Clock(datetime(2026, 11, 2, tzinfo=UTC)))
        base = K.validate_config({"selected": ["scanner", "mean_reversion"], "champion": "scanner", "params": {}})
        new = K.validate_config({**base, "params": {"mean_reversion": {"rsi_entry": 31.0}}})
        book.submit(PT.Proposal("t1", "tune", base, new, {"wf_verdict": r["verdict"]}, author="claude"))
        with self.assertRaises(PM.PromotionRefused):
            book.start_shadow("t1", actor="claude")

    def _applied(self, pid="a1"):
        clock = Clock(datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
        ctl = K.TemplateControls(os.path.join(self.tmp.name, "ctl_%s.db" % pid), clock=clock)
        base = K.validate_config({"selected": ["scanner", "mean_reversion"], "champion": "scanner",
                                  "params": {"mean_reversion": {"rsi_entry": 30.0}}})
        v = ctl.propose(base, author="claude", reason="b")
        ctl.approve(v, approver="mike", reason="b")
        ctl.activate(v, actor="mike")
        book = PM.PromotionBook(os.path.join(self.tmp.name, "pb_%s.db" % pid), clock=clock)
        new = K.validate_config({**base, "params": {"mean_reversion": {"rsi_entry": 36.0}}})
        book.submit(PT.Proposal(pid, "tune", base, new, {"wf_verdict": "PASS"}, author="claude"))
        book.start_shadow(pid, actor="claude")
        book.record_shadow(pid, PEV.compare_shadow([PEV.ShadowObservation("s%d" % i, 0.1, 0.3) for i in range(60)],
                                                   min_signals=50), actor="claude")
        row = book._row(pid)
        tok = SVC.issue(proposal_id=pid, proposal_digest=row["digest"], tier=row["tier"], approver="mike",
                        issued=clock.t - timedelta(minutes=1), expires=clock.t + timedelta(days=30))
        book.approve(pid, tok, SVC.verifier())
        book.apply(pid, ctl, token=tok, verifier=SVC.verifier())
        return clock, ctl, book, base, tok

    def test_n11_forged_token_is_refused(self):
        clock, ctl, book, base, _ = self._applied("f")
        forger = ApprovalService("operator-approvals", b"x" * 32)
        book.monitor("f", [0.3] * 30, ctl, sink=MemorySink())
        new = K.validate_config({**base, "params": {"mean_reversion": {"rsi_entry": 37.0}}})
        book.submit(PT.Proposal("f2", "tune", K.read_active(ctl.path).config, new, {"wf_verdict": "PASS"},
                                author="claude"))
        book.start_shadow("f2", actor="claude")
        book.record_shadow("f2", PEV.compare_shadow([PEV.ShadowObservation("s%d" % i, 0.1, 0.3) for i in range(30)],
                                                    min_signals=20), actor="claude")
        bad = forger.issue(proposal_id="f2", proposal_digest=book._row("f2")["digest"], tier="RED", approver="mike",
                           issued=clock.t - timedelta(minutes=1), expires=clock.t + timedelta(days=1))
        with self.assertRaises(PM.PromotionRefused):
            book.approve("f2", bad, SVC.verifier())

    def test_n12_second_apply_within_seven_days_is_refused(self):
        clock, ctl, book, base, _ = self._applied("w")
        book.monitor("w", [0.3] * 30, ctl, sink=MemorySink())
        new = K.validate_config({**base, "params": {"mean_reversion": {"rsi_entry": 37.0}}})
        book.submit(PT.Proposal("w2", "tune", K.read_active(ctl.path).config, new, {"wf_verdict": "PASS"},
                                author="claude"))
        book.start_shadow("w2", actor="claude")
        book.record_shadow("w2", PEV.compare_shadow([PEV.ShadowObservation("s%d" % i, 0.1, 0.3) for i in range(30)],
                                                    min_signals=20), actor="claude")
        row = book._row("w2")
        tok = SVC.issue(proposal_id="w2", proposal_digest=row["digest"], tier=row["tier"], approver="mike",
                        issued=clock.t - timedelta(minutes=1), expires=clock.t + timedelta(days=30))
        book.approve("w2", tok, SVC.verifier())
        with self.assertRaises(PM.PromotionRefused):
            book.apply("w2", ctl, token=tok, verifier=SVC.verifier())

    def test_n13_decay_reverts_config_never_orders(self):
        clock, ctl, book, base, _ = self._applied("d")
        sink = MemorySink()
        self.assertEqual(book.monitor("d", [-0.6, 0.1] * 6, ctl, sink=sink), PM.REVERTED)
        self.assertEqual(K.read_active(ctl.path).config, base)
        self.assertIs(sink.events[0]["cancel_orders"], False)

    def test_n14_holdout_cannot_be_reused(self):
        L = Ledger()
        sh = W.SealedHistory(L, "h", self.bars, W.timeline_of(self.bars)[400])
        sh.release("wf:mean_reversion", "g")
        with self.assertRaises(LedgerError):
            sh.release("wf:mean_reversion", "g2")

    def test_n15_context_outage_abstains_and_never_unblocks(self):
        from bot.market_context import features as F
        from bot.market_context import throttle as TH
        from bot.market_context.store import ContextStore
        cs = ContextStore(os.path.join(self.tmp.name, "ctx.db"))
        now = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)
        d = TH.decide(F.build_context(cs, now, index_bars={}, sector_bars={}, universe_bars={}, events=None))
        self.assertTrue(d.abstain)
        self.assertEqual(TH.apply_to(d, allowed=False, size=1.0), (False, 0.0))
        self.assertFalse(d.can_grant_authority)

    def test_n16_live_gateway_and_unknown_health_alarm(self):
        now = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)
        sink = MemorySink()
        mon = Monitor(os.path.join(self.tmp.name, "m.db"), sinks=[sink], clock=lambda: now)
        live = MCK.check_gateway({"as_of_utc": now.isoformat(), "expected_port": 4001, "status": MCK.GATEWAY_READY},
                                 now)
        unknown = MCK.check_duplicate_orders([], None)
        out = mon.evaluate([live, unknown])
        self.assertEqual(sorted(o["transition"] for o in out), ["RAISED", "RAISED"])
        self.assertEqual({e["severity"] for e in sink.events}, {MCK.CRITICAL})

    def test_n17_enabled_ledger_that_cannot_record_blocks_the_entry(self):
        from test_m21_1_wp7_ledger_wiring import LedgerDB, with_ledger
        from test_m21_1extra_d2_3_learning_loop import LoopHarness
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        db.down = True
        r = h.tick()
        self.assertEqual(r["entry_block"], "ledger_audit_unavailable")
        self.assertEqual(h.submits(), 0)


class E4_OperationalChain(unittest.TestCase):
    """Scanner -> evidence -> scoring -> M14 -> ledger -> journal -> broker sim -> outcome, one id."""

    def setUp(self):
        from unittest import mock
        p = mock.patch.dict(os.environ, {"ALPACA_KEY": "test-key", "ALPACA_SECRET": "test-secret"})
        p.start()
        self.addCleanup(p.stop)

    def chain(self, forge=False, min_score=30.0):
        import sqlite3
        from unittest import mock
        import test_m14_f_preflight as F
        import test_m21_1_wp5_evidence as W5
        import test_m21_1_wp7_ledger_wiring as W7
        import test_m21_1extra_d2_3_learning_loop as D23
        from bot.broker_allocation import save_policy
        from bot.candidate_ledger.loop_sink import LoopLedger
        from bot.paper_loop_d2.risk_gate import make_risk_authority_gate
        from bot.paper_loop_d2.scheduler import LoopConfig
        from bot.risk_authority import engine as ENG
        from tools.signal_scoring.evidence import loop_candidates, rank_candidates, record_scored

        ranked = rank_candidates([W5.sig("AAA")], W5.evidence(symbols=("AAA",)))   # scanner signal + evidence
        self.assertTrue(ranked[0].selectable, ranked[0].block_reasons)
        cands, notes = loop_candidates(ranked)
        self.assertEqual((len(cands), notes), (1, []))
        c = cands[0]
        ev = datetime.fromisoformat(W5.EV)
        fx = F._DB()
        self.addCleanup(fx.cleanup)
        with fx.conn() as conn:
            save_policy(conn, F._valid_policy())
            F._seed_fresh_all_scopes(conn)
        # The loop's min_score gates the M19 research rank (0..100); this clean fixture ranks ~33.
        h = D23.LoopHarness(self, candidates=[c], prices={"AAA": c.entry_ref_price}, once=True,
                            config=LoopConfig(interval_minutes=15, max_hold_minutes=600, min_score=min_score))
        h.clock.t = ev

        class _At(datetime):                                   # the engine's wall clock = the tick
            @classmethod
            def now(cls, tz=None):
                return h.clock()
        pt = mock.patch.object(ENG, "datetime", _At)
        pt.start()
        self.addCleanup(pt.stop)
        ms = D23.market_state(now=ev, ts="2026-11-03T16:05:00Z", next_close="2026-11-03T21:00:00Z",
                              next_open="2026-11-04T14:30:00Z",
                              cal=({"date": "2026-11-03", "open": "09:30", "close": "16:00"},))
        real = make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(fx.path), market_state=ms,
                                        clock=lambda: ev)

        def gate(**kw):
            ok, why, did = real(**kw)
            return ok, why, ("forged-" + str(did) if forge else did)
        db = W7.LedgerDB(self)
        record_scored(db.writer(), ranked, cycle_key="scan:e4", now=ev)
        h.loop = h.make_loop(risk_gate=gate, ledger=LoopLedger(db.writer(), m14_db=fx.path))
        return h, db, c, fx

    def test_scanner_signal_to_outcome_through_the_real_gate(self):
        import sqlite3
        h, db, c, fx = self.chain()
        r1 = h.tick()
        self.assertEqual(r1["entry"]["state"], "SUBMITTED")
        o = h.orders()[0]
        self.assertEqual(o["candidate_id"], c.candidate_id)                    # one id, scan to outcome
        p = db.payload(c.candidate_id, "risk_evaluated")
        self.assertEqual((p["verdict"], p["evidence"], p["risk_decision_id"]),
                         ("pass", "M14_LIVE", o["risk_decision_id"]))
        with sqlite3.connect(fx.path) as m:
            self.assertEqual(m.execute("SELECT result, broker_scope, requested_action FROM risk_decisions "
                                       "WHERE decision_id=?", (o["risk_decision_id"],)).fetchone(),
                             ("allow", "ibkr_paper", "trade_open"))
        self.assertEqual(db.payload(c.candidate_id, "scored")["selectable"], True)
        h.tick(15)
        h.prices["AAA"] = c.take_profit + 1.0
        r3 = h.tick(15)
        self.assertEqual(r3["closed"][0]["exit_reason"], "take_profit")
        self.assertEqual(db.stages(c.candidate_id), ["scanned", "scored", "risk_evaluated", "intent_logged",
                                                     "broker_result", "outcome_resolved", "closed"])
        out = db.payload(c.candidate_id, "outcome_resolved")
        self.assertEqual((out["outcome"], out["data_source"]), ("WIN", "simulated"))
        acc, cons = db.report(h.db)
        self.assertTrue(acc.ok, acc.violations)
        self.assertTrue(cons.ok, cons.problems)
        self.assertEqual(h.submits(), 1)

    def test_the_default_threshold_applies_to_the_research_rank(self):
        h, db, c, _ = self.chain(min_score=60.0)
        self.assertLess(c.score, 60.0)
        r = h.tick()
        self.assertEqual(r["entry_block"], "no_candidate")
        self.assertEqual(h.submits(), 0)
        self.assertNotIn("risk_evaluated", db.stages(c.candidate_id))

    def test_an_unauthenticated_decision_never_reaches_the_broker(self):
        h, db, c, _ = self.chain(forge=True)
        r = h.tick()
        self.assertIsNotNone(r["entry_block"])
        self.assertEqual(h.submits(), 0)
        p = db.payload(c.candidate_id, "risk_evaluated")
        self.assertEqual((p["evidence"], p["evidence_reason"]), ("UNVERIFIED", "decision_not_in_m14_store"))
        self.assertNotIn("intent_logged", db.stages(c.candidate_id))


class E3_Determinism(unittest.TestCase):
    def test_the_research_chain_is_deterministic(self):
        _, bars = CN.market(78, n_symbols=6, n_sessions=500)
        a = W.run(bars, W.WFConfig(templates=MR), ledger=Ledger(), data_seed=78)
        b = W.run(bars, W.WFConfig(templates=MR), ledger=Ledger(), data_seed=78)
        self.assertEqual(a["templates"], b["templates"])
        self.assertEqual(a["seal_ts_utc"], b["seal_ts_utc"])


if __name__ == "__main__":
    unittest.main()


# ── M21 E1-E7 release candidate: cross-lane chain and default-OFF checks ────
class R1_ReleaseCandidateChain(unittest.TestCase):
    """Statement-proven IBKR paper trades flow from E5 (statement reconciliation) into E4
    (score versus outcome); journal-only and simulated trades never become evidence; and every
    new runtime path added in E1-E7 stays OFF by default. Fixtures only; made-up account ids."""

    T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)

    def _fixture(self, d):
        import sqlite3
        from bot.candidate_ledger import CandidateLedgerWriter, migrate
        from bot.paper_loop_d2.journal import LoopJournal
        jpath, lpath = os.path.join(d, "journal.db"), os.path.join(d, "ledger.db")
        LoopJournal(jpath)
        with sqlite3.connect(lpath) as c:
            migrate(c, now_utc=self.T0.isoformat())
        w = CandidateLedgerWriter(lambda: sqlite3.connect(lpath), enabled=True)
        trades = [("R1", "AAA", 72.0, 1.5, "ibkr_paper"), ("R2", "BBB", 65.0, -1.0, "ibkr_paper"),
                  ("R3", "CCC", 81.0, 2.0, "ibkr_paper"), ("R4", "DDD", 90.0, 3.0, "simulated"),
                  ("R5", "EEE", 70.0, 1.0, "ibkr_paper")]
        rows = []
        jc = sqlite3.connect(jpath)
        for i, (ref, sym, score, r, src) in enumerate(trades):
            t_in, t_out = self.T0 + timedelta(minutes=i), self.T0 + timedelta(hours=2, minutes=i)
            oref = "M21L-%s" % ref
            with jc:
                jc.execute("INSERT INTO loop_orders (client_ref, cycle_key, symbol, direction, qty, entry_ref_price, "
                           "state, data_source, planned_at_utc, updated_at_utc, entry_fill_price, entry_filled_at_utc, "
                           "entry_fees, exit_fill_price, exit_filled_at_utc, exit_fees, order_ref) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (ref, "scan:x", sym, "long", 10, 50.0, "CLOSED", src, t_in.isoformat(), t_out.isoformat(),
                            50.0, t_in.isoformat(), 1.0, 50.0 + r, t_out.isoformat(), 1.0, oref))
            at = t_in.isoformat()
            cid = w.open_candidate(symbol=sym, direction="long", strategy_id="scanner:v1", decision_ts_utc=at,
                                   cycle_key="scan:%s" % at, scanner_stage="final_signal", at_utc=at)
            w.record(cid, "scored", at_utc=at, payload={"research_rank": score, "selectable": True})
            w.record(cid, "risk_evaluated", at_utc=at, payload={"verdict": "pass", "evidence": "M14_LIVE"})
            w.record(cid, "intent_logged", at_utc=at, payload={"data_source": src})
            w.record(cid, "broker_result", at_utc=at, payload={"status": "accepted", "data_source": src})
            w.record(cid, "outcome_resolved", at_utc=t_out.isoformat(), payload={
                "outcome": "WIN" if r > 0 else "LOSS", "trade_id": ref, "r_multiple": r, "fees_final": 1,
                "realized_pnl": 10 * r, "data_source": src})
            w.close(cid, "outcome_resolved", at_utc=t_out.isoformat())
            if ref in ("R1", "R2", "R5"):    # R3 never reached the statement; R4 is simulated; R5 has no account id
                acct = "" if ref == "R5" else "DUX0000001"
                ny = lambda t: (t - timedelta(hours=5)).strftime("%Y%m%d;%H%M%S")      # EST in November
                rows.append((acct, "STK", sym, "BUY", "10", "50", "-1", "USD", ny(t_in), oref, "X%sa" % ref))
                rows.append((acct, "STK", sym, "SELL", "-10", "%g" % (50.0 + r), "-1", "USD", ny(t_out), oref,
                             "X%sb" % ref))
        jc.close()
        stmt = os.path.join(d, "flex.csv")
        with open(stmt, "w") as fh:
            fh.write("ClientAccountID,AssetClass,Symbol,Buy/Sell,Quantity,TradePrice,IBCommission,"
                     "IBCommissionCurrency,DateTime,OrderReference,IBExecID\n")
            fh.writelines(",".join(r) + "\n" for r in rows)
        return jpath, lpath, stmt

    def test_statement_proof_flows_from_reconciliation_into_score_vs_outcome(self):
        import json
        import sqlite3
        from research.economics import statement as E5
        from tools.candidate_ledger import score_outcome as E4
        with tempfile.TemporaryDirectory() as d:
            jpath, lpath, stmt = self._fixture(d)
            files = [E5.parse_statement(stmt, tz="America/New_York")]
            claims, stats = E5.ws2_claims(jpath)
            rec = E5.build_report(files, claims, {"ws2_journal": stats}, tz="America/New_York")
            self.assertEqual(rec["matched_trade_ids"], ["R1", "R2"])
            self.assertEqual(rec["counts"]["UNMATCHED_CLAIM"], 2)                 # both legs of R3
            self.assertEqual(rec["counts"]["MISMATCH"], 2)                        # R5: account unverified
            self.assertIn("ACCOUNT_UNVERIFIED_EXECUTIONS", [a["alert"] for a in rec["alerts"]])
            self.assertEqual(stats["orders_simulated_excluded"], 1)
            recon = os.path.join(d, "recon.json")
            with open(recon, "w") as fh:
                json.dump(rec, fh)
            out = os.path.join(d, "so.json")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(E4.main(["--ledger-db", lpath, "--statement-matches", recon, "--out-json", out]), 0)
            with open(out) as fh:
                so = json.load(fh)
            cls = so["classes"]
            self.assertEqual(cls[E4.IBKR_MATCHED]["resolved_trades"], 2)
            self.assertEqual(cls[E4.IBKR_MATCHED]["verdict"], E4.INSUFFICIENT)    # 2 < 30: no claim at all
            self.assertEqual((cls[E4.IBKR_PAPER]["resolved_trades"], cls[E4.IBKR_PAPER]["verdict"]),
                             (2, E4.NOT_CONFIRMED))                       # R3 and the unverified-account R5
            self.assertEqual((cls[E4.SIMULATED]["resolved_trades"], cls[E4.SIMULATED]["verdict"]),
                             (1, E4.NOT_MARKET))
            # the reconciliation and the report only read their inputs
            c = sqlite3.connect("file:%s?mode=ro" % lpath, uri=True)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM candidate_ledger").fetchone()[0], 5)
            c.close()

    def test_new_runtime_paths_are_off_by_default(self):
        from unittest import mock
        import sqlite3
        from bot.candidate_ledger import CandidateLedgerWriter
        from tools.paper_loop import context_audit as CA
        from tools.paper_loop import m21_1extra_d2_3_scheduler as SCH
        from dashboard import template_controls_view as TV
        clean = {k: v for k, v in os.environ.items()
                 if k not in (CA.FLAG, TV.DB_KEY)}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertFalse(CA.enabled_from_env())
            self.assertIsNone(SCH._context_audit())                                # E2 shadow record OFF
            self.assertFalse(CandidateLedgerWriter(lambda: sqlite3.connect(":memory:")).enabled)
            from flask import Flask
            app = Flask("r1")
            with app.app_context():
                self.assertIsNone(TV._path())                                      # E6: 503 not_configured
            app.register_blueprint(TV.bp)
            self.assertEqual(sorted((r.rule, sorted(r.methods - {"HEAD", "OPTIONS"}))      # E6: read-only
                                    for r in app.url_map.iter_rules() if r.endpoint.startswith("template_controls.")),
                             [("/api/template-controls", ["GET"]), ("/template-controls", ["GET"])])
        self.assertNotIn("template_controls_view", (REPO / "dashboard" / "app.py").read_text())
        self.assertEqual(RT.LIVE_TEMPLATES, frozenset({"scanner"}))

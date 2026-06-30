#!/usr/bin/env python3
"""M21.1extra-A — tests for the simulation-only run-once paper loop.

Covers ChatGPT's required proofs (1-12):
  1. research candidate (only model-readiness/calibration review) -> eligible
  2. eligible candidate still has execution_eligible=False, hard_gate_passed=False
  3. missing-liquidity candidate rejected
  4. stale-data candidate rejected
  5. risk-authority-blocked candidate rejected
  6. PIT/adjusted-price-risk candidate rejected
  7. kill switch active -> no simulated order opens
  8. duplicate signal cannot double-open / double-record
  9. SL close -> loss with correct realised PnL and R-multiple
 10. TP close -> win with correct realised PnL and R-multiple
 11. summary includes all required fields
 12. AST import guard: no eToro / ibkr_broker / paper_broker submit / live /
     Telegram / main.py / scheduler
"""
from __future__ import annotations

import ast
import dataclasses
import unittest

from bot.signal_scoring import (
    ScoringProfile, score_candidate, default_config,
)
from bot.signal_scoring import keys as K
from tools.signal_scoring.scanner_bridge import enrich_signal, score_signal
from bot.runtime.m21_1extra_research_paper_decision import (
    decide_research_paper_eligibility,
)
from tools.paper_loop import m21_1extra_a_run_once as A

_TS = "2026-06-26T15:00:00+00:00"


def _sig(symbol="AAA", entry=100.0, stop=95.0, target=115.0, rsi=62.0,
         macd=0.9, vc=4, atr=2.0, vr=1.4, direction="long", with_vol=True):
    d = dict(timestamp=_TS, symbol=symbol, direction=direction,
             entry_price=entry, stop_loss=stop, target_price=target, rsi=rsi,
             macd_hist=macd, vol_ratio=vr, valid_count=vc, available_tfs=4,
             atr=atr)
    if with_vol:
        d["avg_volume_20d"] = 500000
    return d


def _clean_research_scored(symbol="AAA"):
    return score_signal(_sig(symbol), profile=ScoringProfile.RESEARCH,
                        avg_dollar_volume=100.0 * 500000)


class TestEligibilityRule(unittest.TestCase):
    def test_1_research_candidate_is_eligible(self):
        d = decide_research_paper_eligibility(_clean_research_scored())
        self.assertTrue(d.research_paper_eligible)
        self.assertEqual(d.reason, "research_paper_eligible")

    def test_2_eligible_candidate_keeps_m19_truth(self):
        sc = _clean_research_scored()
        self.assertFalse(sc.execution_eligible)
        self.assertFalse(sc.hard_gate_passed)
        # eligibility decision did not mutate the candidate
        decide_research_paper_eligibility(sc)
        self.assertFalse(sc.execution_eligible)
        self.assertFalse(sc.hard_gate_passed)

    def test_3_missing_liquidity_rejected(self):
        # scanner-shaped, no avg_volume, no liquidity supplied
        sc = score_signal(_sig(with_vol=False), profile=ScoringProfile.RESEARCH)
        d = decide_research_paper_eligibility(sc)
        self.assertFalse(d.research_paper_eligible)
        self.assertEqual(d.reason, "real_safety_block")
        self.assertIn("missing_context_key", d.rejected_blocks)

    def test_4_stale_data_rejected(self):
        ci = enrich_signal(_sig(), avg_dollar_volume=5e7, stale=True,
                           data_freshness_minutes=999)
        sc = score_candidate(ci, default_config(profile=ScoringProfile.RESEARCH))
        d = decide_research_paper_eligibility(sc)
        self.assertFalse(d.research_paper_eligible)
        self.assertIn("stale_data", d.rejected_blocks)

    def test_5_risk_authority_blocked_rejected(self):
        ci = enrich_signal(_sig(), avg_dollar_volume=5e7)
        rp = dict(ci.risk_preview)
        rp["risk_authority_status"] = "blocked"
        ci = dataclasses.replace(ci, risk_preview=rp)
        sc = score_candidate(ci, default_config(profile=ScoringProfile.RESEARCH))
        d = decide_research_paper_eligibility(sc)
        self.assertFalse(d.research_paper_eligible)
        self.assertTrue(d.rejected_blocks)  # a real block is present

    def test_6_pit_adjusted_price_risk_rejected(self):
        ci = enrich_signal(_sig(), avg_dollar_volume=5e7)
        ml = dict(ci.ml_context)
        ml["price_adjustment_mode"] = "adjusted"
        ml["allow_adjusted_prices_for_ml"] = False
        ci = dataclasses.replace(ci, ml_context=ml)
        sc = score_candidate(ci, default_config(profile=ScoringProfile.RESEARCH))
        d = decide_research_paper_eligibility(sc)
        self.assertFalse(d.research_paper_eligible)
        self.assertIn("adjusted_price_pit_risk", d.rejected_blocks)

    def test_short_side_rejected(self):
        sc = score_signal(_sig(direction="short"),
                          profile=ScoringProfile.RESEARCH,
                          avg_dollar_volume=5e7)
        d = decide_research_paper_eligibility(sc)
        self.assertFalse(d.research_paper_eligible)
        self.assertEqual(d.reason, "not_long_side")


class TestKillSwitchAndIdempotency(unittest.TestCase):
    def test_7_kill_switch_blocks_open(self):
        summary = A.run_once(A.fixture_signals(),
                             exit_plan=A._fixture_exit_plan(),
                             kill_switch_active=True)
        self.assertEqual(summary.opened_positions, 0)
        self.assertEqual(summary.closed_positions, 0)
        self.assertTrue(all(
            o.rejection_reason == "kill_switch_active"
            for o in summary.outcomes if o.stage == "rejected"))

    def test_8_duplicate_signal_not_double_opened(self):
        sigs = A.fixture_signals()
        dup = sigs + [dict(sigs[0])]  # same symbol+timestamp again
        summary = A.run_once(dup, exit_plan=A._fixture_exit_plan(),
                            kill_switch_active=False)
        # 3 in, but the duplicate is rejected; only 2 unique opened
        self.assertEqual(summary.signals_in, 3)
        self.assertEqual(summary.opened_positions, 2)
        self.assertTrue(any(o.rejection_reason == "duplicate_signal"
                            for o in summary.outcomes))


class TestCloseOutcomes(unittest.TestCase):
    def test_9_sl_close_is_loss_with_correct_pnl_and_r(self):
        summary = A.run_once([_sig("LOSER", entry=50.0, stop=48.0,
                                    target=56.0)],
                            exit_plan={"LOSER": "SL"}, kill_switch_active=False)
        o = [x for x in summary.outcomes if x.symbol == "LOSER"][0]
        self.assertEqual(o.exit_reason, "SL")
        self.assertLess(o.realized_pnl, 0.0)
        # stopped exactly at stop -> -1R
        self.assertAlmostEqual(o.r_multiple, -1.0, places=4)
        self.assertEqual(summary.losses, 1)

    def test_10_tp_close_is_win_with_correct_pnl_and_r(self):
        # entry 100, stop 95 (risk 5), target 115 (reward 15) -> +3R
        summary = A.run_once([_sig("WINNER")],
                            exit_plan={"WINNER": "TP"}, kill_switch_active=False)
        o = [x for x in summary.outcomes if x.symbol == "WINNER"][0]
        self.assertEqual(o.exit_reason, "TP")
        self.assertGreater(o.realized_pnl, 0.0)
        self.assertAlmostEqual(o.r_multiple, 3.0, places=4)
        self.assertEqual(summary.wins, 1)

    def test_11_summary_has_all_required_fields(self):
        summary = A.run_once(A.fixture_signals(),
                             exit_plan=A._fixture_exit_plan(),
                             kill_switch_active=False)
        d = summary.to_dict()
        for field in ("signals_in", "scored_count",
                      "research_paper_eligible_count", "simulated_orders",
                      "simulated_fills", "opened_positions",
                      "closed_positions", "wins", "losses", "average_win",
                      "average_loss", "win_loss_ratio", "max_drawdown"):
            self.assertIn(field, d)
        self.assertEqual(d["max_drawdown"], "not_available_in_A")


class TestImportSafety(unittest.TestCase):
    _FORBIDDEN_SUBSTRINGS = (
        "etoro", "ibkr_broker", "paper_broker", "live_broker", "telegram",
        "notifier", "scheduler", "schedule", "apscheduler",
    )

    def _imports(self, path):
        with open(path) as fh:
            tree = ast.parse(fh.read())
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
        return mods

    def test_12_runner_imports_are_safe(self):
        mods = self._imports("tools/paper_loop/m21_1extra_a_run_once.py")
        for m in mods:
            for bad in self._FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(bad, m,
                                 "forbidden import %r in runner" % m)
        # explicitly assert no main / dashboard import
        for m in mods:
            self.assertNotEqual(m, "main")
            self.assertFalse(m.startswith("dashboard"))

    def test_12_decision_imports_are_safe(self):
        mods = self._imports(
            "bot/runtime/m21_1extra_research_paper_decision.py")
        for m in mods:
            for bad in self._FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(bad, m,
                                 "forbidden import %r in decision" % m)

    def test_runner_does_not_reference_real_broker_submit(self):
        with open("tools/paper_loop/m21_1extra_a_run_once.py") as fh:
            src = fh.read()
        self.assertNotIn(".submit(", src)
        self.assertNotIn("placeOrder", src)
        self.assertNotIn("4001", src)


class TestLiveKillSwitchDelegation(unittest.TestCase):
    """run_live must NOT force kill_switch_active=False; it must delegate to the
    real kill-switch state via run_once(kill_switch_active=None)."""

    def test_run_live_does_not_force_kill_switch_false(self):
        # Static proof: the run_live body must not pass kill_switch_active=False
        # (or any hardcoded value) into run_once.
        import inspect
        src = inspect.getsource(A.run_once.__globals__["run_live"])
        self.assertNotIn("kill_switch_active=False", src)
        self.assertNotIn("kill_switch_active=True", src)
        # and it must call run_once
        self.assertIn("run_once(", src)

    def test_run_once_default_reads_real_kill_switch(self):
        # When kill_switch_active is None, run_once consults the real
        # is_kill_switch_active(). Patch it active -> nothing opens.
        import bot.kill_switch as ks
        original = ks.is_kill_switch_active
        try:
            ks.is_kill_switch_active = lambda: True
            summary = A.run_once(A.fixture_signals(),
                                exit_plan=A._fixture_exit_plan())
            self.assertEqual(summary.opened_positions, 0)
            self.assertTrue(any(o.rejection_reason == "kill_switch_active"
                                for o in summary.outcomes))
        finally:
            ks.is_kill_switch_active = original


if __name__ == "__main__":
    unittest.main(verbosity=2)

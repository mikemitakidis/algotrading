#!/usr/bin/env python3
"""M21.1 — Scoring bridge tests (read-only, research-grade).

Proves the approved Path-2 contract:
  1. STRICT still hard-BLOCKS model_readiness_failed.
  2. RESEARCH downgrades model_readiness_failed to MANUAL_REVIEW (scores instead
     of blocking), so a real composite ranking is produced.
  3. calibration-unavailable remains REVIEW (not BLOCK) in RESEARCH.
  4. execution_eligible is False on every candidate, in both profiles.
  5. Ranked output is research-grade scoring only (REJECT/REVIEW; never EXECUTE;
     hard_gate_passed False) — not execution approval.
Plus:
  - import guard: the tool imports no broker / live / paper / etoro / telegram /
    main / risk module.
  - no-fake guard: the bridge never sets model_readiness_passed True and never
    invents a calibrated probability (prediction_calibrated stays None).
  - ranking spread: stronger signals outrank weaker ones.

A01 (truthful contract): the bridge no longer assumes freshness, schema,
timeframe availability, Risk Authority 'ok', raw-price safety or production
thinness. Tests that need a scoring (non-BLOCKED) outcome therefore supply
complete fixture evidence via ``_evidence``; without it M19 blocks.
"""
import ast
import os
import subprocess
import sys
import unittest

# M21 C5a: keep this test process (and its Python children) off production
# runtime files, secrets (.env) and brokers. See _test_isolation.py.
import _test_isolation  # noqa: E402
_test_isolation.install()

from bot.signal_scoring import ScoringProfile
from bot.signal_scoring import keys as K
from bot.scoring_truth import CorporateActionRecord, RiskDecisionRef
from tools.signal_scoring.scanner_bridge import enrich_signal, score_signal
from tools.signal_scoring import score_rank_harness as H

_TS = "2026-06-26T15:00:00+00:00"


def _evidence(symbol="AAA", side="long", **over):
    """Complete fixture evidence (simulated calendar; after-close bars; a
    validated M14 approval; raw prices with a verified clean record)."""
    kw = dict(
        evaluated_at_utc="2026-06-26T20:30:00+00:00",
        calendar=H.FIXTURE_CALENDAR,
        bar_asof={"1D": "2026-06-26T04:00:00+00:00", "4H": "2026-06-26T16:00:00+00:00",
                  "1H": "2026-06-26T19:30:00+00:00", "15m": "2026-06-26T19:45:00+00:00"},
        risk_decision=RiskDecisionRef("m14_risk_authority", "fixture-1", "approved",
                                      symbol, side, "2026-06-26T20:20:00+00:00"),
        price_basis="raw", detector_suspected=False,
        corporate_action_record=CorporateActionRecord(
            symbol, "2026-06-01T00:00:00+00:00", "2026-06-27T00:00:00+00:00", (),
            "fixture", True),
        lookback_start_utc="2026-06-02T00:00:00+00:00",
        production_thinness_status="ok")
    kw.update(over)
    return kw


def _sig(symbol="AAA", rsi=62.0, macd=0.9, vc=4, atr=2.0, entry=100.0, vr=1.4):
    return dict(timestamp=_TS, symbol=symbol, direction="long",
                entry_price=entry, stop_loss=entry * 0.95,
                target_price=entry * 1.15, rsi=rsi, macd_hist=macd,
                vol_ratio=vr, valid_count=vc, available_tfs=4, atr=atr,
                avg_volume_20d=500000)


class TestModelReadinessGateByProfile(unittest.TestCase):
    def test_1_strict_still_blocks_model_readiness(self):
        sc = score_signal(_sig(), profile=ScoringProfile.STRICT)
        self.assertFalse(sc.hard_gate_passed)
        self.assertEqual(sc.final_score_100, 0.0)
        self.assertIn("model_readiness_failed", sc.hard_gate_failures)
        self.assertFalse(sc.execution_eligible)

    def test_2_research_downgrades_model_readiness_to_review(self):
        sc = score_signal(_sig(), profile=ScoringProfile.RESEARCH, **_evidence())
        # no longer a blocking failure
        self.assertNotIn("model_readiness_failed", sc.hard_gate_failures)
        # and it actually scores (real composite, not 0)
        self.assertGreater(sc.final_score_100, 0.0)
        self.assertFalse(sc.execution_eligible)

    def test_3_calibration_unavailable_remains_review_in_research(self):
        sc = score_signal(_sig(), profile=ScoringProfile.RESEARCH, **_evidence())
        # calibration is honestly unavailable; under research it must NOT block
        self.assertNotIn("calibration_unavailable", sc.hard_gate_failures)
        # and the uncalibrated state is surfaced honestly in reason codes
        self.assertTrue(
            any("calib" in r or "uncalibrated" in r for r in sc.reason_codes))

    def test_4_execution_eligible_false_in_both_profiles(self):
        for prof in (ScoringProfile.STRICT, ScoringProfile.RESEARCH):
            sc = score_signal(_sig(), profile=prof)
            self.assertFalse(sc.execution_eligible,
                             "exec must be False in %s" % prof)

    def test_5_research_output_is_not_execution_approval(self):
        # research candidates score but are never EXECUTE / never gate-passed
        for s in (_sig("AAA"), _sig("CCC", rsi=70, macd=1.2, atr=3.0,
                                     entry=200.0, vr=1.5)):
            sc = score_signal(s, profile=ScoringProfile.RESEARCH)
            self.assertFalse(sc.hard_gate_passed)
            self.assertNotEqual(getattr(sc.decision_bucket, "value",
                                        sc.decision_bucket), "EXECUTE")
            self.assertFalse(sc.execution_eligible)


class TestNoFakeReadiness(unittest.TestCase):
    def test_bridge_never_sets_model_ready_true_by_default(self):
        ci = enrich_signal(_sig())
        self.assertIs(ci.ml_context[K.ML_MODEL_READINESS_PASSED], False)

    def test_bridge_never_invents_calibrated_probability(self):
        ci = enrich_signal(_sig())
        self.assertIsNone(ci.ml_context[K.ML_PRED_CALIBRATED])
        self.assertIs(ci.ml_context[K.ML_CALIBRATION_APPLIED], False)

    def test_price_mode_never_claimed_without_evidence(self):
        # A01: the pre-A01 bridge claimed 'raw' / no PIT risk unconditionally
        # (yfinance actually serves auto-adjusted prices). Now: unknown -> absent.
        ci = enrich_signal(_sig())
        self.assertNotIn(K.ML_PRICE_ADJUSTMENT_MODE, ci.ml_context)
        self.assertNotIn("adjusted_price_pit_risk", ci.advisory_context)
        ci = enrich_signal(_sig(), **_evidence())
        self.assertEqual(ci.ml_context[K.ML_PRICE_ADJUSTMENT_MODE], "raw")
        self.assertIs(ci.advisory_context["adjusted_price_pit_risk"], False)
        ci = enrich_signal(_sig(), **_evidence(price_basis="total_return_adjusted"))
        self.assertEqual(ci.ml_context[K.ML_PRICE_ADJUSTMENT_MODE], "adjusted")
        self.assertIs(ci.advisory_context["adjusted_price_pit_risk"], True)

    def test_risk_authority_never_ok_by_default(self):
        ci = enrich_signal(_sig())
        self.assertNotIn("risk_authority_status", ci.risk_preview)
        sc = score_signal(_sig())
        self.assertEqual(sc.decision_bucket.value, "BLOCKED")
        self.assertIn("missing_context_key", sc.hard_gate_failures)


class TestRankingSpread(unittest.TestCase):
    def test_stronger_signals_outrank_weaker(self):
        signals = [
            _sig("STRONG", rsi=70, macd=1.2, vc=4, atr=3.0, entry=200.0, vr=1.5),
            _sig("WEAK", rsi=48, macd=0.0, vc=1, atr=0.4, entry=20.0, vr=0.8),
        ]
        result = H.build_result(signals)
        ranked = result["ranked"]
        self.assertEqual(ranked[0]["symbol"], "STRONG")
        self.assertEqual(ranked[-1]["symbol"], "WEAK")
        self.assertGreater(ranked[0]["research_rank_score"],
                           ranked[-1]["research_rank_score"])
        # without evidence both are M19-BLOCKED (final 0); order is research-only
        self.assertEqual({r["decision_bucket"] for r in ranked}, {"BLOCKED"})
        self.assertFalse(result["execution_eligible_any"])
        self.assertFalse(result["any_hard_gate_passed"])

    def test_harness_raises_if_execution_ever_eligible(self):
        # build_result asserts exec False internally; fixture run must succeed
        result = H.build_result(H.fixture_signals())
        self.assertEqual(result["n_scored"], 4)
        self.assertFalse(result["execution_eligible_any"])


class TestImportSafety(unittest.TestCase):
    _FORBIDDEN = ("broker", "etoro", "telegram", "live_broker", "paper_broker",
                  "main", "bot.risk", "placeOrder", "execution_intents")

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

    def test_bridge_imports_are_safe(self):
        mods = self._imports("tools/signal_scoring/scanner_bridge.py")
        for m in mods:
            for bad in self._FORBIDDEN:
                self.assertNotIn(bad, m, "forbidden import %r in bridge" % m)

    def test_harness_imports_are_safe(self):
        # the harness may import bot.scanner for the live path only; that is the
        # read-only scan_cycle (no broker). Forbid broker/live/paper/etoro/tg.
        mods = self._imports("tools/signal_scoring/score_rank_harness.py")
        for m in mods:
            for bad in ("broker", "etoro", "telegram", "live", "paper",
                        "execution_intents"):
                self.assertNotIn(bad, m, "forbidden import %r in harness" % m)


class TestRealScannerShapedSignal(unittest.TestCase):
    """A real scan_cycle signal does NOT carry avg_volume_20d/avg_volume, so the
    bridge cannot derive liquidity from the signal alone. These tests prove we
    handle that honestly: without liquidity it is gate-incomplete (not faked);
    with liquidity supplied (as the live path does from real bars) it scores."""

    def _scanner_shaped(self, symbol="REAL"):
        # mirrors bot/scanner.py signal keys: no avg_volume / avg_volume_20d
        return dict(timestamp=_TS, symbol=symbol, direction="long",
                    route="ibkr", tf_15m=1, tf_1h=1, tf_4h=1, tf_1d=1,
                    valid_count=4, available_tfs=4, entry_price=100.0,
                    stop_loss=95.0, target_price=115.0, strategy_version="v1",
                    rsi=62.0, macd_hist=0.9, ema20=101.0, ema50=99.0,
                    vwap_dev=0.005, vol_ratio=1.4, atr=2.0, price=100.0)

    def test_bridge_does_not_invent_liquidity_when_absent(self):
        # No avg_volume in a scanner-shaped signal, and none supplied:
        # avg_dollar_volume_20d must be ABSENT (not a fabricated number).
        ci = enrich_signal(self._scanner_shaped())
        self.assertNotIn("avg_dollar_volume_20d", ci.liquidity_context or {})

    def test_scanner_signal_without_liquidity_is_gate_incomplete(self):
        # Honest consequence: M19 flags missing liquidity context rather than
        # passing on a faked value. It must NOT score as a clean pass.
        sc = score_signal(self._scanner_shaped(), profile=ScoringProfile.RESEARCH)
        self.assertFalse(sc.hard_gate_passed)
        self.assertFalse(sc.execution_eligible)

    def test_supplying_real_derived_liquidity_lets_it_score(self):
        # The live path derives avg_dollar_volume_20d from real bars and passes
        # it in. Simulate that here with an explicit value (price * avg vol).
        sc = score_signal(self._scanner_shaped(),
                          profile=ScoringProfile.RESEARCH,
                          avg_dollar_volume=100.0 * 500000,
                          **_evidence("REAL"))
        self.assertNotIn("model_readiness_failed", sc.hard_gate_failures)
        self.assertGreater(sc.final_score_100, 0.0)
        self.assertFalse(sc.execution_eligible)


class TestLiveOutputPathSafety(unittest.TestCase):
    """Issue 2: live mode must only ever write under /tmp."""

    def test_live_mode_refuses_reports_dir(self):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        r = subprocess.run(
            [sys.executable, "-m", "tools.signal_scoring.score_rank_harness",
             "--mode", "live",
             "--report", "reports/m21_1_scoring_bridge_readonly.md"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/tmp/", (r.stderr + r.stdout))

    def test_live_mode_refuses_non_tmp_path(self):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        r = subprocess.run(
            [sys.executable, "-m", "tools.signal_scoring.score_rank_harness",
             "--mode", "live", "--report", "somewhere/live.md"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/tmp/", (r.stderr + r.stdout))


if __name__ == "__main__":
    unittest.main(verbosity=2)

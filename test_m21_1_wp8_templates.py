"""test_m21_1_wp8_templates.py — M21 WP8 strategy templates on the paper path (A15).

Pins the common template contract: identities and parameter bounds (matching
the research registry), bar-by-bar signal parity with
``research/genome/templates.py``, causality on completed bars, the common
exit policy, live-vs-shadow routing (templates 2-4 can never become a broker
candidate), one candidate per symbol, the idempotent shadow record, the WS2
shadow hook being env-gated, and per-template D3 metrics. Offline only.
"""
from __future__ import annotations

import ast
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from bot.paper_loop_d2.candidates import InvalidCandidate, LoopCandidate
from bot.templates import core as T
from bot.templates import routing as RT
from bot.templates.ids import template_of
from bot.templates.shadow import ShadowStore, shadow_enabled_from_env

REPO = Path(__file__).resolve().parent
NOW = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
STARTERS = ("mean_reversion", "breakout_momentum", "pullback_in_trend", "trend_confluence")


def market(seed=7, n=320):
    from research.common.synthetic import MarketSpec, generate
    return generate(MarketSpec(n_symbols=3, n_sessions=n, timeframe="1D", seed=seed)).bars


def research_signals(tid, bars, params):
    from research.genome import templates as RG
    from research.genome.schema import Genome
    return RG.signals(Genome(tid, tuple(sorted(params.items()))), bars)


def plunge(n=120):
    """Steady drift, then a sharp five-day drop: mean_reversion fires on the last bar."""
    idx = pd.date_range("2026-05-01", periods=n, freq="B", tz="America/New_York").tz_convert("UTC")
    c = 100 + np.sin(np.arange(n) / 3.0) * 0.5
    c[-5:] = c[-6] * np.cumprod(np.full(5, 0.96))
    return pd.DataFrame({"open": c, "high": c * 1.005, "low": c * 0.995, "close": c,
                         "volume": np.full(n, 1e6)}, index=idx)


class T1_RegistryAndParams(unittest.TestCase):
    def test_appendix_a_identities(self):
        self.assertEqual(T.APPENDIX_A, ("scanner", "mean_reversion", "breakout_momentum", "pullback_in_trend"))
        self.assertEqual([T.get(t).number for t in T.APPENDIX_A], [1, 2, 3, 4])
        self.assertIsNone(T.get("trend_confluence").number)
        with self.assertRaises(T.TemplateError):
            T.get("martingale")
        with self.assertRaises(T.TemplateError):
            T.signals("scanner", market()["S000"])

    def test_bounds_match_the_research_registry(self):
        from research.genome.templates import TEMPLATES as RT_
        for tid in STARTERS:
            b, r = T.get(tid), RT_[tid]
            self.assertEqual(sorted(b.params), sorted(r.params), tid)
            for k, spec in r.params.items():
                self.assertEqual((b.params[k].kind, b.params[k].default, b.params[k].lo, b.params[k].hi,
                                  tuple(b.params[k].choices)),
                                 (spec.kind, spec.default, spec.lo, spec.hi, tuple(spec.choices)), (tid, k))
            self.assertEqual(tuple(b.constraints), tuple(r.constraints), tid)

    def test_validation_is_fail_closed_and_never_clamps(self):
        self.assertEqual(T.validate_params("mean_reversion")["rsi_entry"], 30.0)
        for bad in ({"rsi_entry": 50.0}, {"rsi_entry": 9.0}, {"rsi_period": 14.5}, {"rsi_period": True},
                    {"rsi_mode": "ema"}, {"rsi_entry": float("nan")}, {"colour": 1}):
            with self.assertRaises(T.TemplateError, msg=bad):
                T.validate_params("mean_reversion", bad)
        with self.assertRaises(T.TemplateError) as cm:
            T.validate_params("pullback_in_trend", {"ema_fast": 4, "rsi_max": 99.0})
        self.assertIn("ema_fast", str(cm.exception))
        self.assertIn("rsi_max", str(cm.exception))
        with self.assertRaises(T.TemplateError):
            T.validate_params("pullback_in_trend", {"ema_fast": 60, "ema_slow": 50})

    def test_strategy_ids_are_stable_and_parse_back(self):
        a = T.strategy_id("breakout_momentum")
        self.assertEqual(a, T.strategy_id("breakout_momentum", {"lookback": 20}))
        self.assertNotEqual(a, T.strategy_id("breakout_momentum", {"lookback": 25}))
        self.assertRegex(a, r"^tpl:breakout_momentum@v1#[0-9a-f]{8}$")
        self.assertEqual(template_of(a), "breakout_momentum")
        self.assertEqual(template_of("scanner_v3"), "scanner")
        self.assertEqual(template_of("meanrev_v1"), "meanrev_v1")
        self.assertEqual(template_of(None), "")


class T2_ResearchParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = market()

    def _param_sets(self, tid):
        rng = np.random.default_rng(42)
        spec = T.get(tid).params
        sets = [T.validate_params(tid)]
        while len(sets) < 4:
            p = {}
            for k, s in spec.items():
                if s.kind == "choice":
                    p[k] = s.choices[int(rng.integers(len(s.choices)))]
                elif s.kind == "int":
                    p[k] = int(rng.integers(int(s.lo), int(s.hi) + 1))
                else:
                    p[k] = float(np.round(rng.uniform(s.lo, s.hi), 2))
            try:
                sets.append(T.validate_params(tid, p))
            except T.TemplateError:
                continue
        return sets

    def test_every_template_matches_research_bar_by_bar(self):
        n_entries = 0
        for tid in STARTERS:
            for p in self._param_sets(tid):
                for sym, b in self.bars.items():
                    live = T.signals(tid, b, p)
                    ref = research_signals(tid, b, p)[list(T.SIGNAL_COLUMNS)]
                    pd.testing.assert_frame_equal(live, ref, check_exact=True, obj="%s %s %s" % (tid, p, sym))
                    n_entries += int(live["entry"].sum())
        self.assertGreater(n_entries, 50)

    def test_signals_on_a_truncated_history_equal_the_full_history(self):
        """Causality: the verdict on bar k uses bars <= k only, so evaluating the
        completed history up to k gives the same row as the full frame."""
        b = self.bars["S001"]
        for tid in STARTERS:
            full = T.signals(tid, b)
            for k in (60, 150, 233, len(b) - 1):
                part = T.signals(tid, b.iloc[:k + 1]).iloc[-1]
                pd.testing.assert_series_equal(part, full.iloc[k], check_names=False, obj="%s@%d" % (tid, k))


class T3_ExitPolicyAndSignal(unittest.TestCase):
    def test_bracket(self):
        self.assertEqual(T.ExitPolicy().bracket(100.0, 2.0), (96.0, 106.0))
        for e, a in ((100.0, 0.0), (100.0, float("nan")), (-1.0, 1.0), (10.0, 6.0)):
            with self.assertRaises(T.TemplateError):
                T.ExitPolicy().bracket(e, a)
        with self.assertRaises(T.TemplateError):
            T.ExitPolicy(stop_atr=0)

    def test_entry_signal_carries_identity_and_bracket(self):
        b = plunge()
        s = T.evaluate_last("mean_reversion", b, symbol="AAA", decision_ts_utc="2026-10-15T20:00:00+00:00")
        self.assertTrue(s.entry)
        self.assertLess(s.stop_loss, s.entry_ref_price)
        self.assertLess(s.entry_ref_price, s.take_profit)
        self.assertEqual((s.template_id, s.template_version, s.direction), ("mean_reversion", 1, "long"))
        self.assertEqual(s.strategy_id, T.strategy_id("mean_reversion"))
        s2 = T.evaluate_last("mean_reversion", b.copy(), symbol="AAA", decision_ts_utc="2026-10-15T20:00:00+00:00")
        self.assertEqual(s.digest, s2.digest)
        self.assertEqual(len(s.digest), 16)

    def test_setup_without_entry_and_no_verdict(self):
        b = plunge()
        s = T.evaluate_last("mean_reversion", b, symbol="AAA", decision_ts_utc="t", params={"bb_std": 3.0})
        self.assertFalse(s.entry)
        self.assertEqual(s.reject_reason, "not_below_lower_band")
        self.assertIsNone(s.stop_loss)
        self.assertIsNone(T.evaluate_last("mean_reversion", b.iloc[:60], symbol="AAA", decision_ts_utc="t"))
        self.assertIsNone(T.evaluate_last("mean_reversion", b.iloc[:0], symbol="AAA", decision_ts_utc="t"))

    def test_missing_atr_blocks_the_entry(self):
        b = plunge()
        with mock.patch.object(T, "_atr", return_value=pd.Series(np.nan, index=b.index)):
            s = T.evaluate_last("mean_reversion", b, symbol="AAA", decision_ts_utc="t")
        self.assertEqual((s.entry, s.reject_reason, s.stop_loss), (False, "bracket_unavailable", None))


def _sig(tid="mean_reversion", entry=True, symbol="AAA"):
    sid = "scanner_v3" if tid == "scanner" else T.strategy_id(tid)
    return T.TemplateSignal(tid, 1, sid, "d" * 12, symbol, "long", "2026-11-03T21:00:00+00:00", entry, True,
                            "" if entry else "x", 100.0, 96.0 if entry else None, 106.0 if entry else None, 2.0, 1.0)


class T4_Routing(unittest.TestCase):
    def test_only_template_1_is_live(self):
        self.assertEqual(RT.LIVE_TEMPLATES, frozenset({"scanner"}))
        for tid in ("mean_reversion", "breakout_momentum", "pullback_in_trend", "trend_confluence"):
            with self.assertRaises(RT.TemplateNotLive):
                RT.to_loop_candidate(_sig(tid), score_100=90.0)

    def test_live_template_candidate_keeps_its_identity(self):
        c = RT.to_loop_candidate(_sig("scanner"), score_100=82.0, candidate_id="C-abc")
        self.assertIsInstance(c, LoopCandidate)
        self.assertEqual((c.strategy_id, c.stop_loss, c.take_profit, c.candidate_id), ("scanner_v3", 96.0, 106.0,
                                                                                      "C-abc"))
        with self.assertRaises(T.TemplateError):
            RT.to_loop_candidate(_sig("scanner", entry=False), score_100=82.0)
        with self.assertRaises(InvalidCandidate):
            RT.to_loop_candidate(_sig("scanner"), score_100=float("nan"))

    def test_route_sends_everything_but_live_entries_to_shadow(self):
        sigs = [_sig("scanner"), _sig("scanner", entry=False), _sig("mean_reversion"), _sig("pullback_in_trend")]
        live, shadow = RT.route(sigs)
        self.assertEqual([s.template_id for s in live], ["scanner"])
        self.assertEqual(len(shadow), 3)

    def test_one_candidate_per_symbol(self):
        mk = lambda sym, d, sid, sc: LoopCandidate.build(
            symbol=sym, direction=d, score=sc, strategy_id=sid, entry_ref_price=100.0,
            stop_loss=95.0 if d == "long" else 105.0, take_profit=110.0 if d == "long" else 90.0)
        tpl2 = T.strategy_id("mean_reversion")
        out, notes = RT.merge_tradable([mk("AAA", "long", tpl2, 99.0), mk("AAA", "long", "scanner_v3", 70.0),
                                        mk("BBB", "long", "scanner_v3", 80.0), mk("BBB", "short", tpl2, 90.0),
                                        mk("CCC", "long", tpl2, 60.0), mk("CCC", "long", tpl2 + "x", 75.0)])
        self.assertEqual([(c.symbol, c.strategy_id) for c in out], [("AAA", "scanner_v3"), ("CCC", tpl2 + "x")])
        self.assertTrue(any(n.startswith("conflict_dropped:BBB") for n in notes))
        self.assertIn("duplicate_suppressed:AAA:%s" % tpl2, notes)
        self.assertIn("duplicate_suppressed:CCC:%s" % tpl2, notes)           # same template: higher score wins


class FakeCompletion:
    """Every row complete except the last (forming); close = start + 6.5 h."""

    def __init__(self, df):
        self.last = df.index[-1]

    def classify(self, tf, start):
        if start == self.last:
            return "forming", None
        return "complete", start + pd.Timedelta(hours=20)


class T5_Shadow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ShadowStore(os.path.join(self.tmp.name, "shadow.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_is_idempotent_per_bar(self):
        s = _sig()
        self.assertTrue(self.store.record(s, now=NOW))
        self.assertFalse(self.store.record(s, now=NOW))
        self.assertTrue(self.store.record(T.TemplateSignal(**{**s.__dict__, "decision_ts_utc": "later"}), now=NOW))
        self.assertTrue(self.store.record(_sig("pullback_in_trend"), now=NOW))
        self.assertEqual(len(self.store.rows()), 3)
        self.assertEqual(len(self.store.rows("pullback_in_trend")), 1)
        self.assertEqual(self.store.rows()[0]["strategy_id"], s.strategy_id)
        with self.assertRaises(ValueError):
            ShadowStore("")

    def test_env_gate_is_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(shadow_enabled_from_env())
        for v, want in (("1", True), ("true", False), ("0", False)):
            with mock.patch.dict(os.environ, {"TEMPLATE_SHADOW_ENABLED": v}):
                self.assertEqual(shadow_enabled_from_env(), want)

    def test_runner_judges_the_newest_completed_bar_only(self):
        from tools.signal_scoring.template_shadow import run_template_shadow
        b = plunge(121)                                              # last row is forming
        res = run_template_shadow(self.store, {"AAA": b, "EMPTY": b.iloc[:0]}, FakeCompletion(b), now=NOW)
        rows = self.store.rows("mean_reversion")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_ts_utc"], (b.index[-2] + pd.Timedelta(hours=20)).isoformat())
        self.assertEqual(rows[0]["entry"], 1)
        self.assertTrue(any(s.startswith("EMPTY:") for s in res["skipped"]))
        again = run_template_shadow(self.store, {"AAA": b}, FakeCompletion(b), now=NOW)
        self.assertEqual((again["recorded"], again["already_recorded"]), (0, res["recorded"]))
        with self.assertRaises(ValueError):
            run_template_shadow(self.store, {}, FakeCompletion(b), now=NOW, templates=("scanner",))

    def test_ws2_store_is_env_gated_and_uses_the_repo_data_dir(self):
        import tools.paper_loop.m21_1extra_d2_3_scheduler as ws2
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ws2._template_shadow_store())
        Path(self.tmp.name, "data").mkdir()
        with mock.patch.dict(os.environ, {"TEMPLATE_SHADOW_ENABLED": "1"}), \
                mock.patch.object(ws2, "REPO", Path(self.tmp.name)):
            st = ws2._template_shadow_store()
        self.assertEqual(Path(st.path), Path(self.tmp.name, "data", "template_shadow.db"))
        src = (REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py").read_text()
        block = src[src.index("cands, _notes = loop_candidates(ranked)"):src.index("    return provider")]
        self.assertIn("except Exception", block)                      # a shadow failure never breaks the loop
        self.assertEqual(block.count("cands ="), 0)                   # and never touches the candidates


class T6_D3PerTemplate(unittest.TestCase):
    def test_outcomes_pool_by_template(self):
        from bot.runtime import paper_outcome_metrics as M
        from test_m21_1extra_d3_metrics import D3DbFixture, _outcome
        fx = D3DbFixture()
        try:
            a, b = T.strategy_id("mean_reversion"), T.strategy_id("mean_reversion", {"bb_std": 2.5})
            fx.create(outcomes=[_outcome("1", 4.0, "2026-10-01T15:00:00+00:00", strategy=a),
                                _outcome("2", -1.0, "2026-10-02T15:00:00+00:00", strategy=b),
                                _outcome("3", 2.0, "2026-10-02T16:00:00+00:00", strategy="scanner_v3")],
                      lifecycles=[("F", 1, 1, "2026-10-20T15:00:00+00:00")])
            e = M.compute_paper_metrics(fx.path)["edge_outcomes"]
        finally:
            fx.cleanup()
        self.assertEqual(sorted(e["by_template"]), ["mean_reversion", "scanner"])
        self.assertEqual(e["by_template"]["mean_reversion"]["trades"], 2)
        self.assertEqual(len(e["by_strategy"]), 3)


class T7_Isolation(unittest.TestCase):
    FILES = ["bot/templates/__init__.py", "bot/templates/core.py", "bot/templates/ids.py",
             "bot/templates/routing.py", "bot/templates/shadow.py"]

    def _imports(self, rel):
        tree = ast.parse((REPO / rel).read_text())
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
        return out

    def test_no_broker_network_messaging_or_research_imports(self):
        bad = ("bot.brokers", "bot.notifier", "bot.paper_loop_d2.scheduler", "bot.kill_switch", "ib_insync",
               "alpaca", "requests", "urllib.request", "socket", "telegram", "research", "tools", "subprocess")
        for f in self.FILES:
            for m in self._imports(f):
                self.assertFalse(any(m == b or m.startswith(b + ".") for b in bad), (f, m))
            text = (REPO / f).read_text()
            for s in ("scoring_truth", "scanner_bridge", "candidate_ledger"):
                self.assertNotIn(s, text, (f, s))

    def test_ids_module_is_light(self):
        self.assertEqual(self._imports("bot/templates/ids.py"), {"__future__", "typing"})


if __name__ == "__main__":
    unittest.main()

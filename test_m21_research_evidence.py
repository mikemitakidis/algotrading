"""test_m21_research_evidence.py — lane A counterfactual evidence engine (B03, B04).

Hand-built bar paths pin every labelling rule (barrier order, gaps, ambiguity,
time stop, pending, unfillable, data defects, costs, finer-data resolution);
synthetic markets prove exact M17 parity on unambiguous paths, stage replay
consistency with the M17 scanner replica, effective-N behaviour and gate
economics. Offline only. Run via
``python _test_isolation.py test_m21_research_evidence``.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research.common.bars import multi_timeframe
from research.common.rng import stream
from research.common.synthetic import MarketSpec, generate
from research.evidence import evidence as EVA
from research.evidence import gate_economics as GE
from research.evidence import parity
from research.evidence.candidates import Bracket, CandidateEvent, make_candidate_id, stage_counts, to_frame
from research.evidence.costs import ZERO, CostModel
from research.evidence.effective_n import average_uniqueness, cluster_variance_of_mean, effective_n
from research.evidence.labeler import SourceInfo, label_candidates, validate_finer
from research.evidence.replay import NO_POSITION, apply_gates, position_release_times, scanner_stage_candidates
from research.evidence.snapshots import from_candidate_snapshots


def _daily(rows, start="2024-01-02"):
    """rows: list of (o, h, l, c[, v[, q]]). Business days, stamped at 21:00 UTC."""
    days = pd.bdate_range(start, periods=len(rows), tz="UTC") + pd.Timedelta(21 * 60, unit="m")
    out = []
    for r in rows:
        o, h, l, c = r[:4]
        v = r[4] if len(r) > 4 else 1000.0
        q = r[5] if len(r) > 5 else 0
        out.append((o, h, l, c, v, q))
    a = np.array(out, dtype=float)
    return pd.DataFrame({"ts_utc": days, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3],
                         "volume": a[:, 4], "quality_flags": a[:, 5].astype(np.int64)})


def _cand(bars, i, direction="long", atr=1.0, stop=1.0, target=2.0, H=5, symbol="X", stage="eligible", **kw):
    ts = pd.Timestamp(bars["ts_utc"].iloc[i])
    return CandidateEvent(candidate_id=make_candidate_id(symbol, ts, direction, "t"), symbol=symbol,
                          decision_ts_utc=ts, direction=direction, stage=stage, source="synthetic", timeframe="1D",
                          strategy_id="t", atr_at_decision=atr, bracket=Bracket(stop, target, H), **kw)


def _label(bars, cands, **kw):
    kw.setdefault("cost", ZERO)
    kw.setdefault("stress_cost", ZERO)
    return label_candidates(cands, {"X": bars}, timeframe="1D", **kw).set_index("candidate_id")


FLAT = (100, 100.5, 99.5, 100)


class A1_LabelRules(unittest.TestCase):
    def test_target_stop_and_r_multiples(self):
        b = _daily([FLAT, (100, 101, 99.5, 100.5), (100.5, 102.5, 100, 102)])
        r = _label(b, [_cand(b, 0)]).iloc[0]
        self.assertEqual((r.status, r.outcome), ("definitive", "target"))
        self.assertEqual((r.entry_price, r.stop_price, r.target_price, r.exit_price), (100, 99, 102, 102))
        self.assertAlmostEqual(r.r_gross, 2.0)
        self.assertTrue(r.path_observed and r.simulated_execution and not r.ambiguous_intrabar)
        self.assertEqual(r.holding_bars, 2)
        b2 = _daily([FLAT, (100, 100.5, 98.8, 99.2)])
        r2 = _label(b2, [_cand(b2, 0)]).iloc[0]
        self.assertEqual((r2.outcome, r2.r_gross), ("stop", -1.0))

    def test_both_barriers_in_one_bar_is_ambiguous(self):
        for bar_idx in (1, 2):                                  # entry bar, and a later bar
            rows = [FLAT, (100, 100.5, 99.6, 100)] if bar_idx == 2 else [FLAT]
            rows.append((100, 102.5, 98.5, 100))
            b = _daily(rows)
            r = _label(b, [_cand(b, 0)]).iloc[0]
            self.assertEqual(r.status, "ambiguous")
            self.assertIsNone(r.outcome)
            self.assertFalse(r.path_observed)
            self.assertTrue(np.isnan(r.r_gross))
            self.assertEqual((r.r_gross_pessimistic, r.r_gross_optimistic), (-1.0, 2.0))

    def test_gap_fills_at_open(self):
        b = _daily([FLAT, (100, 100.5, 99.5, 100), (97.0, 98.0, 96.0, 97.5)])
        r = _label(b, [_cand(b, 0)]).iloc[0]
        self.assertEqual((r.outcome, r.exit_price, r.gap_fill), ("stop", 97.0, True))
        self.assertAlmostEqual(r.r_gross, -3.0)
        b2 = _daily([FLAT, (100, 100.5, 99.5, 100), (104.0, 105.0, 98.0, 99.0)])
        r2 = _label(b2, [_cand(b2, 0)]).iloc[0]
        self.assertEqual((r2.status, r2.outcome, r2.exit_price), ("definitive", "target", 104.0))

    def test_time_stop_pending_and_series_end(self):
        rows = [FLAT] + [(100, 100.5, 99.5, 100.2)] * 6
        b = _daily(rows)
        r = _label(b, [_cand(b, 0, H=5)]).iloc[0]
        self.assertEqual((r.outcome, r.exit_idx, r.exit_price, r.holding_bars), ("timeout", 5, 100.2, 5))
        p = _label(b, [_cand(b, 3, H=5)]).iloc[0]
        self.assertEqual(p.status, "pending")
        other = _daily([FLAT] * 30)
        lab = label_candidates([_cand(b, 3, H=5)], {"X": b, "Y": other}, timeframe="1D", cost=ZERO,
                               stress_cost=ZERO).iloc[0]
        self.assertEqual(lab.status, "unobserved")
        self.assertIn("series_ended_early", lab.data_flags)

    def test_short_direction_is_mirrored(self):
        b = _daily([FLAT, (100, 100.5, 99.5, 99.8), (99.8, 100.0, 97.9, 98.0)])
        r = _label(b, [_cand(b, 0, direction="short")]).iloc[0]
        self.assertEqual((r.outcome, r.stop_price, r.target_price, r.exit_price), ("target", 101.0, 98.0, 98.0))
        self.assertAlmostEqual(r.r_gross, 2.0)
        self.assertAlmostEqual(r.mae_r, -0.5)

    def test_unfillable_cases(self):
        b = _daily([FLAT, (100, 101, 99, 100, 0.0), FLAT])
        r = _label(b, [_cand(b, 0), _cand(b, 1, atr=float("nan"))])
        self.assertEqual(list(r.status), ["unfillable", "unfillable"])
        self.assertEqual(set(r.unfillable_reason), {"entry_bar_not_tradable", "no_atr_at_decision"})
        gap = pd.concat([b.iloc[:1], b.iloc[1:].assign(ts_utc=b.ts_utc.iloc[1:] + pd.Timedelta(10, unit="D"))])
        g = _label(gap.reset_index(drop=True), [_cand(b, 0)]).iloc[0]
        self.assertEqual(g.unfillable_reason, "entry_delayed_by_data_gap")
        empty = label_candidates([_cand(b, 0)], {}, timeframe="1D").iloc[0]
        self.assertEqual((empty.status, empty.data_flags), ("unobserved", "no_bars"))

    def test_entry_is_first_bar_opening_after_decision(self):
        ts = pd.date_range("2024-01-02 14:45", periods=6, freq="15min", tz="UTC")
        b = pd.DataFrame({"ts_utc": ts, "open": [100, 101, 102, 103, 104, 105.0], "high": 110.0, "low": 90.0,
                          "close": [100, 101, 102, 103, 104, 105.0], "volume": 10.0, "quality_flags": 0})
        dec = pd.Timestamp("2024-01-02 15:07", tz="UTC")              # inside the 15:00-15:15 bar
        c = CandidateEvent(candidate_id="c", symbol="X", decision_ts_utc=dec, direction="long", stage="eligible",
                           source="synthetic", timeframe="15m", strategy_id="t", atr_at_decision=100.0,
                           bracket=Bracket(1.0, 1.0, 2))
        r = label_candidates([c], {"X": b}, timeframe="15m").iloc[0]
        self.assertEqual(r.entry_idx, 3)        # bar [15:15, 15:30): the first that OPENS after 15:07
        self.assertEqual(r.entry_price, 103)
        # PR #6 review P1: the entry time is the OPEN of the bar whose open is the fill price.
        self.assertEqual(r.entry_ts_utc, pd.Timestamp("2024-01-02 15:15", tz="UTC"))
        self.assertEqual(r.entry_time_basis, "entry_bar_open")
        self.assertEqual(r.exit_ts_utc, pd.Timestamp(b.ts_utc.iloc[r.exit_idx]))     # exit bar CLOSE
        self.assertEqual(r.exit_time_basis, "exit_bar_close_observation")
        self.assertGreater(r.entry_ts_utc, dec)
        d = _daily([FLAT, (100, 101, 99.5, 100.5), (100.5, 102.5, 100, 102)])
        rd = _label(d, [_cand(d, 0)]).iloc[0]
        self.assertEqual(rd.entry_ts_utc, pd.Timestamp(d.ts_utc.iloc[1]) - pd.Timedelta(390, unit="m"))

    def test_data_defects_block_definitive_labels(self):
        rows = [FLAT, (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100.1)]
        b = _daily(rows)
        gap2 = b.copy()
        gap2.loc[2:, "ts_utc"] = gap2.loc[2:, "ts_utc"] + pd.Timedelta(2, unit="D")   # skip 2 business days
        r = _label(gap2, [_cand(gap2, 0, H=3)]).iloc[0]
        self.assertEqual(r.status, "unobserved")
        self.assertIn("missing_bars_in_window", r.data_flags)
        gap1 = b.copy()
        gap1.loc[2:, "ts_utc"] = gap1.loc[2:, "ts_utc"] + pd.Timedelta(1, unit="D")   # one day: maybe a holiday
        r1 = _label(gap1, [_cand(gap1, 0, H=3)]).iloc[0]
        self.assertEqual(r1.status, "definitive")
        self.assertIn("possible_holiday_or_missing_bar", r1.data_flags)
        split = _daily([FLAT, (50, 50.2, 49.8, 50.1), (50.1, 50.3, 49.9, 50.0)])
        rs = _label(split, [_cand(split, 0, atr=0.1, H=2)]).iloc[0]
        self.assertIn("split_suspect", rs.data_flags)
        self.assertNotEqual(rs.status, "definitive")
        qf = _daily([FLAT, (100, 100.5, 99.5, 100, 1000, 4), (100, 100.5, 99.5, 100.2)])
        self.assertEqual(_label(qf, [_cand(qf, 0, H=2)]).iloc[0].status, "unobserved")

    def test_costs_and_stress(self):
        b = _daily([FLAT, (100, 100.5, 99.5, 100), (97.0, 98.0, 96.0, 97.5)])
        c = CostModel()
        r = _label(b, [_cand(b, 0)], cost=c, stress_cost=c.scale(2)).iloc[0]
        self.assertLess(r.r_net, r.r_gross)
        self.assertLess(r.r_net_stress, r.r_net)
        no_gap = CostModel(stop_gap_extra_bps=0.0)
        r0 = _label(b, [_cand(b, 0)], cost=no_gap, stress_cost=no_gap).iloc[0]
        self.assertLess(r.r_net, r0.r_net)                             # gap-through stop costs extra
        long_cost = c.round_trip_cost(100, 100, short=False, holding_bars=10, stop_gap=False)
        short_cost = c.round_trip_cost(100, 100, short=True, holding_bars=10, stop_gap=False)
        self.assertGreater(short_cost, long_cost)
        self.assertEqual(ZERO.round_trip_cost(100, 120, short=True, holding_bars=5, stop_gap=True), 0.0)
        s2 = c.scale(2.0)
        for f in ("half_spread_bps", "slippage_bps", "commission_per_share", "stop_gap_extra_bps",
                  "borrow_bps_per_day"):
            self.assertAlmostEqual(getattr(s2, f), 2.0 * getattr(c, f), msg=f)
        self.assertAlmostEqual(s2.round_trip_cost(100, 101, short=True, holding_bars=3, stop_gap=True),
                               2.0 * c.round_trip_cost(100, 101, short=True, holding_bars=3, stop_gap=True))

    @staticmethod
    def _fine_day(coarse_close, events, o=100.0, c=100.0, h=102.5, l=98.5):
        """26 x 15m bars covering the coarse day exactly; `events` maps bar
        index -> (high, low) placing the day's extremes."""
        ts = pd.date_range(pd.Timestamp(coarse_close) - pd.Timedelta(375, unit="m"), periods=26, freq="15min")
        rows = []
        px = o
        for k in range(26):
            hi, lo = events.get(k, (max(px, 100.2), min(px, 99.8)))
            close = c if k == 25 else min(max(px, lo), hi)
            rows.append((px, max(hi, px, close), min(lo, px, close), close))
            px = close
        f = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
        f.insert(0, "ts_utc", ts)
        f["volume"] = 1.0
        f["quality_flags"] = 0
        return f

    def test_finer_data_resolves_ambiguity(self):
        b = _daily([FLAT, (100, 102.5, 98.5, 100)])
        cc = b.ts_utc.iloc[1]
        stop_first = self._fine_day(cc, {3: (100.2, 98.5), 10: (102.5, 99.0)})
        tgt_first = self._fine_day(cc, {3: (102.5, 99.8), 10: (101.0, 98.5)})
        still = self._fine_day(cc, {5: (102.5, 98.5)})
        lab = lambda f: _label(b, [_cand(b, 0)], finer=lambda sym, a, z: f).iloc[0]   # noqa: E731
        self.assertEqual(lab(stop_first).outcome, "stop")
        r = lab(tgt_first)
        self.assertEqual((r.outcome, r.status, r.resolved_by_finer_data), ("target", "definitive", True))
        self.assertEqual(lab(still).status, "ambiguous")

    def test_partial_or_dirty_finer_data_never_resolves(self):
        """PR #6 review P2: finer bars must cover the coarse bar exactly, be
        clean and agree with it; otherwise the label stays ambiguous."""
        b = _daily([FLAT, (100, 102.5, 98.5, 100)])
        cc = b.ts_utc.iloc[1]
        good = self._fine_day(cc, {3: (102.5, 99.8), 10: (101.0, 98.5)})
        bad = {
            "partial": good.iloc[:5],
            "truncated_end": good.iloc[:20],      # consistent extremes and close, but stops before the coarse close
            "late_start": good.iloc[2:],
            "gap": good.drop(index=12),
            "dirty": good.assign(quality_flags=[0] * 25 + [3]),
            "inconsistent": good.assign(low=good["low"].clip(lower=99.0)),       # never touches the coarse low
            "empty": good.iloc[0:0],
            "unsorted": good.iloc[::-1],
        }
        for name, f in bad.items():
            r = _label(b, [_cand(b, 0)], finer=lambda sym, a, z, f=f: f).iloc[0]
            self.assertEqual(r.status, "ambiguous", name)
            self.assertFalse(r.resolved_by_finer_data, name)
            self.assertIn("finer_data_rejected:", r.data_flags, name)
        none = _label(b, [_cand(b, 0)], finer=lambda sym, a, z: None).iloc[0]
        self.assertEqual(none.status, "ambiguous")
        ok, why = validate_finer(good, pd.Timestamp(cc) - pd.Timedelta(390, unit="m"), pd.Timestamp(cc),
                                 100.0, 102.5, 98.5, 100.0)
        self.assertEqual((ok, why), (True, "ok"))

    def test_candidate_validation_and_frames(self):
        b = _daily([FLAT, FLAT])
        ok = _cand(b, 0, reasons=("a", "b"))
        for bad in (dict(stage="won"), dict(direction="up"), dict(source="web")):
            with self.assertRaises(ValueError):
                CandidateEvent(**{**ok.__dict__, **bad})
        with self.assertRaises(ValueError):
            CandidateEvent(**{**ok.__dict__, "decision_ts_utc": pd.Timestamp("2024-01-02")})
        with self.assertRaises(ValueError):
            Bracket(0, 1, 1)
        f = to_frame([ok])
        self.assertEqual(f.loc[0, "reasons"], "a|b")
        self.assertEqual(stage_counts([ok, ok]), {"eligible": 2})
        self.assertEqual(make_candidate_id("X", ok.decision_ts_utc, "long", "t"), ok.candidate_id)
        # backtest entries have their own stage: simulated, never counted as an executed order
        sim = CandidateEvent(**{**ok.__dict__, "stage": "simulated_entry"})
        self.assertEqual(stage_counts([sim]), {"simulated_entry": 1})
        self.assertNotIn("executed", stage_counts([sim]))


class A2_EffectiveN(unittest.TestCase):
    def test_average_uniqueness(self):
        u = average_uniqueness(["A"] * 3, [0, 10, 20], [4, 14, 24])
        np.testing.assert_allclose(u, 1.0)
        u = average_uniqueness(["A"] * 4, [0, 0, 0, 0], [9, 9, 9, 9])
        np.testing.assert_allclose(u, 0.25)
        u = average_uniqueness(["A", "B"], [0, 0], [9, 9])            # different symbols do not overlap
        np.testing.assert_allclose(u, 1.0)
        with self.assertRaises(ValueError):
            average_uniqueness(["A"], [5], [4])

    def test_degenerate_clustering_fails_closed(self):
        """PR #6 review P1: one decision day, one sector or a constant outcome
        must give NO effective N (NaN + insufficient_evidence), never raw N."""
        rng = stream(3, "degenerate")
        x = rng.standard_normal(200)
        one_day = effective_n(x, dates=np.zeros(200), symbols=np.arange(200) % 7)
        self.assertTrue(np.isnan(one_day["n_effective"]))
        self.assertEqual(one_day["status"], "insufficient_evidence")
        self.assertTrue(np.isnan(one_day["n_cluster_date"]))
        one_sector = effective_n(x, dates=np.arange(200) % 40, sectors=np.array(["S"] * 200))
        self.assertTrue(np.isnan(one_sector["n_effective"]))
        const = effective_n(np.ones(50), dates=np.arange(50) % 10)
        self.assertTrue(np.isnan(const["n_effective"]))
        self.assertIn("constant outcome", const["reasons"])
        tiny = effective_n([1.0, 2.0], dates=[0, 1])
        self.assertTrue(np.isnan(tiny["n_effective"]))
        bare = effective_n(x)
        self.assertTrue(np.isnan(bare["n_effective"]))
        ok = effective_n(x, dates=np.arange(200) % 40, symbols=np.arange(200) % 7)
        self.assertEqual(ok["status"], "ok")
        self.assertTrue(1.0 <= ok["n_effective"] <= 200)

    def test_cluster_effective_n(self):
        rng = stream(1, "neff")
        x = rng.standard_normal(600)
        e = effective_n(x, dates=np.arange(600), symbols=np.arange(600) % 30)
        self.assertGreater(e["n_cluster_date"], 450)
        day_effect = np.repeat(rng.standard_normal(20), 30)            # 20 days x 30 identical names
        e2 = effective_n(day_effect + 0.01 * rng.standard_normal(600), dates=np.repeat(np.arange(20), 30),
                         symbols=np.tile(np.arange(30), 20))
        self.assertLess(e2["n_cluster_date"], 30)
        self.assertLessEqual(e2["n_effective"], e2["n_cluster_date"])
        self.assertLessEqual(e2["n_effective"], 600)
        self.assertTrue(np.isnan(cluster_variance_of_mean(x, np.zeros(600))))


class A3_ParityReplayGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = generate(MarketSpec(n_symbols=3, n_sessions=90, timeframe="15m", seed=31))
        cls.cands = []
        for s, b in cls.m.bars.items():
            cls.cands += scanner_stage_candidates(multi_timeframe(b), s, rng=stream(2, s),
                                                  bracket=Bracket(1.0, 1.5, 26), rejected_rate=0.05)
        cls.lab = label_candidates(cls.cands, cls.m.bars, timeframe="15m")

    def test_exact_m17_parity_on_unambiguous_paths(self):
        pr = parity.check(self.lab, self.m.bars, {c.candidate_id: c for c in self.cands}, timeframe="15m")
        self.assertGreater(pr.n_compared, 100)
        self.assertEqual(pr.n_mismatch, 0, pr.mismatches[:3])
        self.assertTrue(pr.exact)

    def test_checker_flags_price_and_outcome_disagreements(self):
        b = _daily([FLAT, (100, 101, 99.5, 100.5), (100.5, 102.5, 100, 102), FLAT])
        c = _cand(b, 0)
        lab = label_candidates([c], {"X": b}, timeframe="1D", cost=ZERO, stress_cost=ZERO)
        ok = parity.check(lab, {"X": b}, {c.candidate_id: c}, timeframe="1D")
        self.assertEqual((ok.n_match, ok.n_mismatch), (1, 0))
        wrong_px = lab.assign(exit_price=lab.exit_price + 0.01)
        self.assertEqual(parity.check(wrong_px, {"X": b}, {c.candidate_id: c}, timeframe="1D").n_mismatch, 1)
        wrong_out = lab.assign(outcome="stop")
        r = parity.check(wrong_out, {"X": b}, {c.candidate_id: c}, timeframe="1D")
        self.assertEqual((r.n_mismatch, r.n_known_divergence), (1, 0))

    def test_known_divergence_is_classified_not_hidden(self):
        b = _daily([FLAT, (100, 100.5, 99.5, 100), (103, 103.5, 98.0, 99.0), FLAT])
        c = _cand(b, 0)
        lab = label_candidates([c], {"X": b}, timeframe="1D", cost=ZERO, stress_cost=ZERO)
        self.assertEqual(lab.iloc[0].outcome, "target")
        pr = parity.check(lab, {"X": b}, {c.candidate_id: c}, timeframe="1D")
        self.assertEqual((pr.n_compared, pr.n_match, pr.n_known_divergence, pr.n_mismatch), (1, 0, 1, 0))

    def test_replay_eligible_matches_m17_scanner_replica_fires(self):
        from bot.backtesting.mtf_context import MultiTimeframeContext
        from bot.backtesting.strategy import ScannerReplicaStrategy
        s = "S000"
        mt = multi_timeframe(self.m.bars[s])
        strat = ScannerReplicaStrategy({})
        strat.attach_context(MultiTimeframeContext(mt, anchor_tf="15m"))
        sig = strat.run(mt["15m"])
        fires = set(pd.to_datetime(mt["15m"]["ts_utc"], utc=True)[sig["signal"] == 1])
        mine = {c.decision_ts_utc for c in self.cands if c.symbol == s and c.direction == "long" and c.stage == "eligible"}
        self.assertEqual(mine, fires)
        self.assertGreater(len(fires), 0)
        for c in self.cands:
            if c.stage == "near_miss":
                self.assertEqual(c.features["valid_count"], c.features["min_valid"] - 1)
            if c.stage == "rejected":
                self.assertAlmostEqual(c.features["sample_weight"], 20.0)
        self.assertEqual(len({c.candidate_id for c in self.cands}), len(self.cands))

    def test_gate_layer_and_gate_economics(self):
        rules = {"low_volume": lambda e: e.features["vol_ratio"] < 0.8}
        with self.assertRaises(ValueError):                       # no wall-clock fallback
            apply_gates(self.cands, rules, max_entries_per_day=2)
        g = apply_gates(self.cands, rules, max_entries_per_day=2,
                        release_times=position_release_times(self.lab))
        ex = {e.candidate_id: e for e in g if e.stage == "executed"}
        rel = position_release_times(self.lab)
        for e in g:                                                # bar-time capacity holds
            if e.stage == "executed":
                same = [x for x in ex.values() if x.symbol == e.symbol and x.decision_ts_utc < e.decision_ts_utc]
                for x in same:
                    self.assertTrue(rel.get(x.candidate_id) is not None and e.decision_ts_utc >= rel[x.candidate_id])
        counts = stage_counts(g)
        for st in ("executed", "blocked_gate", "blocked_risk"):
            self.assertIn(st, counts)
        self.assertEqual(sum(counts.values()), len(self.cands))
        self.assertTrue(all(e.reasons == ("low_volume",) for e in g if e.stage == "blocked_gate"))
        per_day = pd.Series([e.decision_ts_utc.floor("D") for e in g if e.stage == "executed"]).value_counts()
        self.assertLessEqual(per_day.max(), 2)
        lab = pd.DataFrame({"decision_ts_utc": pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC").repeat(2),
                            "stage": ["executed", "blocked_gate"] * 40, "status": "definitive",
                            "reasons": ["", "g1"] * 40, "r_net": [1.0, -1.0] * 40})
        amb = pd.DataFrame({"decision_ts_utc": pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC"),
                            "stage": "blocked_gate", "status": "ambiguous", "reasons": "g1", "r_net": np.nan})
        rows = GE.gate_table(pd.concat([lab, amb], ignore_index=True), n_boot=200)
        self.assertEqual((rows[0].n_blocked, rows[0].n_blocked_definitive), (50, 40))
        self.assertAlmostEqual(rows[0].share_ambiguous, 0.2)
        self.assertAlmostEqual(rows[0].mean_r_blocked, -1.0)
        rows = GE.gate_table(lab, n_boot=200)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].diff_passed_minus_blocked, 2.0)
        self.assertGreater(rows[0].diff_ci_lo, 0)
        self.assertEqual(rows[0].avoided_loss_r, 40.0)
        self.assertIn("Observational", GE.to_markdown(rows))


class A3b_BarTimeCapacity(unittest.TestCase):
    def test_position_held_overnight_blocks_next_session(self):
        """PR #6 review P2: a 26-bar hold opened late in a session is still open
        the next morning; a 6.5-hour wall-clock rule would wrongly free it."""
        ts = [pd.Timestamp("2024-01-02 20:45", tz="UTC"), pd.Timestamp("2024-01-03 14:45", tz="UTC"),
              pd.Timestamp("2024-01-04 20:00", tz="UTC")]
        evs = [CandidateEvent(candidate_id="c%d" % i, symbol="X", decision_ts_utc=t, direction="long",
                              stage="eligible", source="synthetic", timeframe="15m", strategy_id="t",
                              atr_at_decision=1.0) for i, t in enumerate(ts)]
        rel = {"c0": pd.Timestamp("2024-01-03 20:30", tz="UTC"), "c1": None, "c2": None}
        g = {e.candidate_id: e.stage for e in apply_gates(evs, {}, release_times=rel)}
        self.assertEqual(g, {"c0": "executed", "c1": "blocked_risk", "c2": "executed"})
        rel_pending = {"c0": None}
        g2 = {e.candidate_id: e.stage for e in apply_gates(evs, {}, release_times=rel_pending)}
        self.assertEqual(g2, {"c0": "executed", "c1": "blocked_risk", "c2": "blocked_risk"})
        lab = pd.DataFrame({"candidate_id": ["a", "b", "c"], "status": ["definitive", "unfillable", "pending"],
                            "exit_ts_utc": [pd.Timestamp("2024-01-03", tz="UTC"), None, None]})
        self.assertEqual(position_release_times(lab), {"a": pd.Timestamp("2024-01-03", tz="UTC"), "b": NO_POSITION,
                                                      "c": None})

    @staticmethod
    def _evs(times, prefix="c"):
        return [CandidateEvent(candidate_id="%s%d" % (prefix, i), symbol="X", decision_ts_utc=t, direction="long",
                               stage="eligible", source="synthetic", timeframe="1D", strategy_id="t",
                               atr_at_decision=1.0) for i, t in enumerate(times)]

    def test_missing_release_entries_fail_closed(self):
        """Second review P1: an empty or partial release map must never turn an
        unknown exit into free capacity."""
        evs = self._evs([pd.Timestamp("2024-01-02 21:00", tz="UTC"), pd.Timestamp("2024-01-03 21:00", tz="UTC"),
                         pd.Timestamp("2024-01-04 21:00", tz="UTC")])
        with self.assertRaisesRegex(ValueError, "no entry for accepted candidate c0"):
            apply_gates(evs, {}, release_times={})
        with self.assertRaisesRegex(ValueError, "no entry for accepted candidate c0"):
            apply_gates(evs, {}, release_times={"c1": None, "c2": None})            # first position unknown
        with self.assertRaisesRegex(ValueError, "no entry for accepted candidate c1"):
            apply_gates(evs, {}, release_times={"c0": pd.Timestamp("2024-01-02 21:00", tz="UTC")})
        rej = apply_gates(evs, {"always": lambda e: True}, release_times={})         # nothing accepted: no lookup
        self.assertEqual({e.stage for e in rej}, {"blocked_gate"})

    def test_weekend_overlap_and_unfillable_positions(self):
        fri, mon, wed = (pd.Timestamp(d + " 21:00", tz="UTC") for d in ("2024-01-05", "2024-01-08", "2024-01-10"))
        evs = self._evs([fri, mon, wed])
        rel = {"c0": pd.Timestamp("2024-01-09 21:00", tz="UTC"), "c1": None, "c2": None}
        self.assertEqual([e.stage for e in apply_gates(evs, {}, release_times=rel)],
                         ["executed", "blocked_risk", "executed"])                  # held over the weekend
        rel_unf = {"c0": NO_POSITION, "c1": pd.Timestamp("2024-01-09 21:00", tz="UTC"), "c2": None}
        out = apply_gates(evs, {}, release_times=rel_unf)
        # Third review (PR #6): an unfillable candidate is never "executed" and never holds the slot
        self.assertEqual([e.stage for e in out], ["unfillable", "executed", "executed"])   # c1 released Tue
        self.assertEqual(out[0].reasons, ("no_position_possible",))

    def test_unfillable_candidates_use_no_daily_capacity(self):
        """Third review (PR #6): with a daily cap of one, an unfillable candidate first in the day must not take
        the day's slot from the next fillable candidate."""
        d1, d2 = (pd.Timestamp(d, tz="UTC") for d in ("2024-01-08 15:00", "2024-01-08 18:00"))
        evs = [CandidateEvent(candidate_id="u", symbol="A", decision_ts_utc=d1, direction="long", stage="eligible",
                              source="synthetic", timeframe="1H", strategy_id="t", atr_at_decision=1.0),
               CandidateEvent(candidate_id="f", symbol="B", decision_ts_utc=d2, direction="long", stage="eligible",
                              source="synthetic", timeframe="1H", strategy_id="t", atr_at_decision=1.0)]
        out = apply_gates(evs, {}, max_entries_per_day=1, release_times={"u": NO_POSITION, "f": None})
        self.assertEqual([(e.candidate_id, e.stage) for e in out], [("u", "unfillable"), ("f", "executed")])
        self.assertNotIn("executed", [e.stage for e in out if e.candidate_id == "u"])
        # the same pair with the unfillable one made fillable: now the cap binds on the second
        out2 = apply_gates(evs, {}, max_entries_per_day=1, release_times={"u": None, "f": None})
        self.assertEqual([e.stage for e in out2], ["executed", "skipped_capacity"])


class A3c_BarOpenConvention(unittest.TestCase):
    def test_real_data_needs_explicit_bar_opens(self):
        """Second review P2: the synthetic 390-minute daily-bar convention is refused for real providers."""
        m = generate(MarketSpec(n_symbols=1, n_sessions=60, seed=4))
        b = m.bars["S000"].copy()
        ts = pd.Timestamp(b["ts_utc"].iloc[30])
        cd = CandidateEvent(candidate_id="x", symbol="S000", decision_ts_utc=ts, direction="long",
                            stage="eligible", source="historical_replay", timeframe="1D", strategy_id="t", atr_at_decision=1.0)
        with self.assertRaisesRegex(ValueError, "explicit bar_open_ts"):
            label_candidates([cd], {"S000": b}, timeframe="1D", source=SourceInfo("vendorX", "1D", True))
        real = b.assign(bar_open_ts=pd.to_datetime(b["ts_utc"], utc=True) - pd.Timedelta(7, unit="h"))
        lab = label_candidates([cd], {"S000": real}, timeframe="1D", source=SourceInfo("vendorX", "1D", True))
        self.assertEqual(lab["entry_ts_utc"].iloc[0], real["bar_open_ts"].iloc[31])
        with self.assertRaisesRegex(ValueError, "strictly before"):
            label_candidates([cd], {"S000": b.assign(bar_open_ts=b["ts_utc"])}, timeframe="1D",
                             source=SourceInfo("vendorX", "1D", True))
        syn = label_candidates([cd], {"S000": b}, timeframe="1D", source=SourceInfo("synthetic", "1D", False))
        self.assertEqual(syn["entry_ts_utc"].iloc[0], pd.Timestamp(b["ts_utc"].iloc[31]) - pd.Timedelta(390, unit="m"))


class A4_SnapshotAdapter(unittest.TestCase):
    def _fixture(self):
        return pd.DataFrame({
            "id": [1, 2, 3, 4], "cycle_id": [7, 7, 7, 8],
            "timestamp": ["2026-10-20T14:45:00+00:00"] * 3 + ["2026-10-20T15:00:00+00:00"],
            "symbol": ["AAA", "AAA", "BBB", "AAA"], "direction": ["LONG", "LONG", "short", "long"],
            "stage": ["scanned", "final_signal", "partial_confluence", "rejected"],
            "valid_count": [0, 3, 2, 1], "tfs_passing": ["", "1D+4H+1H", "1D+4H", "1D"],
            "available_tfs": [4, 4, 4, 4], "min_valid": [3, 3, 3, 3],
            "rejection_reason": ["", "", "", "confluence"], "route": ["", "IBKR", "", ""],
            "strategy_version": [1, 1, 1, 1], "signal_id": [None, 55, None, None],
            "rsi": [50.0, 55.0, 60.0, np.nan], "macd_hist": [0.1] * 4, "atr": [1.2, 1.2, 0.8, 1.1],
            "bb_pos": [0.5] * 4, "vwap_dev": [0.0] * 4, "vol_ratio": [1.0] * 4})

    def test_mapping_and_dedup(self):
        ev = from_candidate_snapshots(self._fixture())
        self.assertEqual(len(ev), 3)
        by = {(e.symbol, e.decision_ts_utc.minute): e for e in ev}
        a = by[("AAA", 45)]
        self.assertEqual((a.stage, a.direction, a.source, a.strategy_id), ("eligible", "long", "scanner_live", "scanner_v1"))
        self.assertEqual(a.features["signal_id"], 55)
        self.assertEqual(by[("BBB", 45)].stage, "near_miss")
        self.assertEqual(by[("AAA", 0)].reasons, ("confluence",))
        self.assertIsNone(by[("AAA", 0)].features["rsi"])

    def test_rejects_unknown_values(self):
        f = self._fixture()
        with self.assertRaisesRegex(ValueError, "unknown candidate_snapshots stage"):
            from_candidate_snapshots(f.assign(stage=["won"] * 4))
        with self.assertRaises(ValueError):
            from_candidate_snapshots(f.assign(direction=["up"] * 4))
        with self.assertRaises(ValueError):
            from_candidate_snapshots(f.drop(columns=["atr"]))


class A5_EvidenceRun(unittest.TestCase):
    def test_small_evidence_run(self):
        with tempfile.TemporaryDirectory() as d:
            s = EVA.run(Path(d), n_symbols=3, n_sessions=90)
            self.assertEqual(s["stage_replay"]["parity"]["mismatch"], 0)
            self.assertGreater(s["stage_replay"]["eff_all_stages"]["n_effective"],
                               s["stage_replay"]["eff_executed_only"]["n_effective"])
            dq = s["data_quality"]
            self.assertEqual(dq["windows_crossing_a_split"], dq["of_which_not_definitive"])
            tight = s["eod_ambiguity"]["grid"][0]
            self.assertGreater(tight["share_ambiguous_eod"], tight["share_ambiguous_after_15m"])
            md = (Path(d) / "lane_a_evidence.md").read_text()
            self.assertIn("not P&L", md)
            self.assertNotIn("nan %", md)


if __name__ == "__main__":
    unittest.main()

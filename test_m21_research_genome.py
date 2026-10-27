"""test_m21_research_genome.py — lane D: Strategy Genome v0, the four templates,
the multi-symbol backtester and ledgered search (B09, B10, B11, A15).

Offline and deterministic. The tests prove that:
- genomes are validated fail-closed;
- templates are causal (perturbing the future changes nothing in the past);
- the portfolio engine reproduces M17 trade-for-trade and keeps exact
  accounting;
- the engine enforces capacity, exposure, cash, membership, delisting, time
  and trailing exits;
- every search evaluation is a ledgered trial.

Run via ``python _test_isolation.py test_m21_research_genome``.
"""
from __future__ import annotations

import json
import unittest
import warnings

import numpy as np
import pandas as pd

from research.common.synthetic import MarketSpec, generate
from research.genome import engine as E
from research.genome import parity as PAR
from research.genome import schema as G
from research.genome import search as SR
from research.genome import templates as T
from research.integrity.ledger import Ledger

warnings.filterwarnings("ignore", category=RuntimeWarning)


def _market(n_symbols=4, n_sessions=500, seed=5, **kw):
    return generate(MarketSpec(n_symbols=n_symbols, n_sessions=n_sessions, seed=seed, **kw))


def _bars(rows, start="2020-01-02"):
    """Hand-built daily bars from (open, high, low, close[, volume]) tuples."""
    ts = pd.date_range(start, periods=len(rows), freq="D", tz="UTC") + pd.Timedelta(21, unit="h")
    a = np.array([r[:4] for r in rows], float)
    vol = [r[4] if len(r) > 4 else 1e6 for r in rows]
    return pd.DataFrame({"ts_utc": ts, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3],
                         "volume": vol, "quality_flags": 0})


def _sig(n, entries=(), exits=(), atr=1.0, score=0.0):
    s = pd.DataFrame({"entry": np.zeros(n, bool), "exit": np.zeros(n, bool), "atr": np.full(n, atr),
                      "score": np.full(n, score, dtype=float)})
    s.loc[list(entries), "entry"] = True
    s.loc[list(exits), "exit"] = True
    return s


FLAT = (100.0, 101.0, 99.0, 100.0)
NOFEE = E.EngineConfig(initial_equity=100_000.0, fee_bps=0.0, slippage_bps=0.0, m17_compat=True)


class D1_Genome(unittest.TestCase):
    def test_every_template_seeds_a_valid_genome(self):
        for t in list(T.STARTER_TEMPLATES) + ["sma_crossover"]:
            g = G.seed(t)
            self.assertTrue(g.genome_id.startswith(t + "-"))
            self.assertEqual(G.from_dict(json.loads(json.dumps(g.to_dict()))), g)
        self.assertEqual(len(T.STARTER_TEMPLATES), 4)

    def test_semantic_id_tracks_behaviour_not_lineage(self):
        g = G.seed("mean_reversion")
        self.assertEqual(G.seed("mean_reversion").genome_id, g.genome_id)
        h = G.with_params(g, rsi_entry=25.0)
        self.assertNotEqual(h.genome_id, g.genome_id)
        self.assertEqual(h.parent_id, g.genome_id)
        relabelled = G.Genome(h.template, h.params, h.exits, h.risk, h.timeframe, h.direction, "other", "manual")
        self.assertEqual(relabelled.genome_id, h.genome_id)

    def test_validator_is_fail_closed_and_reports_everything(self):
        bad = dict(G.seed("breakout_momentum").to_dict())
        bad.update({"direction": "short", "timeframe": "1m"})
        bad["params"] = {**bad["params"], "lookback": 999, "mystery": 1}
        bad["exits"] = {**bad["exits"], "stop_atr": None}
        bad["risk"] = {"risk_per_trade": 0.05, "max_position_pct": 0.9}
        bad.pop("genome_id")
        with self.assertRaises(G.GenomeError) as cm:
            G.from_dict(bad)
        msg = " | ".join(cm.exception.reasons)
        for s in ("long-only", "timeframe", "lookback=999", "unknown parameter 'mystery'", "stop_atr is mandatory",
                  "risk_per_trade", "max_position_pct"):
            self.assertIn(s, msg)
        self.assertGreaterEqual(len(cm.exception.reasons), 7)

    def test_unknown_keys_missing_params_constraints_and_forged_ids(self):
        d = G.seed("trend_confluence").to_dict()
        with self.assertRaises(G.GenomeError):
            G.from_dict({**d, "broker": "ibkr"})
        with self.assertRaises(G.GenomeError):
            G.from_dict({**d, "exits": {**d["exits"], "leverage": 3}})
        p = dict(d["params"])
        p.pop("min_votes")
        with self.assertRaises(G.GenomeError):
            G.from_dict({**{k: v for k, v in d.items() if k != "genome_id"}, "params": p})
        with self.assertRaises(G.GenomeError):
            G.with_params(G.seed("trend_confluence"), ema_fast=60, ema_slow=50)
        with self.assertRaises(G.GenomeError):
            G.from_dict({**d, "genome_id": "trend_confluence-000000000000"})
        with self.assertRaises(G.GenomeError):
            G.with_params(G.seed("mean_reversion"), rsi_period=14.5)
        with self.assertRaises(G.GenomeError):
            G.with_params(G.seed("mean_reversion"), rsi_mode="ema")

    def test_mutation_stays_in_bounds_and_records_lineage(self):
        rng = np.random.default_rng(0)
        g = G.seed("pullback_in_trend")
        for _ in range(200):
            h = G.mutate(g, rng, n_changes=2)
            G.validate(h)
            if h is not g:
                self.assertEqual(h.parent_id, g.genome_id)
                self.assertEqual(h.origin, "mutation")
            g = h


class D2_Templates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _market(n_symbols=3, n_sessions=600)

    def test_templates_are_causal(self):
        rng = np.random.default_rng(1)
        for t in list(T.STARTER_TEMPLATES) + ["sma_crossover"]:
            g = G.seed(t)
            for s, b in self.m.bars.items():
                base = T.signals(g, b)
                k = 400
                b2 = b.copy()
                f = rng.uniform(0.5, 1.5, size=len(b) - k - 1)
                for col in ("open", "high", "low", "close"):
                    b2.loc[k + 1:, col] = b2.loc[k + 1:, col].to_numpy() * f
                b2.loc[k + 1:, "volume"] = b2.loc[k + 1:, "volume"].to_numpy() * rng.uniform(0.2, 5, len(f))
                pert = T.signals(g, b2)
                pd.testing.assert_frame_equal(base.iloc[:k + 1], pert.iloc[:k + 1], obj="%s/%s" % (t, s))

    def test_templates_fire_and_respect_contract(self):
        for t in T.STARTER_TEMPLATES:
            g = G.seed(t)
            n = sum(int(T.signals(g, b)["entry"].sum()) for b in self.m.bars.values())
            self.assertGreater(n, 5, t)
            out = T.signals(g, next(iter(self.m.bars.values())))
            self.assertEqual(tuple(out.columns), T.SIGNAL_COLUMNS)
        g = G.seed("mean_reversion")
        g = G.Genome(g.template, g.params, G.ExitSpec(signal_exit=False))
        self.assertFalse(T.signals(g, next(iter(self.m.bars.values())))["exit"].any())

    def test_breakout_fires_on_a_volume_breakout(self):
        rows = [(100.0, 100.5, 99.5, 100.0 + 0.1 * np.sin(i), 1e6) for i in range(80)]
        rows.append((100.2, 106.0, 100.0, 105.0, 4e6))
        b = _bars(rows)
        sig = T.signals(G.seed("breakout_momentum"), b)
        self.assertTrue(sig["entry"].iloc[-1])
        self.assertFalse(sig["entry"].iloc[:-1].any())


class D3_Engine(unittest.TestCase):
    def test_m17_parity_trade_for_trade(self):
        m = _market(n_symbols=3, n_sessions=700, seed=9)
        n_trades = 0
        for s, b in m.bars.items():
            for sp, tp in ((0.05, 0.10), (None, None), (0.02, None), (None, 0.03)):
                r = PAR.compare(b, s, stop_pct=sp, target_pct=tp)
                self.assertEqual(r["mismatches"], [], (s, sp, tp))
                n_trades += r["trades_m17"]
        self.assertGreater(n_trades, 100)

    def test_next_open_execution_and_accounting_identity(self):
        b = _bars([FLAT] * 5 + [(101, 103, 100, 102)] * 5 + [FLAT] * 5)
        res = E.run({"A": b}, {"A": _sig(15, entries=[4], exits=[9])}, G.ExitSpec(stop_atr=50), G.RiskSpec(),
                    E.EngineConfig(fee_bps=5, slippage_bps=5))
        t = res.trades[0]
        self.assertEqual(t.entry_ts, pd.Timestamp(b["ts_utc"].iloc[5]))
        self.assertAlmostEqual(t.entry_price, 101 * 1.0005)
        self.assertEqual(t.exit_ts, pd.Timestamp(b["ts_utc"].iloc[10]))
        self.assertEqual(t.exit_reason, "signal")
        self.assertAlmostEqual(res.equity.iloc[-1], 100_000 + sum(x.pnl_absolute for x in res.trades), places=6)

    def test_stop_first_on_ties_and_gap_fills_at_open(self):
        rows = [FLAT] * 3 + [(100, 120, 80, 100), FLAT]
        res = E.run({"A": _bars(rows)}, {"A": _sig(5, entries=[2], atr=5.0)}, G.ExitSpec(stop_atr=2, target_atr=2),
                    G.RiskSpec(), NOFEE)
        self.assertEqual(res.trades[0].exit_reason, "stop_loss")
        self.assertAlmostEqual(res.trades[0].exit_price, 90.0)
        rows = [FLAT] * 3 + [(100, 101, 99, 100), (80, 81, 79, 80), FLAT]
        res = E.run({"A": _bars(rows)}, {"A": _sig(6, entries=[2], atr=5.0)}, G.ExitSpec(stop_atr=2, target_atr=None),
                    G.RiskSpec(), NOFEE)
        self.assertEqual((res.trades[0].exit_reason, res.trades[0].exit_price), ("stop_loss", 80.0))
        self.assertLess(res.trades[0].r_multiple, -1.0)

    def test_capacity_ranking_and_exposure_caps(self):
        n = 8
        bars = {s: _bars([FLAT] * n) for s in ("A", "B", "C", "D")}
        sigs = {s: _sig(n, entries=[2], score=sc) for s, sc in zip("ABCD", (0.1, 0.9, 0.5, 0.7))}
        res = E.run(bars, sigs, G.ExitSpec(stop_atr=5, target_atr=None), G.RiskSpec(0.01, 0.2),
                    E.EngineConfig(max_positions=2))
        self.assertEqual(sorted(t.symbol for t in res.trades), ["B", "D"])
        self.assertEqual(res.skipped["max_positions"], 2)
        res = E.run(bars, sigs, G.ExitSpec(stop_atr=5, target_atr=None), G.RiskSpec(0.02, 0.25),
                    E.EngineConfig(max_positions=4, max_gross_exposure=0.3))
        gross = sum(t.qty * t.entry_price for t in res.trades)
        self.assertLessEqual(gross, 0.3 * 100_000 + 1e-6)
        self.assertGreater(res.skipped["gross_exposure"], 0)

    def test_cash_never_negative_and_deterministic(self):
        m = _market(n_symbols=6, n_sessions=500, seed=2)
        g = G.with_params(G.seed("trend_confluence"), min_votes=3)
        g = G.Genome(g.template, g.params, g.exits, G.RiskSpec(0.02, 0.25), parent_id=g.parent_id)
        r1 = E.run_genome(g, m.bars, E.EngineConfig(max_positions=6, max_gross_exposure=1.0))
        r2 = E.run_genome(g, m.bars, E.EngineConfig(max_positions=6, max_gross_exposure=1.0))
        self.assertEqual(r1.digest, r2.digest)
        self.assertGreater(len(r1.trades), 20)
        self.assertGreater(r1.equity.min(), 0)
        self.assertAlmostEqual(r1.equity.iloc[-1], 100_000 + sum(t.pnl_absolute for t in r1.trades), places=4)

    def test_membership_blocks_entries(self):
        n = 6
        bars = {"A": _bars([FLAT] * n), "B": _bars([FLAT] * n)}
        sigs = {s: _sig(n, entries=[2]) for s in bars}
        mem = {"A": np.ones(n, bool), "B": np.zeros(n, bool)}
        res = E.run(bars, sigs, G.ExitSpec(stop_atr=5), G.RiskSpec(), E.EngineConfig(), membership=mem)
        self.assertEqual([t.symbol for t in res.trades], ["A"])
        self.assertEqual(res.skipped["not_in_universe"], 1)

    def test_delisting_time_stop_trailing_and_missing_bars(self):
        long_b, short_b = _bars([FLAT] * 10), _bars([FLAT] * 5)
        res = E.run({"A": long_b, "B": short_b}, {"A": _sig(10), "B": _sig(5, entries=[1])},
                    G.ExitSpec(stop_atr=5, target_atr=None, time_stop_bars=50), G.RiskSpec(), NOFEE)
        self.assertEqual([(t.symbol, t.exit_reason) for t in res.trades], [("B", "delisted")])
        self.assertEqual(res.trades[0].exit_ts, pd.Timestamp(short_b["ts_utc"].iloc[-1]))
        cfg = E.EngineConfig(fee_bps=0, slippage_bps=0)
        res = E.run({"A": long_b}, {"A": _sig(10, entries=[1])}, G.ExitSpec(stop_atr=5, target_atr=None,
                                                                            time_stop_bars=3), G.RiskSpec(), cfg)
        self.assertEqual((res.trades[0].exit_reason, res.trades[0].bars_held), ("time", 3))
        up = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(8)] + [(107, 108, 103.5, 104)]
        res = E.run({"A": _bars(up)}, {"A": _sig(9, entries=[0], atr=1.0)},
                    G.ExitSpec(stop_atr=5, target_atr=None, time_stop_bars=50, trail_atr=2.0), G.RiskSpec(), cfg)
        self.assertEqual(res.trades[0].exit_reason, "stop_loss")
        self.assertAlmostEqual(res.trades[0].exit_price, 107 - 2.0)
        b = _bars([FLAT] * 6).drop(index=3).reset_index(drop=True)
        res = E.run({"A": b, "Z": _bars([FLAT] * 6)}, {"A": _sig(5, entries=[2]), "Z": _sig(6)},
                    G.ExitSpec(stop_atr=5, target_atr=None), G.RiskSpec(), cfg)
        self.assertEqual(res.trades[0].entry_ts, pd.Timestamp(b["ts_utc"].iloc[3]))

    def test_engine_rejects_misaligned_or_unsorted_inputs(self):
        b = _bars([FLAT] * 5)
        with self.assertRaises(ValueError):
            E.run({"A": b}, {"A": _sig(4)}, G.ExitSpec(), G.RiskSpec())
        with self.assertRaises(ValueError):
            E.run({"A": b.iloc[::-1].reset_index(drop=True)}, {"A": _sig(5)}, G.ExitSpec(), G.RiskSpec())
        with self.assertRaises(ValueError):
            E.EngineConfig(stop_mode="none")


class D4_Search(unittest.TestCase):
    def test_grid_evaluate_plateau_and_ledger(self):
        m = _market(n_symbols=3, n_sessions=400, seed=4)
        axes = {"rsi_entry": [20.0, 30.0, 45.0], "rsi_exit": [45.0, 55.0]}
        gs, bad = SR.grid("mean_reversion", axes)
        self.assertEqual((len(gs), bad), (5, 1))                  # rsi_entry=45 would need rsi_exit > 45
        L = Ledger()
        df = SR.evaluate(gs, m.bars, ledger=L, family_id="d:test", data_digest=SR.data_digest(m.bars),
                         keep_equity=True)
        self.assertEqual(L.family_trial_count("d:test"), 5)
        L.assert_ledgered(df["trial_id"])
        sel = SR.select(df, {k: v for k, v in axes.items()}, ledger=L, family_id="d:test")
        self.assertEqual(sel["n_trials_family"], 5)
        self.assertEqual(sel["dsr_best_isolated"]["n_trials"], 5.0)
        self.assertLessEqual(sel["best_isolated"]["plateau"], sel["best_isolated"]["sharpe_ann"] + 1e-12)

    def test_plateau_is_the_neighbourhood_median(self):
        axes = {"a": [1, 2, 3], "b": [1, 2]}
        df = pd.DataFrame([{"a": a, "b": b, "m": v} for (a, b), v in
                           {(1, 1): 0.0, (2, 1): 9.0, (3, 1): 0.0, (1, 2): 1.0, (2, 2): 1.0, (3, 2): 1.0}.items()])
        pl = SR.plateau(df, axes, "m")
        self.assertEqual(pl[1], 0.5)          # the 9.0 spike: median of {9, 0, 0, 1}
        self.assertEqual(pl[4], 1.0)

    def test_baselines(self):
        m = _market(n_symbols=3, n_sessions=300, seed=1)
        b = SR.baselines(m.bars)
        self.assertEqual(b["no_trade"]["total_return"], 0.0)
        manual = np.mean([bb["close"].iloc[-1] * (1 - 5e-4) / (bb["open"].iloc[0] * (1 + 5e-4) * (1 + 5e-4))
                          for bb in m.bars.values()]) - 1
        self.assertAlmostEqual(b["buy_and_hold_equal_weight"]["total_return"], manual, places=10)


class D5_EvidenceRun(unittest.TestCase):
    def test_tiny_evidence_run(self):
        import tempfile
        from pathlib import Path
        from research.genome import evidence as EVD
        j = EVD.run_evidence(EVD.DConfig(n_symbols=4, n_sessions=300, n_seeds=2, parity_symbols=1,
                                         parity_sessions=300, profile="test"))
        self.assertEqual(j["parity"]["mismatches"], 0)
        self.assertGreater(j["parity"]["trades"], 10)
        for t, e in EVD.MATCH.items():
            self.assertTrue(j["matrix"][t][e]["matched"])
        self.assertTrue(j["search"]["ledger"]["verified"])
        self.assertEqual(j["search"]["planted_rsi_oversold"]["selection"]["n_trials_family"],
                         j["search"]["planted_rsi_oversold"]["selection"]["n_points"])
        with tempfile.TemporaryDirectory() as d:
            jp, mp = EVD.write(j, Path(d) / "lane_d")
            self.assertEqual(json.loads(jp.read_text())["report_digest"], j["report_digest"])
            md = mp.read_text()
        for s in ("not** evidence of any edge", "Trade-for-trade parity", "paired null", "What this does and does not show"):
            self.assertIn(s, md)


if __name__ == "__main__":
    unittest.main()

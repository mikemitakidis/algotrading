"""test_m21_research_integration.py — the research lanes connected end to end (offline).

The test runs ``research.integration.pipeline``:
- synthetic M16-layout store, snapshot, inventory; point-in-time quality and eligibility (F);
- knowledge library and hypothesis (E);
- ledgered genome search and portfolio run with decision records (D);
- every engine decision labelled with its real stage (A);
- meta-labeling lab on point-in-time candidates (C);
- report card with verified outcome provenance (B).

It asserts the cross-lane invariants; that a split or flag introduced AFTER a cutoff changes nothing before
it (while the old retrospective rule would have); that no broker, network or messaging module was imported;
and, in-process, that no socket connection, DNS lookup or subprocess was attempted during the run.
That last check is an in-process guard, not OS-level isolation (see the pipeline's render()).
Run via ``python _test_isolation.py test_m21_research_integration``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Broker / market-data SDKs, network clients, secret loaders and production runtime modules that the
# research pipeline must never pull in, directly or transitively. (Low-level stdlib modules such as
# socket or subprocess are excluded: third-party libraries may import them without using them; the
# runtime guard below checks that they are not USED.)
BANNED = ("ib_insync", "ibapi", "alpaca", "alpaca_trade_api", "yfinance", "telegram", "requests", "httpx",
          "aiohttp", "websocket", "websockets", "urllib3", "paramiko", "dotenv",
          "bot.brokers", "bot.etoro", "bot.notifier", "bot.paper", "bot.paper_loop_d2", "bot.runtime",
          "bot.scanner", "bot.gateway_health", "bot.gateway_watchdog", "bot.recovery_executor",
          "bot.risk_authority", "bot.kill_switch", "dashboard", "main")


# Local, read-only CPU-topology probes that joblib/loky (used by scikit-learn) attempts in order to count
# physical cores. The guard refuses them too (loky then falls back to os.cpu_count); they are listed so the test
# can tell them apart from anything else.
KNOWN_LIBRARY_PROBES = (("lscpu", "--parse=core"), ("cat", "/proc/cpuinfo"))


class _NoNetwork:
    """Replace the Python-level network / process entry points with recorders that refuse. Each call is
    recorded as (entry point, argv tuple or first argument)."""
    TARGETS = ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
               (socket, "getaddrinfo"), (socket, "gethostbyname"), (subprocess, "Popen"), (os, "system"))

    def __init__(self):
        self.calls, self.saved = [], []

    def __enter__(self):
        for obj, name in self.TARGETS:
            self.saved.append((obj, name, getattr(obj, name)))

            def refuse(*a, _n="%s.%s" % (getattr(obj, "__name__", obj), name), **k):
                first = a[0] if a else k.get("args")
                if _n.startswith("socket.socket"):
                    first = a[1] if len(a) > 1 else None          # (self, address)
                self.calls.append((_n, tuple(first) if isinstance(first, (list, tuple)) else first))
                raise PermissionError("network/process access refused in the offline research pipeline: " + _n)
            setattr(obj, name, refuse)
        return self

    def __exit__(self, *exc):
        for obj, name, fn in self.saved:
            setattr(obj, name, fn)
        return False


class I1_EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from research.integration import pipeline as P
        cls.before = set(sys.modules)
        cls.tmp = tempfile.TemporaryDirectory()
        with _NoNetwork() as guard:
            cls.out = P.run(Path(cls.tmp.name), n_symbols=12, n_sessions=500)
        cls.guard_calls = list(guard.calls)
        cls.loaded = set(sys.modules) - cls.before
        cls.files = sorted(p.relative_to(cls.tmp.name).as_posix() for p in Path(cls.tmp.name).rglob("*")
                           if p.is_file())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cross_lane_invariants(self):
        for name, ok in self.out["checks"].items():
            self.assertTrue(ok, name)

    def test_lanes_produced_results(self):
        o = self.out
        self.assertGreaterEqual(o["genome"]["grid_points"], 6)
        st = o["decisions"]["candidate_stages"]
        self.assertGreater(st.get("simulated_entry", 0), 10)
        self.assertGreater(st.get("rejected", 0), 0)                   # rule rejections are recorded, not traded
        self.assertEqual(st.get("simulated_entry"), o["genome"]["portfolio"]["n_trades"])
        self.assertGreater(o["decisions"]["per_stage"]["simulated_entry"]["definitive"], 10)
        self.assertIn("hgb", o["lab"]["verdicts"])
        self.assertEqual(o["hypothesis"]["status"], "rejected")       # cannot PASS without an integrated canary
        self.assertNotEqual(o["report_card"]["verdict"], "PASS")
        self.assertEqual(o["knowledge"]["lineage"], 1)
        self.assertEqual(o["knowledge"]["operator_verified_sources"], 0)
        self.assertIn("synthetic convention", o["genome"]["fill_time_basis"])

    def test_no_broker_network_or_messaging_modules_loaded(self):
        bad = sorted(m for m in self.loaded for b in BANNED if m == b or m.startswith(b + "."))
        self.assertEqual(bad, [])

    def test_no_network_or_process_calls_at_runtime(self):
        network = [c for c in self.guard_calls if c[0].startswith("socket")]
        self.assertEqual(network, [])                                  # no connect / resolve attempted at all
        other = [c for c in self.guard_calls if not c[0].startswith("socket") and c[1] not in KNOWN_LIBRARY_PROBES]
        self.assertEqual(other, [])                                    # only loky's CPU probe, and it was refused

    def test_runtime_guard_actually_refuses(self):
        with _NoNetwork() as g:
            with self.assertRaises(PermissionError):
                socket.create_connection(("192.0.2.1", 9))           # TEST-NET-1; never reached
            with self.assertRaises(PermissionError):
                subprocess.Popen(["true"])
        self.assertEqual(len(g.calls), 2)
        self.assertNotEqual(socket.create_connection.__name__, "refuse")

    def test_writes_stay_in_the_work_directory(self):
        self.assertTrue(self.files)
        self.assertTrue(all(f.startswith("store/synthetic/1D/") for f in self.files), self.files[:5])


class I2_PointInTime(unittest.TestCase):
    """Second review (PR #11): a data-quality event introduced after a cutoff must not change anything decided
    before it. The old retrospective universe rule is the leaky control."""

    @classmethod
    def setUpClass(cls):
        from research.common.synthetic import MarketSpec, generate
        from research.dataqa.quality import BLOCKING_FLAG_BITS
        from research.genome import schema as G
        from research.integration import pipeline as P
        from research.mllab.datasets import symbol_features
        cls.P, cls.feat = P, staticmethod(symbol_features)
        m = generate(MarketSpec(n_symbols=4, n_sessions=320, seed=3))
        cls.raw = {s: b.reset_index(drop=True) for s, b in m.bars.items()}
        cls.g = G.with_params(G.seed("mean_reversion"), rsi_entry=40.0, rsi_exit=55.0)
        cls.sym, cls.cut = sorted(cls.raw)[1], 220
        mod = {s: b.copy() for s, b in cls.raw.items()}
        b = mod[cls.sym]
        after = b.index > cls.cut
        for c in ("open", "high", "low", "close"):
            b.loc[after, c] = b.loc[after, c] / 2.0                     # unadjusted 2:1 split after the cutoff
        b.loc[after, "volume"] = b.loc[after, "volume"] * 2.0
        b.loc[cls.cut + 40, "quality_flags"] = int(next(iter(BLOCKING_FLAG_BITS)))   # later blocking defect
        cls.mod = mod
        cls.cut_ts = pd.Timestamp(cls.raw[cls.sym]["ts_utc"].iloc[cls.cut])

    def _run(self, raw):
        bars, member, _ = self.P.point_in_time_universe(raw)
        return bars, member, self.P.decide(self.g, bars, member)

    def test_future_split_and_flag_do_not_change_earlier_decisions(self):
        b0, m0, r0 = self._run(self.raw)
        b1, m1, r1 = self._run(self.mod)
        for s in self.raw:
            np.testing.assert_array_equal(m0[s][:self.cut + 1], m1[s][:self.cut + 1])
        self.assertFalse(m1[self.sym][self.cut + 1:].any())             # quarantined once the split is knowable
        self.assertTrue(m0[self.sym][self.cut + 1:].all())
        early = lambda r: [{k: str(v) for k, v in d.items()} for d in r.decisions
                           if pd.Timestamp(d["decision_ts"]) <= self.cut_ts]
        self.assertTrue(early(r0))
        self.assertEqual(early(r0), early(r1))
        f0 = self.feat(b0[self.sym]).iloc[:self.cut + 1]
        f1 = self.feat(b1[self.sym]).iloc[:self.cut + 1]
        pd.testing.assert_frame_equal(f0, f1)
        # the planted change is real: decisions after the cutoff differ for that symbol
        late = lambda r: [d["stage"] for d in r.decisions if d["symbol"] == self.sym
                          and pd.Timestamp(d["decision_ts"]) > self.cut_ts]
        self.assertNotEqual(late(r0), late(r1))

    def test_old_retrospective_rule_leaked_the_future(self):
        self.assertNotIn(self.sym, self.P.retrospective_exclusions(self.raw))
        self.assertIn(self.sym, self.P.retrospective_exclusions(self.mod))   # whole history dropped

    def test_nonfinite_volume_never_reaches_research(self):
        """Third review (PR #8 -> #11): NaN, infinite and non-numeric volume bars are excluded before any lane."""
        raw = {s: b.copy() for s, b in self.raw.items()}
        b = raw[self.sym]
        b["volume"] = b["volume"].astype(object)
        bad_ts = [b["ts_utc"].iloc[i] for i in (50, 51, 52)]
        b.loc[50, "volume"], b.loc[51, "volume"], b.loc[52, "volume"] = np.nan, np.inf, "n/a"
        bars, _, info = self.P.point_in_time_universe(raw)
        self.assertEqual(info[self.sym]["inadmissible_rows_removed"], 3)
        self.assertFalse(set(bad_ts) & set(bars[self.sym]["ts_utc"]))
        self.assertTrue(np.isfinite(bars[self.sym]["volume"].astype(float)).all())


class I3_SplitCrossing(unittest.TestCase):
    """Third review (PR #11): a position already open when an unadjusted split happens keeps its share count,
    stop and target while the price halves. Such economics must be flagged and fail closed, without pre-closing
    the position with hindsight."""

    @classmethod
    def setUpClass(cls):
        from research.integration import pipeline as P
        cls.P = P

    def _fixture(self):
        n, split = 30, 15
        ts = pd.date_range("2021-01-04", periods=n, freq="B", tz="UTC") + pd.Timedelta(21, unit="h")
        px = np.full(n, 100.0)
        vol = np.full(n, 1e6)
        px[split:] /= 2.0                                        # unadjusted 2:1 split at bar 15's open
        vol[split:] *= 2.0
        b = pd.DataFrame({"ts_utc": ts, "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
                          "volume": vol, "quality_flags": 0})
        return b, split

    def test_hand_built_split_crossing_trade_is_flagged_and_fails_closed(self):
        from research.genome import engine as E
        from research.genome import schema as G
        raw, split = self._fixture()
        bars, member, info = self.P.point_in_time_universe({"X": raw})
        self.assertEqual(info["X"]["quarantined_from"], str(raw["ts_utc"].iloc[split]))
        sig = pd.DataFrame({"entry": np.zeros(30, bool), "exit": np.zeros(30, bool), "atr": np.full(30, 1.0),
                            "score": np.zeros(30)})
        sig.loc[10, "entry"] = True                              # decided at bar 10, filled at bar 11's open
        res = E.run(bars, {"X": sig}, G.ExitSpec(stop_atr=2.0, target_atr=None), G.RiskSpec(0.01, 0.25),
                    self.P.ENGINE_CFG, member, genome_id="GX")
        t = res.trades[0]
        # the defect is real: the raw engine books an artificial gap loss far beyond the 1R stop
        self.assertEqual((t.exit_reason, t.exit_bar_close_ts), ("stop_loss", raw["ts_utc"].iloc[split]))
        self.assertLess(t.r_multiple, -10.0)
        # the guard flags it; the trade is still recorded as simulated (no hindsight pre-close)
        sus = self.P.split_suspect_rows(bars)
        self.assertIn(split, list(sus["X"]))
        bad = self.P.contaminated_trades(res, bars, sus)
        self.assertEqual([(x["candidate_id"], x["split_rows"]) for x in bad], [(t.candidate_id, [split])])
        for card in ("PASS", "FAIL"):
            v, why = self.P.economics_verdict(card, len(bad), 1, 1)
            self.assertEqual(v, "INCONCLUSIVE")
            self.assertTrue(any("unadjusted split" in r for r in why))
        # a trade that closes before the split, and the same fixture without a split, are not flagged
        sig2 = sig.copy()
        sig2.loc[12, "exit"] = True
        res2 = E.run(bars, {"X": sig2}, G.ExitSpec(stop_atr=2.0, target_atr=None), G.RiskSpec(0.01, 0.25),
                     self.P.ENGINE_CFG, member, genome_id="GX")
        self.assertEqual(self.P.contaminated_trades(res2, bars, sus), [])
        flat = raw.copy()
        flat.loc[split:, ["open", "high", "low", "close"]] *= 2.0
        flat.loc[split:, "volume"] /= 2.0
        fb, fm, _ = self.P.point_in_time_universe({"X": flat})
        self.assertEqual(len(self.P.split_suspect_rows(fb)["X"]), 0)
        self.assertEqual(self.P.economics_verdict("FAIL", 0, 0, 1), ("FAIL", []))

    def test_multi_seed_control(self):
        """A seed with split crossings fails closed end to end; a seed without them is left alone."""
        outs = {}
        for seed, sp in ((23, 0.5), (7, 0.15)):
            with tempfile.TemporaryDirectory() as d:
                outs[seed] = self.P.run(Path(d), seed=seed, n_symbols=10, n_sessions=400, split_prob=sp)
        hit, clean = outs[23], outs[7]
        ca = hit["corporate_actions"]
        self.assertGreater(len(ca["selected_run_contaminated_trades"]), 0)
        self.assertTrue(ca["family_trials_contaminated"])
        self.assertEqual(hit["integrated_verdict"]["verdict"], "INCONCLUSIVE")
        self.assertFalse(hit["integrated_verdict"]["economics_valid"])
        self.assertGreater(ca["labels_contaminated"], 0)
        used = sum(x["definitive_used"] for x in hit["decisions"]["per_stage"].values())
        dfn = sum(x["definitive"] for x in hit["decisions"]["per_stage"].values())
        self.assertEqual(sum(x["split_contaminated"] for x in hit["decisions"]["per_stage"].values()),
                         ca["labels_contaminated"])
        self.assertLessEqual(used, dfn)          # contaminated labels never enter outcome statistics (lane A may
                                                 # already have marked a split window unobserved)
        self.assertFalse(hit["genome"]["baselines"]["buy_and_hold_equal_weight"]["valid"])
        for o in outs.values():
            for name, ok in o["checks"].items():
                self.assertTrue(ok, name)
        cc = clean["corporate_actions"]
        self.assertEqual((cc["selected_run_contaminated_trades"], cc["family_trials_contaminated"]), ([], {}))
        self.assertEqual(clean["integrated_verdict"]["verdict"], clean["report_card"]["verdict"])
        self.assertTrue(clean["integrated_verdict"]["economics_valid"])


if __name__ == "__main__":
    unittest.main()

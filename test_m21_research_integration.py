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


class _NoNetwork:
    """Replace the Python-level network / process entry points with recorders that refuse."""
    TARGETS = ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
               (socket, "getaddrinfo"), (socket, "gethostbyname"), (subprocess, "Popen"), (os, "system"))

    def __init__(self):
        self.calls, self.saved = [], []

    def __enter__(self):
        for obj, name in self.TARGETS:
            self.saved.append((obj, name, getattr(obj, name)))

            def refuse(*a, _n="%s.%s" % (getattr(obj, "__name__", obj), name), **k):
                self.calls.append(_n)
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
        self.assertEqual(self.guard_calls, [])

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


if __name__ == "__main__":
    unittest.main()

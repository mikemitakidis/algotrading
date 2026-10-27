"""test_m21_research_integration.py — the research lanes connected end to end (offline).

The test runs ``research.integration.pipeline``:
- synthetic M16-layout store, snapshot, inventory and quality audit (F);
- knowledge library and hypothesis (E);
- ledgered genome search and portfolio run (D);
- counterfactual labels for every candidate (A);
- meta-labeling lab (C);
- ledger-reconciled report card (B).

It asserts the cross-lane invariants, and that no broker, network or
messaging module was imported along the way.
Run via ``python _test_isolation.py test_m21_research_integration``.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Broker / market-data SDKs, network clients, secret loaders and production runtime modules that the
# research pipeline must never pull in, directly or transitively. (Low-level stdlib modules such as
# socket or subprocess are excluded: third-party libraries may import them without using them; the
# static import boundary in test_m21_research_foundation covers research code itself.)
BANNED = ("ib_insync", "ibapi", "alpaca", "alpaca_trade_api", "yfinance", "telegram", "requests", "httpx",
          "aiohttp", "websocket", "websockets", "urllib3", "paramiko", "dotenv",
          "bot.brokers", "bot.etoro", "bot.notifier", "bot.paper", "bot.paper_loop_d2", "bot.runtime",
          "bot.scanner", "bot.gateway_health", "bot.gateway_watchdog", "bot.recovery_executor",
          "bot.risk_authority", "bot.kill_switch", "dashboard", "main")


class I1_EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from research.integration import pipeline as P
        cls.before = set(sys.modules)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = P.run(Path(cls.tmp.name), n_symbols=12, n_sessions=500)
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
        self.assertGreater(o["data"]["usable"], 6)
        self.assertGreaterEqual(o["genome"]["grid_points"], 6)
        self.assertGreater(o["counterfactual"]["candidates"], o["counterfactual"]["executed_trades"] - 1)
        self.assertGreater(o["counterfactual"]["definitive"], 10)
        self.assertIn("hgb", o["lab"]["verdicts"])
        self.assertEqual(o["hypothesis"]["status"], "rejected")       # cannot PASS without a calibrated canary
        self.assertEqual(o["knowledge"]["lineage"], 1)

    def test_no_broker_network_or_messaging_modules_loaded(self):
        bad = sorted(m for m in self.loaded for b in BANNED if m == b or m.startswith(b + "."))
        self.assertEqual(bad, [])

    def test_writes_stay_in_the_work_directory(self):
        self.assertTrue(self.files)
        self.assertTrue(all(f.startswith("store/synthetic/1D/") for f in self.files), self.files[:5])


if __name__ == "__main__":
    unittest.main()

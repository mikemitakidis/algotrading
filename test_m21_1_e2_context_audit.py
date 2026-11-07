"""test_m21_1_e2_context_audit.py — M21 E2: market-context shadow record per WS2 paper cycle.

Offline only: temp context stores, synthetic close-stamped bars, a stub
completed-bar policy. No network, broker or production database.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

import _test_isolation  # noqa: F401  (M21 C5a)

from bot.market_context.sources import SECTOR_ETFS
from bot.market_context.store import Observation
from tools.paper_loop import context_audit as CA

REPO = Path(__file__).resolve().parent
UTC = timezone.utc
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)          # Wed 10:00 New York (EST)


def closes(n=260, end=date(2026, 11, 3), drift=0.001, start_px=100.0):
    """Close-stamped daily bars ending on ``end`` (21:00 UTC = 16:00 New York, EST)."""
    days = pd.bdate_range(end=end, periods=n)
    px = [start_px * (1 + drift) ** i for i in range(n)]
    return pd.DataFrame({"ts_utc": [pd.Timestamp(d.date().isoformat() + " 21:00", tz="UTC") for d in days],
                         "close": px})


def vix(store, value=18.0, published=NOW - timedelta(hours=10), obs=date(2026, 11, 3), n=6):
    for i in range(n):
        store.record(Observation("VIXCLS", obs - timedelta(days=n - 1 - i), value + i * 0.1,
                                 published - timedelta(days=n - 1 - i), published, "FRED:VIXCLS",
                                 "FRED / CBOE terms", "observed_retrieval"))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="e2_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "ctx.db")
        self.events = os.path.join(self.dir, "events.json")
        self.audit = CA.CycleContextAudit(self.path, events_path=self.events)

    def full_bars(self):
        idx = {s: closes() for s in CA.INDEX_SYMBOLS}
        sec = {s: closes() for s in SECTOR_ETFS}
        uni = {"U%d" % i: closes(n=80) for i in range(10)}
        return idx, sec, uni

    def write_events(self, at=NOW + timedelta(days=7)):
        Path(self.events).write_text(json.dumps([
            {"kind": k, "at_utc": at.isoformat(), "announced_at_utc": (NOW - timedelta(days=30)).isoformat(),
             "source": "https://www.federalreserve.gov/"} for k in ("FOMC", "CPI", "NFP")]))


class E2_Record(Base):
    def test_full_context_is_recorded_once_per_cycle_and_never_authorises(self):
        vix(self.audit.store)
        self.write_events()
        idx, sec, uni = self.full_bars()
        r = self.audit.record(cycle_key="scan:k1", now=NOW, index_bars=idx, sector_bars=sec, universe_bars=uni,
                              candidates=[("AAA", "long")])
        self.assertTrue(r["recorded"])
        rec = self.audit.get("scan:k1")
        self.assertEqual((rec["shadow_only"], rec["enforced"], rec["can_grant_authority"]), (True, False, False))
        self.assertEqual(rec["features"]["vix_level"]["status"], "OK")
        self.assertEqual(rec["features"]["spy_trend"]["value"], "up")
        self.assertEqual(set(rec["unavailable"]), {"earnings_proximity", "news_risk"})   # stated, not defaulted
        self.assertEqual(rec["unavailable"]["earnings_proximity"], "source_and_licence_undecided_G4")
        self.assertFalse(rec["shadow_decision"]["abstain"])
        self.assertEqual(rec["would_apply_to"], [{"symbol": "AAA", "direction": "long", "would_block": False,
                                                  "would_size_multiplier": rec["shadow_decision"]["multiplier"]}])
        again = self.audit.record(cycle_key="scan:k1", now=NOW + timedelta(minutes=5), index_bars={},
                                  sector_bars={}, universe_bars={})
        self.assertFalse(again["recorded"])                                  # a re-run never rewrites history
        self.assertEqual(self.audit.get("scan:k1")["decision_time_utc"], NOW.isoformat())

    def test_missing_and_stale_sources_are_explicit(self):
        vix(self.audit.store, published=NOW - timedelta(days=6), obs=date(2026, 10, 28))     # 6 days old
        r = self.audit.record(cycle_key="scan:k2", now=NOW, index_bars={}, sector_bars={}, universe_bars={})
        rec = self.audit.get("scan:k2")
        self.assertIn("vix_level", rec["stale"])
        for name, why in (("spy_trend", "fewer_than_200_closed_bars"), ("event_window", "no_calendar"),
                          ("breadth_above_sma50", "coverage_0_of_0")):
            self.assertEqual(rec["unavailable"][name], why)
        self.assertEqual(rec["events_source"], "event_calendar_file_missing")
        self.assertTrue(rec["shadow_decision"]["abstain"])                    # outage abstains, never unblocks
        self.assertEqual((r["stale"] >= 1, r["unavailable"] >= 3), (True, True))

    def test_point_in_time_vix_and_start_stamped_bars(self):
        vix(self.audit.store, published=NOW + timedelta(hours=2), n=1)       # published after the decision
        idx, sec, uni = self.full_bars()
        start_stamped = {s: b.assign(ts_utc=b["ts_utc"] - pd.Timedelta(hours=16)) for s, b in idx.items()}  # NY midnight
        self.audit.record(cycle_key="scan:k3", now=NOW, index_bars=start_stamped, sector_bars=sec,
                          universe_bars=uni)
        rec = self.audit.get("scan:k3")
        self.assertEqual(rec["unavailable"]["vix_level"], "no_vix_published_by_t")
        self.assertEqual(rec["unavailable"]["spy_trend"], "start_stamped_bars_refused")

    def test_unreadable_event_file_is_unavailable_not_empty(self):
        Path(self.events).write_text("{not json")
        self.audit.record(cycle_key="scan:k4", now=NOW, index_bars={}, sector_bars={}, universe_bars={})
        rec = self.audit.get("scan:k4")
        self.assertEqual(rec["events_source"], "event_calendar_unreadable:JSONDecodeError")
        self.assertEqual(rec["unavailable"]["event_window"], "no_calendar")

    def test_refusals_and_tamper_detection(self):
        for kw in (dict(cycle_key="", now=NOW), dict(cycle_key="k", now=NOW.replace(tzinfo=None))):
            with self.assertRaises(ValueError):
                self.audit.record(index_bars={}, sector_bars={}, universe_bars={}, **kw)
        self.audit.record(cycle_key="scan:k5", now=NOW, index_bars={}, sector_bars={}, universe_bars={})
        c = sqlite3.connect(self.path)
        c.execute("UPDATE context_cycle_audit SET record_json = replace(record_json, 'true', 'false') "
                  "WHERE cycle_key='scan:k5'")
        c.commit()
        c.close()
        with self.assertRaises(ValueError):
            self.audit.get("scan:k5")
        with self.assertRaises(ValueError):
            CA.CycleContextAudit("")


class E2_CompletedBars(unittest.TestCase):
    def policy(self, today=date(2026, 11, 4), unknown_day=None):
        def classify(tf, start):
            d = pd.Timestamp(start).date()
            if unknown_day is not None and d == unknown_day:
                raise RuntimeError("calendar gap")
            if d.weekday() >= 5:
                return "outside", None
            close = pd.Timestamp(d.isoformat() + " 21:00", tz="UTC")
            return ("forming", close) if d >= today else ("complete", close)
        return classify

    def frame(self, days):
        return pd.DataFrame({"close": [100.0 + i for i in range(len(days))]},
                            index=pd.DatetimeIndex([pd.Timestamp(d.isoformat(), tz="America/New_York")
                                                    for d in days]))

    def test_only_completed_sessions_enter_stamped_at_the_close(self):
        days = [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
        out, counts = CA.close_stamped_daily(self.frame(days), self.policy())
        self.assertEqual(counts, {"complete": 3, "forming": 1, "outside": 1, "unknown": 0})
        self.assertEqual(list(out["ts_utc"].dt.strftime("%Y-%m-%d %H:%M")),
                         ["2026-10-30 21:00", "2026-11-02 21:00", "2026-11-03 21:00"])
        self.assertTrue((out["ts_utc"] > out["bar_open_ts"].dt.tz_convert("UTC")).all())
        out, counts = CA.close_stamped_daily(self.frame(days), self.policy(unknown_day=date(2026, 11, 2)))
        self.assertEqual(counts["unknown"], 1)                                # unprovable: dropped, counted
        self.assertEqual(CA.close_stamped_daily(None, self.policy()), (None, {"complete": 0, "forming": 0,
                                                                            "outside": 0, "unknown": 0}))
        out, counts = CA.close_stamped_daily(pd.DataFrame({"x": [1]}, index=[pd.Timestamp("2026-11-03")]),
                                             self.policy())
        self.assertIsNone(out)
        self.assertEqual(counts["unknown"], 1)

    def test_record_cycle_with_and_without_a_verified_calendar(self):
        d = tempfile.mkdtemp(prefix="e2_")
        self.addCleanup(shutil.rmtree, d, True)
        audit = CA.CycleContextAudit(os.path.join(d, "ctx.db"))
        days = pd.bdate_range(end="2026-11-04", periods=260)
        daily = {s: self.frame([x.date() for x in days]) for s in CA.CONTEXT_SYMBOLS}
        r = CA.record_cycle(audit, cycle_key="scan:a", now=NOW, daily_by_symbol=daily, classify=self.policy(),
                            focus=[], candidates=[("AAA", "long")])
        rec = audit.get("scan:a")
        self.assertTrue(r["recorded"] and rec["calendar_verified"])
        self.assertEqual(rec["bar_counts"]["SPY"]["forming"], 1)              # today's bar never used
        self.assertEqual(rec["features"]["spy_trend"]["status"], "OK")
        CA.record_cycle(audit, cycle_key="scan:b", now=NOW, daily_by_symbol=daily, classify=None, focus=[])
        rec = audit.get("scan:b")
        self.assertFalse(rec["calendar_verified"])
        self.assertEqual(rec["unavailable"]["spy_trend"], "fewer_than_200_closed_bars")
        self.assertEqual(rec["bar_counts"]["SPY"]["unknown"], 260)


    def test_a_future_close_never_enters_even_if_misclassified(self):
        """Release-safety review (30 Oct): a classifier that wrongly calls today's (still forming) session
        complete cannot make a value from after the decision time reach the record."""
        d = tempfile.mkdtemp(prefix="e2_")
        self.addCleanup(shutil.rmtree, d, True)
        audit = CA.CycleContextAudit(os.path.join(d, "ctx.db"))
        days = pd.bdate_range(end="2026-11-04", periods=260)
        daily = {s: self.frame([x.date() for x in days]) for s in CA.CONTEXT_SYMBOLS}
        wrong = self.policy(today=date(2026, 11, 5))           # calls 4 Nov (closes 21:00 > NOW) complete
        CA.record_cycle(audit, cycle_key="scan:f", now=NOW, daily_by_symbol=daily, classify=wrong, focus=[])
        rec = audit.get("scan:f")
        self.assertEqual(rec["bar_counts"]["SPY"]["complete"], 260)
        for name, f in rec["features"].items():
            if f["as_of"] is not None:
                self.assertLessEqual(pd.Timestamp(f["as_of"]), pd.Timestamp(NOW), name)
        self.assertEqual(pd.Timestamp(rec["features"]["spy_trend"]["as_of"]),
                         pd.Timestamp("2026-11-03 21:00", tz="UTC"))


class E2_Wiring(unittest.TestCase):
    TOOL = REPO / "tools/paper_loop/m21_1extra_d2_3_scheduler.py"

    def test_off_by_default_and_only_the_flag_turns_it_on(self):
        self.assertFalse(CA.enabled_from_env({}))
        self.assertFalse(CA.enabled_from_env({CA.FLAG: "true"}))
        self.assertTrue(CA.enabled_from_env({CA.FLAG: "1"}))
        from tools.paper_loop import m21_1extra_d2_3_scheduler as T
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CA.FLAG, None)
            self.assertIsNone(T._context_audit())
        with mock.patch.dict(os.environ, {CA.FLAG: "1"}), \
                mock.patch.object(CA, "CycleContextAudit", lambda path, events_path: (path, events_path)):
            path, events = T._context_audit()
        self.assertTrue(path.endswith("data/market_context.db") and events.endswith("data/context_events.json"))

    def test_shadow_record_comes_after_the_candidates_and_cannot_change_them(self):
        src = self.TOOL.read_text()
        prov = src[src.index("def _scanner_provider("):src.index("def _template_shadow_store(")]
        i_cands, i_e2, i_ret = (prov.index("cands, _notes = loop_candidates(ranked)"),
                                prov.index("record_cycle(context_audit"), prov.rindex("return cands"))
        self.assertTrue(i_cands < i_e2 < i_ret)
        block = prov[prov.index("if context_audit is not None:"):i_ret]
        self.assertIn("except Exception as exc:", block)
        self.assertNotIn("cands =", block)
        self.assertIn("context_audit=_context_audit()", src[src.index("def _ibkr_parts("):])

    def test_module_has_no_broker_order_or_network_code(self):
        tree = ast.parse((REPO / "tools/paper_loop/context_audit.py").read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(n.module or "")
        for bad in ("bot.brokers", "ib_insync", "requests", "urllib", "socket", "bot.paper_loop_d2",
                    "bot.candidate_ledger", "research", "bot.risk", "bot.risk_authority", "bot.flywheel",
                    "bot.kill_switch", "bot.templates", "bot.promotion", "bot.database", "subprocess"):
            self.assertFalse(any(m == bad or m.startswith(bad + ".") for m in mods), (bad, mods))


if __name__ == "__main__":
    unittest.main()

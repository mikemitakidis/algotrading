"""test_m21_1_wp15_market_context.py — M21 WP15 market context (B18, M21.6), shadow only.

Pins point-in-time behaviour (a value or revision published after t is
invisible at t), the FRED CSV and event-calendar parsers, explicit
OK / STALE / UNAVAILABLE states (never a filled default), lookahead-free bar
features, and a reduce-only throttle: outages and event windows abstain, any
input gives a multiplier in [0, 1], and applying a decision never unblocks or
enlarges a trade. Offline; no network, no data purchase.
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot.market_context import features as F
from bot.market_context import sources as SRC
from bot.market_context import throttle as TH
from bot.market_context.store import ContextStore, Observation

REPO = Path(__file__).resolve().parent
UTC = timezone.utc
T = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def obs(d, v, pub, vintage=1, basis="assumed_lag", series="VIXCLS"):
    return Observation(series, d, v, pub, max(pub, T + timedelta(days=30)), "FRED:VIXCLS", "FRED / CBOE terms",
                       basis, vintage)


def bars(n=260, end=T - timedelta(hours=16), slope=0.001, start=100.0):
    ts = pd.date_range(end=end, periods=n, freq="B")
    c = start * np.exp(slope * np.arange(n))
    return pd.DataFrame({"ts_utc": ts, "open": c, "high": c, "low": c, "close": c, "volume": 1e6})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ContextStore(os.path.join(self.tmp.name, "ctx.db"))

    def tearDown(self):
        self.tmp.cleanup()


class X1_PointInTime(Base):
    def test_revisions_and_late_publication_are_invisible_before_they_happen(self):
        mon = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
        self.store.record(obs(date(2026, 11, 2), 20.0, mon))
        self.store.record(obs(date(2026, 11, 2), 21.5, mon + timedelta(days=2), vintage=2))   # revised Wed
        self.store.record(obs(date(2026, 11, 3), 30.0, datetime(2026, 11, 5, 9, 0, tzinfo=UTC)))   # late
        tue = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
        self.assertEqual(self.store.asof("VIXCLS", tue).value, 20.0)
        self.assertEqual(self.store.asof("VIXCLS", T).value, 20.0)                   # revision lands Wed 22:00
        self.assertEqual(self.store.asof("VIXCLS", T + timedelta(hours=8)).value, 21.5)  # revised; Tue not yet out
        self.assertEqual(self.store.asof("VIXCLS", T + timedelta(days=2)).value, 30.0)
        self.assertIsNone(self.store.asof("VIXCLS", mon - timedelta(seconds=1)))
        self.assertEqual(self.store.coverage("VIXCLS")["max_vintage"], 2)

    def test_append_only_and_well_formed(self):
        p = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
        self.store.record(obs(date(2026, 11, 2), 20.0, p))
        for bad in (obs(date(2026, 11, 2), 19.0, p + timedelta(days=1)),              # same vintage
                    obs(date(2026, 11, 2), 19.0, p - timedelta(days=1), vintage=2)):  # revision published earlier
            with self.assertRaises(ValueError):
                self.store.record(bad)
        with self.assertRaises(ValueError):
            Observation("VIXCLS", date(2026, 11, 2), 1.0, datetime(2026, 11, 3), T, "s", "l", "assumed_lag")
        with self.assertRaises(ValueError):
            Observation("VIXCLS", date(2026, 11, 2), 1.0, T, T - timedelta(hours=1), "s", "l", "observed_retrieval")
        with self.assertRaises(ValueError):
            self.store.asof("VIXCLS", datetime(2026, 11, 4))


FRED = "observation_date,VIXCLS\n2026-10-29,18.5\n2026-10-30,.\n2026-11-02,19.1\n2026-11-03,20.4\n"


class X2_Parsers(Base):
    def test_fred_csv_both_headers_missing_values_and_basis(self):
        got, rep = SRC.parse_fred_csv(FRED, "VIXCLS", retrieved_at=T)
        self.assertEqual((len(got), rep["missing"]), (3, 1))
        self.assertTrue(all(o.published_at_utc == T and o.publication_basis == "observed_retrieval" for o in got))
        old, _ = SRC.parse_fred_csv(FRED.replace("observation_date", "DATE"), "VIXCLS", retrieved_at=T,
                                    assumed_lag=timedelta(hours=34))
        self.assertEqual(old[0].published_at_utc, datetime(2026, 10, 30, 10, 0, tzinfo=UTC))
        self.assertEqual(old[0].publication_basis, "assumed_lag")
        self.assertLessEqual(old[-1].published_at_utc, T)
        far, _ = SRC.parse_fred_csv(FRED, "VIXCLS", retrieved_at=T, assumed_lag=timedelta(hours=48))
        self.assertEqual(far[-1].published_at_utc, T)                  # never later than we actually had it

    def test_fred_csv_refusals(self):
        for text in ("date,value\n2026-11-02,1\n", FRED + "2026-11-04,x\n", FRED + "2027-01-04,20\n",
                     FRED + "garbage\n", ""):
            with self.assertRaises(ValueError, msg=text[-30:]):
                SRC.parse_fred_csv(text, "VIXCLS", retrieved_at=T)
        with self.assertRaises(ValueError):
            SRC.parse_fred_csv(FRED, "VIXCLS", retrieved_at=datetime(2026, 11, 4))

    def test_event_calendar(self):
        ev = SRC.parse_event_calendar(json.dumps([
            {"kind": "CPI", "at_utc": "2026-11-12T13:30:00Z", "announced_at_utc": "2025-12-01T00:00:00Z",
             "source": "bls.gov"},
            {"kind": "FOMC", "at_utc": "2026-12-09T19:00:00Z", "announced_at_utc": "2025-08-01T00:00:00Z",
             "source": "federalreserve.gov"}]))
        self.assertEqual([e.kind for e in ev], ["CPI", "FOMC"])
        for bad in ([{"kind": "GDP", "at_utc": "2026-11-12T13:30:00Z", "announced_at_utc": "2025-01-01T00:00:00Z",
                      "source": "x"}],
                    [{"kind": "CPI", "at_utc": "2026-11-12T13:30:00", "announced_at_utc": "2025-01-01T00:00:00Z",
                      "source": "x"}],
                    [{"kind": "CPI", "at_utc": "2026-11-12T13:30:00Z", "announced_at_utc": "2026-11-13T00:00:00Z",
                      "source": "x"}],
                    [{"kind": "CPI", "at_utc": "2026-11-12T13:30:00Z", "announced_at_utc": "2025-01-01T00:00:00Z",
                      "source": " "}]):
            with self.assertRaises(ValueError):
                SRC.parse_event_calendar(json.dumps(bad))

    def test_registry_states_what_is_decided(self):
        self.assertEqual(SRC.SOURCES["EARNINGS"].status, "unavailable")
        self.assertEqual(len(SRC.SECTOR_ETFS), 11)


class X3_Features(Base):
    def _vix(self, values, last_pub):
        d0 = date(2026, 10, 27)
        for i, v in enumerate(values):
            self.store.record(obs(d0 + timedelta(days=i), v, last_pub - timedelta(days=len(values) - 1 - i)))

    def test_vix_states_and_regimes(self):
        self.assertEqual(F.vix_features(self.store, T)["vix_level"].status, F.UNAVAILABLE)
        self._vix([14, 16, 18, 20, 30, 36], T - timedelta(hours=10))
        f = F.vix_features(self.store, T)
        self.assertEqual((f["vix_level"].value, f["vix_regime"].value, f["vix_change_5d"].value), (36, "extreme", 22))
        self.assertEqual(f["vix_level"].status, F.OK)
        later = F.vix_features(self.store, T + timedelta(days=6))
        self.assertEqual(later["vix_level"].status, F.STALE)
        self.assertEqual([n for hi, n in F.VIX_REGIMES], ["low", "normal", "high", "extreme"])

    def test_trend_uses_only_bars_closed_by_t(self):
        b = bars()
        self.assertEqual(F.trend_feature("spy_trend", b, T).value, "up")
        crash = pd.concat([b, pd.DataFrame({"ts_utc": [T + timedelta(hours=6)], "open": [1.0], "high": [1.0],
                                            "low": [1.0], "close": [1.0], "volume": [1e6]})], ignore_index=True)
        self.assertEqual(F.trend_feature("spy_trend", crash, T).value, "up")          # the future crash is unseen
        self.assertEqual(F.trend_feature("spy_trend", b.iloc[:150], T).status, F.UNAVAILABLE)
        self.assertEqual(F.trend_feature("spy_trend", bars(slope=-0.001), T).value, "down")
        self.assertEqual(F.trend_feature("spy_trend", bars(end=T - timedelta(days=9)), T).status, F.STALE)

    def test_start_stamped_bars_are_refused_not_used(self):
        days = pd.bdate_range("2025-01-02", periods=260)
        start = pd.DataFrame({"ts_utc": [d.tz_localize("America/New_York").tz_convert("UTC") for d in days],
                              "close": np.linspace(100, 150, len(days))})          # M16 style: local midnight
        t = pd.Timestamp(days[-1].date()).tz_localize("America/New_York").tz_convert("UTC") + pd.Timedelta(hours=15)
        f = F.trend_feature("spy_trend", start, t.to_pydatetime())
        self.assertEqual((f.status, f.detail), (F.UNAVAILABLE, "start_stamped_bars_refused"))
        at_open = start.assign(ts_utc=start["ts_utc"] + pd.Timedelta(hours=9, minutes=30))   # stamped at the open
        self.assertEqual(F.trend_feature("spy_trend", at_open, t.to_pydatetime()).detail, "start_stamped_bars_refused")
        close = start.assign(ts_utc=start["ts_utc"] + pd.Timedelta(hours=16))     # 16:00 New York closes
        self.assertEqual(F.trend_feature("spy_trend", close, t.to_pydatetime()).status, F.OK)
        b = F.breadth_feature({"A": start, "B": start}, t.to_pydatetime())
        self.assertEqual(b.status, F.UNAVAILABLE)

    def test_sector_and_breadth_need_coverage(self):
        etfs = {e: bars(slope=0.001 if i % 2 else -0.001) for i, e in enumerate(SRC.SECTOR_ETFS)}
        s = F.sector_feature(etfs, T)
        self.assertEqual((s.status, round(s.value, 3)), (F.OK, round(5 / 11, 3)))
        self.assertEqual(F.sector_feature(dict(list(etfs.items())[:7]), T).status, F.UNAVAILABLE)
        uni = {str(i): bars(n=80, slope=0.002 if i < 7 else -0.002) for i in range(10)}
        self.assertAlmostEqual(F.breadth_feature(uni, T).value, 0.7)
        thin = dict(uni)
        for i in range(3):
            thin[str(i)] = bars(n=30)
        self.assertEqual(F.breadth_feature(thin, T).status, F.UNAVAILABLE)

    def test_events_are_known_only_after_announcement(self):
        e = [SRC.ScheduledEvent("FOMC", T + timedelta(hours=1), T - timedelta(days=90), "federalreserve.gov"),
             SRC.ScheduledEvent("CPI", T + timedelta(days=8), T + timedelta(hours=2), "bls.gov")]
        f = F.event_features(e, T)
        self.assertEqual(f["event_window"].value, ("FOMC",))
        self.assertAlmostEqual(f["event_FOMC_hours"].value, 1.0)
        self.assertEqual(f["event_CPI_hours"].status, F.UNAVAILABLE)                  # not announced yet at T
        self.assertEqual(F.event_features(e, T + timedelta(hours=3))["event_window"].value, ())
        self.assertEqual(F.event_features(None, T)["event_window"].status, F.UNAVAILABLE)

    def test_earnings_unavailable_and_news_adapter(self):
        from bot.sentiment.base import SentimentResult
        self.assertEqual(F.earnings_feature().status, F.UNAVAILABLE)
        self.assertEqual(F.news_feature(SentimentResult(-0.7, "bearish", "yf", "ok")).value, -0.7)
        self.assertEqual(F.news_feature(SentimentResult.unavailable("yf")).status, F.UNAVAILABLE)
        self.assertEqual(F.news_feature(None).status, F.UNAVAILABLE)
        with self.assertRaises(ValueError):
            F.build_context(self.store, datetime(2026, 11, 4), index_bars={}, sector_bars={}, universe_bars={},
                            events=None)


def fv(name, value, status=F.OK):
    return F.FeatureValue(name, value, status, None, "test")


def ctx(**kw):
    base = {"vix_level": fv("vix_level", 18.0), "vix_regime": fv("vix_regime", "normal"),
            "spy_trend": fv("spy_trend", "up"), "breadth_above_sma50": fv("breadth_above_sma50", 0.6),
            "event_window": fv("event_window", ()), "news_risk": fv("news_risk", 0.1)}
    base.update(kw)
    return base


class X4_Throttle(unittest.TestCase):
    def test_rules(self):
        self.assertEqual((TH.decide(ctx()).multiplier, TH.decide(ctx()).abstain), (1.0, False))
        out = TH.decide(ctx(vix_level=fv("vix_level", None, F.UNAVAILABLE)))
        self.assertTrue(out.abstain)
        self.assertIn("source_outage:vix_level:UNAVAILABLE", out.reasons)
        self.assertTrue(TH.decide(ctx(spy_trend=fv("spy_trend", "up", F.STALE))).abstain)
        self.assertTrue(TH.decide({k: v for k, v in ctx().items() if k != "spy_trend"}).abstain)
        self.assertTrue(TH.decide(ctx(event_window=fv("event_window", ("CPI",)))).abstain)
        self.assertEqual(TH.decide(ctx(event_window=fv("event_window", None, F.UNAVAILABLE))).multiplier, 0.75)
        self.assertEqual(TH.decide(ctx(vix_regime=fv("vix_regime", "extreme"))).multiplier, 0.5)
        self.assertEqual(TH.decide(ctx(vix_regime=fv("vix_regime", "high"))).multiplier, 0.75)
        self.assertEqual(TH.decide(ctx(spy_trend=fv("spy_trend", "down"),
                                       breadth_above_sma50=fv("b", 0.2))).multiplier, 0.75)
        self.assertEqual(TH.decide(ctx(news_risk=fv("news_risk", -0.8))).multiplier, 0.75)

    def test_reduce_only_for_any_input(self):
        rng = np.random.default_rng(1)
        regimes, trends, sts = ("low", "normal", "high", "extreme"), ("up", "down", "mixed"), (F.OK, F.STALE,
                                                                                          F.UNAVAILABLE)
        for _ in range(500):
            c = ctx(vix_level=fv("vix_level", 20.0, sts[rng.integers(3)]),
                    vix_regime=fv("vix_regime", regimes[rng.integers(4)], sts[rng.integers(3)]),
                    spy_trend=fv("spy_trend", trends[rng.integers(3)], sts[rng.integers(3)]),
                    breadth_above_sma50=fv("b", float(rng.uniform()), sts[rng.integers(3)]),
                    event_window=fv("event_window", ("NFP",) if rng.uniform() < 0.2 else (), sts[rng.integers(3)]),
                    news_risk=fv("news_risk", float(rng.uniform(-1, 1)), sts[rng.integers(3)]))
            d = TH.decide(c)
            self.assertTrue(0.0 <= d.multiplier <= 1.0)
            self.assertFalse(d.can_grant_authority)
            for allowed in (True, False):
                a, s = TH.apply_to(d, allowed=allowed, size=100.0)
                self.assertLessEqual(s, 100.0)
                self.assertFalse(a and not allowed)
        with self.assertRaises(TypeError):
            TH.ContextDecision(1.0, False, (), {}, can_grant_authority=True)

    def test_decision_is_logged_with_its_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = ContextStore(os.path.join(tmp, "c.db"))
            d = TH.decide(ctx(vix_regime=fv("vix_regime", "high")))
            st.log_decision(d.to_json(), T)
            import sqlite3
            row = sqlite3.connect(st.path).execute("SELECT decision_json FROM context_decisions").fetchone()
            body = json.loads(row[0])
            self.assertEqual(body["multiplier"], 0.75)
            self.assertIn("vix_regime", body["features"])
            self.assertIs(body["can_grant_authority"], False)


class X5_Isolation(unittest.TestCase):
    def test_no_network_broker_or_order_code(self):
        bad = ("socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "ib_insync", "alpaca", "yfinance",
               "bot.brokers", "bot.notifier", "bot.paper_loop_d2", "bot.kill_switch", "telegram", "subprocess",
               "research")
        for p in sorted((REPO / "bot" / "market_context").glob("*.py")):
            tree = ast.parse(p.read_text())
            mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            for m in mods:
                self.assertFalse(any(m == b or m.startswith(b + ".") for b in bad), (p.name, m))
            for s in ("scoring_truth", "scanner_bridge", "candidate_ledger"):
                self.assertNotIn(s, p.read_text(), (p.name, s))


if __name__ == "__main__":
    unittest.main()

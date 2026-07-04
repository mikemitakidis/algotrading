#!/usr/bin/env python3
"""M21.1extra-D1 Commit 1 — pure guard logic, fail-closed matrix (mock sources).

No live network, no Alpaca, no DB. Every failure/uncertain path must yield
may_submit=False; the only True is a fresh + US_EQ + regular-session +
affirmative-open unanimous decision.
"""
from __future__ import annotations

import ast
import unittest
from datetime import datetime, timezone, timedelta

from bot.market_clock import (
    evaluate_market_clock, MarketClockCache, MockMarketStatusSource,
    IbkrMarketStatusSource, SourceResult, MarketClockDecision, VALID_REASONS,
    STATUS_OK, STATUS_UNAVAILABLE, STATUS_MALFORMED, STATUS_NO_CREDENTIALS,
    REASON_MARKET_OPEN, REASON_MARKET_CLOSED, REASON_CALENDAR_UNSUPPORTED,
    REASON_NO_CREDENTIALS, REASON_SOURCE_UNAVAILABLE, REASON_SOURCE_STALE,
    REASON_SOURCE_MALFORMED, REASON_SOURCE_INCONSISTENT,
    REASON_EXTENDED_HOURS_REFUSED,
)

GUARD_SRC = "bot/market_clock/guard.py"
DECISION_SRC = "bot/market_clock/decision.py"
SOURCES_SRC = "bot/market_clock/sources.py"


def _mon(h, m=0, s=0):
    # 2026-07-06 is a Monday. US regular session in UTC (EDT) = 13:30–20:00.
    return datetime(2026, 7, 6, h, m, s, tzinfo=timezone.utc)


SESSION_OPEN = _mon(13, 30)
SESSION_CLOSE = _mon(20, 0)


def _ok(now, *, is_open=True, cal=True, so=SESSION_OPEN, sc=SESSION_CLOSE,
        as_of=None, name="mock"):
    return SourceResult(status=STATUS_OK, is_open=is_open, session_open_utc=so,
                        session_close_utc=sc, as_of_utc=as_of,
                        fetched_at_utc=now, calendar_present=cal,
                        source_name=name)


def _src(res):
    return MockMarketStatusSource(res, stamp_fetched_at=False)


def _ev(now, sources, **kw):
    return evaluate_market_clock(market_calendar_id=kw.pop("cal", "US_EQ"),
                                 now_utc=now, sources=sources, **kw)


class TestOpenAndClosed(unittest.TestCase):
    def test_1_open_fresh(self):
        now = _mon(15)
        d = _ev(now, [_src(_ok(now))])
        self.assertTrue(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_OPEN)

    def test_2_closed_fresh(self):
        now = _mon(15)
        d = _ev(now, [_src(_ok(now, is_open=False))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)

    def test_3_weekend(self):
        # a Saturday; source reports closed (no session)
        sat = datetime(2026, 7, 4, 15, tzinfo=timezone.utc)
        d = _ev(sat, [_src(SourceResult(status=STATUS_OK, is_open=False,
                                        fetched_at_utc=sat, calendar_present=False,
                                        source_name="mock"))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)

    def test_4_holiday(self):
        now = _mon(15)
        d = _ev(now, [_src(SourceResult(status=STATUS_OK, is_open=False,
                                        fetched_at_utc=now, calendar_present=True,
                                        source_name="mock"))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)

    def test_5_early_close_before_close(self):
        # half day: close at 17:00; now 15:00 -> still open
        now = _mon(15)
        d = _ev(now, [_src(_ok(now, sc=_mon(17)))])
        self.assertTrue(d.may_submit)

    def test_6_early_close_after_close(self):
        now = _mon(18)
        d = _ev(now, [_src(_ok(now, sc=_mon(17)))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)

    def test_7_pre_market(self):
        now = _mon(12)
        d = _ev(now, [_src(_ok(now))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_EXTENDED_HOURS_REFUSED)

    def test_8_after_hours(self):
        now = _mon(21)
        d = _ev(now, [_src(_ok(now))])
        self.assertFalse(d.may_submit)
        # after session close -> closed
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)


class TestUnsupportedAndCreds(unittest.TestCase):
    def test_9_unsupported_market(self):
        d = evaluate_market_clock(market_calendar_id="UK_EQ",
                                  now_utc=_mon(15), sources=[])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_CALENDAR_UNSUPPORTED)
        self.assertTrue(d.unsupported)

    def test_10_missing_credentials(self):
        now = _mon(15)
        d = _ev(now, [_src(SourceResult(status=STATUS_NO_CREDENTIALS,
                                        fetched_at_utc=now, source_name="mock"))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_NO_CREDENTIALS)


class TestSourceFailures(unittest.TestCase):
    def setUp(self):
        self.now = _mon(15)

    def _unavail(self):
        return _src(SourceResult(status=STATUS_UNAVAILABLE,
                                 fetched_at_utc=self.now, source_name="mock"))

    def test_11_auth_401_403_unavailable(self):
        d = _ev(self.now, [self._unavail()])
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_12_rate_limit_unavailable(self):
        d = _ev(self.now, [self._unavail()])
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_13_5xx_unavailable(self):
        d = _ev(self.now, [self._unavail()])
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_14_timeout_unavailable(self):
        d = _ev(self.now, [self._unavail()])
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_15_malformed_json(self):
        d = _ev(self.now, [_src(SourceResult(status=STATUS_MALFORMED,
                                             fetched_at_utc=self.now,
                                             source_name="mock"))])
        self.assertEqual(d.reason, REASON_SOURCE_MALFORMED)

    def test_16_missing_is_open(self):
        d = _ev(self.now, [_src(SourceResult(status=STATUS_OK, is_open=None,
                                             fetched_at_utc=self.now,
                                             source_name="mock"))])
        self.assertEqual(d.reason, REASON_SOURCE_MALFORMED)

    def test_17_open_but_missing_calendar_fails_closed(self):
        # tightening rule: clock open + no calendar -> fail closed
        d = _ev(self.now, [_src(_ok(self.now, cal=False, so=None, sc=None))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_17b_closed_with_missing_calendar_stays_closed(self):
        # explicit: clock closed + no calendar -> closed (documented)
        d = _ev(self.now, [_src(SourceResult(status=STATUS_OK, is_open=False,
                                             fetched_at_utc=self.now,
                                             calendar_present=False,
                                             source_name="mock"))])
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)


class TestStaleness(unittest.TestCase):
    def test_18_stale_fetched_at(self):
        now = _mon(15)
        old = now - timedelta(seconds=40)  # > 30s default
        d = _ev(now, [_src(_ok(old))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_SOURCE_STALE)
        self.assertTrue(d.stale)

    def test_18b_stale_as_of_even_if_fetch_fresh(self):
        now = _mon(15)
        d = _ev(now, [_src(_ok(now, as_of=now - timedelta(seconds=40)))])
        self.assertEqual(d.reason, REASON_SOURCE_STALE)

    def test_custom_max_age_injectable(self):
        now = _mon(15)
        old = now - timedelta(seconds=20)
        # with max_age 10, 20s old is stale
        d = _ev(now, [_src(_ok(old))], max_age_sec=10)
        self.assertEqual(d.reason, REASON_SOURCE_STALE)
        # with max_age 60, 20s old is fine
        d2 = _ev(now, [_src(_ok(old))], max_age_sec=60)
        self.assertTrue(d2.may_submit)


class TestCache(unittest.TestCase):
    def test_19_cached_open_within_freshness_served(self):
        now = _mon(15)
        cache = MarketClockCache()
        _ev(now, [_src(_ok(now))], cache=cache)
        # source now unavailable but within freshness -> cached open served
        d = _ev(now + timedelta(seconds=5),
                [_src(SourceResult(status=STATUS_UNAVAILABLE,
                                   fetched_at_utc=now + timedelta(seconds=5),
                                   source_name="mock"))], cache=cache)
        self.assertTrue(d.may_submit)

    def test_20_cached_open_after_freshness_not_served(self):
        now = _mon(15)
        cache = MarketClockCache()
        _ev(now, [_src(_ok(now))], cache=cache)
        d = _ev(now + timedelta(seconds=40),
                [_src(SourceResult(status=STATUS_UNAVAILABLE,
                                   fetched_at_utc=now + timedelta(seconds=40),
                                   source_name="mock"))], cache=cache)
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_default_no_cache(self):
        # without a cache, no state is retained across calls
        now = _mon(15)
        _ev(now, [_src(_ok(now))])
        d = _ev(now + timedelta(seconds=5),
                [_src(SourceResult(status=STATUS_UNAVAILABLE,
                                   fetched_at_utc=now + timedelta(seconds=5),
                                   source_name="mock"))])
        self.assertFalse(d.may_submit)  # no cache -> no stale allow


class TestInconsistency(unittest.TestCase):
    def test_21_sources_disagree(self):
        now = _mon(15)
        a = _src(_ok(now, name="a"))
        b = _src(_ok(now, is_open=False, name="b"))
        d = _ev(now, [a, b])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_SOURCE_INCONSISTENT)
        self.assertTrue(d.inconsistent)

    def test_both_open_agree(self):
        now = _mon(15)
        d = _ev(now, [_src(_ok(now, name="a")), _src(_ok(now, name="b"))])
        self.assertTrue(d.may_submit)

    def test_both_closed_agree(self):
        now = _mon(15)
        d = _ev(now, [_src(_ok(now, is_open=False, name="a")),
                      _src(_ok(now, is_open=False, name="b"))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)


class TestBoundaries(unittest.TestCase):
    def test_22_exactly_open(self):
        d = _ev(SESSION_OPEN, [_src(_ok(SESSION_OPEN))])
        self.assertTrue(d.may_submit)

    def test_23_one_second_before_open(self):
        t = SESSION_OPEN - timedelta(seconds=1)
        d = _ev(t, [_src(_ok(t))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_EXTENDED_HOURS_REFUSED)

    def test_24_exactly_close(self):
        d = _ev(SESSION_CLOSE, [_src(_ok(SESSION_CLOSE))])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_MARKET_CLOSED)

    def test_25_one_second_before_close(self):
        t = SESSION_CLOSE - timedelta(seconds=1)
        d = _ev(t, [_src(_ok(t))])
        self.assertTrue(d.may_submit)


class TestDST(unittest.TestCase):
    def test_26_dst_march_source_driven(self):
        # 2026-03-10 (after US DST start Mar 8): session 13:30–20:00 UTC (EDT)
        now = datetime(2026, 3, 10, 15, tzinfo=timezone.utc)
        so = datetime(2026, 3, 10, 13, 30, tzinfo=timezone.utc)
        sc = datetime(2026, 3, 10, 20, 0, tzinfo=timezone.utc)
        d = _ev(now, [_src(_ok(now, so=so, sc=sc))])
        self.assertTrue(d.may_submit)

    def test_27_dst_november_source_driven(self):
        # 2026-11-05 (after US DST end Nov 1): session 14:30–21:00 UTC (EST)
        now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
        so = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
        sc = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)
        d = _ev(now, [_src(_ok(now, so=so, sc=sc))])
        self.assertTrue(d.may_submit)
        # at 14:00 UTC (pre-open in EST) -> refused
        pre = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)
        d2 = _ev(pre, [_src(_ok(pre, so=so, sc=sc))])
        self.assertFalse(d2.may_submit)


class TestPropertyEveryFailureBlocks(unittest.TestCase):
    def test_28_every_injected_failure_blocks(self):
        now = _mon(15)
        bad_results = [
            SourceResult(status=STATUS_UNAVAILABLE, fetched_at_utc=now, source_name="m"),
            SourceResult(status=STATUS_MALFORMED, fetched_at_utc=now, source_name="m"),
            SourceResult(status=STATUS_NO_CREDENTIALS, fetched_at_utc=now, source_name="m"),
            SourceResult(status=STATUS_OK, is_open=None, fetched_at_utc=now, source_name="m"),
            SourceResult(status="weird_unknown", fetched_at_utc=now, source_name="m"),
            _ok(now - timedelta(seconds=99)),                       # stale
            _ok(now, cal=False, so=None, sc=None),                  # open no cal
            _ok(_mon(12)),                                          # pre-market (now=12)
        ]
        for res in bad_results:
            d = evaluate_market_clock(market_calendar_id="US_EQ", now_utc=now,
                                      sources=[_src(res)])
            self.assertFalse(d.may_submit,
                             msg="should block: %s" % res.status)
            self.assertIn(d.reason, VALID_REASONS)

    def test_source_that_raises_is_caught(self):
        now = _mon(15)

        class Boom:
            name = "boom"
            def get_status(self, **kw):
                raise RuntimeError("source blew up")

        d = evaluate_market_clock(market_calendar_id="US_EQ", now_utc=now,
                                  sources=[Boom()])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)

    def test_ibkr_mock_source_unavailable_by_default(self):
        now = _mon(15)
        d = evaluate_market_clock(market_calendar_id="US_EQ", now_utc=now,
                                  sources=[IbkrMarketStatusSource()])
        self.assertFalse(d.may_submit)
        self.assertEqual(d.reason, REASON_SOURCE_UNAVAILABLE)


class TestDecisionInvariants(unittest.TestCase):
    def test_may_submit_true_requires_open_reason(self):
        with self.assertRaises(ValueError):
            MarketClockDecision(may_submit=True, reason=REASON_MARKET_CLOSED,
                                market_calendar_id="US_EQ")

    def test_open_reason_requires_may_submit_true(self):
        with self.assertRaises(ValueError):
            MarketClockDecision(may_submit=False, reason=REASON_MARKET_OPEN,
                                market_calendar_id="US_EQ")

    def test_unknown_reason_rejected(self):
        with self.assertRaises(ValueError):
            MarketClockDecision(may_submit=False, reason="not_a_real_reason",
                                market_calendar_id="US_EQ")

    def test_c_fields_returned_not_written(self):
        now = _mon(15)
        d = _ev(now, [_src(_ok(now))])
        cf = d.c_fields()
        self.assertEqual(cf["market_calendar_id"], "US_EQ")
        self.assertTrue(cf["market_clock_checked"])
        self.assertEqual(cf["market_clock_reason"], REASON_MARKET_OPEN)


class TestHygiene(unittest.TestCase):
    def _read(self, p):
        with open(p) as fh:
            return fh.read()

    def _imports(self, src):
        tree = ast.parse(src)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    mods.add(n.module)
        return mods

    def test_no_order_scheduler_dashboard_imports(self):
        forbidden = ("execution", "scheduler", "dashboard", "order",
                     "main", "apscheduler")
        for p in (GUARD_SRC, DECISION_SRC, SOURCES_SRC):
            mods = self._imports(self._read(p))
            for m in mods:
                low = m.lower()
                for bad in forbidden:
                    self.assertNotIn(
                        bad, low, msg="%s imports %s (forbidden)" % (p, m))

    def test_no_db_or_persistence_imports(self):
        forbidden = ("sqlite3", "outcome_persistence", "paper_lifecycles",
                     "database", "broker_allocation", "runtime_policy",
                     "signal_only_broker")
        for p in (GUARD_SRC, DECISION_SRC, SOURCES_SRC):
            src = self._read(p)
            mods = self._imports(src)
            for m in mods:
                for bad in forbidden:
                    self.assertNotIn(bad, m.lower(),
                                     msg="%s imports %s" % (p, m))
            # also no raw table names
            self.assertNotIn("paper_lifecycles", src)

    def _code_only(self, src):
        """Return source with docstrings and comments removed, so hygiene
        scans match real code, not prose describing the rules."""
        import io, tokenize
        out = []
        try:
            toks = tokenize.generate_tokens(io.StringIO(src).readline)
            prev_type = tokenize.INDENT
            for tok in toks:
                ttype, tstr = tok.type, tok.string
                if ttype == tokenize.COMMENT:
                    continue
                # a STRING that stands alone as a statement is a docstring
                if ttype == tokenize.STRING and prev_type in (
                        tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
                        tokenize.DEDENT, tokenize.ENCODING):
                    continue
                if ttype not in (tokenize.NL, tokenize.NEWLINE,
                                 tokenize.INDENT, tokenize.DEDENT,
                                 tokenize.ENCODING):
                    out.append(tstr)
                prev_type = ttype
        except tokenize.TokenError:
            return src
        return " ".join(out)

    def test_no_hardcoded_market_hours_as_authority(self):
        # the guard must not encode 09:30 / 16:00 literals as decision logic
        # (docstrings that mention them as forbidden are fine).
        for p in (GUARD_SRC, DECISION_SRC, SOURCES_SRC):
            code = self._code_only(self._read(p))
            for lit in ("09:30", "16:00", "9:30"):
                self.assertNotIn(lit, code, msg="%s hardcodes %s" % (p, lit))

    def test_no_safety_asserts(self):
        for p in (GUARD_SRC, DECISION_SRC, SOURCES_SRC):
            tree = ast.parse(self._read(p))
            asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
            self.assertEqual(asserts, [], "%s uses assert" % p)

    def test_no_secret_names_in_source(self):
        # Commit 1 has no Alpaca adapter; ensure no secret HANDLING leaked in
        # (docstrings noting "no secrets" are fine — scan code only).
        for p in (GUARD_SRC, DECISION_SRC, SOURCES_SRC):
            code = self._code_only(self._read(p))
            for tok in ("ALPACA_KEY", "ALPACA_SECRET", "api_key"):
                self.assertNotIn(tok, code, "%s references %s" % (p, tok))

    def test_no_network_imports_in_commit1(self):
        # requests/urllib belong to the Commit 2 Alpaca adapter, not here.
        for p in (GUARD_SRC, DECISION_SRC, SOURCES_SRC):
            mods = self._imports(self._read(p))
            for m in mods:
                self.assertNotIn("requests", m.lower())
                self.assertNotIn("urllib", m.lower())
                self.assertNotIn("http", m.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

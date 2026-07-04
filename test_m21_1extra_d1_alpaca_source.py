#!/usr/bin/env python3
"""M21.1extra-D1 Commit 2 — Alpaca Clock/Calendar source tests (mocked HTTP).

No live network. Every credential/HTTP/parse failure must yield a fail-closed
SourceResult; the guard turns those into may_submit=False. Secrets must never
leak into any returned/logged field.
"""
from __future__ import annotations

import ast
import os
import unittest
from datetime import datetime, timezone

from bot.market_clock.alpaca_clock import (
    AlpacaClockSource, HttpResponse, HttpError,
    _parse_iso_utc, _parse_session_dt, _get_keys,
)
from bot.market_clock.sources import (
    STATUS_OK, STATUS_NO_CREDENTIALS, STATUS_UNAVAILABLE, STATUS_MALFORMED,
)
from bot.market_clock import evaluate_market_clock

ALPACA_SRC = "bot/market_clock/alpaca_clock.py"

# Monday 2026-07-06 15:00 UTC = 11:00 ET (mid regular session, EDT).
NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
CLOCK_OPEN = {"is_open": True, "timestamp": "2026-07-06T15:00:00Z",
              "next_open": "2026-07-07T13:30:00Z",
              "next_close": "2026-07-06T20:00:00Z"}
CLOCK_CLOSED = {"is_open": False, "timestamp": "2026-07-06T23:00:00Z",
                "next_open": "2026-07-07T13:30:00Z",
                "next_close": "2026-07-07T20:00:00Z"}
CAL_TODAY = [{"date": "2026-07-06", "open": "09:30", "close": "16:00"}]


def _transport(*, clock_body=CLOCK_OPEN, cal_body=CAL_TODAY,
               clock_code=200, cal_code=200,
               clock_json_ok=True, cal_json_ok=True, raise_on=None,
               record_headers=None):
    def _get(url, *, headers, params, timeout):
        if record_headers is not None:
            record_headers.append(dict(headers))
        if raise_on and raise_on in url:
            raise HttpError("Timeout")
        if "/v2/clock" in url:
            return HttpResponse(status_code=clock_code, json_body=clock_body,
                                ok_json=clock_json_ok)
        return HttpResponse(status_code=cal_code, json_body=cal_body,
                            ok_json=cal_json_ok)
    return _get


class _Creds(unittest.TestCase):
    def setUp(self):
        self._saved = (os.environ.get("ALPACA_KEY"),
                       os.environ.get("ALPACA_SECRET"))
        os.environ["ALPACA_KEY"] = "testkey"
        os.environ["ALPACA_SECRET"] = "testsecret"

    def tearDown(self):
        for k, v in zip(("ALPACA_KEY", "ALPACA_SECRET"), self._saved):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _src(self, **kw):
        return AlpacaClockSource(http_get=_transport(**kw))

    def _status(self, **kw):
        return self._src(**kw).get_status(market_calendar_id="US_EQ",
                                          now_utc=NOW)


class TestOpenClosed(_Creds):
    def test_open_with_calendar_ok(self):
        r = self._status()
        self.assertEqual(r.status, STATUS_OK)
        self.assertIs(r.is_open, True)
        self.assertTrue(r.calendar_present)
        self.assertEqual(r.session_open_utc,
                         datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(r.session_close_utc,
                         datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc))

    def test_open_flows_to_may_submit(self):
        d = evaluate_market_clock(market_calendar_id="US_EQ", now_utc=NOW,
                                  sources=[self._src()])
        self.assertTrue(d.may_submit)

    def test_closed_with_missing_calendar_returns_closed(self):
        # documented: clock closed + no calendar -> closed (safe).
        r = self._status(clock_body=CLOCK_CLOSED, cal_body=[])
        self.assertEqual(r.status, STATUS_OK)
        self.assertIs(r.is_open, False)

    def test_open_but_calendar_unavailable_fails_closed(self):
        r = self._status(cal_code=500)
        self.assertIs(r.is_open, True)
        self.assertFalse(r.calendar_present)
        d = evaluate_market_clock(market_calendar_id="US_EQ", now_utc=NOW,
                                  sources=[self._src(cal_code=500)])
        self.assertFalse(d.may_submit)

    def test_open_but_empty_calendar_fails_closed(self):
        r = self._status(cal_body=[])
        self.assertFalse(r.calendar_present)


class TestCredentials(_Creds):
    def test_missing_key(self):
        os.environ["ALPACA_KEY"] = ""
        self.assertEqual(self._status().status, STATUS_NO_CREDENTIALS)

    def test_missing_secret(self):
        os.environ["ALPACA_SECRET"] = ""
        self.assertEqual(self._status().status, STATUS_NO_CREDENTIALS)

    def test_whitespace_only_creds(self):
        os.environ["ALPACA_KEY"] = "   "
        self.assertEqual(self._status().status, STATUS_NO_CREDENTIALS)

    def test_get_keys_strips(self):
        os.environ["ALPACA_KEY"] = "  k  "
        os.environ["ALPACA_SECRET"] = "  s  "
        self.assertEqual(_get_keys(), ("k", "s"))


class TestHttpFailures(_Creds):
    def test_401(self):
        self.assertEqual(self._status(clock_code=401).status, STATUS_UNAVAILABLE)

    def test_403(self):
        self.assertEqual(self._status(clock_code=403).status, STATUS_UNAVAILABLE)

    def test_429(self):
        self.assertEqual(self._status(clock_code=429).status, STATUS_UNAVAILABLE)

    def test_500(self):
        self.assertEqual(self._status(clock_code=500).status, STATUS_UNAVAILABLE)

    def test_503(self):
        self.assertEqual(self._status(clock_code=503).status, STATUS_UNAVAILABLE)

    def test_unexpected_3xx(self):
        self.assertEqual(self._status(clock_code=302).status, STATUS_UNAVAILABLE)

    def test_timeout_clock(self):
        self.assertEqual(self._status(raise_on="/v2/clock").status,
                         STATUS_UNAVAILABLE)

    def test_timeout_calendar_when_open(self):
        r = self._status(raise_on="/v2/calendar")
        self.assertIs(r.is_open, True)
        self.assertFalse(r.calendar_present)


class TestMalformed(_Creds):
    def test_non_json_clock(self):
        self.assertEqual(self._status(clock_json_ok=False).status,
                         STATUS_MALFORMED)

    def test_missing_is_open(self):
        self.assertEqual(
            self._status(clock_body={"timestamp": "2026-07-06T15:00:00Z"}).status,
            STATUS_MALFORMED)

    def test_is_open_not_boolean(self):
        self.assertEqual(
            self._status(clock_body={"is_open": "yes"}).status, STATUS_MALFORMED)

    def test_malformed_timestamp(self):
        self.assertEqual(
            self._status(clock_body={"is_open": True,
                                     "timestamp": "not-a-date"}).status,
            STATUS_MALFORMED)

    def test_malformed_next_open(self):
        self.assertEqual(
            self._status(clock_body={"is_open": True,
                                     "next_open": "garbage"}).status,
            STATUS_MALFORMED)

    def test_malformed_next_close(self):
        self.assertEqual(
            self._status(clock_body={"is_open": True,
                                     "next_close": "garbage"}).status,
            STATUS_MALFORMED)

    def test_malformed_calendar_time_when_open(self):
        r = self._status(cal_body=[{"date": "2026-07-06",
                                    "open": "xx:yy", "close": "16:00"}])
        self.assertFalse(r.calendar_present)

    def test_calendar_close_before_open_rejected(self):
        r = self._status(cal_body=[{"date": "2026-07-06",
                                    "open": "16:00", "close": "09:30"}])
        self.assertFalse(r.calendar_present)

    def test_non_json_calendar_when_open(self):
        r = self._status(cal_json_ok=False)
        self.assertFalse(r.calendar_present)


class TestDateParsing(unittest.TestCase):
    def test_parse_iso_utc_z_suffix(self):
        self.assertEqual(_parse_iso_utc("2026-07-06T15:00:00Z"),
                         datetime(2026, 7, 6, 15, tzinfo=timezone.utc))

    def test_parse_iso_utc_offset(self):
        # +02:00 normalized to UTC
        self.assertEqual(_parse_iso_utc("2026-07-06T17:00:00+02:00"),
                         datetime(2026, 7, 6, 15, tzinfo=timezone.utc))

    def test_parse_iso_utc_naive_rejected(self):
        self.assertIsNone(_parse_iso_utc("2026-07-06T15:00:00"))

    def test_parse_iso_utc_garbage(self):
        self.assertIsNone(_parse_iso_utc("nonsense"))
        self.assertIsNone(_parse_iso_utc(None))
        self.assertIsNone(_parse_iso_utc(123))

    def test_session_dt_edt_july(self):
        # 09:30 ET in July (EDT) -> 13:30 UTC
        self.assertEqual(_parse_session_dt("2026-07-06", "09:30"),
                         datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc))

    def test_session_dt_est_november(self):
        # 09:30 ET in November (EST) -> 14:30 UTC (no hardcoded offset)
        self.assertEqual(_parse_session_dt("2026-11-05", "09:30"),
                         datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc))

    def test_session_dt_early_close(self):
        # 13:00 ET early close in July -> 17:00 UTC
        self.assertEqual(_parse_session_dt("2026-07-06", "13:00"),
                         datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc))

    def test_session_dt_malformed(self):
        self.assertIsNone(_parse_session_dt("2026-07-06", "xx:yy"))
        self.assertIsNone(_parse_session_dt("bad-date", "09:30"))
        self.assertIsNone(_parse_session_dt("2026-07-06", "0930"))
        self.assertIsNone(_parse_session_dt(None, "09:30"))


class TestDstThroughGuard(_Creds):
    def test_november_pre_open_refused(self):
        # 2026-11-05 14:00 UTC = 09:00 ET (pre-open in EST) -> refused
        pre = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)
        clk = {"is_open": True, "timestamp": "2026-11-05T14:00:00Z"}
        cal = [{"date": "2026-11-05", "open": "09:30", "close": "16:00"}]
        d = evaluate_market_clock(
            market_calendar_id="US_EQ", now_utc=pre,
            sources=[AlpacaClockSource(http_get=_transport(clock_body=clk,
                                                           cal_body=cal))])
        self.assertFalse(d.may_submit)

    def test_november_mid_session_open(self):
        mid = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)  # 11:00 ET
        clk = {"is_open": True, "timestamp": "2026-11-05T16:00:00Z"}
        cal = [{"date": "2026-11-05", "open": "09:30", "close": "16:00"}]
        d = evaluate_market_clock(
            market_calendar_id="US_EQ", now_utc=mid,
            sources=[AlpacaClockSource(http_get=_transport(clock_body=clk,
                                                           cal_body=cal))])
        self.assertTrue(d.may_submit)


class TestUnsupported(_Creds):
    def test_non_us_eq_unavailable(self):
        r = self._src().get_status(market_calendar_id="UK_EQ", now_utc=NOW)
        self.assertEqual(r.status, STATUS_UNAVAILABLE)


class TestNoSecretLeak(_Creds):
    def test_secret_never_in_source_result(self):
        for kw in ({}, {"clock_code": 401}, {"clock_json_ok": False},
                   {"cal_code": 500}, {"raise_on": "/v2/clock"}):
            r = self._status(**kw)
            blob = repr(r) + repr(r.raw_summary)
            self.assertNotIn("testkey", blob)
            self.assertNotIn("testsecret", blob)

    def test_secret_sent_in_headers_only(self):
        rec = []
        src = AlpacaClockSource(http_get=_transport(record_headers=rec))
        src.get_status(market_calendar_id="US_EQ", now_utc=NOW)
        # headers carried the key (that's correct), but nothing returned did.
        self.assertTrue(any(h.get("APCA-API-KEY-ID") == "testkey" for h in rec))

    def test_no_credentials_result_has_no_secret(self):
        os.environ["ALPACA_KEY"] = "leakme"
        os.environ["ALPACA_SECRET"] = ""
        r = self._status()
        self.assertNotIn("leakme", repr(r) + repr(r.raw_summary))


class TestExplicitTimeout(_Creds):
    def test_timeout_is_passed_to_transport(self):
        seen = {}

        def _get(url, *, headers, params, timeout):
            seen["timeout"] = timeout
            return HttpResponse(status_code=200, json_body=CLOCK_CLOSED)

        AlpacaClockSource(http_get=_get, timeout_sec=3.5).get_status(
            market_calendar_id="US_EQ", now_utc=NOW)
        self.assertEqual(seen["timeout"], 3.5)

    def test_default_timeout_is_explicit(self):
        seen = {}

        def _get(url, *, headers, params, timeout):
            seen["timeout"] = timeout
            return HttpResponse(status_code=200, json_body=CLOCK_CLOSED)

        AlpacaClockSource(http_get=_get).get_status(
            market_calendar_id="US_EQ", now_utc=NOW)
        self.assertIsNotNone(seen.get("timeout"))
        self.assertGreater(seen["timeout"], 0)


class TestHygiene(unittest.TestCase):
    def _read(self):
        with open(ALPACA_SRC) as fh:
            return fh.read()

    def _code_only(self, src):
        import io, tokenize
        out = []
        prev = tokenize.INDENT
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    continue
                if tok.type == tokenize.STRING and prev in (
                        tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
                        tokenize.DEDENT, tokenize.ENCODING):
                    prev = tok.type
                    continue
                if tok.type not in (tokenize.NL, tokenize.NEWLINE,
                                    tokenize.INDENT, tokenize.DEDENT,
                                    tokenize.ENCODING):
                    out.append(tok.string)
                prev = tok.type
        except tokenize.TokenError:
            return src
        return " ".join(out)

    def _toplevel_imports(self, src):
        tree = ast.parse(src)
        mods = set()
        for n in tree.body:  # top-level only (lazy imports inside funcs are ok)
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    mods.add(n.module)
        return mods

    def test_no_order_scheduler_dashboard_imports(self):
        mods = self._toplevel_imports(self._read())
        for m in mods:
            for bad in ("execution", "scheduler", "dashboard", "order", "main"):
                self.assertNotIn(bad, m.lower())

    def test_no_db_or_persistence_imports(self):
        src = self._read()
        mods = self._toplevel_imports(src)
        for m in mods:
            for bad in ("sqlite3", "outcome_persistence", "database",
                        "broker_allocation", "runtime_policy",
                        "signal_only_broker"):
                self.assertNotIn(bad, m.lower())
        self.assertNotIn("paper_lifecycles", src)

    def test_no_ibkr_imports(self):
        mods = self._toplevel_imports(self._read())
        for m in mods:
            self.assertNotIn("ib_insync", m.lower())
            self.assertNotIn("ibkr", m.lower())

    def test_no_hardcoded_session_hours_as_authority(self):
        code = self._code_only(self._read())
        for lit in ("09:30", "16:00", "9:30"):
            self.assertNotIn(lit, code)

    def test_no_safety_assert(self):
        tree = ast.parse(self._read())
        self.assertEqual(
            [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])

    def test_requests_is_lazy_not_toplevel(self):
        # requests must be imported lazily inside the default transport, so the
        # module has no hard network dependency at import time.
        mods = self._toplevel_imports(self._read())
        self.assertNotIn("requests", {m.lower() for m in mods})


if __name__ == "__main__":
    unittest.main(verbosity=2)

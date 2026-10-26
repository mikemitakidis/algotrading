#!/usr/bin/env python3
"""M21.1extra-D1 Commit 3 — proof-runner contract tests.

Verifies the read-only VPS proof runner: the no-credentials path proves
fail-closed with RC=0, the offline safe checks all hold, the output contract
strings exist, and the runner has no forbidden imports / no secret printing /
no top-level network.
"""
from __future__ import annotations

import ast
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import tools.paper_loop.m21_1extra_d1_market_clock_proof as PROOF
from bot.market_clock.alpaca_clock import HttpError, HttpResponse
import bot.market_clock.alpaca_clock as AC

RUNNER_SRC = "tools/paper_loop/m21_1extra_d1_market_clock_proof.py"
_US_EASTERN = ZoneInfo("America/New_York")


def _session_date(now_utc: datetime) -> str:
    # The Alpaca calendar is keyed by the US/Eastern trading date, and the
    # production adapter looks up now_utc.astimezone(America/New_York).date().
    # Using the UTC date here made the row "tomorrow" between 00:00 and
    # 04:00/05:00 UTC, so the open check failed in that window (C5b).
    return now_utc.astimezone(_US_EASTERN).date().isoformat()


class _NoCreds(unittest.TestCase):
    def setUp(self):
        self._saved = (os.environ.get("ALPACA_KEY"),
                       os.environ.get("ALPACA_SECRET"))
        os.environ.pop("ALPACA_KEY", None)
        os.environ.pop("ALPACA_SECRET", None)

    def tearDown(self):
        for k, v in zip(("ALPACA_KEY", "ALPACA_SECRET"), self._saved):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestNoCredentialsPath(_NoCreds):
    def test_rc_zero_all_failclosed(self):
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 0)
        self.assertEqual(proof["failed"], [])

    def test_offline_checks_present_and_true(self):
        _, proof = PROOF.run_proof()
        for name in ("unsupported_blocks", "no_source_blocks",
                     "mock_unavailable_blocks", "mock_malformed_blocks",
                     "mock_open_with_calendar_allows",
                     "mock_open_missing_calendar_blocks",
                     "decision_exposes_c_fields"):
            self.assertTrue(proof["checks"].get(name), "missing/false: %s" % name)

    def test_no_credentials_marked_pending(self):
        _, proof = PROOF.run_proof()
        self.assertFalse(proof["live"]["have_credentials"])
        self.assertEqual(proof["live"]["live_fresh_response_proof"],
                         "pending_credentials")
        self.assertTrue(proof["checks"]["alpaca_no_credentials_blocks"])

    def test_output_contract_strings(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = PROOF.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(PROOF.JSON_MARKER, out)
        self.assertIn(PROOF.OK_MARKER, out)
        self.assertIn("%s=0" % PROOF.RC_MARKER, out)


class TestCredentialsPathMocked(unittest.TestCase):
    def setUp(self):
        self._saved = (os.environ.get("ALPACA_KEY"),
                       os.environ.get("ALPACA_SECRET"))
        self._orig_get = AC._default_http_get
        os.environ["ALPACA_KEY"] = "PKREALISTICKEY1234567890"
        os.environ["ALPACA_SECRET"] = "realisticsecretABCDEF1234567890abcdef12"

    def tearDown(self):
        AC._default_http_get = self._orig_get
        for k, v in zip(("ALPACA_KEY", "ALPACA_SECRET"), self._saved):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _patch(self, is_open, session_date=_session_date):
        def fake(url, *, headers, params, timeout):
            now = PROOF._utcnow()
            if "/v2/clock" in url:
                return HttpResponse(status_code=200, json_body={
                    "is_open": is_open,
                    "timestamp": now.isoformat()})
            return HttpResponse(status_code=200, json_body=[
                {"date": session_date(now), "open": "00:00", "close": "23:59"}])
        AC._default_http_get = fake

    def _patch_raw(self, fn):
        AC._default_http_get = fn

    def test_live_open_fresh_rc0(self):
        self._patch(is_open=True)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 0)
        self.assertTrue(
            proof["checks"]["alpaca_live_source_returned_fresh_open_or_closed"])
        self.assertEqual(proof["live"]["live_source_proof"], "passed")

    def _pin_now(self, iso):
        pinned = datetime.fromisoformat(iso)
        orig = PROOF._utcnow
        PROOF._utcnow = lambda: pinned
        self.addCleanup(setattr, PROOF, "_utcnow", orig)

    def test_live_open_fresh_rc0_in_utc_rollover_window(self):
        # 02:30 UTC on 27 Oct is 22:30 ET on 26 Oct: UTC and ET dates differ.
        # Deterministic regression for the former 00:00-04:00 UTC failure.
        self._pin_now("2026-10-27T02:30:00+00:00")
        self._patch(is_open=True)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 0)
        self.assertEqual(proof["live"]["live_source_proof"], "passed")

    def test_utc_dated_calendar_row_fails_in_rollover_window(self):
        # Non-vacuity: at the same instant the old UTC-dated row (27 Oct) is
        # not today's ET session, so the proof fails. This is the bug the
        # ET-dated fixture fixes, not a production defect.
        self._pin_now("2026-10-27T02:30:00+00:00")
        self._patch(is_open=True,
                    session_date=lambda now: now.date().isoformat())
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 1)
        self.assertNotEqual(proof["live"]["live_source_proof"], "passed")

    def test_live_closed_fresh_rc0(self):
        self._patch(is_open=False)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 0)
        self.assertTrue(
            proof["checks"]["alpaca_live_source_returned_fresh_open_or_closed"])

    def test_live_unavailable_rc1(self):
        # 503 on the clock -> unavailable -> live-source proof fails (RC=1)
        def fake(url, *, headers, params, timeout):
            return HttpResponse(status_code=503, json_body=None)
        self._patch_raw(fake)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 1)
        self.assertFalse(
            proof["checks"]["alpaca_live_source_returned_fresh_open_or_closed"])
        self.assertEqual(proof["live"]["live_source_proof"],
                         "failed_or_pending_rerun")

    def test_live_timeout_rc1(self):
        def fake(url, *, headers, params, timeout):
            raise HttpError("Timeout")
        self._patch_raw(fake)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 1)

    def test_live_malformed_rc1(self):
        def fake(url, *, headers, params, timeout):
            return HttpResponse(status_code=200, json_body=None, ok_json=False)
        self._patch_raw(fake)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 1)

    def test_live_stale_rc1(self):
        # clock timestamp far in the past -> stale -> RC=1
        def fake(url, *, headers, params, timeout):
            if "/v2/clock" in url:
                old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
                return HttpResponse(status_code=200,
                                    json_body={"is_open": True,
                                               "timestamp": old})
            ds = _session_date(PROOF._utcnow())
            return HttpResponse(status_code=200,
                                json_body=[{"date": ds, "open": "00:00",
                                            "close": "23:59"}])
        self._patch_raw(fake)
        rc, proof = PROOF.run_proof()
        self.assertEqual(rc, 1)
        self.assertFalse(
            proof["checks"]["alpaca_live_source_returned_fresh_open_or_closed"])

    def test_offline_checks_still_pass_when_live_fails(self):
        # even when the live-source proof fails, the fail-closed guard checks
        # must still be TRUE — the two claims are separated.
        def fake(url, *, headers, params, timeout):
            return HttpResponse(status_code=503, json_body=None)
        self._patch_raw(fake)
        _, proof = PROOF.run_proof()
        for name in ("unsupported_blocks", "no_source_blocks",
                     "mock_unavailable_blocks", "mock_malformed_blocks",
                     "mock_open_with_calendar_allows",
                     "mock_open_missing_calendar_blocks"):
            self.assertTrue(proof["checks"][name])

    def test_no_secret_in_proof_blob(self):
        self._patch(is_open=True)
        _, proof = PROOF.run_proof()
        import json
        blob = json.dumps(proof)
        self.assertNotIn(os.environ["ALPACA_KEY"], blob)
        self.assertNotIn(os.environ["ALPACA_SECRET"], blob)
        self.assertTrue(proof["checks"]["no_secret_in_proof"])

    def test_no_secret_in_proof_even_when_live_fails(self):
        def fake(url, *, headers, params, timeout):
            raise HttpError("Timeout")
        self._patch_raw(fake)
        _, proof = PROOF.run_proof()
        import json
        blob = json.dumps(proof)
        self.assertNotIn(os.environ["ALPACA_KEY"], blob)
        self.assertNotIn(os.environ["ALPACA_SECRET"], blob)


class TestHygiene(unittest.TestCase):
    def _read(self):
        with open(RUNNER_SRC) as fh:
            return fh.read()

    def _toplevel_imports(self, src):
        tree = ast.parse(src)
        mods = set()
        for n in tree.body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    mods.add(n.module)
        return mods

    def _code_only(self, src):
        import io as _io, tokenize
        out = []
        prev = tokenize.INDENT
        try:
            for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
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

    def test_no_db_imports(self):
        src = self._read()
        mods = self._toplevel_imports(src)
        for m in mods:
            for bad in ("sqlite3", "outcome_persistence", "database"):
                self.assertNotIn(bad, m.lower())
        # scan code only (docstring mentions "no paper_lifecycles" as a promise)
        self.assertNotIn("paper_lifecycles", self._code_only(src))

    def test_no_broker_order_scheduler_dashboard_imports(self):
        mods = self._toplevel_imports(self._read())
        for m in mods:
            for bad in ("brokers", "execution", "scheduler", "dashboard",
                        "order"):
                self.assertNotIn(bad, m.lower())

    def test_no_d0_imports_or_calls(self):
        src = self._read()
        mods = self._toplevel_imports(src)
        for m in mods:
            self.assertNotIn("broker_allocation", m.lower())
            self.assertNotIn("auth_cli", m.lower())
        # no enable/disable CALLS (docstring mentions them as forbidden)
        code = self._code_only(src)
        self.assertNotIn("enable_ibkr_paper", code)
        self.assertNotIn("disable_ibkr_paper", code)

    def test_no_ibkr_gateway_imports(self):
        mods = self._toplevel_imports(self._read())
        for m in mods:
            self.assertNotIn("ib_insync", m.lower())
            self.assertNotIn("gateway", m.lower())

    def test_no_toplevel_network_import(self):
        mods = self._toplevel_imports(self._read())
        for m in mods:
            self.assertNotIn("requests", m.lower())
            self.assertNotIn("urllib", m.lower())

    def test_no_safety_assert(self):
        tree = ast.parse(self._read())
        self.assertEqual(
            [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])

    def test_contract_markers_defined(self):
        self.assertEqual(PROOF.OK_MARKER, "D1_VPS_VERIFY_OK")
        self.assertEqual(PROOF.RC_MARKER, "M21_1EXTRA_D1_VPS_VERIFY_RC")
        self.assertEqual(PROOF.JSON_MARKER, "D1_PROOF_JSON")


if __name__ == "__main__":
    unittest.main(verbosity=2)

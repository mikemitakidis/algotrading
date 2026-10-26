#!/usr/bin/env python3
"""M21.1 — signals-only BUY / SHORT Telegram alerts.

Everything runs offline under C5a isolation: Telegram is a fake transport or
a loopback HTTP server, scoring is the real read-only M19 bridge (or a fake
scorer where a specific bucket is needed). Proves:
  * alert model: long -> BUY, short -> SHORT, strict fail-closed validation,
    score/side consistency, M19 execution_eligible contract;
  * honest, HTML-escaped message with the SIGNAL ONLY banner;
  * policy: bucket floor, BLOCKED never alerts, staleness, short/buy toggles,
    per-cycle cap; real fixture signals produce no alert at the default floor;
  * reliability: write-ahead outbox, dedup across restarts, cooldown,
    compare-and-swap claims, 429 retry_after, 5xx backoff, permanent 4xx,
    max attempts, expiry, interrupted-send recovery (possible-duplicate);
  * transport: classification, token never leaks, real urllib path against a
    loopback server;
  * CLI: dry run writes/sends nothing, --send refusals, credentials file reads
    only TELEGRAM_* keys;
  * SIGNALS ONLY: static import ban, no broker parameter or order flag,
    subprocess sys.modules check after a full cycle, broker submit sentinels.
"""
from __future__ import annotations

import ast
import contextlib
import http.server
import inspect
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _test_isolation  # noqa: E402
_test_isolation.install()

from tools.signal_scoring import run_signal_alerts as cli  # noqa: E402
from tools.signal_scoring import telegram_transport as tt  # noqa: E402
from tools.signal_scoring.score_rank_harness import fixture_signals  # noqa: E402
from tools.signal_scoring.scanner_bridge import score_signal  # noqa: E402
from tools.signal_scoring.signal_alerts import (  # noqa: E402
    SIGNALS_ONLY_BANNER, AlertOutbox, AlertPolicy, InvalidSignal, SignalAlert,
    SignalAlertService, action_for_direction, build_alert, format_alert_message,
    policy_rejection,
)
from tools.signal_scoring.telegram_transport import SendResult, TelegramTransport  # noqa: E402

REPO = Path(__file__).resolve().parent
T0 = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
WS3_FILES = ("tools/signal_scoring/signal_alerts.py",
             "tools/signal_scoring/telegram_transport.py",
             "tools/signal_scoring/run_signal_alerts.py")
FORBIDDEN_MODULE_PREFIXES = (
    "bot.brokers", "bot.etoro", "tools.etoro", "ib_insync", "bot.paper", "bot.paper_loop_d2",
    "bot.risk_authority", "bot.gateway", "bot.flywheel", "bot.execution", "bot.risk",
    "bot.kill_switch", "main", "bot.notifier", "tools.paper_loop", "bot.dashboard_db",
)
FORBIDDEN_CALLS = ("submit", "submit_live", "submit_bracket", "place_order", "placeOrder",
                   "flatten", "flatten_paper_position", "open_position_in_account",
                   "insert_execution_intent")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


def sig(symbol="AAA", direction="long", ts=None, entry=100.0, sl=95.0, tp=115.0, **kw):
    base = dict(symbol=symbol, direction=direction, timestamp=(ts or T0).isoformat(),
                entry_price=entry, stop_loss=sl, target_price=tp, valid_count=3,
                available_tfs=4, route="IBKR", strategy_version=2, rsi=60.0, atr=2.0)
    base.update(kw)
    return base


def short_sig(symbol="SSS", ts=None, entry=100.0, sl=105.0, tp=88.0, **kw):
    base = dict(avg_volume_20d=500000, rsi=38.0, macd_hist=-0.8, vol_ratio=1.3)
    base.update(kw)
    return sig(symbol=symbol, direction="short", ts=ts, entry=entry, sl=sl, tp=tp, **base)


def fake_score(signal, *, score=70.0, bucket="ELIGIBLE", conf="MEDIUM_HIGH", hard_gate=True,
               side=None, eligible=False, symbol=None):
    return SimpleNamespace(
        symbol=symbol or signal["symbol"], side=side or signal["direction"].upper(),
        final_score_100=score, decision_bucket=bucket, confidence_bucket=conf,
        hard_gate_passed=hard_gate, execution_eligible=eligible,
        reason_codes=["calibration_unavailable"])


def scorer_by_symbol(table):
    """table: symbol -> kwargs for fake_score."""
    def scorer(signal):
        return fake_score(signal, **table.get(signal["symbol"], {}))
    return scorer


class FakeTransport:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return SendResult(ok=True, status=200, message_id=str(len(self.sent)))


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="m21_alerts_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.db = os.path.join(self.d, "alerts.db")
        self.clock = Clock()

    def svc(self, transport=None, policy=None, scorer=None, db=None):
        return SignalAlertService(outbox=AlertOutbox(db or self.db), transport=transport,
                                  policy=policy or AlertPolicy(), clock=self.clock,
                                  scorer=scorer or scorer_by_symbol({}))

    def rows(self, db=None):
        return AlertOutbox(db or self.db).rows()


# ───────────────────────────── model & message ─────────────────────────────

class AlertModel(unittest.TestCase):
    def test_direction_maps_to_buy_and_short(self):
        self.assertEqual(action_for_direction("long"), "BUY")
        self.assertEqual(action_for_direction("short"), "SHORT")
        for bad in ("LONG", "buy", None, "", 1):
            with self.assertRaises(InvalidSignal):
                action_for_direction(bad)

    def test_build_from_real_m19_score(self):
        s = fixture_signals()[0]
        s["timestamp"] = T0.isoformat()
        a = build_alert(s, score_signal(s))
        self.assertEqual((a.symbol, a.action, a.direction), ("AAA", "BUY", "long"))
        self.assertEqual(a.decision_bucket, "REJECT")           # enum normalised to value
        self.assertIn(a.confidence_bucket, ("LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH"))
        self.assertEqual(a.alert_key, "AAA|BUY|2026-10-26T14:00:00+00:00")
        self.assertAlmostEqual(a.reward_risk, 3.0)
        self.assertEqual(SignalAlert.from_json(a.to_json()), a)

    def test_short_from_real_m19_score(self):
        s = short_sig()
        a = build_alert(s, score_signal(s))
        self.assertEqual(a.action, "SHORT")
        self.assertNotIn(a.decision_bucket, ("ELIGIBLE", "HIGH_CONVICTION"))  # M19 short cap

    def test_fail_closed_validation(self):
        cases = [
            (sig(symbol="aaa"), {}), (sig(symbol=""), {}), (sig(direction="up"), {}),
            (sig(ts=None, timestamp="not-a-time"), {}),
            (dict(sig(), timestamp="2026-10-26T14:00:00"), {}),          # naive
            (sig(entry=float("nan")), {}), (sig(sl=0), {}), (sig(tp=True), {}),
            (sig(sl=101.0), {}),                                       # long stop above entry
            (short_sig(sl=99.0), {}),                                  # short stop below entry
            (sig(), {"side": "SHORT"}),                                # score side mismatch
            (sig(), {"symbol": "ZZZ"}),                                # score for another symbol
            (sig(), {"eligible": True}),                               # M19 contract breach
            (sig(), {"score": float("inf")}), (sig(), {"bucket": "MAYBE"}),
        ]
        for s, kw in cases:
            with self.subTest(s=s, kw=kw), self.assertRaises(InvalidSignal):
                build_alert(s, fake_score(s, **kw))

    def test_message_is_honest_and_escaped(self):
        s = sig(route="<b>IBKR</b>", strategy_version="2&<script>")
        a = build_alert(s, fake_score(s, score=71.26))
        m = format_alert_message(a)
        self.assertIn("🟢 <b>BUY signal — AAA</b>", m)
        self.assertIn(SIGNALS_ONLY_BANNER, m)
        self.assertIn("not a win probability", m)
        self.assertIn("Score 71.3/100", m)
        self.assertNotIn("<script>", m)
        self.assertIn("&lt;b&gt;IBKR&lt;/b&gt;", m)
        self.assertIn("2&amp;&lt;script&gt;", m)
        self.assertLessEqual(len(m), 4096)
        for word in ("probability of", "guaranteed", "will rise", "order placed\n"):
            self.assertNotIn(word, m.lower())

    def test_short_message(self):
        s = short_sig()
        m = format_alert_message(build_alert(s, fake_score(s, bucket="MANUAL_REVIEW")))
        self.assertIn("🔴 <b>SHORT signal — SSS</b>", m)
        self.assertIn("never execution-eligible", m)
        self.assertIn(SIGNALS_ONLY_BANNER, m)

    def test_possible_duplicate_banner(self):
        s = sig()
        a = build_alert(s, fake_score(s))
        self.assertIn("Possible duplicate", format_alert_message(a, possible_duplicate=True))
        self.assertNotIn("Possible duplicate", format_alert_message(a))


# ───────────────────────────── policy ─────────────────────────────

class AlertPolicyTests(unittest.TestCase):
    def a(self, s=None, **kw):
        s = s or sig()
        return build_alert(s, fake_score(s, **kw))

    def test_bucket_floor_and_blocked(self):
        p = AlertPolicy()
        self.assertIsNone(policy_rejection(self.a(bucket="MANUAL_REVIEW"), p, T0))
        self.assertEqual(policy_rejection(self.a(bucket="WATCH"), p, T0), "below_min_bucket")
        lo = AlertPolicy(min_decision_bucket="REJECT")
        self.assertEqual(policy_rejection(self.a(bucket="BLOCKED"), lo, T0), "blocked_by_hard_gate")
        with self.assertRaises(ValueError):
            AlertPolicy(min_decision_bucket="BLOCKED")

    def test_score_gate_toggles_and_age(self):
        self.assertEqual(policy_rejection(self.a(score=61), AlertPolicy(min_score_100=65), T0),
                         "below_min_score")
        self.assertEqual(policy_rejection(self.a(hard_gate=False),
                                          AlertPolicy(require_hard_gate=True), T0),
                         "hard_gate_not_passed")
        self.assertEqual(policy_rejection(self.a(short_sig(), bucket="MANUAL_REVIEW"),
                                          AlertPolicy(allow_short=False), T0),
                         "short_alerts_disabled")
        self.assertEqual(policy_rejection(self.a(), AlertPolicy(allow_buy=False), T0),
                         "buy_alerts_disabled")
        self.assertEqual(policy_rejection(self.a(), AlertPolicy(), T0 + timedelta(minutes=31)),
                         "stale_signal")
        self.assertEqual(policy_rejection(self.a(), AlertPolicy(), T0 - timedelta(minutes=5)),
                         "signal_from_future")

    def test_real_fixture_signals_do_not_alert_at_default_floor(self):
        sigs = [dict(s, timestamp=T0.isoformat()) for s in fixture_signals()]
        svc = SignalAlertService(outbox=None, transport=None, clock=Clock())
        alerts, skipped = svc.evaluate(sigs)
        self.assertEqual(alerts, [])
        self.assertEqual({r["reason"] for r in skipped}, {"below_min_bucket"})

    def test_real_scores_rank_when_floor_lowered(self):
        sigs = [dict(s, timestamp=T0.isoformat()) for s in fixture_signals()]
        svc = SignalAlertService(outbox=None, transport=None, clock=Clock(),
                                 policy=AlertPolicy(min_decision_bucket="REJECT"))
        alerts, _ = svc.evaluate(sigs)
        scores = [a.score_100 for a in alerts]
        self.assertEqual(len(alerts), 4)
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_cap_bad_items_and_scorer_errors_never_crash(self):
        def scorer(s):
            if s["symbol"] == "ERR":
                raise RuntimeError("boom")
            return fake_score(s, score=50 + len(s["symbol"]))
        sigs = [sig("A"), sig("BB"), sig("CCC"), sig("ERR"), "junk", sig(symbol="bad")]
        svc = SignalAlertService(outbox=None, transport=None, clock=Clock(), scorer=scorer,
                                 policy=AlertPolicy(max_alerts_per_cycle=2))
        alerts, skipped = svc.evaluate(sigs)
        self.assertEqual([a.symbol for a in alerts], ["CCC", "BB"])
        reasons = sorted(r["reason"].split(":")[0] for r in skipped)
        self.assertEqual(reasons, ["invalid_signal", "invalid_signal", "per_cycle_cap",
                                   "scoring_error"])


# ───────────────────────────── outbox & delivery ─────────────────────────────

class OutboxReliability(TmpDir):
    def test_send_once_and_never_again_across_restarts(self):
        ft = FakeTransport()
        r = self.svc(ft).run_cycle([sig()])
        self.assertEqual(r["delivery"]["sent"], ["AAA|BUY|2026-10-26T14:00:00+00:00"])
        self.assertTrue(r["signals_only"])
        self.clock.advance(minutes=1)
        r2 = self.svc(FakeTransport()).run_cycle([sig()])       # "restart": new objects
        self.assertEqual(r2["enqueued"], [])
        self.assertEqual(r2["skipped"], [{"symbol": "AAA", "reason": "duplicate_signal"}])
        row, = self.rows()
        self.assertEqual((row["status"], row["attempts"], row["telegram_message_id"]),
                         ("sent", 1, "1"))
        self.assertEqual(len(ft.sent), 1)

    def test_cooldown_blocks_rescans_but_not_other_side_or_after_window(self):
        ft = FakeTransport()
        self.svc(ft).run_cycle([sig()])
        self.clock.advance(minutes=15)
        r = self.svc(ft).run_cycle([sig(ts=self.clock.t)])       # scanner re-emits, new ts
        self.assertEqual(r["skipped"], [{"symbol": "AAA", "reason": "cooldown"}])
        r = self.svc(ft, scorer=scorer_by_symbol({"AAA": {"bucket": "MANUAL_REVIEW"}})
                     ).run_cycle([short_sig("AAA", ts=self.clock.t)])
        self.assertEqual(len(r["enqueued"]), 1)                 # SHORT is a different alert
        self.clock.advance(minutes=240)
        r = self.svc(ft).run_cycle([sig(ts=self.clock.t)])
        self.assertEqual(len(r["delivery"]["sent"]), 1)
        self.assertEqual(len(ft.sent), 3)

    def test_failed_or_expired_alerts_do_not_hold_cooldown(self):
        self.svc(FakeTransport([SendResult(ok=False, retryable=False, status=400,
                                           error="rejected")])).run_cycle([sig()])
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.clock.advance(minutes=1)
        r = self.svc(FakeTransport()).run_cycle([sig(ts=self.clock.t)])
        self.assertEqual(len(r["delivery"]["sent"]), 1)

    def test_write_ahead_survives_crash_before_send(self):
        self.svc(None).run_cycle([sig()])                       # enqueue only, then "crash"
        self.assertEqual(self.rows()[0]["status"], "pending")
        ft = FakeTransport()
        r = self.svc(ft).deliver_pending()
        self.assertEqual(len(r["sent"]), 1)
        self.assertEqual(len(ft.sent), 1)

    def test_rate_limit_honours_retry_after(self):
        ft = FakeTransport([SendResult(ok=False, retryable=True, status=429, retry_after=42.0,
                                       error="rate_limited")])
        r = self.svc(ft).run_cycle([sig()])
        self.assertEqual(len(r["delivery"]["retry"]), 1)
        row, = self.rows()
        self.assertEqual(row["next_attempt_at_utc"], (T0 + timedelta(seconds=42)).isoformat())
        self.clock.advance(seconds=41)
        self.assertEqual(self.svc(ft).deliver_pending()["sent"], [])
        self.clock.advance(seconds=1)
        self.assertEqual(len(self.svc(ft).deliver_pending()["sent"]), 1)
        self.assertEqual(self.rows()[0]["attempts"], 2)

    def test_server_errors_back_off_then_fail_at_max_attempts(self):
        err = lambda: SendResult(ok=False, retryable=True, status=502, error="server")  # noqa: E731
        ft = FakeTransport([err() for _ in range(10)])
        pol = AlertPolicy(max_attempts=3, backoff_base_seconds=10, max_delivery_delay_minutes=60)
        self.svc(ft, policy=pol).run_cycle([sig()])
        gaps = []
        for _ in range(2):
            nxt = datetime.fromisoformat(self.rows()[0]["next_attempt_at_utc"])
            gaps.append((nxt - self.clock.t).total_seconds())
            self.clock.t = nxt
            self.svc(ft, policy=pol).deliver_pending()
        self.assertEqual(gaps, [10.0, 20.0])                    # exponential backoff
        row, = self.rows()
        self.assertEqual((row["status"], row["attempts"]), ("failed", 3))
        self.assertIn("max attempts", row["last_error"])
        self.assertEqual(len(ft.sent), 3)

    def test_permanent_rejection_is_not_retried(self):
        ft = FakeTransport([SendResult(ok=False, retryable=False, status=403, error="rejected:403")])
        r = self.svc(ft).run_cycle([sig()])
        self.assertEqual(len(r["delivery"]["failed"]), 1)
        self.clock.advance(minutes=5)
        self.svc(ft).deliver_pending()
        self.assertEqual(len(ft.sent), 1)

    def test_transport_exception_is_retryable(self):
        ft = FakeTransport([RuntimeError("socket closed")])
        r = self.svc(ft).run_cycle([sig()])
        self.assertEqual(len(r["delivery"]["retry"]), 1)
        self.assertIn("transport_exception", self.rows()[0]["last_error"])

    def test_undelivered_alert_expires_instead_of_arriving_late(self):
        ft = FakeTransport([SendResult(ok=False, retryable=True, status=503, error="server")] * 20)
        self.svc(ft).run_cycle([sig()])
        self.clock.advance(minutes=31)
        r = self.svc(FakeTransport()).deliver_pending()
        self.assertEqual(len(r["expired"]), 1)
        self.assertEqual(r["sent"], [])
        self.assertEqual(self.rows()[0]["status"], "expired")

    def test_interrupted_send_is_requeued_as_possible_duplicate(self):
        svc = self.svc(None)
        svc.run_cycle([sig()])
        key = self.rows()[0]["alert_key"]
        self.assertTrue(svc.outbox.claim(key, now=self.clock.t))   # process dies mid-send
        ft = FakeTransport()
        self.clock.advance(seconds=60)                              # inside the lease
        self.assertEqual(self.svc(ft).deliver_pending()["sent"], [])
        self.clock.advance(seconds=61)                              # lease expired
        r = self.svc(ft).deliver_pending()
        self.assertEqual(r["requeued_interrupted"], 1)
        self.assertEqual(len(r["sent"]), 1)
        self.assertIn("Possible duplicate", ft.sent[0])
        self.assertEqual(self.rows()[0]["attempts"], 2)

    def test_claim_is_compare_and_swap(self):
        svc = self.svc(None)
        svc.run_cycle([sig()])
        key = self.rows()[0]["alert_key"]
        self.assertTrue(AlertOutbox(self.db).claim(key, now=T0))
        self.assertFalse(AlertOutbox(self.db).claim(key, now=T0))

    def test_concurrent_enqueue_inserts_once(self):
        s = sig()
        alert = build_alert(s, fake_score(s))
        results, barrier = [], threading.Barrier(8)

        def worker():
            ob = AlertOutbox(self.db)
            barrier.wait()
            results.append(ob.enqueue(alert, now=T0, cooldown_minutes=240)[0])
        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), [False] * 7 + [True])
        self.assertEqual(len(self.rows()), 1)

    def test_schema_constraints(self):
        AlertOutbox(self.db)
        c = sqlite3.connect(self.db)
        self.addCleanup(c.close)
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO signal_alert_outbox (alert_key, symbol, action, signal_ts_utc,"
                      " created_at_utc, alert_json, status, next_attempt_at_utc)"
                      " VALUES ('k','AAA','SELL','t','t','{}','pending','t')")


# ───────────────────────────── transport ─────────────────────────────

TOKEN = "123456:TEST-ONLY-NOT-A-REAL-TOKEN"


class Transport(unittest.TestCase):
    def tr(self, status=200, body=None, exc=None):
        calls = []

        def post(url, data, timeout):
            calls.append((url, dict(data), timeout))
            if exc:
                raise exc
            return status, json.dumps(body if body is not None else
                                      {"ok": True, "result": {"message_id": 77}})
        return TelegramTransport(token=TOKEN, chat_id="-100123", http_post=post), calls

    def test_success_request_shape(self):
        t, calls = self.tr()
        r = t.send("<b>hi</b>")
        self.assertEqual((r.ok, r.message_id), (True, "77"))
        url, data, timeout = calls[0]
        self.assertEqual(url, "https://api.telegram.org/bot%s/sendMessage" % TOKEN)
        self.assertEqual((data["chat_id"], data["parse_mode"], data["text"]),
                         ("-100123", "HTML", "<b>hi</b>"))
        self.assertEqual(timeout, 10.0)

    def test_classification(self):
        cases = [
            (429, {"ok": False, "parameters": {"retry_after": 7}}, True, 7.0),
            (429, {"ok": False}, True, None),
            (500, {"ok": False}, True, None),
            (200, {"ok": False, "description": "odd"}, True, None),
            (400, {"ok": False, "description": "Bad Request: chat not found"}, False, None),
            (401, {"ok": False, "description": "Unauthorized"}, False, None),
        ]
        for status, body, retryable, ra in cases:
            with self.subTest(status=status, body=body):
                r = self.tr(status, body)[0].send("x")
                self.assertFalse(r.ok)
                self.assertEqual((r.retryable, r.retry_after), (retryable, ra))
        r = self.tr(502, None)[0]
        r._post = lambda *a: (502, "<html>bad gateway</html>")
        self.assertTrue(r.send("x").retryable)

    def test_token_never_leaks(self):
        t, _ = self.tr(exc=OSError("cannot reach https://api.telegram.org/bot%s/sendMessage" % TOKEN))
        r = t.send("x")
        self.assertTrue(r.retryable)
        self.assertNotIn(TOKEN, r.error)
        self.assertIn("<redacted>", r.error)
        t2, _ = self.tr(401, {"ok": False, "description": "token %s invalid" % TOKEN})
        self.assertNotIn(TOKEN, t2.send("x").error)
        self.assertNotIn(TOKEN, repr(t2))

    def test_constructor_requires_credentials(self):
        for kw in ({"token": "", "chat_id": "1"}, {"token": "t", "chat_id": " "},
                   {"token": None, "chat_id": "1"}):
            with self.assertRaises(ValueError):
                TelegramTransport(**kw)

    def test_real_urllib_path_against_loopback_server(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                seen.append((self.path, self.rfile.read(n).decode()))
                code = 429 if len(seen) == 1 else 200
                body = ({"ok": False, "parameters": {"retry_after": 3}} if code == 429
                        else {"ok": True, "result": {"message_id": 5}})
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        def post(url, data, timeout):
            local = url.replace("https://api.telegram.org", "http://127.0.0.1:%d" % port)
            return tt.urllib_post(local, data, timeout)
        t = TelegramTransport(token=TOKEN, chat_id="42", http_post=post, timeout=5)
        r1, r2 = t.send("first & <b>x</b>"), t.send("second")
        self.assertEqual((r1.ok, r1.retryable, r1.retry_after), (False, True, 3.0))
        self.assertEqual((r2.ok, r2.message_id), (True, "5"))
        self.assertTrue(seen[0][0].endswith("/sendMessage"))
        self.assertIn("parse_mode=HTML", seen[0][1])
        self.assertIn("text=first+%26+%3Cb%3Ex%3C%2Fb%3E", seen[0][1])


# ───────────────────────────── CLI ─────────────────────────────

class Cli(TmpDir):
    def write_signals(self, signals):
        p = os.path.join(self.d, "signals.json")
        Path(p).write_text(json.dumps(signals))
        return p

    def run_main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        base = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
        base.update(env or {})
        with mock.patch.dict(os.environ, base, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def fresh(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return [dict(s, timestamp=now.isoformat()) for s in fixture_signals()]

    def test_dry_run_writes_and_sends_nothing(self):
        p = self.write_signals(self.fresh())
        with mock.patch.object(tt, "urllib_post", side_effect=AssertionError("network")):
            rc, out, _ = self.run_main("--from-json", p, "--db", self.db, "--min-bucket", "REJECT",
                                       env={"TELEGRAM_ENABLED": "true",
                                            "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1"})
        self.assertEqual(rc, 0)
        self.assertIn("----- would send -----", out)
        self.assertIn(SIGNALS_ONLY_BANNER, out)
        report = json.loads(out[out.index("\n{") + 1:])
        self.assertEqual((report["mode"], len(report["would_alert"])), ("dry-run", 4))
        self.assertFalse(os.path.exists(self.db))

    def test_default_floor_dry_run_reports_no_alerts_for_fixtures(self):
        rc, out, _ = self.run_main("--from-json", self.write_signals(self.fresh()))
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertEqual(report["would_alert"], [])
        self.assertTrue(report["signals_only"])

    def test_send_refusals(self):
        p = self.write_signals(self.fresh())
        rc, _, err = self.run_main("--from-json", p, "--send", "--db", self.db)
        self.assertEqual(rc, 2)
        self.assertIn("TELEGRAM_ENABLED", err)
        rc, _, err = self.run_main("--from-json", p, "--send", "--db", self.db,
                                   env={"TELEGRAM_ENABLED": "true"})
        self.assertEqual(rc, 2)
        self.assertIn("missing", err)
        self.assertFalse(os.path.exists(self.db))

    def test_send_path_with_fake_http(self):
        p = self.write_signals(self.fresh())
        creds = os.path.join(self.d, "telegram_credentials.txt")
        Path(creds).write_text("# test only\nexport TELEGRAM_ENABLED=true\n"
                               "TELEGRAM_BOT_TOKEN='%s'\nTELEGRAM_CHAT_ID=\"42\"\n"
                               "IBKR_ACCOUNT=SHOULD-NOT-LOAD\n" % TOKEN)
        posts = []

        class Patched(TelegramTransport):
            def __init__(self, **kw):
                super().__init__(http_post=lambda u, d, t: (posts.append(d) or
                                 (200, '{"ok":true,"result":{"message_id":9}}')), **kw)
        with mock.patch.object(tt, "TelegramTransport", Patched):
            rc, out, _ = self.run_main("--from-json", p, "--send", "--db", self.db,
                                       "--credentials-file", creds, "--min-bucket", "REJECT",
                                       "--max-alerts", "2")
            self.assertNotIn("IBKR_ACCOUNT", os.environ)
            rc2, out2, _ = self.run_main("--from-json", p, "--send", "--db", self.db,
                                         "--credentials-file", creds, "--min-bucket", "REJECT")
        self.assertEqual((rc, rc2), (0, 0))
        r1, r2 = json.loads(out), json.loads(out2)
        self.assertEqual(len(r1["delivery"]["sent"]), 2)
        self.assertEqual(len(r2["delivery"]["sent"]), 2)        # the other two; first two dedup
        self.assertEqual(sorted(x["reason"] for x in r2["skipped"]),
                         ["duplicate_signal", "duplicate_signal"])
        self.assertEqual(len(posts), 4)
        self.assertNotIn(TOKEN, out + out2)

    def test_credentials_reader_only_reads_telegram_keys(self):
        f = os.path.join(self.d, "creds.txt")
        Path(f).write_text("TELEGRAM_BOT_TOKEN=abc\nETORO_API_KEY=secret\nTELEGRAM_CHAT_ID=1\n")
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "env-wins"}):
            vals = cli.read_telegram_credentials(f)
        self.assertEqual(vals, {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "env-wins"})

    def test_source_is_required_and_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in ([], ["--scan", "--drain-only"]):
                with self.assertRaises(SystemExit):
                    cli.build_parser().parse_args(argv)


# ───────────────────────────── signals-only safety ─────────────────────────────

class SignalsOnlySafety(unittest.TestCase):
    def test_static_import_and_call_ban(self):
        for rel in WS3_FILES:
            tree = ast.parse((REPO / rel).read_text(), rel)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    for f in FORBIDDEN_MODULE_PREFIXES:
                        self.assertFalse(m == f or m.startswith(f + "."),
                                         "%s imports %s" % (rel, m))
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                    self.assertNotIn(name, FORBIDDEN_CALLS, "%s calls %s" % (rel, name))

    def test_no_broker_parameter_and_no_order_flag(self):
        params = inspect.signature(SignalAlertService.__init__).parameters
        self.assertEqual(set(params), {"self", "outbox", "transport", "policy", "clock", "scorer"})
        opts = [o for a in cli.build_parser()._actions for o in a.option_strings]
        for o in opts:
            for word in ("order", "trade", "broker", "live", "paper", "execute", "i-understand"):
                self.assertNotIn(word, o)

    def test_full_cycle_in_fresh_interpreter_loads_no_broker_code(self):
        code = r"""
import json, os, sys, tempfile
sys.path.insert(0, %r)
from datetime import datetime, timezone
from tools.signal_scoring.signal_alerts import AlertOutbox, AlertPolicy, SignalAlertService
from tools.signal_scoring.telegram_transport import TelegramTransport
from tools.signal_scoring.score_rank_harness import fixture_signals
from tools.signal_scoring import run_signal_alerts
import bot.scanner, bot.focus, bot.universe.active_selection   # the --scan import path
now = datetime.now(timezone.utc)
sigs = [dict(s, timestamp=now.isoformat()) for s in fixture_signals()]
t = TelegramTransport(token="1:x", chat_id="1",
                      http_post=lambda u, d, to: (200, '{"ok":true,"result":{"message_id":1}}'))
d = tempfile.mkdtemp()
svc = SignalAlertService(outbox=AlertOutbox(os.path.join(d, "a.db")), transport=t,
                         policy=AlertPolicy(min_decision_bucket="REJECT"))
r = svc.run_cycle(sigs)
print(json.dumps({"sent": len(r["delivery"]["sent"]), "modules": sorted(sys.modules)}))
""" % str(REPO)
        p = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        res = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(res["sent"], 4)
        loaded = [m for m in res["modules"] for f in FORBIDDEN_MODULE_PREFIXES
                  if m == f or m.startswith(f + ".")]
        self.assertEqual(loaded, [], "signals-only path loaded broker/order modules")

    def test_broker_submit_sentinels_never_fire(self):
        import importlib
        fired = []

        def sentinel(name):
            def boom(*a, **k):
                fired.append(name)
                raise AssertionError("ORDER PATH REACHED: %s" % name)
            return boom
        patches = []
        for modname, cls, meths in (
                ("bot.brokers.ibkr_broker", "IBKRBroker", ("submit",)),
                ("bot.brokers.paper_broker", "PaperBroker", ("submit",)),
                ("bot.etoro.live_broker", "EtoroLiveBroker", ("submit", "submit_live")),
                ("bot.etoro.paper_broker", "PaperEtoroBroker", ("submit",)),
                ("bot.etoro.signal_only_broker", "SignalOnlyBroker", ("submit",)),
                ("ib_insync", "IB", ("placeOrder",))):
            klass = getattr(importlib.import_module(modname), cls)
            for m in meths:
                patches.append(mock.patch.object(klass, m, sentinel("%s.%s" % (cls, m))))
        d = tempfile.mkdtemp(prefix="m21_sentinel_")
        self.addCleanup(shutil.rmtree, d, True)
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            now = datetime.now(timezone.utc)
            sigs = [dict(s, timestamp=now.isoformat()) for s in fixture_signals()] + [
                short_sig(ts=now)]
            svc = SignalAlertService(outbox=AlertOutbox(os.path.join(d, "a.db")),
                                     transport=FakeTransport(),
                                     policy=AlertPolicy(min_decision_bucket="REJECT"))
            r = svc.run_cycle(sigs)
            r2 = svc.deliver_pending()
        self.assertEqual(fired, [])
        self.assertEqual(len(r["delivery"]["sent"]), 5)
        self.assertEqual(r2["sent"], [])


if __name__ == "__main__":
    unittest.main()

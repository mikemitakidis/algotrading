#!/usr/bin/env python3
"""M21 WP7 — candidate-ledger runtime wiring (offline, temporary fixture DBs).

Drives the REAL WS2 paper loop (simulated broker, harness from the WS2 suite)
with the ledger sink. Proves:
  * off by default: a disabled writer (or no sink) changes nothing;
  * one candidate id from scan to terminal outcome; every stage recorded;
    rejected / not-selected / risk-blocked / broker-rejected / not-persisted
    candidates are preserved with reasons;
  * the mandatory audit point: with the ledger enabled, an entry whose intent
    cannot be durably recorded is BLOCKED before planning or submission, and
    the loop survives; when the ledger is back the next slot proceeds;
  * exactly-once logical processing through repeated ticks and restarts;
  * post-submit events lost to a ledger failure are backfilled from the
    journal (the system of record); an intent whose order was never planned
    is closed not_persisted;
  * simulated outcomes carry data_source="simulated";
  * accounting and journal-consistency checks catch missing, duplicated and
    contradictory transitions;
  * the scan-stage records (scored / scoring_blocked / scanner rejects).
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

from bot.candidate_ledger import (  # noqa: E402
    CandidateLedgerWriter, account, make_candidate_id, migrate,
)
from bot.candidate_ledger.loop_sink import (  # noqa: E402
    LoopLedger, journal_consistency, outcome_label,
)
from bot.candidate_ledger import loop_sink as L  # noqa: E402
from bot.paper_loop_d2.journal import LoopJournal  # noqa: E402
from test_m21_1extra_d2_3_learning_loop import LoopHarness, cand, entry_orders  # noqa: E402

NOW = "2026-10-28T09:00:00+00:00"


class LedgerDB:
    def __init__(self, tc, migrated=True):
        self.dir = tempfile.mkdtemp(prefix="wp7_")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "ledger.db")
        self.m14_path = os.path.join(self.dir, "m14.db")     # M14 risk_decisions, as the gate persists them
        self.down = False
        if migrated:
            with sqlite3.connect(self.path) as c:
                migrate(c, now_utc=NOW)
        from bot.flywheel import RISK_DECISIONS_SCHEMA
        with sqlite3.connect(self.m14_path) as c:
            c.execute(RISK_DECISIONS_SCHEMA)

    def decide(self, did, symbol, side, notional, at, result="allow"):
        """Persist an M14 decision exactly as the real gate's audit would."""
        req = json.dumps({"symbol": symbol, "amount_usd": float(notional), "side": side, "leverage": 1})
        t = at.isoformat() if hasattr(at, "isoformat") else str(at)
        with sqlite3.connect(self.m14_path) as c:
            c.execute("INSERT INTO risk_decisions (decision_id, taken_at, broker_scope, requested_action, "
                      "request_json, result, authority_before, authority_after, reason_codes, source, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (did, t, "ibkr_paper", "trade_open", req, result,
                                                         "AUTO_ALLOWED", "AUTO_ALLOWED", "[]", "auto", t))

    def connect(self):
        if self.down:
            raise sqlite3.OperationalError("ledger database unavailable")
        return sqlite3.connect(self.path, timeout=5)

    def writer(self, enabled=True):
        return CandidateLedgerWriter(self.connect, enabled=enabled, writer="paper_loop")

    def sink(self, enabled=True, m14=False):
        return LoopLedger(self.writer(enabled), m14_db=self.m14_path if m14 else None)

    def rows(self, sql, *a):
        with sqlite3.connect(self.path) as c:
            return c.execute(sql, a).fetchall()

    def stages(self, cid):
        return [r[0] for r in self.rows(
            "SELECT stage FROM candidate_ledger_events WHERE candidate_id=? ORDER BY rowid", cid)]

    def terminal(self, cid):
        r = self.rows("SELECT terminal_kind FROM candidate_ledger WHERE candidate_id=?", cid)
        return r[0][0] if r else "<absent>"

    def payload(self, cid, stage):
        r = self.rows("SELECT payload_json FROM candidate_ledger_events WHERE candidate_id=? "
                      "AND stage=?", cid, stage)
        return json.loads(r[0][0]) if r else None

    def report(self, journal_db):
        with sqlite3.connect(self.path) as lc, sqlite3.connect(journal_db) as jc:
            return account(lc), journal_consistency(lc, jc)


def with_ledger(tc, h, db, enabled=True):
    """The loop with the ledger ON and an M14 store. The harness gate's decisions are persisted the way
    the real gate's audit persists them (unique ids, the tick's time, the requested notional), so the
    ledger can authenticate them."""
    used = {}

    def gate(**kw):
        allowed, why, did = h._risk(**kw)
        if did:
            n = used[did] = used.get(did, 0) + 1
            did = did if n == 1 else "%s.%d" % (did, n)
            db.decide(did, kw["symbol"], kw["side"], kw["notional_usd"], h.clock(),
                      "allow" if allowed is True else "block")
        return allowed, why, did
    h.ledger_gate = gate
    h.loop = h.make_loop(ledger=db.sink(enabled, m14=True), risk_gate=gate)
    return h


def relaunch(h, db):
    """A process restart with the same ledger, M14 store and gate."""
    h.loop = h.make_loop(ledger=db.sink(m14=True), risk_gate=h.ledger_gate)


def cid_of(h, c):
    return LoopLedger.candidate_id(c) if c.signal_ts_utc else make_candidate_id(
        c.symbol, h.clock().isoformat(), c.direction, c.strategy_id)


class W1_OffByDefault(unittest.TestCase):
    def run_round_trip(self, h):
        r1 = h.tick()
        h.tick(15)
        h.prices["AAA"] = 111.0
        r3 = h.tick(15)
        return r1, r3

    def test_disabled_or_absent_ledger_changes_nothing(self):
        plain = LoopHarness(self)
        a1, a3 = self.run_round_trip(plain)
        for enabled, migrated in ((False, True), (True, False)):
            db = LedgerDB(self, migrated=migrated)
            h = with_ledger(self, LoopHarness(self), db, enabled=enabled)
            self.assertFalse(h.loop.ledger.enabled)
            b1, b3 = self.run_round_trip(h)
            self.assertEqual(b1["entry"]["state"], a1["entry"]["state"])
            self.assertEqual(b3["closed"][0]["exit_reason"], a3["closed"][0]["exit_reason"])
            self.assertEqual(h.submits(), plain.submits())
            if migrated:
                self.assertEqual(db.rows("SELECT COUNT(*) FROM candidate_ledger")[0][0], 0)

    def test_sink_requires_a_real_writer(self):
        with self.assertRaises(TypeError):
            LoopLedger(object())


class W2_FullTrace(unittest.TestCase):
    def test_one_id_from_scan_to_outcome(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        r1 = h.tick()
        cid = h.orders()[0]["candidate_id"]
        self.assertTrue(cid.startswith("C-"))
        self.assertEqual(cid, make_candidate_id("AAA", h.clock().isoformat(), "long", "trend_v1"))
        h.tick(15)
        h.prices["AAA"] = 111.0
        r3 = h.tick(15)
        self.assertEqual(r3["closed"][0]["exit_reason"], "take_profit")
        self.assertEqual(db.stages(cid), ["scanned", "risk_evaluated", "intent_logged",
                                          "broker_result", "outcome_resolved", "closed"])
        self.assertEqual(db.terminal(cid), "outcome_resolved")
        out = db.payload(cid, "outcome_resolved")
        self.assertEqual((out["outcome"], out["data_source"], out["trade_id"]),
                         ("WIN", "simulated", r1["entry"]["client_ref"]))
        self.assertEqual(db.payload(cid, "risk_evaluated")["risk_decision_id"], "dec-1")
        self.assertEqual(db.rows("SELECT risk_decision_id FROM candidate_ledger")[0][0], "dec-1")
        self.assertEqual(db.payload(cid, "intent_logged")["client_ref"], r1["entry"]["client_ref"])
        acc, cons = db.report(h.db)
        self.assertTrue(acc.ok, acc.violations)
        self.assertTrue(cons.ok, cons.problems)

    def test_rejected_skipped_and_blocked_candidates_are_preserved(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self, candidates=[cand("AAA", 90.0),
                                                            cand("BBB", 70.0, entry=50.0,
                                                                 sl=48.0, tp=55.0)]), db)
        h.risk = (False, "global_kill", "dec-9")
        r = h.tick()
        self.assertEqual(r["entry_block"], "risk:global_kill")
        a = make_candidate_id("AAA", h.clock().isoformat(), "long", "trend_v1")
        b = make_candidate_id("BBB", h.clock().isoformat(), "long", "trend_v1")
        self.assertEqual(db.terminal(a), "risk_rejected")
        self.assertEqual(db.payload(a, "risk_evaluated")["verdict"], "reject")
        self.assertEqual(db.payload(a, "risk_evaluated")["risk_decision_id"], "dec-9")
        self.assertEqual(db.terminal(b), "not_selected")
        # broker rejection
        h.risk = (True, "allow", "dec-2")
        h.broker.reject_next_submit = "insufficient_margin"
        with mock.patch.object(LoopLedger, "reconcile", return_value={}):   # sink alone
            r2 = h.tick(15)
        self.assertEqual(r2["entry_block"], "broker_rejected:rejected")
        a2 = make_candidate_id("AAA", h.clock().isoformat(), "long", "trend_v1")
        self.assertEqual(db.terminal(a2), "broker_not_accepted")
        h.loop.ledger.reconcile(h.db, h.clock())                          # and idempotent
        self.assertEqual(db.payload(a2, "broker_result")["status"], "rejected")
        acc, cons = db.report(h.db)
        self.assertTrue(acc.ok, acc.violations)
        self.assertTrue(cons.ok, cons.problems)

    def test_pre_risk_loop_blocks_are_recorded_as_risk_rejections(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self, candidates=[cand("AAA", entry=100.0)]), db)
        h.prices["AAA"] = 104.0                         # drift beyond the entry tolerance
        r = h.tick()
        self.assertIsNotNone(r["entry_block"])
        a = make_candidate_id("AAA", h.clock().isoformat(), "long", "trend_v1")
        self.assertEqual(db.terminal(a), "risk_rejected")
        self.assertEqual(db.payload(a, "risk_evaluated")["reason"], r["entry_block"])


class W3_MandatoryAudit(unittest.TestCase):
    def test_unrecordable_intent_blocks_the_entry_and_the_loop_survives(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        sink = h.loop.ledger
        real_record = sink.w.record

        def failing(cid, stage, **kw):
            if stage == "intent_logged":
                return False                            # e.g. the write did not commit
            return real_record(cid, stage, **kw)
        with mock.patch.object(sink.w, "record", side_effect=failing):
            r = h.tick()
        self.assertEqual(r["entry_block"], "ledger_audit_unavailable")
        self.assertEqual(h.submits(), 0)                # nothing reached the broker
        self.assertEqual(h.orders(), [])                # nothing was planned
        r2 = h.tick(15)                                 # ledger healthy again
        self.assertEqual(r2["entry"]["state"], "SUBMITTED")

    def test_ledger_database_down_blocks_entries_not_the_loop(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        db.down = True
        r = h.tick()
        self.assertEqual(r["entry_block"], "ledger_audit_unavailable")
        self.assertEqual(h.submits(), 0)
        self.assertTrue(r["skipped"] is None)
        db.down = False
        r2 = h.tick(15)
        self.assertEqual(r2["entry"]["state"], "SUBMITTED")

    def test_sink_exception_is_contained(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        with mock.patch.object(LoopLedger, "selection", side_effect=RuntimeError("boom")), \
                mock.patch.object(LoopLedger, "reconcile", side_effect=RuntimeError("boom")):
            r = h.tick()
        self.assertEqual(r["entry"]["state"], "SUBMITTED")   # non-mandatory failure
        self.assertIn("ledger_selection:RuntimeError", r["errors"])
        self.assertIn("ledger_reconcile:RuntimeError", r["errors"])


class W4_ExactlyOnceAndReconcile(unittest.TestCase):
    def test_repeated_ticks_and_restart_never_duplicate(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        h.tick()
        cid = h.orders()[0]["candidate_id"]
        before = db.rows("SELECT COUNT(*) FROM candidate_ledger_events")[0][0]
        h.loop.tick()                                    # same slot again
        relaunch(h, db)                                  # process restart
        h.loop.tick()
        h.loop.ledger.reconcile(h.db, h.clock())
        h.loop.ledger.reconcile(h.db, h.clock())
        self.assertEqual(db.rows("SELECT COUNT(*) FROM candidate_ledger_events")[0][0], before)
        self.assertEqual(db.stages(cid).count("intent_logged"), 1)
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        relaunch(h, db)
        h.tick(15)
        self.assertEqual(db.stages(cid).count("outcome_resolved"), 1)
        acc, cons = db.report(h.db)
        self.assertTrue(acc.ok and cons.ok, (acc.violations, cons.problems))

    def test_lost_post_submit_events_are_backfilled_from_the_journal(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        sink = h.loop.ledger
        real_record = sink.w.record

        def drop_broker(cid, stage, **kw):
            if stage == "broker_result" and kw.get("payload", {}).get("source") is None:
                return False
            return real_record(cid, stage, **kw)
        with mock.patch.object(sink.w, "record", side_effect=drop_broker), \
                mock.patch.object(LoopLedger, "reconcile", return_value={}):
            h.tick()
        cid = h.orders()[0]["candidate_id"]
        self.assertNotIn("broker_result", db.stages(cid))
        res = h.loop.ledger.reconcile(h.db, h.clock())
        self.assertEqual(res.get("backfilled:broker_result"), 1)
        self.assertEqual(db.payload(cid, "broker_result")["source"], "journal_reconcile")
        acc, cons = db.report(h.db)
        self.assertTrue(acc.ok and cons.ok, (acc.violations, cons.problems))

    def test_unknown_submit_resolves_through_reconcile(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        h.broker.raise_after_place = True if hasattr(h.broker, "raise_after_place") else None
        real = h.broker.submit_bracket

        def lost_ack(req):
            real(req)
            raise TimeoutError("ack lost")
        with mock.patch.object(h.broker, "submit_bracket", side_effect=lost_ack):
            r = h.tick()
        self.assertEqual(r["entry_block"], "submit_outcome_unknown")
        cid = h.orders()[0]["candidate_id"]
        self.assertEqual(db.stages(cid)[-1], "intent_logged")       # in flight, unknown
        for _ in range(4):
            h.tick(15)                                               # recovery adopts it
        state = h.orders()[0]["state"]
        self.assertIn(state, ("SUBMITTED", "OPEN"))
        self.assertIn("broker_result", db.stages(cid))
        acc, cons = db.report(h.db)
        self.assertTrue(cons.ok, cons.problems)

    def test_intent_without_a_planned_order_is_closed_not_persisted(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        with mock.patch.object(LoopJournal, "plan_order",
                               side_effect=sqlite3.IntegrityError("busy")):
            r = h.tick()
        self.assertEqual(r["entry_block"], "duplicate_or_symbol_busy")
        a = make_candidate_id("AAA", h.clock().isoformat(), "long", "trend_v1")
        self.assertEqual(db.terminal(a), "not_persisted")
        # crash between the audit write and planning: reconcile closes it
        db2 = LedgerDB(self)
        sink = db2.sink(m14=True)
        c2 = cand("CCC", entry=50.0, sl=48.0, tp=55.0)
        import dataclasses
        c2 = dataclasses.replace(c2, signal_ts_utc=h.clock().isoformat())
        # out of order (no risk evaluation yet) is refused ...
        self.assertFalse(sink.intent(c2, client_ref="slot:x|CCC", order_ref="o",
                                     data_source="sim", now=h.clock(), cycle_key="slot:x"))
        # ... the real sequence: risk passed, intent recorded, crash before planning
        db2.decide("dec-c", "CCC", "long", 50.0, h.clock())
        sink.risk(c2, passed=True, reason=None, now=h.clock(), cycle_key="slot:x",
                  decision_id="dec-c", notional_usd=50.0)
        self.assertTrue(sink.intent(c2, client_ref="slot:x|CCC", order_ref="o", data_source="sim",
                                    now=h.clock(), cycle_key="slot:x"))
        res = sink.reconcile(h.db, h.clock())
        self.assertEqual(res.get("closed_unplanned_intent"), 1)
        self.assertEqual(db2.terminal(LoopLedger.candidate_id(c2)), "not_persisted")


class W5_ConsistencyChecks(unittest.TestCase):
    def test_contradictions_and_gaps_are_reported(self):
        db = LedgerDB(self)
        h = with_ledger(self, LoopHarness(self), db)
        h.tick()
        cid = h.orders()[0]["candidate_id"]
        acc, cons = db.report(h.db)
        self.assertTrue(cons.ok, cons.problems)
        with sqlite3.connect(db.path) as c:                 # tamper: close an active order
            c.execute("UPDATE candidate_ledger SET terminal_kind='cancelled', "
                      "terminal_at_utc='x' WHERE candidate_id=?", (cid,))
        acc, cons = db.report(h.db)
        self.assertTrue(any("active" in p for p in cons.problems), cons.problems)
        self.assertFalse(acc.ok)
        with sqlite3.connect(h.db) as j:                    # duplicate candidate id
            j.execute("UPDATE loop_orders SET candidate_id=?", (cid,))
            j.execute("INSERT INTO loop_orders (client_ref, cycle_key, symbol, direction, qty, "
                      "state, data_source, planned_at_utc, updated_at_utc, candidate_id) "
                      "VALUES ('x|Z','x','Z','long',1,'REJECTED','simulated','t','t',?)", (cid,))
        _acc, cons = db.report(h.db)
        self.assertTrue(any("share one candidate id" in p for p in cons.problems), cons.problems)
        with sqlite3.connect(h.db) as j:                    # journal order the ledger lacks
            j.execute("UPDATE loop_orders SET candidate_id='C-0000000000000000' "
                      "WHERE client_ref='x|Z'")
        _acc, cons = db.report(h.db)
        self.assertTrue(any("no ledger candidate" in p for p in cons.problems), cons.problems)

    def test_outcome_labels(self):
        self.assertEqual(outcome_label({"exit_reason": "time_stop", "realized_pnl": 5}), "TIMEOUT")
        self.assertEqual(outcome_label({"exit_reason": "take_profit", "realized_pnl": 5}), "WIN")
        self.assertEqual(outcome_label({"exit_reason": "stop_loss", "realized_pnl": -5}), "LOSS")
        self.assertEqual(outcome_label({"exit_reason": "x", "realized_pnl": None,
                                        "gross_pnl": 1.0}), "WIN")
        self.assertEqual(outcome_label({"exit_reason": "x", "realized_pnl": 0.0}), "LOSS")


class M14Store:
    """A risk_decisions table with M14's own schema (bot.flywheel)."""

    def __init__(self, tc):
        from bot.flywheel import RISK_DECISIONS_SCHEMA
        self.dir = tempfile.mkdtemp(prefix="wp7m14_")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "signals.db")
        with sqlite3.connect(self.path) as c:
            c.execute(RISK_DECISIONS_SCHEMA)

    def add(self, did, *, taken_at, symbol="QQQ", side="long", amount=1000.0, leverage=1,
            scope="ibkr_paper", action="trade_open", result="allow", request_json=None):
        req = request_json if request_json is not None else json.dumps(
            {"symbol": symbol, "amount_usd": amount, "side": side, "leverage": leverage})
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO risk_decisions (decision_id, taken_at, broker_scope, requested_action, "
                      "request_json, result, authority_before, authority_after, reason_codes, source, "
                      "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (did, taken_at, scope, action, req, result, "AUTO_ALLOWED", "AUTO_ALLOWED", "[]", "auto",
                       taken_at))


class W7_RiskProvenance(unittest.TestCase):
    """Review 5686442374: an order in the journal is never turned into an M14 PASS."""

    PLAN = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.db = LedgerDB(self)
        self.m14 = M14Store(self)
        jdir = tempfile.mkdtemp(prefix="wp7j_")
        self.addCleanup(shutil.rmtree, jdir, True)
        self.jpath = os.path.join(jdir, "journal.db")
        self.journal = LoopJournal(self.jpath)
        self.n = 0

    def order(self, did="dec-q", symbol="QQQ", qty=10.0, ref=100.0, state=None):
        """A journal order for a candidate the ledger knows only as scanned."""
        self.n += 1
        ts = (self.PLAN - timedelta(minutes=5 + self.n)).isoformat()
        w = self.db.writer()
        cid = w.open_candidate(symbol=symbol, direction="long", strategy_id="trend_v1", decision_ts_utc=ts,
                               cycle_key="slot:%d" % self.n, cycle_id=None, scanner_stage="loop_candidate",
                               at_utc=ts, payload={})
        ref_ = "slot:%d|%s" % (self.n, symbol)
        self.journal.plan_order(client_ref=ref_, cycle_key="slot:%d" % self.n, strategy_id="trend_v1",
                                symbol=symbol, direction="long", qty=qty, entry_ref_price=ref, stop_loss=ref * 0.95,
                                take_profit=ref * 1.1, score=80, data_source="simulated", risk_decision_id=did,
                                equity_at_entry=100000.0, now=self.PLAN, order_ref="o-%d" % self.n,
                                candidate_id=cid)
        if state:
            with sqlite3.connect(self.jpath) as c:
                c.execute("UPDATE loop_orders SET state=? WHERE client_ref=?", (state, ref_))
        return cid

    def risk(self, cid):
        return self.db.payload(cid, "risk_evaluated")

    def linked(self, cid):
        return self.db.rows("SELECT risk_decision_id FROM candidate_ledger WHERE candidate_id=?", cid)[0][0]

    def test_without_an_m14_store_backfill_is_inferred_never_a_pass(self):
        cid = self.order()
        res = self.db.sink().reconcile(self.jpath, self.PLAN)
        self.assertEqual(res.get("backfilled:risk_evaluated:RECONSTRUCTED_INFERRED"), 1)
        p = self.risk(cid)
        self.assertEqual((p["verdict"], p["evidence"], p["risk_decision_id"]),
                         ("unverified", "RECONSTRUCTED_INFERRED", None))
        self.assertEqual(p["journal_risk_decision_id"], "dec-q")
        self.assertFalse(L.authorising(p))
        self.assertIsNone(self.linked(cid))
        with sqlite3.connect(self.db.path) as lc, sqlite3.connect(self.jpath) as jc:
            rep = journal_consistency(lc, jc)
        self.assertEqual(rep.risk_evidence["RECONSTRUCTED_INFERRED"], 1)
        self.assertIn("not an M14 PASS", rep.non_authorising_risk[0])

    def test_matching_persisted_decision_is_verified_and_linked(self):
        self.m14.add("dec-q", taken_at=(self.PLAN - timedelta(minutes=1)).isoformat(), amount=1000.0)
        cid = self.order()
        res = L.LoopLedger(self.db.writer(), m14_db=self.m14.path).reconcile(self.jpath, self.PLAN)
        self.assertEqual(res.get("backfilled:risk_evaluated:VERIFIED"), 1)
        p = self.risk(cid)
        self.assertEqual((p["verdict"], p["evidence"], p["risk_decision_id"]), ("pass", "VERIFIED", "dec-q"))
        self.assertEqual(self.linked(cid), "dec-q")

    def test_invented_or_contradictory_approvals_are_never_accepted(self):
        before = (self.PLAN - timedelta(minutes=1)).isoformat()
        cases = {
            "decision_not_in_m14_store": ("dec-missing", None),
            "m14_decision_not_allow:block": ("d-block", dict(result="block")),
            "decision_for_other_symbol": ("d-sym", dict(symbol="SPY")),
            "decision_for_other_side": ("d-side", dict(side="short")),
            "decision_scope_mismatch:ibkr_live": ("d-scope", dict(scope="ibkr_live")),
            "decision_not_trade_open:trade_close": ("d-act", dict(action="trade_close")),
            "decision_amount_below_order_notional": ("d-amt", dict(amount=999.0)),
            "decision_leverage_2": ("d-lev", dict(leverage=2)),
            "decision_payload_unreadable": ("d-json", dict(request_json="{not json")),
            "decision_taken_more_than_600s_after_the_tick": ("d-late", dict(taken_at=(
                self.PLAN + timedelta(minutes=11)).isoformat())),
            "decision_older_than_600s_before_the_tick": ("d-old", dict(taken_at=(
                self.PLAN - timedelta(minutes=11)).isoformat())),
            "no_decision_id_in_journal": (None, None),
        }
        cids = {}
        for i, (why, (did, kw)) in enumerate(cases.items()):
            sym = "Q%02d" % i                                   # one active order per symbol
            if kw is not None:
                self.m14.add(did, **dict(dict(taken_at=before, symbol=sym), **kw))
            cids[why] = self.order(did=did, symbol=sym)
        sink = L.LoopLedger(self.db.writer(), m14_db=self.m14.path)
        res = sink.reconcile(self.jpath, self.PLAN)
        self.assertEqual(res.get("backfilled:risk_evaluated:UNVERIFIED"), len(cases))
        for why, cid in cids.items():
            p = self.risk(cid)
            self.assertEqual((p["verdict"], p["evidence"], p["reason"]), ("unverified", "UNVERIFIED", why), why)
            self.assertIsNone(p["risk_decision_id"])
            self.assertIsNone(self.linked(cid))
            self.assertFalse(L.authorising(p))
        # replay is idempotent: nothing new, nothing upgraded
        n = self.db.rows("SELECT COUNT(*) FROM candidate_ledger_events")[0][0]
        self.assertFalse(any(k.startswith("backfilled") for k in sink.reconcile(self.jpath, self.PLAN)))
        self.assertEqual(self.db.rows("SELECT COUNT(*) FROM candidate_ledger_events")[0][0], n)

    def test_a_decision_taken_during_the_tick_verifies(self):
        """Production timing: the journal stamps the tick start, M14 stamps the wall clock seconds later."""
        self.m14.add("dec-t", taken_at=(self.PLAN + timedelta(seconds=40)).isoformat(), amount=1000.0)
        cid = self.order(did="dec-t")
        L.LoopLedger(self.db.writer(), m14_db=self.m14.path).reconcile(self.jpath, self.PLAN)
        self.assertEqual(self.risk(cid)["evidence"], "VERIFIED")

    def test_unreadable_store_is_inferred_not_verified(self):
        Path(self.m14.path).write_bytes(b"garbage" * 64)
        cid = self.order()
        L.LoopLedger(self.db.writer(), m14_db=self.m14.path).reconcile(self.jpath, self.PLAN)
        self.assertEqual((self.risk(cid)["evidence"], self.risk(cid)["reason"]),
                         ("RECONSTRUCTED_INFERRED", "m14_store_unreadable:DatabaseError"))

    def test_inferred_risk_can_never_authorise_an_intent(self):
        cid = self.order()
        sink = self.db.sink()
        sink.reconcile(self.jpath, self.PLAN)
        c = SimpleNamespace(candidate_id=cid, symbol="QQQ")
        self.assertFalse(sink.intent(c, client_ref="x", order_ref="o", data_source="sim", now=self.PLAN,
                                     cycle_key="slot:1"))
        self.assertEqual(sink.stats["intent_blocked:risk_evidence_RECONSTRUCTED_INFERRED"], 1)

    def test_live_pass_without_a_decision_id_blocks_the_entry(self):
        h = with_ledger(self, LoopHarness(self), self.db)
        h.risk = (True, None, None)
        r = h.tick()
        self.assertEqual(r["entry_block"], "ledger_audit_unavailable")
        self.assertEqual(h.orders(), [])
        self.assertEqual(entry_orders(h.broker), 0)
        a = make_candidate_id("AAA", h.clock().isoformat(), "long", "trend_v1")
        p = self.db.payload(a, "risk_evaluated")            # recorded, but never as a pass
        self.assertEqual((p["verdict"], p["evidence"], p["reason"]),
                         ("unverified", "UNVERIFIED", "pass_not_authenticated:no_m14_decision_id"))
        self.assertNotIn("intent_logged", self.db.stages(a))

    def test_reconstruction_never_changes_a_terminal_and_reports_contradictions(self):
        cid = self.order(state="REJECTED")
        w = self.db.writer()
        w.record(cid, "risk_evaluated", at_utc=self.PLAN.isoformat(),
                 payload=dict(L.risk_payload(False, "risk:blocked", {}), risk_decision_id="dec-x",
                              evidence="M14_LIVE"))
        w.close(cid, "risk_rejected", at_utc=self.PLAN.isoformat(), reason_codes=["risk:blocked"])
        res = self.db.sink().reconcile(self.jpath, self.PLAN)
        self.assertEqual(self.db.terminal(cid), "risk_rejected")
        self.assertEqual(res.get("contradiction:risk_rejected_but_ordered"), 1)
        self.assertEqual(res.get("contradiction:terminal"), 1)
        # a live pass whose decision id differs from the journal's is a contradiction, not repaired
        cid2 = self.order(did="dec-journal", symbol="RRR")
        w.record(cid2, "risk_evaluated", at_utc=self.PLAN.isoformat(),
                 payload=dict(L.risk_payload(True, None, {}), risk_decision_id="dec-live", evidence="M14_LIVE"))
        res = self.db.sink().reconcile(self.jpath, self.PLAN)
        self.assertEqual(res.get("contradiction:risk_decision_id"), 1)
        self.assertEqual(self.risk(cid2)["risk_decision_id"], "dec-live")

    def test_legacy_fabricated_backfill_is_classified_inferred(self):
        legacy = dict(L.risk_payload(True, None, {}), risk_decision_id="dec-1", source="journal_reconcile")
        self.assertEqual(L.risk_evidence(legacy), "RECONSTRUCTED_INFERRED")
        self.assertFalse(L.authorising(legacy))
        # a pre-label pass carrying only a decision id was never authenticated
        self.assertEqual(L.risk_evidence(dict(L.risk_payload(True, None, {}), risk_decision_id="d")), "UNVERIFIED")
        self.assertEqual(L.risk_evidence({"verdict": "pass", "evidence": "TRUST_ME"}), "UNVERIFIED")
        self.assertFalse(L.authorising({"verdict": "pass", "evidence": "TRUST_ME"}))

    def test_verifier_rejects_a_record_for_another_decision(self):
        order = {"risk_decision_id": "dec-a", "symbol": "QQQ", "direction": "long", "qty": 1.0,
                 "entry_ref_price": 10.0, "planned_at_utc": self.PLAN.isoformat()}
        rec = {"decision_id": "dec-b", "taken_at": self.PLAN.isoformat(), "broker_scope": "ibkr_paper",
               "requested_action": "trade_open", "result": "allow",
               "request_json": json.dumps({"symbol": "QQQ", "side": "long", "amount_usd": 10.0, "leverage": 1})}
        self.assertEqual(L.verify_m14_decision(order, rec), ("UNVERIFIED", "record_for_other_decision"))
        self.assertEqual(L.verify_m14_decision(order, dict(rec, decision_id="dec-a"))[0], "VERIFIED")

    def test_m14_reader_is_read_only(self):
        src = (Path(__file__).resolve().parent / "bot/candidate_ledger/loop_sink.py").read_text()
        self.assertIn("mode=ro", src)
        self.assertIn("PRAGMA query_only = ON", src)
        for bad in ("INSERT INTO risk_decisions", "UPDATE risk_decisions", "DELETE FROM risk_decisions"):
            self.assertNotIn(bad, src)
        self.assertIsNone(L.read_m14_decision(self.m14.path, "nope"))
        with self.assertRaises(sqlite3.Error):
            L.read_m14_decision(os.path.join(self.m14.dir, "absent.db"), "x")
        self.assertFalse(os.path.exists(os.path.join(self.m14.dir, "absent.db")))


from test_m21_1extra_d2_3_learning_loop import MS_NOW, _AlpacaCreds, market_state  # noqa: E402


class W8_RealM14GateProvenance(_AlpacaCreds):
    """The real WS2 caller through the REAL M14 gate (temp signals DB) into the ledger: the decision the
    gate persisted authenticates the live pass; a forged or misattributed id never opens an intent."""

    def setUp(self):
        super().setUp()
        import test_m14_f_preflight as F
        from bot.broker_allocation import save_policy
        fx = F._DB()
        self.addCleanup(fx.cleanup)
        with fx.conn() as c:
            save_policy(c, F._valid_policy())
            F._seed_fresh_all_scopes(c)
        self.m14 = fx.path

    def run_loop(self):
        from bot.paper_loop_d2.risk_gate import make_risk_authority_gate
        from bot.risk_authority import engine as ENG
        h = LoopHarness(self)
        h.clock.t = MS_NOW                                              # market open, as the gate sees it

        class _At(datetime):                                            # the engine's wall clock = the tick
            @classmethod
            def now(cls, tz=None):
                return h.clock()
        p = mock.patch.object(ENG, "datetime", _At)
        p.start()
        self.addCleanup(p.stop)
        g = make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(self.m14),
                                     market_state=market_state(), clock=lambda: MS_NOW)
        db = LedgerDB(self)
        sink = L.LoopLedger(db.writer(), m14_db=self.m14)
        h.loop = h.make_loop(risk_gate=g, ledger=sink)
        return h, db, sink

    def test_genuine_decision_is_m14_live_and_linked(self):
        h, db, sink = self.run_loop()
        r = h.tick()
        self.assertEqual(r["entry"]["state"], "SUBMITTED")
        o = h.orders()[0]
        p = db.payload(o["candidate_id"], "risk_evaluated")
        self.assertEqual((p["verdict"], p["evidence"], p["evidence_reason"]),
                         ("pass", "M14_LIVE", "matched_persisted_m14_decision"))
        self.assertEqual(p["risk_decision_id"], o["risk_decision_id"])
        self.assertEqual(db.rows("SELECT risk_decision_id FROM candidate_ledger WHERE candidate_id=?",
                                 o["candidate_id"])[0][0], o["risk_decision_id"])
        self.assertIn("intent_logged", db.stages(o["candidate_id"]))

    def test_forged_or_misattributed_decision_cannot_open_an_intent(self):
        h, db, sink = self.run_loop()
        h.tick()
        real = h.orders()[0]["risk_decision_id"]
        import dataclasses
        for i, (sym, did, why) in enumerate((("BBB", "forged-decision-id", "decision_not_in_m14_store"),
                                             ("BBB", real, "decision_for_other_symbol"))):
            c = dataclasses.replace(cand(sym), signal_ts_utc=(h.clock() + timedelta(minutes=i)).isoformat())
            sink.risk(c, passed=True, reason=None, now=h.clock(), cycle_key="slot:f%d" % i, decision_id=did,
                      notional_usd=100.0)
            cid = LoopLedger.candidate_id(c)
            p = db.payload(cid, "risk_evaluated")
            self.assertEqual((p["verdict"], p["evidence"], p["evidence_reason"]), ("unverified", "UNVERIFIED", why))
            self.assertIsNone(db.rows("SELECT risk_decision_id FROM candidate_ledger WHERE candidate_id=?", cid)[0][0])
            self.assertFalse(sink.intent(c, client_ref="x%d" % i, order_ref="o", data_source="sim", now=h.clock(),
                                         cycle_key="slot:f%d" % i))
        no_store = L.LoopLedger(db.writer())                            # no M14 store: never authenticated
        c = dataclasses.replace(cand("CCC"), signal_ts_utc=(h.clock() + timedelta(minutes=5)).isoformat())
        no_store.risk(c, passed=True, reason=None, now=h.clock(), cycle_key="slot:n", decision_id=real,
                      notional_usd=100.0)
        p = db.payload(LoopLedger.candidate_id(c), "risk_evaluated")
        self.assertEqual((p["evidence"], p["evidence_reason"]), ("UNVERIFIED", "m14_store_not_configured"))
        self.assertFalse(no_store.intent(c, client_ref="n", order_ref="o", data_source="sim", now=h.clock(),
                                         cycle_key="slot:n"))
        # a rejection is authenticated only by a BLOCKING decision: an allow decision cannot back it
        c = dataclasses.replace(cand("AAA"), signal_ts_utc=(h.clock() + timedelta(minutes=6)).isoformat())
        sink.risk(c, passed=False, reason="risk:x", now=h.clock(), cycle_key="slot:r", decision_id=real,
                  notional_usd=h.orders()[0]["qty"] * 100.0)
        p = db.payload(LoopLedger.candidate_id(c), "risk_evaluated")
        self.assertEqual((p["verdict"], p["evidence"], p["evidence_reason"]),
                         ("reject", "UNVERIFIED", "m14_decision_not_block:allow"))


class W6_ScanStageRecords(unittest.TestCase):
    def test_scored_blocked_and_scanner_rejects(self):
        import test_m21_1_wp5_evidence as W
        from tools.signal_scoring.evidence import (
            ledger_candidate_id, loop_candidates, rank_candidates, record_scanner_rejects,
            record_scored,
        )
        db = LedgerDB(self)
        w = db.writer()
        syms = ("AAA", "STALE")
        ce = W.evidence(symbols=syms, bar_asof={"AAA": dict(W.ASOF),
                                                "STALE": dict(W.ASOF, **{"15m": "2026-11-03T15:00:00+00:00"})})
        ranked = rank_candidates([W.sig("AAA"), W.sig("STALE")], ce)
        counts = record_scored(w, ranked, cycle_key="scan:t", now=W.U(W.EV))
        self.assertEqual(counts, {"selectable": 1, "scoring_blocked": 1})
        good, bad = (ledger_candidate_id(W.sig(s)) for s in syms)
        self.assertEqual(db.stages(good), ["scanned", "scored"])
        self.assertIsNone(db.terminal(good))
        self.assertEqual(db.terminal(bad), "scoring_blocked")
        scored = db.payload(good, "scored")
        self.assertIs(scored["execution_eligible"], False)
        self.assertTrue(scored["selectable"])
        cands, _ = loop_candidates(ranked)
        self.assertEqual([c.candidate_id for c in cands], [good])
        n = record_scanner_rejects(w, [
            {"symbol": "PART", "direction": "long", "stage": "partial_confluence",
             "valid_count": 2, "rejection_reason": "only_2_of_3_tfs", "strategy_version": 1},
            {"symbol": "WTCH", "direction": "short", "stage": "watch_only", "valid_count": 3,
             "rejection_reason": "below_ibkr_min_watch_only", "strategy_version": 1}],
            cycle_key="scan:t", now=W.U(W.EV))
        self.assertEqual(n, 2)
        self.assertEqual(db.rows("SELECT COUNT(*) FROM candidate_ledger "
                                 "WHERE terminal_kind='not_selected'")[0][0], 2)
        with sqlite3.connect(db.path) as c:
            self.assertTrue(account(c).ok)
        # disabled writer: nothing at all
        off = LedgerDB(self).writer(enabled=False)
        self.assertEqual(record_scored(off, ranked, cycle_key="x", now=W.U(W.EV)), {})
        self.assertEqual(record_scanner_rejects(off, [{}], cycle_key="x", now=W.U(W.EV)), 0)

    def test_ws2_tool_wires_the_ledger_only_behind_the_flag(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent /
               "tools/paper_loop/m21_1extra_d2_3_scheduler.py").read_text()
        self.assertIn("enabled=ledger_enabled_from_env()", src)
        self.assertIn("ledger=LoopLedger(writer, m14_db=signals_db)", src)
        self.assertIn('sqlite3.connect(":memory:")', src)


if __name__ == "__main__":
    unittest.main()

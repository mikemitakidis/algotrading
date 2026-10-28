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
from datetime import timedelta
from unittest import mock

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

from bot.candidate_ledger import (  # noqa: E402
    CandidateLedgerWriter, account, make_candidate_id, migrate,
)
from bot.candidate_ledger.loop_sink import (  # noqa: E402
    LoopLedger, journal_consistency, outcome_label,
)
from bot.paper_loop_d2.journal import LoopJournal  # noqa: E402
from test_m21_1extra_d2_3_learning_loop import LoopHarness, cand  # noqa: E402

NOW = "2026-10-28T09:00:00+00:00"


class LedgerDB:
    def __init__(self, tc, migrated=True):
        self.dir = tempfile.mkdtemp(prefix="wp7_")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "ledger.db")
        self.down = False
        if migrated:
            with sqlite3.connect(self.path) as c:
                migrate(c, now_utc=NOW)

    def connect(self):
        if self.down:
            raise sqlite3.OperationalError("ledger database unavailable")
        return sqlite3.connect(self.path, timeout=5)

    def writer(self, enabled=True):
        return CandidateLedgerWriter(self.connect, enabled=enabled, writer="paper_loop")

    def sink(self, enabled=True):
        return LoopLedger(self.writer(enabled))

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
    h.loop = h.make_loop(ledger=db.sink(enabled))
    return h


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
        h.loop = h.make_loop(ledger=db.sink())           # process restart
        h.loop.tick()
        h.loop.ledger.reconcile(h.db, h.clock())
        h.loop.ledger.reconcile(h.db, h.clock())
        self.assertEqual(db.rows("SELECT COUNT(*) FROM candidate_ledger_events")[0][0], before)
        self.assertEqual(db.stages(cid).count("intent_logged"), 1)
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        h.loop = h.make_loop(ledger=db.sink())
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
        sink = db2.sink()
        c2 = cand("CCC", entry=50.0, sl=48.0, tp=55.0)
        import dataclasses
        c2 = dataclasses.replace(c2, signal_ts_utc=h.clock().isoformat())
        # out of order (no risk evaluation yet) is refused ...
        self.assertFalse(sink.intent(c2, client_ref="slot:x|CCC", order_ref="o",
                                     data_source="sim", now=h.clock(), cycle_key="slot:x"))
        # ... the real sequence: risk passed, intent recorded, crash before planning
        sink.risk(c2, passed=True, reason=None, now=h.clock(), cycle_key="slot:x",
                  decision_id="dec-c")
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
        self.assertIn("ledger=LoopLedger(writer)", src)
        self.assertIn('sqlite3.connect(":memory:")', src)


if __name__ == "__main__":
    unittest.main()

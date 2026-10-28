#!/usr/bin/env python3
"""M21.1 A02 — candidate-decision ledger (offline).

Proves: lane-A id parity and determinism; additive, idempotent,
all-or-nothing migration that touches no existing table; a writer that is OFF
by default, never creates schema, never raises, is idempotent and
order-checked; complete stage accounting and join integrity; an offline
legacy replay that opens its source read-only, writes only under /tmp and
reproduces every candidate path; the A01 score payload never records an
execution grant; and no runtime module calls the ledger.
All databases here are temporary fixtures.
"""
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

from bot.candidate_ledger import (  # noqa: E402
    ENV_FLAG, STAGES, TERMINAL_KINDS, CandidateLedgerWriter, LedgerSchemaError,
    account, ledger_enabled_from_env, make_candidate_id, make_event_id, migrate,
    risk_payload, scanner_strategy_id, schema_present, score_payload,
)
from bot.candidate_ledger import schema as S  # noqa: E402

REPO = Path(__file__).resolve().parent
NOW = "2026-10-28T09:00:00+00:00"
T0 = datetime(2026, 10, 27, 19, 45, tzinfo=timezone.utc)


def tmpdir(tc):
    d = tempfile.mkdtemp(prefix="a02_")
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def ledger_db(tc, *, legacy=False):
    path = os.path.join(tmpdir(tc), "ledger.db")
    if legacy:
        legacy_fixture(path)
    with sqlite3.connect(path) as c:
        migrate(c, now_utc=NOW)
    return path


def writer(path, enabled=True):
    return CandidateLedgerWriter(lambda: sqlite3.connect(path), enabled=enabled)


def legacy_fixture(path):
    """A legacy signals DB built with the production writers."""
    from bot.database import init_db, insert_signal
    from bot.flywheel import init_flywheel_tables, log_candidate, log_intent
    conn = init_db(path)
    init_flywheel_tables(conn)

    def sig(symbol, minute, direction="long"):
        s = dict(timestamp=(T0 + timedelta(minutes=minute)).isoformat(), symbol=symbol,
                 direction=direction, price=100.0, rsi=55.0, atr=2.0, macd_hist=0.1,
                 valid_count=3, tf_15m=1, tf_1h=1, tf_4h=1, route="ibkr",
                 entry_price=100.0, stop_loss=95.0, target_price=115.0, strategy_version=1)
        return s, insert_signal(conn, s)

    ids = {}
    log_candidate(conn, 1, "PART", "long", "partial_confluence", 2, ["1D", "4H"], 4, 3,
                  rejection_reason="below_min_valid")
    log_candidate(conn, 1, "WATCH", "short", "watch_only", 3, ["1D"], 4, 3)
    for name, minute, status in (("RREJ", 0, "risk_rejected"), ("UNRDY", 1, "broker_unready"),
                                 ("WIN", 2, "accepted"), ("OPEN", 3, "paper_logged"),
                                 ("REJ", 4, "rejected"), ("CANC", 5, "cancelled"),
                                 ("NOINT", 6, None), ("PEND", 7, "pending")):
        s, sid = sig(name, minute)
        ids[name] = sid
        log_candidate(conn, 1, name, "long", "final_signal", 3, ["1D", "4H", "1H"], 4, 3,
                      signal_id=sid)
        if status is not None:
            log_intent(conn, sid, name, "long", "ibkr", 100.0, 95.0, 115.0, 10.0, 50.0, 3, 1,
                       "ibkr_paper", status,
                       rejection_reason="max_open_positions_10" if status == "risk_rejected" else None,
                       risk_checks={"verdict": "reject" if status == "risk_rejected" else "pass",
                                    "position_size_usd": 1000.0, "_recon": {"secret": "x"}})
    conn.execute("INSERT INTO signal_outcomes (signal_id, symbol, direction, entry_price, "
                 "exit_price, return_pct, outcome, resolved_at, resolution_method) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (ids["WIN"], "WIN", "long", 100.0, 115.0, 15.0, "WIN",
                  (T0 + timedelta(hours=3)).isoformat(), "target"))
    conn.execute("INSERT INTO signal_outcomes (signal_id, symbol, direction, outcome) "
                 "VALUES (?,?,?,?)", (ids["OPEN"], "OPEN", "long", "OPEN"))
    conn.commit()
    conn.close()
    return ids


def table_fingerprint(path, exclude=S.LEDGER_TABLES):
    with sqlite3.connect(path) as c:
        out = {}
        for name, sql in c.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
            if name in exclude or name.startswith("sqlite_"):
                continue
            rows = c.execute('SELECT * FROM "%s"' % name).fetchall()
            out[name] = (sql, hashlib.sha256(repr(rows).encode()).hexdigest())
        return out


def open_one(w, symbol="AAA", direction="long", stage="final_signal", ts=T0):
    return w.open_candidate(symbol=symbol, direction=direction,
                            strategy_id=scanner_strategy_id(1), decision_ts_utc=ts,
                            cycle_key=ts.isoformat(), scanner_stage=stage,
                            at_utc=ts.isoformat(), bar_asof={"15m": "2026-10-27T19:30:00+00:00"})


# ═════════════════════════════════ ids ═════════════════════════════════════

class L1_Ids(unittest.TestCase):
    def test_lane_a_parity(self):
        from research.evidence.candidates import make_candidate_id as lane_a
        import pandas as pd
        for sym, ts, d, k in (("AAA", T0, "long", "scanner:v1"),
                              ("BRK.B", "2026-03-09T13:45:00+00:00", "short", "scanner:v2"),
                              ("XYZ", "2026-11-02T14:30:00.250000+00:00", "long", "fixture")):
            self.assertEqual(make_candidate_id(sym, ts, d, k),
                             lane_a(sym, pd.Timestamp(ts), d, k))

    def test_deterministic_and_validated(self):
        a = make_candidate_id("AAA", T0, "long", "scanner:v1")
        self.assertEqual(a, make_candidate_id("AAA", T0.isoformat(), "long", "scanner:v1"))
        self.assertEqual(a, make_candidate_id(
            "AAA", T0.astimezone(timezone(timedelta(hours=-4))), "long", "scanner:v1"))
        self.assertNotEqual(a, make_candidate_id("AAA", T0, "short", "scanner:v1"))
        self.assertRegex(a, r"^C-[0-9a-f]{16}$")
        for bad in (("", T0, "long", "k"), ("A", "2026-10-27T19:45:00", "long", "k"),
                    ("A", T0, "up", "k"), ("A", T0, "long", ""), ("A", None, "long", "k")):
            with self.assertRaises(ValueError):
                make_candidate_id(*bad)
        self.assertEqual(make_event_id(a, "scanned"), make_event_id(a, "scanned", 0))
        self.assertNotEqual(make_event_id(a, "scanned"), make_event_id(a, "scanned", 1))
        with self.assertRaises(ValueError):
            make_event_id(a, "scanned", -1)
        self.assertEqual(scanner_strategy_id(3), "scanner:v3")
        for bad in (-1, True, "1"):
            with self.assertRaises(ValueError):
                scanner_strategy_id(bad)


# ═════════════════════════════ migration ═══════════════════════════════════

class L2_Migration(unittest.TestCase):
    def test_additive_idempotent_and_leaves_existing_tables_alone(self):
        path = os.path.join(tmpdir(self), "legacy.db")
        legacy_fixture(path)
        before = table_fingerprint(path)
        with sqlite3.connect(path) as c:
            self.assertFalse(schema_present(c))
            migrate(c, now_utc=NOW)
            migrate(c, now_utc="2030-01-01T00:00:00+00:00")       # idempotent
            self.assertTrue(schema_present(c))
            self.assertEqual(c.execute("SELECT value FROM candidate_ledger_meta WHERE "
                                       "key='created_at_utc'").fetchone()[0], NOW)
        self.assertEqual(table_fingerprint(path), before)

    def test_version_mismatch_refused_and_rolled_back(self):
        path = ledger_db(self)
        with sqlite3.connect(path) as c:
            c.execute("UPDATE candidate_ledger_meta SET value='99' WHERE key='schema_version'")
            c.commit()
            with self.assertRaises(LedgerSchemaError):
                migrate(c, now_utc=NOW)
            self.assertFalse(schema_present(c))

    def test_failure_mid_migration_leaves_nothing(self):
        path = os.path.join(tmpdir(self), "x.db")
        bad = S.DDL[:2] + ("CREATE TABLE broken (",)
        with mock.patch.object(S, "DDL", bad), sqlite3.connect(path) as c:
            with self.assertRaises(sqlite3.Error):
                migrate(c, now_utc=NOW)
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        self.assertEqual(names & set(S.LEDGER_TABLES), set())

    def test_schema_source_is_additive_only(self):
        src = (REPO / "bot" / "candidate_ledger" / "schema.py").read_text().upper()
        for word in ("ALTER TABLE", "DROP ", "DELETE FROM", "UPDATE ", "INSERT INTO SIGNALS",
                     "EXECUTION_INTENTS (", "CANDIDATE_SNAPSHOTS ("):
            self.assertNotIn(word, src.replace("UPDATE CANDIDATE_LEDGER", ""), word)


# ═════════════════════════════ writer ══════════════════════════════════════

class L3_Writer(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(ledger_enabled_from_env({}))
        for v in ("true", "yes", "on", "0", " 1", "1 "):
            self.assertFalse(ledger_enabled_from_env({ENV_FLAG: v}), v)
        self.assertTrue(ledger_enabled_from_env({ENV_FLAG: "1"}))
        path = ledger_db(self)
        w = CandidateLedgerWriter(lambda: sqlite3.connect(path))      # default
        self.assertFalse(w.enabled)
        cid = open_one(w)
        self.assertRegex(cid, r"^C-")                                  # id still returned
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM candidate_ledger").fetchone()[0], 0)

    def test_never_creates_schema(self):
        path = os.path.join(tmpdir(self), "empty.db")
        w = writer(path)
        self.assertFalse(w.enabled)
        self.assertEqual(w.stats["disabled:schema_absent"], 1)
        open_one(w)
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_full_pipeline_idempotent(self):
        path = ledger_db(self)
        w = writer(path)
        for _ in range(2):                                             # replay = no-op
            cid = open_one(w)
            self.assertTrue(w.record(cid, "risk_evaluated", at_utc=NOW,
                                     payload=risk_payload(True, None, {"verdict": "pass"})))
            self.assertTrue(w.record(cid, "intent_logged", at_utc=NOW, payload={"intent_id": 7}))
            self.assertTrue(w.record(cid, "broker_result", at_utc=NOW,
                                     payload={"status": "accepted"}))
            self.assertTrue(w.record(cid, "outcome_resolved", at_utc=NOW,
                                     payload={"outcome": "WIN"}))
            self.assertTrue(w.close(cid, "outcome_resolved", at_utc=NOW))
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM candidate_ledger").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM candidate_ledger_events")
                             .fetchone()[0], 6)
        self.assertEqual(w.stats["idempotent:closed"], 1)
        self.assertEqual(sum(v for k, v in w.stats.items() if k.startswith("conflict")), 0)

    def test_conflicts_never_overwrite(self):
        path = ledger_db(self)
        w = writer(path)
        cid = open_one(w)
        self.assertTrue(w.record(cid, "risk_evaluated", at_utc=NOW, payload={"verdict": "pass"}))
        self.assertFalse(w.record(cid, "risk_evaluated", at_utc=NOW, payload={"verdict": "reject"}))
        self.assertTrue(w.record(cid, "risk_evaluated", at_utc=NOW, payload={"verdict": "reject"},
                                 attempt=1))                          # explicit re-evaluation
        self.assertTrue(w.link(cid, signal_id=5))
        self.assertFalse(w.link(cid, signal_id=6))
        self.assertTrue(w.close(cid, "risk_rejected", at_utc=NOW))
        self.assertFalse(w.close(cid, "not_selected", at_utc=NOW))
        self.assertFalse(w.record(cid, "intent_logged", at_utc=NOW))  # after close
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute("SELECT signal_id, terminal_kind FROM candidate_ledger")
                             .fetchone(), (5, "risk_rejected"))
            p = c.execute("SELECT payload_json FROM candidate_ledger_events WHERE "
                          "stage='risk_evaluated' AND attempt=0").fetchone()[0]
        self.assertEqual(json.loads(p), {"verdict": "pass"})
        for k in ("conflict:risk_evaluated", "conflict:link:signal_id", "conflict:terminal",
                  "rejected:after_close"):
            self.assertEqual(w.stats[k], 1, k)

    def test_order_and_unknown_candidates_rejected(self):
        w = writer(ledger_db(self))
        cid = open_one(w)
        self.assertFalse(w.record(cid, "broker_result", at_utc=NOW))
        self.assertFalse(w.record(cid, "outcome_resolved", at_utc=NOW))
        self.assertFalse(w.record("C-0000000000000000", "risk_evaluated", at_utc=NOW))
        self.assertFalse(w.record(cid, "bogus", at_utc=NOW))
        self.assertFalse(w.record(cid, "closed", at_utc=NOW))
        self.assertFalse(w.close(cid, "made_up", at_utc=NOW))
        self.assertFalse(w.link(cid, symbol="X"))
        self.assertIsNone(w.open_candidate(symbol="", direction="long", strategy_id="k",
                                           decision_ts_utc=T0, cycle_key="c",
                                           scanner_stage="s", at_utc=NOW))

    def test_never_raises(self):
        path = ledger_db(self)
        w = writer(path)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise sqlite3.OperationalError("disk I/O error")
        w._factory = flaky
        cid = open_one(w)
        self.assertRegex(cid, r"^C-")
        self.assertFalse(w.record(cid, "risk_evaluated", at_utc=NOW))
        self.assertFalse(w.close(cid, "not_selected", at_utc=NOW))
        self.assertGreaterEqual(w.stats["error:OperationalError"], 3)
        bad = writer(path)
        self.assertTrue(w.enabled and bad.enabled)
        cid = open_one(bad)
        self.assertFalse(bad.record(cid, "risk_evaluated", at_utc=NOW,
                                    payload={"x": object()}) is None)   # serialisable via str

    def test_own_connection_does_not_touch_callers_transaction(self):
        path = ledger_db(self)
        caller = sqlite3.connect(path)
        caller.execute("CREATE TABLE IF NOT EXISTS caller_work (x)")
        caller.commit()
        caller.execute("INSERT INTO caller_work VALUES (1)")           # uncommitted
        w = CandidateLedgerWriter(lambda: sqlite3.connect(path, timeout=0.1), enabled=True)
        open_one(w)                                # may fail on the lock: must not raise
        caller.rollback()
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 0)
        caller.close()


# ═════════════════════════════ accounting ══════════════════════════════════

class L4_Accounting(unittest.TestCase):
    def test_clean_paths_and_counts(self):
        w = writer(path := ledger_db(self))
        a = open_one(w, "NS", stage="partial_confluence")
        w.close(a, "not_selected", at_utc=NOW)
        b = open_one(w, "RR")
        w.record(b, "risk_evaluated", at_utc=NOW, payload={"verdict": "reject"})
        w.close(b, "risk_rejected", at_utc=NOW)
        c_ = open_one(w, "OPEN")
        w.record(c_, "risk_evaluated", at_utc=NOW, payload={"verdict": "pass"})
        w.record(c_, "intent_logged", at_utc=NOW)
        w.record(c_, "broker_result", at_utc=NOW, payload={"status": "accepted"})
        with sqlite3.connect(path) as conn:
            rep = account(conn, complete_before="2099-01-01")
        self.assertTrue(rep.ok, rep.violations)
        self.assertEqual(dict(rep.terminal), {"not_selected": 1, "risk_rejected": 1})
        self.assertEqual(dict(rep.in_flight), {"awaiting_outcome": 1})

    def test_violations_detected(self):
        path = ledger_db(self)
        w = writer(path)
        gap = open_one(w, "GAP")
        w.record(gap, "risk_evaluated", at_utc=NOW, payload={"verdict": "pass"})
        bad = open_one(w, "BAD", stage="watch_only")
        w.record(bad, "risk_evaluated", at_utc=NOW, payload={"verdict": "pass"})
        w.close(bad, "not_selected", at_utc=NOW)
        rr = open_one(w, "RR2")
        w.record(rr, "risk_evaluated", at_utc=NOW, payload={"verdict": "pass"})
        w.close(rr, "risk_rejected", at_utc=NOW)
        with sqlite3.connect(path) as c:      # direct tampering a writer cannot produce
            c.execute("INSERT INTO candidate_ledger_events (event_id, candidate_id, stage, "
                      "at_utc, writer) VALUES ('E-x', ?, 'outcome_resolved', ?, 't')", (gap, NOW))
            c.execute("INSERT INTO candidate_ledger_events (event_id, candidate_id, stage, "
                      "at_utc, writer) VALUES ('E-y', 'C-ghost', 'scanned', ?, 't')", (NOW,))
            c.execute("UPDATE candidate_ledger SET signal_id=999 WHERE candidate_id=?", (gap,))
            c.commit()
            rep = account(c, complete_before="2099-01-01")
        text = "\n".join(rep.violations)
        for frag in ("outcome_resolved without broker_result",
                     "not_selected but risk was evaluated",
                     "risk_rejected without a reject verdict",
                     "in flight without an accepted order",
                     "events for an unknown candidate"):
            self.assertIn(frag, text)
        self.assertFalse(rep.ok)


class L4b_JoinIntegrity(unittest.TestCase):
    def test_mismatched_joins_detected(self):
        path = os.path.join(tmpdir(self), "legacy.db")
        ids = legacy_fixture(path)
        with sqlite3.connect(path) as c:
            migrate(c, now_utc=NOW)
            intents = dict(c.execute("SELECT signal_id, id FROM execution_intents"))
            outs = dict(c.execute("SELECT signal_id, id FROM signal_outcomes"))
        w = writer(path)
        good = open_one(w, "WIN")
        self.assertTrue(w.link(good, signal_id=ids["WIN"], intent_id=intents[ids["WIN"]],
                               outcome_id=outs[ids["WIN"]]))
        bad = open_one(w, "RREJ")
        self.assertTrue(w.link(bad, signal_id=ids["RREJ"], intent_id=intents[ids["UNRDY"]],
                               outcome_id=outs[ids["OPEN"]], risk_decision_id="nope"))
        ghost = open_one(w, "GHOST")
        self.assertTrue(w.link(ghost, signal_id=987654))
        with sqlite3.connect(path) as c:
            rep = account(c)
        text = "\n".join(rep.violations)
        self.assertNotIn(good, text)
        self.assertIn("%s: intent_id" % bad, text)
        self.assertIn("%s: outcome_id" % bad, text)
        self.assertIn("%s: risk_decision_id not in risk_decisions" % bad, text)
        self.assertIn("%s: signal_id 987654 not in signals" % ghost, text)


# ═════════════════════════════ legacy replay ═══════════════════════════════

class L5_LegacyReplay(unittest.TestCase):
    def setUp(self):
        from tools.candidate_ledger import replay_legacy as R
        self.R = R
        self.src = os.path.join(tmpdir(self), "signals.db")
        self.ids = legacy_fixture(self.src)
        self.out = os.path.join(tempfile.mkdtemp(prefix="a02_out_", dir="/tmp"), "ledger.db")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.out), True)

    def test_every_path_reconstructed_and_accounted(self):
        before = hashlib.sha256(Path(self.src).read_bytes()).hexdigest()
        res = self.R.replay(self.src, self.out, now_utc=NOW)
        self.assertEqual(hashlib.sha256(Path(self.src).read_bytes()).hexdigest(), before)
        self.assertTrue(res["accounting"]["ok"], res["accounting"]["violations"])
        self.assertEqual(res["summary"]["signals"], 8)
        self.assertEqual(res["summary"]["snapshots_not_selected"], 2)
        self.assertEqual(res["accounting"]["terminal"], {
            "not_selected": 2, "risk_rejected": 1, "broker_unready": 1,
            "outcome_resolved": 1, "broker_not_accepted": 1, "cancelled": 1,
            "untracked_after_signal": 1})
        self.assertEqual(res["accounting"]["in_flight"],
                         {"awaiting_outcome": 1, "incomplete": 1})     # OPEN, PEND
        with sqlite3.connect(self.out) as c:
            row = c.execute("SELECT signal_id, intent_id, outcome_id, terminal_kind FROM "
                            "candidate_ledger WHERE symbol='WIN'").fetchone()
            self.assertEqual(row[0], self.ids["WIN"])
            self.assertIsNotNone(row[1])
            self.assertIsNotNone(row[2])
            payload = c.execute("SELECT payload_json FROM candidate_ledger_events e JOIN "
                                "candidate_ledger l USING (candidate_id) WHERE l.symbol='RREJ' "
                                "AND e.stage='risk_evaluated'").fetchone()[0]
        self.assertNotIn("_recon", payload)                       # bounded, no broker dump
        self.assertNotIn("secret", payload)

    def test_idempotent_rerun(self):
        a = self.R.replay(self.src, self.out, now_utc=NOW)
        with sqlite3.connect(self.out) as c:
            n1 = c.execute("SELECT COUNT(*) FROM candidate_ledger_events").fetchone()[0]
        b = self.R.replay(self.src, self.out, now_utc=NOW)
        with sqlite3.connect(self.out) as c:
            n2 = c.execute("SELECT COUNT(*) FROM candidate_ledger_events").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertEqual(a["accounting"], b["accounting"])
        self.assertFalse(any(k.startswith("conflict") for k in b["writer_stats"]))

    def test_paths_refused(self):
        for out in ("relative/ledger.db", os.path.join(str(REPO), "ledger.db"), self.src,
                    "/var/tmp/a02_ledger.db", "/home/a02_ledger.db",
                    "/tmp/../var/tmp/a02_ledger.db", "/tmp/a02_no_such_dir/ledger.db"):
            with self.assertRaises(ValueError):
                self.R.replay(self.src, out, now_utc=NOW)
        with self.assertRaises(ValueError):
            self.R.replay(os.path.join(tmpdir(self), "missing.db"), self.out, now_utc=NOW)
        self.assertEqual(self.R.main(["--source", self.src, "--output", "x.db"]), 2)

    def test_source_opened_read_only(self):
        conn = self.R._ro(Path(self.src))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM signals")
        finally:
            conn.close()


# ═════════════════════════════ A01 payload + safety ════════════════════════

class L6_ScorePayloadAndSafety(unittest.TestCase):
    def test_score_payload_from_real_truthful_score(self):
        from test_m21_1_a01_scoring_truth import full_evidence, sig
        from tools.signal_scoring.scanner_bridge import score_signal_with_truth
        for kw in ({}, full_evidence()):
            p = score_payload(score_signal_with_truth(sig(), **kw))
            self.assertIs(p["execution_eligible"], False)
            for k in ("decision_bucket", "research_rank_score", "unknown_inputs",
                      "risk_authority", "observations", "hard_gate_failures"):
                self.assertIn(k, p)
        w = writer(path := ledger_db(self))
        cid = open_one(w)
        self.assertTrue(w.record(cid, "scored", at_utc=NOW, payload=p))
        with sqlite3.connect(path) as c:
            self.assertEqual(json.loads(c.execute(
                "SELECT payload_json FROM candidate_ledger_events WHERE stage='scored'"
            ).fetchone()[0])["risk_authority"], "OK")

    def test_no_runtime_wiring_and_safe_imports(self):
        for p in [REPO / "main.py"] + sorted((REPO / "bot").rglob("*.py")):
            if "candidate_ledger" in p.parts:
                continue
            self.assertNotIn("candidate_ledger", p.read_text(errors="replace"), p)
        allowed_prefixes = ("bot.candidate_ledger", "bot.signal_scoring.provenance")
        stdlib = {"__future__", "collections", "dataclasses", "datetime", "hashlib", "json",
                  "math", "os", "sqlite3", "typing"}
        for p in sorted((REPO / "bot" / "candidate_ledger").glob("*.py")):
            for node in ast.walk(ast.parse(p.read_text())):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                        [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for m in mods:
                    self.assertTrue(m in stdlib or m.startswith(allowed_prefixes),
                                    "%s imports %s" % (p.name, m))

    def test_terminal_and_stage_vocabulary_matches_schema(self):
        path = ledger_db(self)
        with sqlite3.connect(path) as c:
            for kind in TERMINAL_KINDS:
                c.execute("INSERT INTO candidate_ledger (candidate_id, cycle_key, symbol, "
                          "direction, strategy_id, decision_ts_utc, scanner_stage, "
                          "first_seen_utc, terminal_kind, terminal_at_utc, created_by) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          ("C-" + kind, "k", "S", "long", "k", NOW, "s", NOW, kind, NOW, "t"))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO candidate_ledger (candidate_id, cycle_key, symbol, "
                          "direction, strategy_id, decision_ts_utc, scanner_stage, "
                          "first_seen_utc, terminal_kind, terminal_at_utc, created_by) "
                          "VALUES ('C-z','k','S','long','k',?,'s',?,'bogus',?,'t')",
                          (NOW, NOW, NOW))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO candidate_ledger_events (event_id, candidate_id, stage, "
                          "at_utc, writer) VALUES ('E-z','C-z','bogus',?, 't')", (NOW,))
        self.assertEqual(STAGES[0], "scanned")


if __name__ == "__main__":
    unittest.main(verbosity=2)

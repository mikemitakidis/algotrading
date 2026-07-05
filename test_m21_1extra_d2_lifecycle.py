#!/usr/bin/env python3
"""M21.1extra-D2 — supervised paper lifecycle tests (mocked broker/B2b/C).

No live broker, no gateway, no live DB/network. Proves: gates block -> no B2b
call; both affirmative -> B2b called ONCE -> persisted ONCE; happy -> PERSISTED;
precheck/no-entry -> PRECHECK_FAILED; not-confirmed -> FAILED_CLOSED; persist
failure -> FAILED_CLOSED; no double-flatten; no OrderIntent/direct submit by D2;
market_clock fields persisted via the (updated) C.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from bot.paper_loop_d2 import run_supervised_cycle, LoopState
from tools.paper_loop.m21_1extra_c_outcome_persistence import (
    persist_lifecycle, read_lifecycles,
)

LIFECYCLE_SRC = "bot/paper_loop_d2/lifecycle.py"


def _d0(allowed, reason="authorized"):
    return lambda: (allowed, reason)


def _d1(may, reason="market_open"):
    return lambda: (may, reason)


_PROV = lambda: [("AAPL", 8.0)]


class _Broker:
    def __init__(self):
        self.flatten_calls = 0

    def flatten_paper_position(self, symbol, confirm=True):
        self.flatten_calls += 1
        return {"flatten_confirmed": True}


class _Counter:
    def __init__(self, b2b_result):
        self.b2b_calls = 0
        self.persist_calls = 0
        self._b2b_result = b2b_result

    def b2b(self, symbol, *, confirm, entry_price, stop_loss, target_price,
            kill_switch_active, broker):
        self.b2b_calls += 1
        if confirm is not True:
            raise AssertionError("B2b must be called with confirm=True")
        return dict(self._b2b_result, symbol=symbol)

    def persist(self, enriched, *, db_path=None):
        self.persist_calls += 1
        return {"inserted": True, "lifecycle_id": "L1",
                "market_clock_checked": enriched.get("market_clock_checked")}


def _run(counter, d0=True, d1=True, d0_reason="authorized",
         d1_reason="market_open", broker=None):
    return run_supervised_cycle(
        d0_gate=_d0(d0, d0_reason), d1_gate=_d1(d1, d1_reason),
        candidate_provider=_PROV, threshold=5.0,
        entry_price=100.0, stop_loss=99.0, target_price=102.0,
        b2b_run_lifecycle=counter.b2b, c_persist_lifecycle=counter.persist,
        broker=broker or _Broker())


class TestGatesBlockNoB2b(unittest.TestCase):
    def test_d0_blocked_no_b2b_call(self):
        c = _Counter({})
        r = _run(c, d0=False, d0_reason="d0_blocked")
        self.assertEqual(r.state, LoopState.D0_BLOCKED.value)
        self.assertEqual(c.b2b_calls, 0)
        self.assertEqual(c.persist_calls, 0)

    def test_d1_blocked_no_b2b_call(self):
        c = _Counter({})
        r = _run(c, d1=False, d1_reason="market_closed")
        self.assertEqual(r.state, LoopState.D1_BLOCKED.value)
        self.assertEqual(c.b2b_calls, 0)

    def test_no_candidate_no_b2b(self):
        c = _Counter({})
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True),
            candidate_provider=lambda: [], threshold=5.0,
            entry_price=100.0, stop_loss=99.0, target_price=102.0,
            b2b_run_lifecycle=c.b2b, c_persist_lifecycle=c.persist,
            broker=_Broker())
        self.assertEqual(r.state, LoopState.NO_CANDIDATE.value)
        self.assertEqual(c.b2b_calls, 0)


class TestHappyPath(unittest.TestCase):
    def test_both_affirmative_b2b_once_persist_once_persisted(self):
        c = _Counter({"entry_order_originated": True,
                      "lifecycle_confirmed": True, "account": "DUP623346",
                      "port": 4002, "entry_order_id": "1"})
        b = _Broker()
        r = _run(c, broker=b)
        self.assertEqual(r.state, LoopState.PERSISTED.value)
        self.assertEqual(c.b2b_calls, 1)          # B2b exactly once
        self.assertEqual(c.persist_calls, 1)      # persisted exactly once
        self.assertEqual(b.flatten_calls, 0)      # NO double-flatten by D2

    def test_d0_d1_reasons_carried(self):
        c = _Counter({"entry_order_originated": True,
                      "lifecycle_confirmed": True})
        r = _run(c, d1_reason="market_open")
        self.assertEqual(r.d1_reason, "market_open")
        self.assertTrue(r.d0_allowed)
        self.assertTrue(r.d1_may_submit)


class TestFailurePaths(unittest.TestCase):
    def test_no_entry_precheck_failed_persisted(self):
        c = _Counter({"entry_order_originated": False,
                      "lifecycle_confirmed": False})
        r = _run(c)
        self.assertEqual(r.state, LoopState.PRECHECK_FAILED.value)
        self.assertEqual(c.b2b_calls, 1)
        self.assertEqual(c.persist_calls, 1)      # truthful failure persisted

    def test_entry_not_confirmed_failed_closed(self):
        c = _Counter({"entry_order_originated": True,
                      "lifecycle_confirmed": False})
        r = _run(c)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "lifecycle_not_confirmed")
        self.assertEqual(c.persist_calls, 1)      # persisted truthfully

    def test_b2b_raises_failed_closed_emergency_flatten(self):
        def b2b_raise(symbol, **kw):
            raise RuntimeError("boom")
        b = _Broker()
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True), candidate_provider=_PROV,
            threshold=5.0, entry_price=100.0, stop_loss=99.0,
            target_price=102.0, b2b_run_lifecycle=b2b_raise,
            c_persist_lifecycle=_Counter({}).persist, broker=b)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertIn("b2b_raised:RuntimeError", r.errors)
        # emergency flatten attempted via the injected broker.
        self.assertEqual(b.flatten_calls, 1)

    def test_b2b_raises_flatten_unconfirmed_loud(self):
        def b2b_raise(symbol, **kw):
            raise RuntimeError("boom")

        class NoFlat:
            def flatten_paper_position(self, symbol, confirm=True):
                return {"flatten_confirmed": False}
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True), candidate_provider=_PROV,
            threshold=5.0, entry_price=100.0, stop_loss=99.0,
            target_price=102.0, b2b_run_lifecycle=b2b_raise,
            c_persist_lifecycle=_Counter({}).persist, broker=NoFlat())
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertIn("EMERGENCY_FLATTEN_UNCONFIRMED", r.errors)

    def test_persist_failure_failed_closed(self):
        def persist_raise(enriched, *, db_path=None):
            raise RuntimeError("db down")
        c = _Counter({"entry_order_originated": True,
                      "lifecycle_confirmed": True})
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True), candidate_provider=_PROV,
            threshold=5.0, entry_price=100.0, stop_loss=99.0,
            target_price=102.0, b2b_run_lifecycle=c.b2b,
            c_persist_lifecycle=persist_raise, broker=_Broker())
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "persist_failed")


class TestMarketClockPersistedViaC(unittest.TestCase):
    """End-to-end with the REAL C persister into a temp DB."""

    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)

    def test_market_clock_fields_persisted(self):
        c = _Counter({"entry_order_originated": True,
                      "lifecycle_confirmed": True, "account": "DUP623346",
                      "port": 4002, "entry_order_id": "1"})
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True, "market_open"),
            candidate_provider=_PROV, threshold=5.0, entry_price=100.0,
            stop_loss=99.0, target_price=102.0, b2b_run_lifecycle=c.b2b,
            c_persist_lifecycle=persist_lifecycle, broker=_Broker(),
            db_path=self.db)
        self.assertEqual(r.state, LoopState.PERSISTED.value)
        rows = read_lifecycles(db_path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_clock_checked"], 1)
        self.assertEqual(rows[0]["market_clock_reason"], "market_open")

    def test_old_records_without_market_clock_keep_defaults(self):
        # a plain B2b dict (no market_clock fields) still persists defaults.
        persist_lifecycle({"symbol": "AAPL", "lifecycle_confirmed": True,
                           "account": "DUP623346", "port": 4002,
                           "entry_order_id": "9"}, db_path=self.db)
        row = read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["market_clock_checked"], 0)
        self.assertEqual(row["market_clock_reason"],
                         "not_checked_in_C_deferred_to_D")


class TestProofRunnerValidation(unittest.TestCase):
    """The proof runner validates prices + confirm BEFORE touching anything."""

    def _run(self, **kw):
        import io
        from contextlib import redirect_stdout
        import tools.paper_loop.m21_1extra_d2_lifecycle_proof as PR

        class _Args:
            symbol = kw.get("symbol", "AAPL")
            entry_price = kw.get("entry_price", 100.0)
            stop_loss = kw.get("stop_loss", 99.0)
            target_price = kw.get("target_price", 102.0)
            db_path = None
            confirm = kw.get("confirm", "")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = PR.run(_Args())
        return rc, buf.getvalue()

    def test_valid_prices_helper(self):
        from tools.paper_loop.m21_1extra_d2_lifecycle_proof import (
            validate_prices,
        )
        validate_prices(100.0, 99.0, 102.0)  # no raise
        for bad in [(100, 101, 102), (100, 99, 99), (0, -1, 5),
                    (100, 50, 100), (-5, -6, -4)]:
            with self.assertRaises(ValueError):
                validate_prices(*bad)

    def test_missing_confirm_refuses_rc1(self):
        rc, out = self._run(confirm="")
        self.assertEqual(rc, 1)
        self.assertIn("missing_or_wrong_confirm_flag", out)
        self.assertIn("M21_1EXTRA_D2_LIFECYCLE_VERIFY_RC=1", out)

    def test_wrong_confirm_refuses_rc1(self):
        rc, out = self._run(confirm="sure")
        self.assertEqual(rc, 1)

    def test_bad_prices_refuse_before_confirm(self):
        rc, out = self._run(stop_loss=101.0,
                            confirm="yes-run-one-real-paper-lifecycle")
        self.assertEqual(rc, 1)
        self.assertIn("invalid_prices", out)

    def test_contract_markers(self):
        import tools.paper_loop.m21_1extra_d2_lifecycle_proof as PR
        self.assertEqual(PR.OK_MARKER, "D2_LIFECYCLE_VERIFY_OK")
        self.assertEqual(PR.RC_MARKER, "M21_1EXTRA_D2_LIFECYCLE_VERIFY_RC")

    def test_lock_held_refuses_rc1_no_broker(self):
        # Blocker 2: a lock held by a LIVE pid -> RC=1, no state (no broker).
        import io
        import os
        import tempfile
        import shutil
        from contextlib import redirect_stdout
        import tools.paper_loop.m21_1extra_d2_lifecycle_proof as PR
        from bot.paper_loop_d2 import CycleLock

        d = tempfile.mkdtemp()
        lockp = os.path.join(d, "d2.lock")
        held = CycleLock(lockp, pid=os.getpid())  # our own live pid
        held.acquire()

        class _Args:
            symbol = "AAPL"
            entry_price = 100.0
            stop_loss = 99.0
            target_price = 102.0
            policy_db_path = None
            lifecycle_db_path = None
            lock_path = lockp
            confirm = "yes-run-one-real-paper-lifecycle"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = PR.run(_Args())
            out = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("lock_held", out)
            self.assertNotIn('"state"', out)   # broker never reached
        finally:
            held.release()
            shutil.rmtree(d, ignore_errors=True)

    def test_split_db_paths_are_separate_args(self):
        # Blocker 3: policy and lifecycle DBs are distinct flags.
        import tools.paper_loop.m21_1extra_d2_lifecycle_proof as PR
        ns = PR.build_arg_parser().parse_args([
            "--symbol", "AAPL", "--entry-price", "100", "--stop-loss", "99",
            "--target-price", "102", "--policy-db-path", "/tmp/pol.db",
            "--lifecycle-db-path", "/tmp/life.db", "--confirm", "x"])
        self.assertEqual(ns.policy_db_path, "/tmp/pol.db")
        self.assertEqual(ns.lifecycle_db_path, "/tmp/life.db")
        self.assertNotEqual(ns.policy_db_path, ns.lifecycle_db_path)


class TestCorePriceValidation(unittest.TestCase):
    """Blocker 4: run_supervised_cycle validates prices BEFORE any B2b call."""

    def _run(self, entry, stop, target):
        c = _Counter({"entry_order_originated": True,
                      "lifecycle_confirmed": True})
        b = _Broker()
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True), candidate_provider=_PROV,
            threshold=5.0, entry_price=entry, stop_loss=stop,
            target_price=target, b2b_run_lifecycle=c.b2b,
            c_persist_lifecycle=c.persist, broker=b)
        return r, c, b

    def test_invalid_prices_no_b2b_no_flatten_precheck(self):
        for entry, stop, target in [(100, 101, 102), (100, 99, 99),
                                    (0, -1, 5), (-5, -6, -4), (100, 50, 100)]:
            r, c, b = self._run(entry, stop, target)
            self.assertEqual(r.state, LoopState.PRECHECK_FAILED.value,
                             "prices %r" % ((entry, stop, target),))
            self.assertEqual(c.b2b_calls, 0)
            self.assertEqual(c.persist_calls, 0)
            self.assertEqual(b.flatten_calls, 0)
            self.assertNotEqual(r.state, LoopState.PERSISTED.value)

    def test_valid_prices_reach_b2b(self):
        r, c, b = self._run(100.0, 99.0, 102.0)
        self.assertEqual(c.b2b_calls, 1)
        self.assertEqual(r.state, LoopState.PERSISTED.value)


class TestBrokerNoneEmergency(unittest.TestCase):
    """Blocker 5: broker=None -> D2 cannot flatten B2b's internal broker."""

    def test_broker_none_b2b_raises_explicit_unavailable(self):
        def b2b_raise(symbol, **kw):
            raise RuntimeError("boom")
        r = run_supervised_cycle(
            d0_gate=_d0(True), d1_gate=_d1(True), candidate_provider=_PROV,
            threshold=5.0, entry_price=100.0, stop_loss=99.0,
            target_price=102.0, b2b_run_lifecycle=b2b_raise,
            c_persist_lifecycle=_Counter({}).persist, broker=None)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertIn("emergency_flatten_unavailable_b2b_internal_broker",
                      r.errors)
        self.assertIn("EMERGENCY_FLATTEN_UNCONFIRMED", r.errors)


class TestHygiene(unittest.TestCase):
    def _read(self):
        with open(LIFECYCLE_SRC) as fh:
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
        import io, tokenize
        out = []
        prev = tokenize.INDENT
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
                    tokenize.DEDENT, tokenize.ENCODING):
                prev = tok.type
                continue
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENCODING):
                out.append(tok.string)
            prev = tok.type
        return " ".join(out)

    def test_no_broker_adapter_or_orderintent(self):
        mods = self._toplevel_imports(self._read())
        for m in mods:
            for bad in ("brokers", "ib_insync", "gateway", "OrderIntent",
                        "execution_intents"):
                self.assertNotIn(bad.lower(), m.lower())
        code = self._code_only(self._read())
        self.assertNotIn(" OrderIntent ", code)
        self.assertNotIn(" submit ", code)   # D2 never calls broker.submit

    def test_does_not_import_b2flat(self):
        # correction: no separate B2flat call / no run_flatten wrapper.
        code = self._code_only(self._read())
        self.assertNotIn("run_flatten", code)
        self.assertNotIn("b2flat", code.lower())
        # confirm B2flat is not imported at module top level either.
        mods = self._toplevel_imports(self._read())
        for m in mods:
            self.assertNotIn("b2flat", m.lower())

    def test_no_safety_assert(self):
        tree = ast.parse(self._read())
        self.assertEqual(
            [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

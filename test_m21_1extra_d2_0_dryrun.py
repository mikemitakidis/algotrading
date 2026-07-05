#!/usr/bin/env python3
"""M21.1extra-D2.0 — dry-run cycle tests (no broker/D0/D1/network/DB)."""
from __future__ import annotations

import ast
import unittest
from datetime import datetime, timezone

from bot.paper_loop_d2 import (
    run_dry_cycle, LoopState, DryRunCycleRecord, MalformedCandidate,
)

DRYRUN_SRC = "bot/paper_loop_d2/dryrun.py"
STATE_SRC = "bot/paper_loop_d2/state.py"
LOCK_SRC = "bot/paper_loop_d2/lock.py"
INIT_SRC = "bot/paper_loop_d2/__init__.py"

FIXED = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)


def _clk():
    return FIXED


def _cid():
    return "cyc-TEST"


def _prov(items):
    return lambda: items


def _d0(allowed, reason="ok"):
    return lambda: (allowed, reason)


def _d1(may, reason="market_open"):
    return lambda: (may, reason)


def _run(items=(("AAPL", 8.0),), d0=True, d1=True, threshold=5.0,
         d0_reason="ok", d1_reason="market_open", clock=_clk, cid=_cid):
    return run_dry_cycle(
        d0_gate=_d0(d0, d0_reason), d1_gate=_d1(d1, d1_reason),
        candidate_provider=_prov(list(items)), threshold=threshold,
        clock=clock, cycle_id_factory=cid)


class TestSelector(unittest.TestCase):
    def test_no_candidates(self):
        r = _run(items=[])
        self.assertEqual(r.state, LoopState.NO_CANDIDATE.value)
        self.assertFalse(r.would_submit)

    def test_below_threshold(self):
        r = _run(items=[("AAPL", 2.0)], threshold=5.0)
        self.assertEqual(r.state, LoopState.NO_CANDIDATE.value)

    def test_one_candidate_above_threshold(self):
        r = _run(items=[("AAPL", 8.0)])
        self.assertEqual(r.symbol, "AAPL")
        self.assertEqual(r.candidate_score, 8.0)
        self.assertEqual(r.state, LoopState.DRY_RUN_WOULD_SUBMIT.value)

    def test_multiple_highest_score(self):
        r = _run(items=[("MSFT", 7.0), ("AAPL", 9.0)])
        self.assertEqual(r.symbol, "AAPL")

    def test_multiple_tie_lexicographic(self):
        r = _run(items=[("TSLA", 9.0), ("AAPL", 9.0), ("MSFT", 9.0)])
        self.assertEqual(r.symbol, "AAPL")  # tie -> lexicographic first

    def test_selection_deterministic_across_order(self):
        a = _run(items=[("TSLA", 9.0), ("AAPL", 9.0)])
        b = _run(items=[("AAPL", 9.0), ("TSLA", 9.0)])
        self.assertEqual(a.symbol, b.symbol)


class TestMalformed(unittest.TestCase):
    def test_malformed_variants_fail_closed(self):
        for bad in [("AAPL",), ("AAPL", "x"), (123, 5.0),
                    ("AAPL", float("nan")), ("AAPL", True), "AAPL", None,
                    ("", 5.0)]:
            r = _run(items=[bad])
            self.assertEqual(r.state, LoopState.FAILED_CLOSED.value,
                             msg="should fail closed: %r" % (bad,))
            self.assertFalse(r.would_submit)
            self.assertEqual(r.submit_blocked_reason, "malformed_candidate")

    def test_malformed_does_not_skip_to_next(self):
        # a malformed item present -> whole cycle fails closed, even though a
        # valid one exists (contract ambiguity).
        r = _run(items=[("AAPL",), ("MSFT", 9.0)])
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)

    def test_provider_exception_fail_closed(self):
        def boom():
            raise RuntimeError("scan blew")
        r = run_dry_cycle(d0_gate=_d0(True), d1_gate=_d1(True),
                          candidate_provider=boom, threshold=5.0,
                          clock=_clk, cycle_id_factory=_cid)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "candidate_provider_error")


class TestGates(unittest.TestCase):
    def test_d0_blocked(self):
        r = _run(d0=False, d0_reason="policy_unavailable")
        self.assertEqual(r.state, LoopState.D0_BLOCKED.value)
        self.assertTrue(r.d0_checked)
        self.assertFalse(r.d1_checked)   # D1 not reached
        self.assertEqual(r.submit_blocked_reason, "policy_unavailable")

    def test_d1_blocked(self):
        r = _run(d1=False, d1_reason="market_closed")
        self.assertEqual(r.state, LoopState.D1_BLOCKED.value)
        self.assertTrue(r.d0_checked)
        self.assertTrue(r.d1_checked)
        self.assertEqual(r.submit_blocked_reason, "market_closed")

    def test_both_open_would_submit(self):
        r = _run(d0=True, d1=True)
        self.assertEqual(r.state, LoopState.DRY_RUN_WOULD_SUBMIT.value)
        self.assertTrue(r.would_submit)
        self.assertTrue(r.d0_allowed)
        self.assertTrue(r.d1_may_submit)
        self.assertIsNone(r.submit_blocked_reason)

    def test_d0_gate_exception_fail_closed(self):
        def gboom():
            raise ValueError("gate broke")
        r = run_dry_cycle(d0_gate=gboom, d1_gate=_d1(True),
                          candidate_provider=_prov([("AAPL", 8.0)]),
                          threshold=5.0, clock=_clk, cycle_id_factory=_cid)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)

    def test_d1_gate_exception_fail_closed(self):
        def gboom():
            raise ValueError("gate broke")
        r = run_dry_cycle(d0_gate=_d0(True), d1_gate=gboom,
                          candidate_provider=_prov([("AAPL", 8.0)]),
                          threshold=5.0, clock=_clk, cycle_id_factory=_cid)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)


class TestClockAndCycleId(unittest.TestCase):
    def test_injected_clock_controls_timestamps(self):
        r = _run()
        self.assertEqual(r.started_at_utc, FIXED.isoformat())
        self.assertEqual(r.finished_at_utc, FIXED.isoformat())

    def test_injected_cycle_id(self):
        r = _run()
        self.assertEqual(r.cycle_id, "cyc-TEST")

    def test_default_cycle_id_nonempty_and_unique(self):
        a = run_dry_cycle(d0_gate=_d0(True), d1_gate=_d1(True),
                          candidate_provider=_prov([("AAPL", 8.0)]),
                          threshold=5.0)
        b = run_dry_cycle(d0_gate=_d0(True), d1_gate=_d1(True),
                          candidate_provider=_prov([("AAPL", 8.0)]),
                          threshold=5.0)
        self.assertTrue(a.cycle_id)
        self.assertTrue(b.cycle_id)
        self.assertNotEqual(a.cycle_id, b.cycle_id)

    def test_selection_deterministic_independent_of_cycle_id(self):
        a = run_dry_cycle(d0_gate=_d0(True), d1_gate=_d1(True),
                          candidate_provider=_prov([("AAPL", 9.0), ("TSLA", 9.0)]),
                          threshold=5.0, cycle_id_factory=lambda: "id-A")
        b = run_dry_cycle(d0_gate=_d0(True), d1_gate=_d1(True),
                          candidate_provider=_prov([("AAPL", 9.0), ("TSLA", 9.0)]),
                          threshold=5.0, cycle_id_factory=lambda: "id-B")
        self.assertEqual(a.symbol, b.symbol)
        self.assertNotEqual(a.cycle_id, b.cycle_id)


class TestInvariant(unittest.TestCase):
    def test_would_submit_only_in_would_submit_state(self):
        with self.assertRaises(ValueError):
            DryRunCycleRecord(cycle_id="x", started_at_utc="t",
                              finished_at_utc="t",
                              state=LoopState.D0_BLOCKED.value,
                              would_submit=True, d0_checked=True,
                              d1_checked=False)

    def test_would_submit_state_requires_flag(self):
        with self.assertRaises(ValueError):
            DryRunCycleRecord(cycle_id="x", started_at_utc="t",
                              finished_at_utc="t",
                              state=LoopState.DRY_RUN_WOULD_SUBMIT.value,
                              would_submit=False, d0_checked=True,
                              d1_checked=True)

    def test_property_would_submit_needs_all_three(self):
        # would_submit True ONLY when a valid candidate + D0 allowed + D1 open.
        import itertools
        for d0v, d1v, has_cand in itertools.product(
                [True, False], [True, False], [True, False]):
            items = [("AAPL", 8.0)] if has_cand else []
            r = _run(items=items, d0=d0v, d1=d1v)
            if r.would_submit:
                self.assertTrue(d0v and d1v and has_cand,
                                "would_submit true without all preconditions")

    def test_mode_always_dry_run(self):
        self.assertEqual(_run().mode, "dry_run")
        self.assertEqual(_run().market_calendar_id, "US_EQ")

    def test_dry_run_record_rejects_supervised_states(self):
        # Blocker 1: a dry-run record must reject supervised-only states.
        for st in ("ORDER_SUBMITTED", "POSITION_OBSERVED",
                   "FLATTEN_CONFIRMED", "PERSISTED"):
            with self.assertRaises(ValueError, msg="should reject %s" % st):
                DryRunCycleRecord(cycle_id="x", started_at_utc="t",
                                  finished_at_utc="t", state=st,
                                  would_submit=False, d0_checked=True,
                                  d1_checked=False)

    def test_dry_run_never_returns_order_submitted(self):
        # ORDER_SUBMITTED exists in the enum (supervised mode uses it), but the
        # dry-run path must NEVER return it — dry-run places no orders.
        import itertools
        for d0v, d1v, has_cand in itertools.product(
                [True, False], [True, False], [True, False]):
            items = [("AAPL", 8.0)] if has_cand else []
            r = _run(items=items, d0=d0v, d1=d1v)
            self.assertNotEqual(r.state, "ORDER_SUBMITTED")
            self.assertNotEqual(r.state, "PERSISTED")


class TestDependencyFailClosed(unittest.TestCase):
    """Bad injected clock / cycle_id must yield FAILED_CLOSED, never raise."""

    def _run(self, **kw):
        base = dict(d0_gate=_d0(True), d1_gate=_d1(True),
                    candidate_provider=_prov([("AAPL", 8.0)]), threshold=5.0)
        base.update(kw)
        return run_dry_cycle(**base)

    def test_clock_raises_fail_closed(self):
        def boom():
            raise RuntimeError("clock down")
        r = self._run(clock=boom)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "clock_dependency_error")
        self.assertFalse(r.would_submit)
        self.assertTrue(r.started_at_utc)   # safe fallback timestamp present

    def test_naive_clock_fail_closed(self):
        r = self._run(clock=lambda: datetime(2026, 7, 6, 15, 0))  # naive
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "clock_dependency_error")

    def test_clock_returns_non_datetime_fail_closed(self):
        r = self._run(clock=lambda: "not-a-datetime")
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)

    def test_cycle_id_raises_fail_closed(self):
        def boom():
            raise ValueError("id down")
        r = self._run(cycle_id_factory=boom)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "cycle_id_dependency_error")

    def test_empty_cycle_id_fail_closed(self):
        r = self._run(cycle_id_factory=lambda: "")
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(r.submit_blocked_reason, "cycle_id_dependency_error")

    def test_non_string_cycle_id_fail_closed(self):
        r = self._run(cycle_id_factory=lambda: 12345)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)

    def test_failure_record_satisfies_invariant(self):
        # a dependency-failure record is a valid DryRunCycleRecord.
        r = self._run(clock=lambda: (_ for _ in ()).throw(RuntimeError()))
        self.assertFalse(r.would_submit)
        self.assertTrue(r.terminal)


class TestHygiene(unittest.TestCase):
    def _imports(self, path):
        with open(path) as fh:
            tree = ast.parse(fh.read())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    mods.add(n.module)
        return mods

    def test_no_broker_d0_d1_network_db_imports(self):
        forbidden = ("brokers", "broker_allocation", "signal_only_broker",
                     "runtime_policy", "market_clock", "alpaca", "requests",
                     "urllib", "sqlite3", "execution", "ib_insync",
                     "outcome_persistence")
        for p in (DRYRUN_SRC, STATE_SRC, LOCK_SRC, INIT_SRC):
            for m in self._imports(p):
                for bad in forbidden:
                    self.assertNotIn(bad, m.lower(),
                                     msg="%s imports %s" % (p, m))

    def test_no_safety_assert(self):
        for p in (DRYRUN_SRC, STATE_SRC, LOCK_SRC):
            with open(p) as fh:
                tree = ast.parse(fh.read())
            self.assertEqual(
                [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], [],
                "%s uses assert" % p)


if __name__ == "__main__":
    unittest.main(verbosity=2)

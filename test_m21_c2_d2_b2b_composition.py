#!/usr/bin/env python3
"""C2 (M21 safety-test repair) — real-shaped D2 -> B2b composition test.

Closes audit R1-5 (no automated test exercises real-shaped D2 -> B2b
composition) and documents R1-1 (broker=None cannot emergency-flatten after a
B2b post-fill crash) truthfully.

The B2b fake mirrors the REAL run_lifecycle return contract (account, port,
entry_order_originated, entry_order_id, lifecycle_confirmed, warnings,
pre_existing_position, ...) and models B2b's INTERNAL flatten (it flattens the
broker itself), so D2 must NOT double-flatten on the happy path.

No real orders, no IBKR connection, no D0 enable, no gateway. Everything is
injected fakes + a temp DB.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from bot.paper_loop_d2 import run_supervised_cycle, LoopState
from tools.paper_loop.m21_1extra_c_outcome_persistence import (
    persist_lifecycle, read_lifecycles,
)

_ACCOUNT = "DUP623346"
_PORT = 4002


def _d0(allowed=True, reason="authorized"):
    return lambda: (allowed, reason)


def _d1(may=True, reason="market_open"):
    return lambda: (may, reason)


_PROV = lambda: [("AAPL", 8.0)]
_PRICES = dict(entry_price=100.0, stop_loss=99.0, target_price=102.0)


class _RealShapedBroker:
    """A paper broker fake exposing flatten_paper_position like the real
    IBKRBroker. Tracks how many times D2 flattens it directly."""

    def __init__(self):
        self.direct_flatten_calls = 0

    def flatten_paper_position(self, symbol, confirm=True):
        self.direct_flatten_calls += 1
        return {"flatten_confirmed": True}


class _RealShapedB2b:
    """Mirrors the real run_lifecycle(symbol, confirm=, entry_price=, ...,
    broker=) contract. Internally flattens via the injected broker (like the
    real B2b), and returns the same result-dict keys."""

    def __init__(self, *, entry_originated=True, lifecycle_confirmed=True,
                 pre_existing=False, raise_after_fill=False,
                 internal_flatten=True):
        self.calls = 0
        self._entry = entry_originated
        self._confirmed = lifecycle_confirmed
        self._pre = pre_existing
        self._raise = raise_after_fill
        self._internal_flatten = internal_flatten

    def __call__(self, symbol, *, confirm, entry_price, stop_loss,
                 target_price, kill_switch_active, broker):
        self.calls += 1
        if confirm is not True:
            raise AssertionError("B2b must be called with confirm=True")
        # B2b builds its own broker when broker is None; here, if a broker is
        # injected and internal_flatten is on, model B2b's own flatten.
        if self._raise:
            # crash AFTER a fill/position may exist — the dangerous case.
            raise RuntimeError("b2b crashed after fill")
        if self._internal_flatten and broker is not None:
            broker.flatten_paper_position(symbol, confirm=True)
        return {
            "symbol": symbol,
            "account": _ACCOUNT,
            "port": _PORT,
            "entry_order_originated": self._entry,
            "entry_order_id": "perm-123" if self._entry else None,
            "lifecycle_confirmed": self._confirmed,
            "pre_existing_position": self._pre,
            "kill_switch_active": bool(kill_switch_active),
            "account_verified": True,
            "warnings": [],
        }


class _DBFixture(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)


class TestHappyPathComposition(_DBFixture):
    def test_b2b_called_once_no_double_flatten_persisted(self):
        b2b = _RealShapedB2b()
        broker = _RealShapedBroker()
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(), candidate_provider=_PROV,
            threshold=5.0, **_PRICES, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=broker,
            db_path=self.db)
        # D2 calls B2b exactly once.
        self.assertEqual(b2b.calls, 1)
        # B2b flattened internally (its own broker call); D2 did NOT flatten
        # again on the happy path -> exactly ONE flatten total, from B2b.
        self.assertEqual(broker.direct_flatten_calls, 1)
        # terminal is PERSISTED.
        self.assertEqual(r.state, LoopState.PERSISTED.value)
        # persisted shape is correct: one row, market-clock provenance present.
        rows = read_lifecycles(db_path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_clock_checked"], 1)
        self.assertEqual(rows[0]["market_clock_reason"], "market_open")

    def test_b2b_internal_flatten_result_respected(self):
        # if B2b reports lifecycle_confirmed True, D2 trusts it and persists
        # success WITHOUT issuing its own flatten.
        b2b = _RealShapedB2b(lifecycle_confirmed=True)
        broker = _RealShapedBroker()
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(), candidate_provider=_PROV,
            threshold=5.0, **_PRICES, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=broker,
            db_path=self.db)
        self.assertEqual(r.state, LoopState.PERSISTED.value)
        # only B2b's internal flatten happened (1), no extra D2 flatten.
        self.assertEqual(broker.direct_flatten_calls, 1)


class TestNoDoubleSubmit(_DBFixture):
    def test_single_candidate_single_b2b_call(self):
        b2b = _RealShapedB2b()
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(),
            candidate_provider=lambda: [("AAPL", 8.0), ("MSFT", 7.0)],
            threshold=5.0, **_PRICES, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=_RealShapedBroker(),
            db_path=self.db)
        # even with multiple candidates, D2 runs ONE lifecycle (top pick only).
        self.assertEqual(b2b.calls, 1)
        self.assertEqual(r.symbol, "AAPL")   # highest score selected


class TestPrecheckNoEntry(_DBFixture):
    def test_no_entry_originated_precheck_failed_no_flatten(self):
        b2b = _RealShapedB2b(entry_originated=False, lifecycle_confirmed=False,
                             internal_flatten=False)
        broker = _RealShapedBroker()
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(), candidate_provider=_PROV,
            threshold=5.0, **_PRICES, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=broker,
            db_path=self.db)
        self.assertEqual(r.state, LoopState.PRECHECK_FAILED.value)
        self.assertEqual(broker.direct_flatten_calls, 0)  # nothing to flatten
        # still persisted truthfully.
        self.assertEqual(len(read_lifecycles(db_path=self.db)), 1)


class TestCrashInjectionTruthful(_DBFixture):
    """R1-1: when B2b crashes AFTER a possible fill and broker=None (the real
    runner's mode), D2 CANNOT emergency-flatten B2b's internal broker. The test
    proves D2 fails closed LOUDLY and never claims flatness — it does not paper
    over the limitation."""

    def test_broker_none_crash_cannot_flatten_fails_loud(self):
        b2b = _RealShapedB2b(raise_after_fill=True)
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(), candidate_provider=_PROV,
            threshold=5.0, **_PRICES, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=None,
            db_path=self.db)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        # the limitation is surfaced explicitly, not hidden.
        self.assertIn("emergency_flatten_unavailable_b2b_internal_broker",
                      r.errors)
        self.assertIn("EMERGENCY_FLATTEN_UNCONFIRMED", r.errors)
        # D2 must NOT claim flat — no PERSISTED-as-success, no false flat.
        self.assertNotEqual(r.state, LoopState.PERSISTED.value)

    def test_injected_broker_crash_attempts_flatten_but_stays_failed(self):
        # with an injected broker, D2 CAN attempt an emergency flatten; it still
        # fails closed (the lifecycle did not confirm) but the flatten is tried.
        b2b = _RealShapedB2b(raise_after_fill=True)
        broker = _RealShapedBroker()
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(), candidate_provider=_PROV,
            threshold=5.0, **_PRICES, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=broker,
            db_path=self.db)
        self.assertEqual(r.state, LoopState.FAILED_CLOSED.value)
        self.assertEqual(broker.direct_flatten_calls, 1)  # emergency flatten
        self.assertIn("b2b_raised:RuntimeError", r.errors)


class TestPricePrecheckBeforeB2b(_DBFixture):
    def test_invalid_prices_never_call_b2b(self):
        b2b = _RealShapedB2b()
        broker = _RealShapedBroker()
        r = run_supervised_cycle(
            d0_gate=_d0(), d1_gate=_d1(), candidate_provider=_PROV,
            threshold=5.0, entry_price=100.0, stop_loss=101.0,  # stop>entry
            target_price=102.0, b2b_run_lifecycle=b2b,
            c_persist_lifecycle=persist_lifecycle, broker=broker,
            db_path=self.db)
        self.assertEqual(r.state, LoopState.PRECHECK_FAILED.value)
        self.assertEqual(b2b.calls, 0)               # B2b never called
        self.assertEqual(broker.direct_flatten_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

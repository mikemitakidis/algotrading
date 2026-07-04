#!/usr/bin/env python3
"""M21.1extra-D2 — real D0/D1 gate adapter tests (no orders, no gateway).

D0 uses a controlled temp DB (never the live signals.db); D1 uses injected
mock/stubbed sources (no live Alpaca network, no real credentials). Proves the
full blocked-case matrix and that both-affirmative reaches DRY_RUN_WOULD_SUBMIT
only — never an order submission.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from bot.paper_loop_d2 import (
    make_d0_gate, make_d1_gate, run_dry_cycle, LoopState,
)
from bot.broker_allocation import _ensure_table, load_policy
import tools.paper_loop.m21_1extra_d0_auth_cli as CLI
from test_m21_1extra_d0_auth_cli import _old_pre_d0_policy, _write_raw_policy
from bot.market_clock import (
    MockMarketStatusSource, SourceResult,
    STATUS_OK, STATUS_UNAVAILABLE, STATUS_MALFORMED, STATUS_NO_CREDENTIALS,
)

GATES_SRC = "bot/paper_loop_d2/gates.py"

NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)


def _src(res):
    return MockMarketStatusSource(res, stamp_fetched_at=False)


def _open_res(now=NOW):
    return SourceResult(status=STATUS_OK, is_open=True,
                        session_open_utc=now - timedelta(hours=1),
                        session_close_utc=now + timedelta(hours=1),
                        as_of_utc=now, fetched_at_utc=now,
                        calendar_present=True, source_name="mock")


class _D0Fixture(unittest.TestCase):
    """Temp-DB D0 fixtures. NEVER touches the live signals.db."""

    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        _ensure_table(self.conn)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db):
            os.remove(self.db)

    def _enable(self, hours=1.0):
        CLI.enable_ibkr_paper(self.conn, duration_hours=hours,
                              operator="op", confirm=True)
        self.conn.commit()

    def _seed_raw(self, mutate=None):
        pol = CLI._ensure_d0_fields(_old_pre_d0_policy())
        pol["global"]["auto_trading_enabled"] = False
        if mutate:
            mutate(pol)
        _write_raw_policy(self.conn, pol)
        self.conn.commit()

    def _d0(self):
        return make_d0_gate(db_path=self.db, ttl_sec=0.0)


class TestD0Adapter(_D0Fixture):
    def test_authorized_allows(self):
        self._enable()
        allowed, reason = self._d0()()
        self.assertTrue(allowed)

    def test_disabled_blocks(self):
        self._seed_raw()
        allowed, reason = self._d0()()
        self.assertFalse(allowed)

    def test_expired_blocks(self):
        self._enable()
        pol = load_policy(self.conn)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        pol["global"]["auto_trading_enabled_until_utc"] = past
        pol["ibkr_paper"]["auto_trading_enabled_until_utc"] = past
        _write_raw_policy(self.conn, pol)
        self.conn.commit()
        allowed, reason = self._d0()()
        self.assertFalse(allowed)
        self.assertIn("expired", reason)

    def test_malformed_expiry_blocks(self):
        self._enable()
        pol = load_policy(self.conn)
        pol["ibkr_paper"]["auto_trading_enabled_until_utc"] = "not-a-date"
        pol["global"]["auto_trading_enabled_until_utc"] = "not-a-date"
        _write_raw_policy(self.conn, pol)
        self.conn.commit()
        allowed, reason = self._d0()()
        self.assertFalse(allowed)

    def test_kill_switch_blocks(self):
        self._enable()
        pol = load_policy(self.conn)
        pol["global"]["kill_switch"] = True
        _write_raw_policy(self.conn, pol)
        self.conn.commit()
        allowed, reason = self._d0()()
        self.assertFalse(allowed)
        self.assertIn("kill_switch", reason)

    def test_unreadable_db_blocks_fail_closed(self):
        gate = make_d0_gate(db_path="/nonexistent/dir/nope.db", ttl_sec=0.0)
        allowed, reason = gate()
        self.assertFalse(allowed)

    def test_adapter_never_mutates_policy(self):
        self._enable()
        before = load_policy(self.conn)
        self._d0()()
        after = load_policy(self.conn)
        self.assertEqual(before["global"]["auto_trading_enabled"],
                         after["global"]["auto_trading_enabled"])


class TestD1Adapter(unittest.TestCase):
    def _d1(self, res, now=NOW, **kw):
        return make_d1_gate(sources=[_src(res)], now_utc=now, **kw)

    def test_open_allows(self):
        may, reason = self._d1(_open_res())()
        self.assertTrue(may)
        self.assertEqual(reason, "market_open")

    def test_closed_blocks(self):
        may, reason = self._d1(SourceResult(status=STATUS_OK, is_open=False,
                                            fetched_at_utc=NOW,
                                            source_name="mock"))()
        self.assertFalse(may)
        self.assertEqual(reason, "market_closed")

    def test_unavailable_blocks(self):
        may, reason = self._d1(SourceResult(status=STATUS_UNAVAILABLE,
                                            fetched_at_utc=NOW,
                                            source_name="mock"))()
        self.assertFalse(may)
        self.assertEqual(reason, "market_status_source_unavailable")

    def test_stale_blocks(self):
        old = NOW - timedelta(seconds=99)
        may, reason = self._d1(_open_res(now=old))()
        self.assertFalse(may)
        self.assertEqual(reason, "market_status_source_stale")

    def test_no_credentials_blocks(self):
        may, reason = self._d1(SourceResult(status=STATUS_NO_CREDENTIALS,
                                            fetched_at_utc=NOW,
                                            source_name="mock"))()
        self.assertFalse(may)
        self.assertEqual(reason, "market_status_no_credentials")

    def test_malformed_blocks(self):
        may, reason = self._d1(SourceResult(status=STATUS_MALFORMED,
                                            fetched_at_utc=NOW,
                                            source_name="mock"))()
        self.assertFalse(may)
        self.assertEqual(reason, "market_status_source_malformed")

    def test_adapter_pins_us_eq_by_design(self):
        # The D0/D1 adapter always asks D1 for US_EQ, so "unsupported calendar"
        # cannot arise through this adapter — that is intentional for this
        # milestone (US equities only). D1's own suite covers the unsupported
        # path directly. Here we confirm the pinned-US_EQ open path allows.
        gate = make_d1_gate(sources=[_src(_open_res())], now_utc=NOW)
        may, reason = gate()
        self.assertTrue(may)

    def test_default_source_no_creds_fail_closed(self):
        # no sources injected -> default AlpacaClockSource with no creds present
        # -> fail closed (no live network, no creds needed).
        saved = (os.environ.pop("ALPACA_KEY", None),
                 os.environ.pop("ALPACA_SECRET", None))
        try:
            may, reason = make_d1_gate(now_utc=NOW)()
            self.assertFalse(may)
        finally:
            if saved[0] is not None:
                os.environ["ALPACA_KEY"] = saved[0]
            if saved[1] is not None:
                os.environ["ALPACA_SECRET"] = saved[1]


class TestStateMachineWithRealGates(_D0Fixture):
    def _cycle(self, d0_gate, d1_gate, items=(("AAPL", 8.0),), threshold=5.0):
        return run_dry_cycle(d0_gate=d0_gate, d1_gate=d1_gate,
                             candidate_provider=lambda: list(items),
                             threshold=threshold)

    def test_both_affirmative_would_submit_no_order(self):
        self._enable()
        r = self._cycle(self._d0(),
                        make_d1_gate(sources=[_src(_open_res())], now_utc=NOW))
        self.assertEqual(r.state, LoopState.DRY_RUN_WOULD_SUBMIT.value)
        self.assertTrue(r.would_submit)
        # the dry-run terminal is WOULD_SUBMIT — NOT an order. run_dry_cycle
        # never returns ORDER_SUBMITTED/PERSISTED even though those states now
        # exist for the supervised path.
        self.assertNotEqual(r.state, "ORDER_SUBMITTED")
        self.assertNotEqual(r.state, "PERSISTED")

    def test_d0_blocked_short_circuits_d1(self):
        self._seed_raw()
        r = self._cycle(self._d0(),
                        make_d1_gate(sources=[_src(_open_res())], now_utc=NOW))
        self.assertEqual(r.state, LoopState.D0_BLOCKED.value)
        self.assertFalse(r.d1_checked)

    def test_d0_ok_d1_closed_blocks(self):
        self._enable()
        r = self._cycle(self._d0(),
                        make_d1_gate(sources=[_src(SourceResult(
                            status=STATUS_OK, is_open=False, fetched_at_utc=NOW,
                            source_name="mock"))], now_utc=NOW))
        self.assertEqual(r.state, LoopState.D1_BLOCKED.value)
        self.assertTrue(r.d1_checked)


class TestHygiene(unittest.TestCase):
    def _toplevel_imports(self, path):
        with open(path) as fh:
            tree = ast.parse(fh.read())
        mods = set()
        for n in tree.body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name)
            elif isinstance(n, ast.ImportFrom):
                if n.module:
                    mods.add(n.module)
        return mods

    def test_no_broker_ibkr_imports(self):
        mods = self._toplevel_imports(GATES_SRC)
        for m in mods:
            for bad in ("brokers", "ib_insync", "gateway", "b2b", "b2flat",
                        "execution", "outcome_persistence"):
                self.assertNotIn(bad, m.lower(), "gates imports %s" % m)

    def test_no_lifecycle_or_order_calls(self):
        with open(GATES_SRC) as fh:
            src = fh.read()
        # code-only scan (strip docstrings/comments) for order/lifecycle calls.
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
        code = " ".join(out)
        # tokens are space-joined; a standalone `submit` call would appear as
        # " submit " whereas the D1 field `may_submit` is a single token.
        for bad in ("run_lifecycle", "run_flatten", "persist_lifecycle"):
            self.assertNotIn(bad, code, "gates references %s" % bad)
        self.assertNotIn(" submit ", code, "gates calls submit")
        self.assertNotIn(" OrderIntent ", code, "gates builds an order")

    def test_no_safety_assert(self):
        with open(GATES_SRC) as fh:
            tree = ast.parse(fh.read())
        self.assertEqual(
            [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])

    def test_d0_d1_are_the_only_gate_imports(self):
        # gates legitimately imports D0 (runtime_policy) and D1 (market_clock);
        # confirm it does NOT import broker_allocation writer or etoro writer.
        mods = self._toplevel_imports(GATES_SRC)
        for m in mods:
            self.assertNotIn("broker_allocation", m.lower())
            self.assertNotIn("signal_only_broker", m.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

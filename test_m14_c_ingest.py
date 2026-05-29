"""M14.C — ingestion tests. No network. No live broker calls.

Proves the ChatGPT M14.C corrections:
  1. Unknown ≠ zero (known-zero vs unknown-zero distinguishable).
  2. Required-fields-only gate freshness; opportunistic don't.
  3. IBKR canonical = sum-of-executions; failure → UNKNOWN.
  4. eToro canonical = sum-of-closed-trades; missing keys/auth → UNKNOWN
     with explicit error code.
  5. Compact redacted summary in DB lifecycle_json (no bulky raw).
  6. CLI: --dry-run writes nothing; forbidden flags rejected; --all
     continues through scopes; non-zero on UNKNOWN.
  7. Scanner-isolation: live_broker / ingest_* not imported by
     scanner/strategy/risk/brokers.
  8. eToro adapter is AST-clean of write verbs.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.flywheel import init_flywheel_tables
from bot.risk_authority.reading import (
    BrokerPnLReading,
    OPPORTUNISTIC,
    REQUIRED_FOR_FRESH,
    ReadingQuality,
    finalize_quality,
    has_fresh_pnl,
    is_known_zero,
    is_unknown,
    make_unknown,
)
from bot.risk_authority.ingest import (
    INGESTIBLE_SCOPES,
    VALID_BROKER_SCOPES,
    ingest_all_scopes,
    ingest_once,
    register_adapter_factory,
)
from bot.risk_authority.ingest_etoro import EtoroPnLAdapter
from bot.risk_authority.ingest_ibkr import IBKRPnLAdapter


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────

class _DB:
    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name
        with sqlite3.connect(self.path) as c:
            init_flywheel_tables(c)

    def conn(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _fake_etoro_reader_with(items):
    def reader(min_date):
        return items
    return reader


def _fake_ibkr_reader_with(execs):
    def reader(today):
        return execs
    return reader


def _exec(realized_pnl, time_utc):
    return {"realized_pnl": realized_pnl, "time_utc": time_utc}


def _history_item(net_profit, close_timestamp):
    # Mimic the HistoryItem attribute surface (uses attr/dict both work).
    return {"net_profit": net_profit, "close_timestamp": close_timestamp}


# ─────────────────────────────────────────────────────────────────────────────
# 1. BrokerPnLReading + quality logic
# ─────────────────────────────────────────────────────────────────────────────

class TestReadingQuality(unittest.TestCase):
    def test_all_required_known_is_fresh_or_partial(self):
        r = BrokerPnLReading(
            broker_scope="etoro_real", trading_day="2026-05-28",
            fetched_at_utc="2026-05-28T00:00:00Z", success=True,
            realised_pnl_usd=10.0, realised_daily_loss=0.0,
            open_positions=0, capital_deployed=0.0,
            peak_equity=100.0, drawdown_from_peak=0.0,
            realised_pnl_pct=0.0,
        )
        finalize_quality(r)
        self.assertEqual(r.quality, ReadingQuality.FRESH)

    def test_missing_opportunistic_is_partial(self):
        r = BrokerPnLReading(
            broker_scope="etoro_real", trading_day="2026-05-28",
            fetched_at_utc="2026-05-28T00:00:00Z", success=True,
            realised_pnl_usd=10.0, realised_daily_loss=0.0,
            # All opportunistic fields missing
        )
        finalize_quality(r)
        self.assertEqual(r.quality, ReadingQuality.PARTIAL)
        self.assertTrue(has_fresh_pnl(r))

    def test_missing_required_is_unknown(self):
        r = BrokerPnLReading(
            broker_scope="etoro_real", trading_day="2026-05-28",
            fetched_at_utc="2026-05-28T00:00:00Z", success=True,
            realised_pnl_usd=None, realised_daily_loss=0.0,
        )
        finalize_quality(r)
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertFalse(has_fresh_pnl(r))

    def test_success_false_is_unknown_even_with_fields(self):
        # Belt-and-braces: even with numeric fields populated, success=False
        # must produce UNKNOWN.
        r = BrokerPnLReading(
            broker_scope="etoro_real", trading_day="2026-05-28",
            fetched_at_utc="2026-05-28T00:00:00Z", success=False,
            realised_pnl_usd=0.0, realised_daily_loss=0.0,
        )
        finalize_quality(r)
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)

    def test_known_zero_vs_unknown_zero_distinguishable(self):
        # KNOWN ZERO: successful read, no trades today.
        kz = BrokerPnLReading(
            broker_scope="etoro_real", trading_day="2026-05-28",
            fetched_at_utc="2026-05-28T00:00:00Z", success=True,
            realised_pnl_usd=0.0, realised_daily_loss=0.0,
        )
        finalize_quality(kz)
        # UNKNOWN ZERO: failed read; numeric None.
        uz = make_unknown("etoro_real", trading_day="2026-05-28",
                          error="adapter_error:Timeout")
        # Distinct quality:
        self.assertEqual(kz.quality, ReadingQuality.PARTIAL)
        self.assertEqual(uz.quality, ReadingQuality.UNKNOWN)
        # Predicate helpers must agree:
        self.assertTrue(is_known_zero(kz))
        self.assertFalse(is_known_zero(uz))
        self.assertFalse(is_unknown(kz))
        self.assertTrue(is_unknown(uz))


# ─────────────────────────────────────────────────────────────────────────────
# 2. IBKR adapter
# ─────────────────────────────────────────────────────────────────────────────

class TestIBKRAdapter(unittest.TestCase):
    def test_sum_of_today_executions(self):
        execs = [
            _exec(10.0, "2026-05-28T08:00:00Z"),
            _exec(-3.5, "2026-05-28T09:00:00Z"),
            _exec(100.0, "2026-05-27T23:59:59Z"),  # yesterday — excluded
        ]
        a = IBKRPnLAdapter(broker_scope="ibkr_live",
                            executions_reader=_fake_ibkr_reader_with(execs))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.PARTIAL)
        self.assertEqual(r.realised_pnl_usd, 6.5)
        self.assertEqual(r.realised_daily_loss, 0.0)

    def test_no_trades_today_is_known_zero(self):
        # Empty execution list with successful read → known-zero PARTIAL.
        a = IBKRPnLAdapter(broker_scope="ibkr_paper",
                            executions_reader=_fake_ibkr_reader_with([]))
        r = a.read(today="2026-05-28")
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.realised_pnl_usd, 0.0)
        self.assertTrue(is_known_zero(r))
        self.assertFalse(is_unknown(r))

    def test_reader_raises_yields_unknown(self):
        def boom(today):
            raise ConnectionError("gateway down")
        a = IBKRPnLAdapter(broker_scope="ibkr_live", executions_reader=boom)
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertIn("executions_reader_failed", r.error)
        # Adapter MUST NOT have raised.

    def test_malformed_execution_yields_unknown(self):
        # An exec entry with a non-numeric realized_pnl makes the entire
        # day's sum untrustworthy — UNKNOWN, not partial.
        execs = [{"realized_pnl": "not a number", "time_utc": "2026-05-28T01:00:00Z"}]
        a = IBKRPnLAdapter(broker_scope="ibkr_live",
                            executions_reader=_fake_ibkr_reader_with(execs))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        # Either error code is acceptable: the strict type-check branch
        # rejects non-numerics directly; the float-cast branch is the
        # fallback if a numeric-looking value still fails conversion.
        self.assertTrue(
            "executions_missing_realized_pnl" in r.error
            or "executions_malformed" in r.error,
            f"unexpected error: {r.error}",
        )

    def test_account_reader_failure_is_opportunistic(self):
        def acct_boom():
            raise RuntimeError("account read failed")
        a = IBKRPnLAdapter(
            broker_scope="ibkr_live",
            executions_reader=_fake_ibkr_reader_with([]),
            account_reader=acct_boom,
        )
        r = a.read(today="2026-05-28")
        # PnL reading is still PARTIAL/FRESH despite account failure.
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)

    def test_account_reader_populates_opportunistic(self):
        a = IBKRPnLAdapter(
            broker_scope="ibkr_live",
            executions_reader=_fake_ibkr_reader_with([]),
            account_reader=lambda: {"open_positions": 3,
                                     "capital_deployed": 1234.5,
                                     "peak_equity": 5000.0},
        )
        r = a.read(today="2026-05-28")
        self.assertEqual(r.open_positions, 3)
        self.assertEqual(r.capital_deployed, 1234.5)
        self.assertEqual(r.peak_equity, 5000.0)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError):
            IBKRPnLAdapter(broker_scope="etoro_real",
                            executions_reader=lambda t: [])

    # ── Blocker fix tests (ChatGPT M14.C correction) ──────────────────

    def test_same_day_execution_missing_realized_pnl_is_unknown(self):
        # A same-day execution without `realized_pnl` is untrusted data —
        # MUST return UNKNOWN, never silently treat as 0.0.
        execs = [{"time_utc": "2026-05-28T13:00:00Z"}]  # no realized_pnl
        a = IBKRPnLAdapter(broker_scope="ibkr_live",
                            executions_reader=_fake_ibkr_reader_with(execs))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertIsNone(r.realised_pnl_usd)
        self.assertIn("executions_missing_realized_pnl", r.error)

    def test_mixed_same_day_one_missing_pnl_is_unknown(self):
        # If ANY same-day exec is missing realized_pnl, the whole day is
        # unknown — silent skipping would understate loss.
        execs = [
            _exec(12.0, "2026-05-28T09:00:00Z"),
            {"time_utc": "2026-05-28T13:00:00Z"},   # missing realized_pnl
        ]
        a = IBKRPnLAdapter(broker_scope="ibkr_live",
                            executions_reader=_fake_ibkr_reader_with(execs))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertIsNone(r.realised_pnl_usd)

    def test_previous_day_missing_pnl_is_ignored(self):
        # A previous-day exec with no realized_pnl is filtered out by the
        # date predicate before validation; today's valid execs still
        # produce a clean reading.
        execs = [
            {"time_utc": "2026-05-27T12:00:00Z"},   # yesterday, no pnl
            _exec(7.5, "2026-05-28T09:00:00Z"),
        ]
        a = IBKRPnLAdapter(broker_scope="ibkr_live",
                            executions_reader=_fake_ibkr_reader_with(execs))
        r = a.read(today="2026-05-28")
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.realised_pnl_usd, 7.5)

    def test_empty_execution_list_is_known_zero(self):
        # No executions today, but the read succeeded — KNOWN ZERO,
        # distinguishable from UNKNOWN by quality + is_known_zero.
        a = IBKRPnLAdapter(broker_scope="ibkr_live",
                            executions_reader=lambda t: [])
        r = a.read(today="2026-05-28")
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.realised_pnl_usd, 0.0)
        self.assertEqual(r.realised_daily_loss, 0.0)
        self.assertTrue(is_known_zero(r))


# ─────────────────────────────────────────────────────────────────────────────
# 3. eToro adapter
# ─────────────────────────────────────────────────────────────────────────────

class TestEtoroAdapter(unittest.TestCase):
    def test_sum_of_today_closed_trades(self):
        items = [
            _history_item(5.0, "2026-05-28T10:00:00Z"),
            _history_item(-2.0, "2026-05-28T11:00:00Z"),
            _history_item(100.0, "2026-05-27T10:00:00Z"),   # yesterday excluded
        ]
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with(items))
        r = a.read(today="2026-05-28")
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.realised_pnl_usd, 3.0)
        self.assertEqual(r.realised_daily_loss, 0.0)

    def test_loss_day_realised_daily_loss(self):
        items = [_history_item(-10.0, "2026-05-28T10:00:00Z")]
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with(items))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.realised_pnl_usd, -10.0)
        self.assertEqual(r.realised_daily_loss, 10.0)

    def test_empty_history_is_known_zero(self):
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with([]))
        r = a.read(today="2026-05-28")
        self.assertTrue(is_known_zero(r))
        self.assertEqual(r.realised_pnl_usd, 0.0)

    def test_keys_absent_yields_unknown_keys_absent(self):
        def reader(min_date):
            raise RuntimeError("keys missing for etoro: keys_absent")
        a = EtoroPnLAdapter(broker_scope="etoro_real", history_reader=reader)
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.error, "keys_absent")

    def test_auth_failure_yields_auth_unavailable(self):
        from bot.etoro.errors import EtoroAuthError
        def reader(min_date):
            raise EtoroAuthError("401 unauthorised")
        a = EtoroPnLAdapter(broker_scope="etoro_real", history_reader=reader)
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.error, "auth_unavailable")

    def test_generic_failure_yields_adapter_error(self):
        def reader(min_date):
            raise TimeoutError("network slow")
        a = EtoroPnLAdapter(broker_scope="etoro_real", history_reader=reader)
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertTrue(r.error.startswith("adapter_error:"))

    def test_all_items_missing_net_profit_yields_unknown(self):
        items = [{"net_profit": None, "close_timestamp": "2026-05-28T10:00:00Z"}]
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with(items))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertIn("parse_error", r.error)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError):
            EtoroPnLAdapter(broker_scope="ibkr_live",
                             history_reader=lambda d: [])

    # ── Blocker fix tests (ChatGPT M14.C correction) ──────────────────

    def test_same_day_closed_trade_missing_net_profit_is_unknown(self):
        # A same-day closed trade without `net_profit` is untrusted data —
        # MUST return UNKNOWN, never silently treat as 0.0.
        items = [{"close_timestamp": "2026-05-28T15:00:00Z"}]  # no net_profit
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with(items))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertIsNone(r.realised_pnl_usd)
        self.assertIn("net_profit_missing_or_non_numeric", r.error)

    def test_mixed_same_day_one_missing_net_profit_is_unknown(self):
        # If ANY same-day closed trade is missing net_profit, the whole
        # day is unknown — silent skipping would understate loss.
        items = [
            _history_item(-5.0, "2026-05-28T09:30:00Z"),
            {"close_timestamp": "2026-05-28T15:00:00Z"},  # missing net_profit
        ]
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with(items))
        r = a.read(today="2026-05-28")
        self.assertEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertIsNone(r.realised_pnl_usd)

    def test_previous_day_missing_net_profit_is_ignored(self):
        # A previous-day closed trade with no net_profit is filtered out
        # by the close-timestamp predicate before validation; today's
        # valid item still produces a clean reading.
        items = [
            {"close_timestamp": "2026-05-27T14:00:00Z"},   # yesterday, no profit
            _history_item(4.25, "2026-05-28T09:30:00Z"),
        ]
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=_fake_etoro_reader_with(items))
        r = a.read(today="2026-05-28")
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.realised_pnl_usd, 4.25)

    def test_empty_history_after_stricter_validation_is_known_zero(self):
        # Re-asserts the known-zero contract under the stricter blocker
        # validation: even with the "any missing net_profit → UNKNOWN"
        # rule, an empty same-day list still resolves to KNOWN ZERO
        # because the strict check only runs when same-day items exist.
        a = EtoroPnLAdapter(broker_scope="etoro_real",
                             history_reader=lambda d: [])
        r = a.read(today="2026-05-28")
        self.assertNotEqual(r.quality, ReadingQuality.UNKNOWN)
        self.assertEqual(r.realised_pnl_usd, 0.0)
        self.assertEqual(r.realised_daily_loss, 0.0)
        self.assertTrue(is_known_zero(r))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Orchestrator UPSERT + hysteresis + dry-run
# ─────────────────────────────────────────────────────────────────────────────

class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def _good_adapter(self, scope="etoro_real", pnl=10.0):
        items = [_history_item(pnl, "2026-05-28T10:00:00Z")]
        return EtoroPnLAdapter(broker_scope=scope,
                                history_reader=_fake_etoro_reader_with(items))

    def _empty_adapter(self, scope="etoro_real"):
        return EtoroPnLAdapter(broker_scope=scope,
                                history_reader=_fake_etoro_reader_with([]))

    def _bad_adapter(self, scope="etoro_real", err="boom"):
        def r(d):
            raise RuntimeError(err)
        return EtoroPnLAdapter(broker_scope=scope, history_reader=r)

    def _row(self, conn, today, scope):
        cur = conn.execute(
            "SELECT realised_pnl_usd, realised_daily_loss, "
            "       daily_pnl_available, daily_pnl_source, source, "
            "       fresh_reads_count, lifecycle_json "
            "FROM daily_state_per_broker WHERE date=? AND broker_scope=?",
            (today, scope),
        ).fetchone()
        if not cur:
            return None
        return {
            "realised_pnl_usd":    cur[0],
            "realised_daily_loss": cur[1],
            "daily_pnl_available": cur[2],
            "daily_pnl_source":    cur[3],
            "source":              cur[4],
            "fresh_reads_count":   cur[5],
            "lifecycle":           json.loads(cur[6] or "{}"),
        }

    def test_fresh_insert_populates_columns_and_lifecycle(self):
        with self.fx.conn() as c:
            r = ingest_once(c, scope="etoro_real", today="2026-05-28",
                            adapter=self._good_adapter(pnl=10.0))
        self.assertEqual(r["status"], "inserted")
        with self.fx.conn() as c:
            row = self._row(c, "2026-05-28", "etoro_real")
        self.assertEqual(row["realised_pnl_usd"], 10.0)
        self.assertEqual(row["realised_daily_loss"], 0.0)
        self.assertEqual(row["daily_pnl_available"], 1)
        self.assertEqual(row["daily_pnl_source"], "etoro_real")
        self.assertEqual(row["source"], "ingested")
        self.assertEqual(row["fresh_reads_count"], 1)
        self.assertEqual(row["lifecycle"]["status"], "partial")
        self.assertIn("latest_reading", row["lifecycle"])

    def test_known_zero_vs_unknown_distinguishable_in_db(self):
        # KNOWN ZERO: empty trade history → DB row has rpnl=0, available=1.
        with self.fx.conn() as c:
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=self._empty_adapter())
            kz_row = self._row(c, "2026-05-28", "etoro_real")
        self.assertEqual(kz_row["realised_pnl_usd"], 0.0)
        self.assertEqual(kz_row["daily_pnl_available"], 1)
        self.assertNotEqual(kz_row["lifecycle"]["status"], "unknown")

        # UNKNOWN: adapter fails → DB row has rpnl=0 BUT available=0
        # and lifecycle.status='unknown'. The numeric column alone
        # CANNOT distinguish; lifecycle.status DOES.
        with self.fx.conn() as c:
            ingest_once(c, scope="etoro_paper", today="2026-05-28",
                        adapter=self._bad_adapter(scope="etoro_paper"))
            uk_row = self._row(c, "2026-05-28", "etoro_paper")
        self.assertEqual(uk_row["realised_pnl_usd"], 0.0)   # column default
        self.assertEqual(uk_row["daily_pnl_available"], 0)
        self.assertEqual(uk_row["lifecycle"]["status"], "unknown")
        self.assertEqual(uk_row["daily_pnl_source"], "unavailable")

    def test_hysteresis_fresh_increments_partial_holds_unknown_resets(self):
        # Per ChatGPT M14.C correction #2: PARTIAL means PnL is fresh,
        # opportunistic fields are missing — that still counts as a fresh
        # PnL read. So FRESH and PARTIAL both increment; only UNKNOWN resets.
        # Two consecutive PARTIAL reads → fresh_reads_count = 2.
        with self.fx.conn() as c:
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=self._empty_adapter())   # PARTIAL: rpnl known, opp missing
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=self._empty_adapter())
            row = self._row(c, "2026-05-28", "etoro_real")
        self.assertEqual(row["fresh_reads_count"], 2,
                         "PARTIAL reads must count as fresh PnL (correction #2)")

        # A FRESH reading (all required + all opportunistic) continues to
        # increment from the prior count.
        full = BrokerPnLReading(
            broker_scope="etoro_real", trading_day="2026-05-28",
            fetched_at_utc="2026-05-28T12:00:00Z", success=True,
            realised_pnl_usd=1.0, realised_daily_loss=0.0,
            realised_pnl_pct=0.001, open_positions=2,
            capital_deployed=200.0, peak_equity=300.0,
            drawdown_from_peak=0.0,
        )
        finalize_quality(full)
        class _Static:
            name = "etoro_real"
            def read(self, *, today):
                return full
        with self.fx.conn() as c:
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=_Static())
            row1 = self._row(c, "2026-05-28", "etoro_real")
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=_Static())
            row2 = self._row(c, "2026-05-28", "etoro_real")
        self.assertEqual(row1["fresh_reads_count"], 3)   # 2 PARTIAL + 1 FRESH
        self.assertEqual(row2["fresh_reads_count"], 4)   # + 1 more FRESH

        # UNKNOWN reading immediately resets to 0.
        with self.fx.conn() as c:
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=self._bad_adapter())
            row3 = self._row(c, "2026-05-28", "etoro_real")
        self.assertEqual(row3["fresh_reads_count"], 0)
        self.assertEqual(row3["daily_pnl_available"], 0)

    def test_partial_keeps_pnl_available_and_increments_hysteresis(self):
        # Explicit contract proof (M14.C correction #2):
        #   PARTIAL ⇒ daily_pnl_available=1, hysteresis +1.
        # PARTIAL means required PnL fields are present but at least one
        # opportunistic field (open_positions / capital_deployed /
        # peak_equity) is missing. Such a reading must NOT be penalised
        # for PnL purposes.
        with self.fx.conn() as c:
            r = ingest_once(c, scope="etoro_real", today="2026-05-28",
                            adapter=self._good_adapter(pnl=7.5))
            row = self._row(c, "2026-05-28", "etoro_real")
        # The _good_adapter populates required PnL fields and omits some
        # opportunistic ones → status='partial'.
        self.assertEqual(row["lifecycle"]["status"], "partial")
        self.assertEqual(row["daily_pnl_available"], 1,
                         "PARTIAL must keep daily_pnl_available=1 — "
                         "PnL is known, only exposure data is missing")
        self.assertEqual(row["fresh_reads_count"], 1,
                         "PARTIAL must increment hysteresis")
        # Confirm the dataclass-level helper agrees.
        self.assertIn("partial", str(r).lower() if isinstance(r, str)
                      else r.get("quality", ""))

    def test_dry_run_makes_no_db_writes(self):
        with self.fx.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            r = ingest_once(c, scope="etoro_real", today="2026-05-28",
                            adapter=self._good_adapter(), dry_run=True)
            after = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual(r["would_write"], False)

    def test_invalid_scope_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(ValueError):
                ingest_once(c, scope="GLOBAL", today="2026-05-28",
                            adapter=self._good_adapter())
            with self.assertRaises(ValueError):
                ingest_once(c, scope="not_a_scope", today="2026-05-28",
                            adapter=self._good_adapter())

    def test_compact_summary_excludes_raw_evidence(self):
        # Adapter populates evidence_summary; orchestrator stores it ONLY
        # in the audit log, NOT in DB lifecycle_json.latest_reading.
        with self.fx.conn() as c:
            ingest_once(c, scope="etoro_real", today="2026-05-28",
                        adapter=self._good_adapter(pnl=5.0))
            row = self._row(c, "2026-05-28", "etoro_real")
        latest = row["lifecycle"]["latest_reading"]
        # Compact: contains status fields only, no nested raw response.
        for forbidden in ("evidence_summary", "raw", "raw_response"):
            self.assertNotIn(forbidden, latest)
        # And size sanity: well under 1 KB serialized.
        self.assertLess(len(json.dumps(latest)), 1024)

    def test_global_rollup_scope_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(ValueError):
                ingest_once(c, scope="GLOBAL", today="2026-05-28",
                            adapter=self._good_adapter())


class TestIngestAllScopes(unittest.TestCase):
    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_continues_through_all_and_flags_any_unknown(self):
        def good_factory():
            items = [_history_item(1.0, "2026-05-28T10:00:00Z")]
            return EtoroPnLAdapter(broker_scope="etoro_real",
                                    history_reader=_fake_etoro_reader_with(items))
        def good_paper():
            return EtoroPnLAdapter(broker_scope="etoro_paper",
                                    history_reader=_fake_etoro_reader_with([]))
        def bad_ibkr():
            def r(t):
                raise RuntimeError("gateway down")
            return IBKRPnLAdapter(broker_scope="ibkr_live", executions_reader=r)
        def bad_ibkr_paper():
            def r(t):
                raise RuntimeError("gateway down")
            return IBKRPnLAdapter(broker_scope="ibkr_paper", executions_reader=r)

        register_adapter_factory("etoro_real", good_factory)
        register_adapter_factory("etoro_paper", good_paper)
        register_adapter_factory("ibkr_live", bad_ibkr)
        register_adapter_factory("ibkr_paper", bad_ibkr_paper)

        with self.fx.conn() as c:
            out = ingest_all_scopes(c, today="2026-05-28")
        self.assertTrue(out["any_unknown"])
        self.assertEqual(len(out["results"]), 4)
        # eToro scopes are non-unknown; IBKR scopes are unknown.
        self.assertNotEqual(out["results"]["etoro_real"]["quality"], "unknown")
        self.assertEqual(out["results"]["ibkr_live"]["quality"], "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# 5. CLI safety
# ─────────────────────────────────────────────────────────────────────────────

_REPO = os.path.dirname(os.path.abspath(__file__))


def _run_cli(*args, extra_env=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "tools.ingest_risk_state", *args],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=30,
    )


class TestCLI(unittest.TestCase):
    def test_help_works(self):
        r = _run_cli("--help")
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_demo_flag_rejected(self):
        r = _run_cli("--demo", "--all")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unrecognized arguments", r.stderr + r.stdout)

    def test_base_url_flag_rejected(self):
        r = _run_cli("--base-url", "https://x", "--all")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unrecognized arguments", r.stderr + r.stdout)

    def test_override_realised_pnl_rejected(self):
        r = _run_cli("--override-realised-pnl", "100", "--all")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unrecognized arguments", r.stderr + r.stdout)

    def test_scope_and_all_mutually_exclusive(self):
        r = _run_cli("--scope", "etoro_real", "--all", "--dry-run")
        self.assertEqual(r.returncode, 2)

    def test_no_scope_no_all_returns_2(self):
        r = _run_cli("--dry-run")
        self.assertEqual(r.returncode, 2)


# ─────────────────────────────────────────────────────────────────────────────
# 6. AST scan: eToro adapter is write-free
# ─────────────────────────────────────────────────────────────────────────────

class TestEtoroAdapterNoWrite(unittest.TestCase):
    """Mirror the M13.2/M13.3 contract for the new ingest_etoro module."""

    FORBIDDEN_LITERALS = {"POST", "DELETE", "PUT", "PATCH"}
    FORBIDDEN_FUNCTION_NAMES = {
        "post", "delete", "put", "patch",
        "_post", "_delete", "_put", "_patch",
    }

    def test_ingest_etoro_no_write_verbs(self):
        # Mirrors the M13.2 no-write contract scan: only flag string
        # literals that appear as arguments to a Call (e.g.
        # client.request("POST", …)), NOT arbitrary ast.Constants —
        # otherwise docstrings that legitimately describe what the module
        # forbids (e.g. "NO POST/DELETE/PUT/PATCH") would be false
        # positives. Also flag method names like .post / .delete on Calls.
        path = os.path.join(_REPO, "bot", "risk_authority", "ingest_etoro.py")
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in self.FORBIDDEN_FUNCTION_NAMES:
                    offenders.append(f"def {node.name} at line {node.lineno}")
            if isinstance(node, ast.Call):
                # Positional string-literal args
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        if a.value.upper() in self.FORBIDDEN_LITERALS:
                            offenders.append(
                                f"call arg {a.value!r} at line {a.lineno}"
                            )
                # method=… keyword
                for kw in node.keywords:
                    if (kw.arg == "method"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value.upper() in self.FORBIDDEN_LITERALS):
                        offenders.append(
                            f"call method={kw.value.value!r} at line {kw.value.lineno}"
                        )
                # .post / .delete / .put / .patch call attributes
                fn = node.func
                if isinstance(fn, ast.Attribute) and isinstance(fn.attr, str):
                    if fn.attr in self.FORBIDDEN_FUNCTION_NAMES:
                        offenders.append(
                            f".{fn.attr}(…) at line {fn.lineno}"
                        )
        self.assertEqual(offenders, [],
                         f"write verbs in ingest_etoro.py: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Scanner isolation — see test_m14_c_scanner_isolation.py for subprocess
#    tests. Here we add the in-process sanity check.
# ─────────────────────────────────────────────────────────────────────────────

class TestNoLiveBrokerCoupling(unittest.TestCase):
    """Prove the ingest modules contain no executable references to
    bot.etoro.live_broker / EtoroLiveBroker. Mirrors the M14.C
    no-write-verb AST scan: we walk the AST and only flag real
    code references (imports, attribute accesses, name nodes),
    NOT string literals or docstrings — otherwise a docstring that
    legitimately documents the prohibition (e.g. 'never constructs
    EtoroLiveBroker') would be a false positive.
    """

    FORBIDDEN_MODULE_SUFFIXES = ("live_broker",)
    FORBIDDEN_NAMES = {"EtoroLiveBroker"}

    def _scan_module(self, path: str) -> list:
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        offenders = []
        for node in ast.walk(tree):
            # `from bot.etoro.live_broker import …` / `import bot.etoro.live_broker`
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if any(m == sfx or m.endswith("." + sfx)
                       for sfx in self.FORBIDDEN_MODULE_SUFFIXES):
                    offenders.append(
                        f"ImportFrom {m!r} at line {node.lineno}"
                    )
                for alias in node.names:
                    if alias.name in self.FORBIDDEN_NAMES:
                        offenders.append(
                            f"ImportFrom name {alias.name!r} at line {node.lineno}"
                        )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    n = alias.name or ""
                    if any(n == sfx or n.endswith("." + sfx)
                           for sfx in self.FORBIDDEN_MODULE_SUFFIXES):
                        offenders.append(
                            f"Import {n!r} at line {node.lineno}"
                        )
            # Name / attribute references in code (NOT strings, NOT docstrings).
            if isinstance(node, ast.Name) and node.id in self.FORBIDDEN_NAMES:
                offenders.append(
                    f"Name {node.id!r} at line {node.lineno}"
                )
            if isinstance(node, ast.Attribute) and node.attr in self.FORBIDDEN_NAMES:
                offenders.append(
                    f".{node.attr} at line {node.lineno}"
                )
            # Catch attribute chains like foo.live_broker.something — the
            # `.live_broker` access is a real code reference.
            if isinstance(node, ast.Attribute) and isinstance(node.attr, str):
                if node.attr in self.FORBIDDEN_MODULE_SUFFIXES:
                    offenders.append(
                        f".{node.attr} access at line {node.lineno}"
                    )
        return offenders

    def test_ingest_modules_do_not_import_live_broker_at_import_time(self):
        for mod in (
            "bot/risk_authority/ingest.py",
            "bot/risk_authority/ingest_etoro.py",
            "bot/risk_authority/ingest_ibkr.py",
            "bot/risk_authority/ingest_audit.py",
            "tools/ingest_risk_state.py",
        ):
            path = os.path.join(_REPO, mod)
            offenders = self._scan_module(path)
            self.assertEqual(
                offenders, [],
                f"{mod} has executable live_broker references: {offenders}",
            )

    def test_scan_actually_catches_real_references(self):
        # Self-check: the AST scan must catch real imports and real
        # attribute accesses. Construct a tiny synthetic source and
        # verify the scanner flags it.
        import tempfile
        synthetic = (
            "from bot.etoro.live_broker import EtoroLiveBroker\n"
            "def f():\n"
            "    return EtoroLiveBroker()\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as tf:
            tf.write(synthetic)
            tmp_path = tf.name
        try:
            offenders = self._scan_module(tmp_path)
            self.assertTrue(
                any("ImportFrom" in o for o in offenders),
                f"AST scan missed a real import: {offenders}",
            )
            self.assertTrue(
                any("EtoroLiveBroker" in o for o in offenders),
                f"AST scan missed a real Name reference: {offenders}",
            )
        finally:
            os.unlink(tmp_path)

    def test_scan_ignores_docstring_mentions(self):
        # Self-check: the scanner must NOT flag string literals /
        # docstrings that merely mention live_broker or EtoroLiveBroker
        # to document the prohibition.
        import tempfile
        synthetic = (
            '"""This module never imports bot.etoro.live_broker or '
            'constructs EtoroLiveBroker. Documentation only."""\n'
            "def f():\n"
            "    return 1\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as tf:
            tf.write(synthetic)
            tmp_path = tf.name
        try:
            offenders = self._scan_module(tmp_path)
            self.assertEqual(
                offenders, [],
                f"AST scan false-positived on docstring: {offenders}",
            )
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

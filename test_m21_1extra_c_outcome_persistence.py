#!/usr/bin/env python3
"""M21.1extra-C — tests for paper-lifecycle outcome persistence.

Uses a temp SQLite DB; no gateway needed. Proves:
  - persist a lifecycle and read it back identically
  - append-only (second distinct lifecycle adds; same lifecycle is idempotent)
  - failed lifecycles persisted truthfully (not dropped)
  - honest labels: record_kind / is_edge_outcome / timestamp_source /
    event_timestamps_available, and null event timestamps when absent
  - DST correctness: market_session_date derived from America/New_York, proven
    in the US/UK DST divergence windows (NOT just normal dates)
  - reader summary counts correct
  - no writes outside paper_lifecycles
  - AST/source guard bans scheduler/dashboard/etoro/telegram
"""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
import tokenize
import unittest
from datetime import datetime, timezone

import tools.paper_loop.m21_1extra_c_outcome_persistence as C

_MODULE_PATH = "tools/paper_loop/m21_1extra_c_outcome_persistence.py"


def _b2b(**over):
    d = {
        "symbol": "AAPL", "account": "DUP623346", "port": 4002,
        "entry_order_id": "IB-PERM-1", "entry_result_status": "accepted",
        "entry_order_originated": True, "entry_filled": True,
        "position_observed": True, "observation_attempts": 1,
        "observation_seconds": 0.4, "observation_timeout": False,
        "flatten_called": True, "flatten_confirmed": True,
        "close_order_placed": True, "remaining_positions": [],
        "remaining_open_orders": [], "lifecycle_confirmed": True,
        "data_source": "real_ibkr_paper_gateway",
    }
    d.update(over)
    return d


class _DBCase(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)


class TestPersistAndRead(_DBCase):
    def test_persist_then_read_back(self):
        r = C.persist_lifecycle(_b2b(), db_path=self.db)
        self.assertTrue(r["inserted"])
        rows = C.read_lifecycles(db_path=self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["entry_order_id"], "IB-PERM-1")
        self.assertEqual(row["lifecycle_confirmed"], 1)
        self.assertEqual(row["record_kind"], "mechanical_paper_lifecycle")
        self.assertEqual(row["is_edge_outcome"], 0)

    def test_append_only_two_distinct(self):
        C.persist_lifecycle(_b2b(entry_order_id="IB-PERM-1"), db_path=self.db)
        C.persist_lifecycle(_b2b(entry_order_id="IB-PERM-2"), db_path=self.db)
        self.assertEqual(len(C.read_lifecycles(db_path=self.db)), 2)

    def test_idempotent_same_order_id(self):
        C.persist_lifecycle(_b2b(entry_order_id="IB-PERM-9"), db_path=self.db)
        r2 = C.persist_lifecycle(_b2b(entry_order_id="IB-PERM-9"),
                                 db_path=self.db)
        self.assertFalse(r2["inserted"])
        self.assertTrue(r2["duplicate"])
        self.assertEqual(len(C.read_lifecycles(db_path=self.db)), 1)

    def test_failed_lifecycle_persisted_truthfully(self):
        # policy-blocked style: no order id, not confirmed
        b = _b2b(entry_order_id=None, entry_result_status="signal_only_skipped",
                 entry_filled=False, position_observed=False,
                 observation_timeout=True, close_order_placed=False,
                 lifecycle_confirmed=False)
        r = C.persist_lifecycle(b, db_path=self.db)
        self.assertTrue(r["inserted"])
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["lifecycle_confirmed"], 0)
        self.assertEqual(row["entry_result_status"], "signal_only_skipped")
        self.assertIsNone(row["entry_order_id"])
        self.assertTrue(r["lifecycle_id"].startswith("noid:"))


class TestHonestyLabels(_DBCase):
    def test_timestamp_source_and_null_events(self):
        C.persist_lifecycle(_b2b(), db_path=self.db)
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["timestamp_source"], "c_persist_time_only")
        self.assertEqual(row["event_timestamps_available"], 0)
        # B2b has no event timestamps -> these must be null, not persist-time
        self.assertIsNone(row["submitted_at_utc"])
        self.assertIsNone(row["observed_at_utc"])
        self.assertIsNone(row["flattened_at_utc"])
        # persist-time IS set
        self.assertIsNotNone(row["persisted_at_utc"])
        self.assertEqual(row["created_at_utc"], row["persisted_at_utc"])
        self.assertEqual(row["market_session_date_source"],
                         "persisted_at_utc_not_execution_time")

    def test_event_timestamps_used_only_if_present(self):
        b = _b2b(submitted_at_utc="2026-07-02T18:01:00+00:00")
        C.persist_lifecycle(b, db_path=self.db)
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["submitted_at_utc"], "2026-07-02T18:01:00+00:00")
        self.assertEqual(row["event_timestamps_available"], 1)

    def test_market_clock_deferred_to_d(self):
        C.persist_lifecycle(_b2b(), db_path=self.db)
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["market_clock_checked"], 0)
        self.assertEqual(row["market_clock_reason"],
                         "not_checked_in_C_deferred_to_D")
        self.assertEqual(row["exchange_timezone"], "America/New_York")

    def test_market_calendar_id_default_and_override(self):
        # default identity for US equities
        C.persist_lifecycle(_b2b(entry_order_id="A"), db_path=self.db)
        row = [r for r in C.read_lifecycles(db_path=self.db)
               if r["entry_order_id"] == "A"][0]
        self.assertEqual(row["market_calendar_id"], "US_EQ")
        # source-provided identity is honoured (D-readiness for other exchanges)
        C.persist_lifecycle(
            _b2b(entry_order_id="B", market_calendar_id="UK_EQ",
                 exchange_timezone="Europe/London"), db_path=self.db)
        row2 = [r for r in C.read_lifecycles(db_path=self.db)
                if r["entry_order_id"] == "B"][0]
        self.assertEqual(row2["market_calendar_id"], "UK_EQ")
        self.assertEqual(row2["exchange_timezone"], "Europe/London")

    def test_calendar_id_is_identity_not_a_calendar_check(self):
        # C must NOT set market_clock_checked just because a calendar id exists
        C.persist_lifecycle(_b2b(market_calendar_id="US_EQ"), db_path=self.db)
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["market_clock_checked"], 0)


class TestDSTSessionDate(unittest.TestCase):
    """The crux of Mike's concern: market_session_date must come from
    America/New_York, DST-correct, in the US/UK divergence windows — never from
    the UTC date or a UK local date. Each instant below is chosen so the UTC
    date AND the UK date equal one day, but the correct ET date is the PREVIOUS
    day — so a naive UTC-date or UK-date implementation would fail here."""

    def test_march_divergence_window(self):
        # 2026: US -> EDT Mar 8; UK -> BST Mar 29. Mar 20 is inside the window.
        # 03:30 UTC Mar 20 == 23:30 EDT Mar 19 (ET date is the 19th).
        t = datetime(2026, 3, 20, 3, 30, tzinfo=timezone.utc)
        self.assertEqual(t.date().isoformat(), "2026-03-20")       # UTC date
        self.assertEqual(C.market_session_date_for(t), "2026-03-19")  # ET date

    def test_november_divergence_window(self):
        # 2026: UK -> GMT Oct 25; US -> EST Nov 1. Oct 28 is inside the window.
        # 03:30 UTC Oct 28 == 23:30 EDT Oct 27 (ET date is the 27th).
        t = datetime(2026, 10, 28, 3, 30, tzinfo=timezone.utc)
        self.assertEqual(t.date().isoformat(), "2026-10-28")       # UTC date
        self.assertEqual(C.market_session_date_for(t), "2026-10-27")  # ET date

    def test_late_utc_night_rolls_back_to_prior_et_day(self):
        # 01:30 UTC Jul 3 == 21:30 EDT Jul 2 (ET session date is the 2nd).
        t = datetime(2026, 7, 3, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(C.market_session_date_for(t), "2026-07-02")

    def test_persisted_record_uses_et_session_date(self):
        db = tempfile.mktemp(suffix=".db")
        try:
            t = datetime(2026, 3, 20, 3, 30, tzinfo=timezone.utc)
            r = C.persist_lifecycle(_b2b(), db_path=db, persisted_at_utc=t)
            self.assertEqual(r["market_session_date"], "2026-03-19")
            row = C.read_lifecycles(db_path=db)[0]
            self.assertEqual(row["market_session_date"], "2026-03-19")
        finally:
            if os.path.exists(db):
                os.remove(db)

    def test_naive_datetime_refused(self):
        with self.assertRaises(ValueError):
            C.market_session_date_for(datetime(2026, 3, 20, 3, 30))  # naive


class TestSummaryAndIsolation(_DBCase):
    def test_summary_counts(self):
        C.persist_lifecycle(_b2b(entry_order_id="A", lifecycle_confirmed=True),
                            db_path=self.db)
        C.persist_lifecycle(
            _b2b(entry_order_id="B", lifecycle_confirmed=False,
                 entry_filled=False), db_path=self.db)
        s = C.summarize(db_path=self.db)
        self.assertEqual(s["total_lifecycles"], 2)
        self.assertEqual(s["lifecycle_confirmed_count"], 1)
        self.assertEqual(s["entry_filled_count"], 1)
        self.assertEqual(s["is_edge_outcome"], 0)

    def test_only_paper_lifecycles_table_written(self):
        C.persist_lifecycle(_b2b(), db_path=self.db)
        conn = sqlite3.connect(self.db)
        try:
            tbls = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        # C must create ONLY its own table
        self.assertEqual(tbls, {"paper_lifecycles"})

    def test_read_empty_db_returns_empty(self):
        self.assertEqual(C.read_lifecycles(db_path=self.db), [])
        self.assertEqual(C.summarize(db_path=self.db)["total_lifecycles"], 0)


class TestSourceGuard(unittest.TestCase):
    def test_no_forbidden_tokens(self):
        with open(_MODULE_PATH, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
        names = {t.string for t in toks if t.type == tokenize.NAME}
        forbidden = {"scheduler", "apscheduler", "dashboard", "telegram",
                     "notifier", "etoro", "submit", "placeOrder"}
        self.assertEqual(names & forbidden, set())

    def test_imports_constrained(self):
        with open(_MODULE_PATH) as fh:
            tree = ast.parse(fh.read())
        mods = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                mods.append(n.module or "")
        for m in mods:
            self.assertFalse(m.startswith("bot.etoro"))
            self.assertFalse(m.startswith("bot.brokers"))
            self.assertNotEqual(m, "main")
            self.assertFalse(m.startswith("dashboard"))
            self.assertFalse("scheduler" in m)

    def test_uses_zoneinfo_not_fixed_offset(self):
        with open(_MODULE_PATH) as fh:
            src = fh.read()
        self.assertIn("ZoneInfo", src)
        self.assertIn("America/New_York", src)
        # no hardcoded UK trading window or fixed offset shortcuts
        self.assertNotIn("14:30", src)
        self.assertNotIn("21:00", src)


class TestUTCNormalisation(_DBCase):
    def test_non_utc_aware_persisted_at_normalised(self):
        # pass a non-UTC aware zone (Tokyo +09:00); stored value must be UTC
        from zoneinfo import ZoneInfo
        t = datetime(2026, 7, 3, 10, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
        r = C.persist_lifecycle(_b2b(), db_path=self.db, persisted_at_utc=t)
        row = C.read_lifecycles(db_path=self.db)[0]
        # 10:30 JST == 01:30 UTC
        self.assertEqual(row["persisted_at_utc"], "2026-07-03T01:30:00+00:00")
        self.assertTrue(row["persisted_at_utc"].endswith("+00:00"))
        # market_session_date still from America/New_York: 01:30 UTC -> ET Jul 2
        self.assertEqual(row["market_session_date"], "2026-07-02")

    def test_london_bst_normalised_to_utc(self):
        # London in BST (July) is +01:00; must convert, not store local
        from zoneinfo import ZoneInfo
        t = datetime(2026, 7, 3, 2, 30, tzinfo=ZoneInfo("Europe/London"))
        r = C.persist_lifecycle(_b2b(), db_path=self.db, persisted_at_utc=t)
        row = C.read_lifecycles(db_path=self.db)[0]
        # 02:30 BST == 01:30 UTC
        self.assertEqual(row["persisted_at_utc"], "2026-07-03T01:30:00+00:00")
        self.assertEqual(row["market_session_date"], "2026-07-02")

    def test_naive_persisted_at_refused(self):
        with self.assertRaises(ValueError):
            C.persist_lifecycle(_b2b(), db_path=self.db,
                                persisted_at_utc=datetime(2026, 7, 3, 1, 30))

    def test_source_event_ts_normalised_to_utc(self):
        # a non-UTC source event ts must be normalised, not stored as-is
        b = _b2b(submitted_at_utc="2026-07-03T10:30:00+09:00")  # Tokyo
        C.persist_lifecycle(b, db_path=self.db)
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertEqual(row["submitted_at_utc"], "2026-07-03T01:30:00+00:00")
        self.assertEqual(row["event_timestamps_available"], 1)

    def test_naive_source_event_ts_becomes_null(self):
        # a naive source event ts must NOT be assumed UTC -> stored null
        b = _b2b(submitted_at_utc="2026-07-03T01:30:00")  # no tz
        C.persist_lifecycle(b, db_path=self.db)
        row = C.read_lifecycles(db_path=self.db)[0]
        self.assertIsNone(row["submitted_at_utc"])
        self.assertEqual(row["event_timestamps_available"], 0)


class TestNoOrderIdReplaySafety(_DBCase):
    def _failed(self, **over):
        return _b2b(entry_order_id=None,
                    entry_result_status="signal_only_skipped",
                    entry_filled=False, position_observed=False,
                    lifecycle_confirmed=False, **over)

    def test_same_noid_payload_replayed_is_idempotent(self):
        f = self._failed()
        a = C.persist_lifecycle(
            f, db_path=self.db,
            persisted_at_utc=datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc))
        b = C.persist_lifecycle(
            f, db_path=self.db,
            persisted_at_utc=datetime(2026, 7, 2, 19, 0, tzinfo=timezone.utc))
        self.assertTrue(a["inserted"])
        self.assertFalse(b["inserted"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(len(C.read_lifecycles(db_path=self.db)), 1)

    def test_different_noid_payloads_two_rows(self):
        C.persist_lifecycle(
            self._failed(symbol="AAPL"), db_path=self.db,
            persisted_at_utc=datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc))
        C.persist_lifecycle(
            self._failed(symbol="MSFT"), db_path=self.db,
            persisted_at_utc=datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(len(C.read_lifecycles(db_path=self.db)), 2)

    def test_noid_id_is_payload_derived_not_time(self):
        f = self._failed()
        i1 = C.compute_lifecycle_id(f)
        i2 = C.compute_lifecycle_id(f)
        self.assertEqual(i1, i2)
        self.assertTrue(i1.startswith("noid:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

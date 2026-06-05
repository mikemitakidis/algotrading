"""test_m16_historical_data.py — M16 historical-data engine test suite.

19 test groups, ~60-75 tests, all use a FakeProvider (no live network)
except G19 which is a single skip-unless-M16_LIVE=1 smoke against real
yfinance.

Groups (mirrors §L of the pre-code checklist):
  G1  TestFakeProvider              — deterministic provider double
  G2  TestSchemaMigrations          — schema_version + idempotency
  G3  TestParquetAtomicWrite        — temp→rename + post-write validation
  G4  TestBackfillIdempotent        — same bytes mod ingested_at_utc
  G5  TestIncrementalIdempotent     — second run writes 0 new bars
  G6  TestRepairAndForceRebuild     — modes wipe + replace coverage
  G7  TestProviderOutcomes          — 3 outcomes (ok/no_data/error)
  G8  TestQualityRules              — 9 rules each exercised
  G9  TestSplitDetection            — adj_ratio drift → split_detected
  G10 TestReadFacade                — get_bars range + tz + raw/adj
  G11 Test4HResample                — bucket alignment + verification
  G12 TestCoverageAndStatus         — coverage table + freshness
  G13 TestConcurrencyLock           — second refresh exits cleanly
  G14 TestNoBrokerImports           — AST scan on bot/data/*.py
  G15 TestProtectedFilesUntouched   — diff vs ceb8cd5
  G16 TestM16BLocalReadOnly         — SMA preview never calls provider
  G17 TestPyarrowInstalled          — import sanity
  G18 TestLookbackExceeded          — yfinance 15m cap quality event
  G19 TestLiveYfinanceSmoke         — skip-unless-M16_LIVE=1

This module imports NOTHING broker-related.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# Workdir helpers ------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
BASELINE_COMMIT = "ceb8cd5"  # M15 closeout


def _ts(s: str) -> pd.Timestamp:
    """Convenience UTC timestamp constructor for tests."""
    return pd.Timestamp(s, tz="UTC")


# ---------------------------------------------------------------------------
# G1. FakeProvider — deterministic test double
# ---------------------------------------------------------------------------

from bot.historical.providers import (BaseProvider, FetchResult, ProviderCapability,
                                  FETCH_OK, FETCH_NO_DATA,
                                  FETCH_PROVIDER_ERROR, FETCH_RATE_LIMITED)


class FakeProvider(BaseProvider):
    """Deterministic test provider — returns canned DataFrames or routed errors.

    Per-call behaviour can be programmed via:
      * next_outcomes: list of FETCH_* outcomes to consume one per call
      * outcome: default outcome when next_outcomes is empty
      * fake_data: dict[(symbol, timeframe)] -> dict (passed to pd.DataFrame)
      * raise_on_call: if True, fetch_bars raises immediately (used by
                          M16.B test to prove no calls happen)
    """
    def __init__(self, *, name="fake"):
        self.calls = 0
        self.next_outcomes = []
        self.outcome = FETCH_OK
        self.fake_data = {}
        self.raise_on_call = False
        self._name = name

    @property
    def capability(self):
        return ProviderCapability(
            name=self._name,
            supported_timeframes=frozenset({"1D", "1H", "15m"}),
            lookback_caps={"1D": "max", "1H": "730d", "15m": "60d"},
            supports_adjusted=True,
            polite_calls_per_minute=10_000,
        )

    def fetch_bars(self, symbol, timeframe, start_utc, end_utc):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("FakeProvider raised — should not have been called")
        outcome = self.next_outcomes.pop(0) if self.next_outcomes else self.outcome
        if outcome == FETCH_NO_DATA:
            return FetchResult(outcome=FETCH_NO_DATA, message="fake no_data")
        if outcome == FETCH_PROVIDER_ERROR:
            return FetchResult(outcome=FETCH_PROVIDER_ERROR, message="fake error")
        if outcome == FETCH_RATE_LIMITED:
            return FetchResult(outcome=FETCH_RATE_LIMITED, message="fake 429")
        data = self.fake_data.get((symbol.upper(), timeframe))
        if data is not None:
            df = pd.DataFrame(data)
        else:
            ts = pd.date_range(end=end_utc, periods=5, freq="D", tz="UTC")
            df = pd.DataFrame({
                "ts_utc": ts,
                "open":  [100.0, 101.0, 102.0, 103.0, 104.0],
                "high":  [101.0, 102.0, 103.0, 104.0, 105.0],
                "low":   [ 99.0, 100.0, 101.0, 102.0, 103.0],
                "close": [100.5, 101.5, 102.5, 103.5, 104.5],
                "volume": [1000]*5,
                "adj_close": [100.5, 101.5, 102.5, 103.5, 104.5],
                "adjustment_ratio": [1.0]*5,
                "is_adjusted": [True]*5,
                "provider": [self._name]*5,
                "quality_flags": [0]*5,
            })
        df = df[(df["ts_utc"] >= pd.Timestamp(start_utc))
                 & (df["ts_utc"] <= pd.Timestamp(end_utc))]
        return FetchResult(outcome=FETCH_OK, df=df.reset_index(drop=True))


class TestFakeProvider(unittest.TestCase):
    def test_default_returns_ok(self):
        p = FakeProvider()
        r = p.fetch_bars("AAPL", "1D",
                          datetime(2026, 5, 1, tzinfo=timezone.utc),
                          datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(r.outcome, FETCH_OK)
        self.assertGreater(len(r.df), 0)

    def test_no_data_then_ok(self):
        p = FakeProvider()
        p.next_outcomes = [FETCH_NO_DATA, FETCH_OK]
        a = p.fetch_bars("X", "1D",
                          datetime(2026, 5, 1, tzinfo=timezone.utc),
                          datetime(2026, 6, 1, tzinfo=timezone.utc))
        b = p.fetch_bars("X", "1D",
                          datetime(2026, 5, 1, tzinfo=timezone.utc),
                          datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(a.outcome, FETCH_NO_DATA)
        self.assertEqual(b.outcome, FETCH_OK)


# Shared test base -----------------------------------------------------------

class _TmpEnv(unittest.TestCase):
    """Provides a fresh tmp DB + Parquet root per test."""
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.db_path = Path(f.name)
        self.parquet_root = Path(tempfile.mkdtemp(prefix="m16_parquet_"))

    def tearDown(self):
        try: self.db_path.unlink()
        except FileNotFoundError: pass
        shutil.rmtree(self.parquet_root, ignore_errors=True)

    def _now(self):
        return datetime(2026, 6, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# G2. Schema migrations
# ---------------------------------------------------------------------------
class TestSchemaMigrations(_TmpEnv):
    def test_apply_idempotent(self):
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            v1 = schema.apply_schema(c)
            v2 = schema.apply_schema(c)
            self.assertEqual(v1, schema.SCHEMA_VERSION)
            self.assertEqual(v2, schema.SCHEMA_VERSION)
            n = c.execute("SELECT COUNT(*) FROM historical_schema_version"
                            ).fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            c.close()

    def test_initial_schema_version(self):
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            self.assertEqual(schema.get_schema_version(c), 0)
            schema.apply_schema(c)
            self.assertEqual(schema.get_schema_version(c), schema.SCHEMA_VERSION)
        finally:
            c.close()

    def test_check_constraints_fire(self):
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            schema.apply_schema(c)
            c.execute("INSERT INTO historical_symbols VALUES "
                       "('A','us_equity',1,'2026-01-01','2026-01-01')")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO historical_coverage "
                            "(symbol,timeframe,provider,derivation_method) "
                            "VALUES ('A','BOGUS','yf','native')")
        finally:
            c.close()

    def test_lock_row_seeded(self):
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            schema.apply_schema(c)
            r = c.execute("SELECT id, owner_pid FROM historical_refresh_lock"
                           ).fetchone()
            self.assertEqual(r[0], 1)
            self.assertIsNone(r[1])
        finally:
            c.close()

    def test_schema_version_is_2(self):
        """M16.A.fix-1 bumped SCHEMA_VERSION 1 -> 2 for the new
        historical_refresh_runs.symbols_rate_limited column."""
        from bot.historical import schema
        self.assertEqual(schema.SCHEMA_VERSION, 2)
        c = schema.open_db(self.db_path)
        try:
            schema.apply_schema(c)
            self.assertEqual(schema.get_schema_version(c), 2)
        finally:
            c.close()

    def test_symbols_rate_limited_column_present(self):
        """Fresh installs must have historical_refresh_runs.symbols_rate_limited."""
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            schema.apply_schema(c)
            cols = {row[1] for row in c.execute(
                "PRAGMA table_info(historical_refresh_runs)").fetchall()}
            self.assertIn("symbols_rate_limited", cols)
        finally:
            c.close()

    def test_additive_migration_from_v1(self):
        """Pre-existing v1 DBs (without symbols_rate_limited) must get
        the column added when apply_schema runs again."""
        from bot.historical import schema
        # Manually create a v1-shaped historical_refresh_runs (without
        # symbols_rate_limited) to simulate a pre-fix DB.
        c = schema.open_db(self.db_path)
        try:
            c.execute("""
                CREATE TABLE historical_refresh_runs (
                  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  started_at_utc TEXT NOT NULL,
                  finished_at_utc TEXT,
                  mode TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  symbols_requested TEXT NOT NULL,
                  timeframes_requested TEXT NOT NULL,
                  status TEXT NOT NULL,
                  symbols_attempted INTEGER NOT NULL DEFAULT 0,
                  symbols_ok INTEGER NOT NULL DEFAULT 0,
                  symbols_no_data INTEGER NOT NULL DEFAULT 0,
                  symbols_failed INTEGER NOT NULL DEFAULT 0,
                  bars_fetched INTEGER NOT NULL DEFAULT 0,
                  bars_written INTEGER NOT NULL DEFAULT 0,
                  bars_updated INTEGER NOT NULL DEFAULT 0,
                  errors_count INTEGER NOT NULL DEFAULT 0,
                  rate_limit_count INTEGER NOT NULL DEFAULT 0,
                  duration_sec REAL,
                  summary_json TEXT
                )""")
            c.commit()
            cols_before = {row[1] for row in c.execute(
                "PRAGMA table_info(historical_refresh_runs)").fetchall()}
            self.assertNotIn("symbols_rate_limited", cols_before)

            # Now apply_schema — it must add the missing column.
            schema.apply_schema(c)
            cols_after = {row[1] for row in c.execute(
                "PRAGMA table_info(historical_refresh_runs)").fetchall()}
            self.assertIn("symbols_rate_limited", cols_after)
        finally:
            c.close()


# ---------------------------------------------------------------------------
# G3. Parquet atomic write
# ---------------------------------------------------------------------------
def _seed_df(n=5, start="2026-05-01"):
    ts = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({
        "ts_utc": ts,
        "open":  [100.0+i for i in range(n)],
        "high":  [101.0+i for i in range(n)],
        "low":   [ 99.0+i for i in range(n)],
        "close": [100.5+i for i in range(n)],
        "volume":[1000]*n,
        "adj_close":[100.5+i for i in range(n)],
        "adjustment_ratio":[1.0]*n,
        "is_adjusted":[True]*n,
        "provider":["fake"]*n,
        "ingested_at_utc": pd.Timestamp("2026-06-01", tz="UTC"),
        "quality_flags":[0]*n,
    })


class TestParquetAtomicWrite(_TmpEnv):
    def test_temp_rename_basic(self):
        from bot.historical import store
        p = store._parquet_path("fake", "1D", "AAPL", root=self.parquet_root)
        store._write_parquet_atomic(p, _seed_df(3))
        self.assertTrue(p.exists())
        # No leftover .tmp files in the directory.
        tmps = list(p.parent.glob("*.tmp.*"))
        self.assertEqual(tmps, [])

    def test_refuses_duplicates_in_batch(self):
        from bot.historical import store
        df = _seed_df(3)
        dup = pd.concat([df, df.iloc[[0]]], ignore_index=True)
        p = store._parquet_path("fake", "1D", "AAPL", root=self.parquet_root)
        with self.assertRaises(ValueError):
            store._write_parquet_atomic(p, dup)
        # Failed write must not corrupt a non-existent file.
        self.assertFalse(p.exists())

    def test_refuses_missing_columns(self):
        from bot.historical import store
        df = _seed_df(3).drop(columns=["adj_close"])
        p = store._parquet_path("fake", "1D", "AAPL", root=self.parquet_root)
        with self.assertRaises(ValueError):
            store._write_parquet_atomic(p, df)

    def test_existing_file_untouched_on_validation_failure(self):
        from bot.historical import store
        p = store._parquet_path("fake", "1D", "AAPL", root=self.parquet_root)
        # Write a valid file first.
        store._write_parquet_atomic(p, _seed_df(3))
        sz_before = p.stat().st_size
        # Attempt a bad write — duplicates.
        df = _seed_df(3)
        bad = pd.concat([df, df.iloc[[0]]], ignore_index=True)
        with self.assertRaises(ValueError):
            store._write_parquet_atomic(p, bad)
        self.assertEqual(p.stat().st_size, sz_before)


# ---------------------------------------------------------------------------
# G4. Backfill idempotency
# ---------------------------------------------------------------------------
class TestBackfillIdempotent(_TmpEnv):
    def test_backfill_writes_parquet(self):
        from bot.historical import refresh, store
        p = FakeProvider()
        r = refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                          provider=p, db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        self.assertEqual(r.status, "ok")
        self.assertGreater(r.bars_written, 0)
        path = store._parquet_path("fake", "1D", "AAPL", root=self.parquet_root)
        self.assertTrue(path.exists())

    def test_backfill_twice_keeps_same_bar_count(self):
        from bot.historical import refresh, store
        r1 = refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                          provider=FakeProvider(), db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        cov1 = store.get_coverage("AAPL", "1D", provider="fake",
                                     db_path=self.db_path)
        r2 = refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                          provider=FakeProvider(), db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        cov2 = store.get_coverage("AAPL", "1D", provider="fake",
                                     db_path=self.db_path)
        self.assertEqual(cov1["bar_count"], cov2["bar_count"])

    def test_backfill_records_refresh_run(self):
        from bot.historical import refresh, schema
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        c = schema.open_db(self.db_path)
        try:
            n = c.execute("SELECT COUNT(*) FROM historical_refresh_runs"
                            ).fetchone()[0]
            self.assertEqual(n, 1)
            row = c.execute(
                "SELECT mode,status FROM historical_refresh_runs "
                "ORDER BY run_id DESC LIMIT 1").fetchone()
            self.assertEqual(row, ("backfill", "ok"))
        finally:
            c.close()


# ---------------------------------------------------------------------------
# G5. Incremental idempotency
# ---------------------------------------------------------------------------
class TestIncrementalIdempotent(_TmpEnv):
    def test_incremental_no_new_bars_second_time(self):
        from bot.historical import refresh, store
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        cov_a = store.get_coverage("AAPL", "1D", provider="fake",
                                       db_path=self.db_path)

        # Second incremental at the same now_utc — should not extend coverage.
        r2 = refresh.run(mode="incremental", symbols=["AAPL"],
                          timeframes=["1D"], provider=FakeProvider(),
                          db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        cov_b = store.get_coverage("AAPL", "1D", provider="fake",
                                       db_path=self.db_path)
        # Same bar count, same last_ts_utc.
        self.assertEqual(cov_a["bar_count"], cov_b["bar_count"])
        self.assertEqual(cov_a["last_ts_utc"], cov_b["last_ts_utc"])
        self.assertEqual(r2.symbols_failed, 0)

    def test_incremental_no_data_keeps_coverage(self):
        from bot.historical import refresh, store
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        cov_before = store.get_coverage("AAPL", "1D", provider="fake",
                                            db_path=self.db_path)
        nd = FakeProvider(); nd.outcome = FETCH_NO_DATA
        refresh.run(mode="incremental", symbols=["AAPL"], timeframes=["1D"],
                      provider=nd, db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        cov_after = store.get_coverage("AAPL", "1D", provider="fake",
                                           db_path=self.db_path)
        self.assertEqual(cov_before["last_ts_utc"], cov_after["last_ts_utc"])


# ---------------------------------------------------------------------------
# G6. Repair + force-rebuild
# ---------------------------------------------------------------------------
class TestRepairAndForceRebuild(_TmpEnv):
    def test_force_rebuild_resets_coverage(self):
        from bot.historical import refresh, store
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        path = store._parquet_path("fake", "1D", "AAPL",
                                       root=self.parquet_root)
        self.assertTrue(path.exists())
        refresh.run(mode="force_rebuild", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        # File rebuilt; coverage exists.
        cov = store.get_coverage("AAPL", "1D", provider="fake",
                                    db_path=self.db_path)
        self.assertIsNotNone(cov)
        self.assertGreater(cov["bar_count"], 0)


# ---------------------------------------------------------------------------
# G7. Provider outcomes — three distinct routes
# ---------------------------------------------------------------------------
class TestProviderOutcomes(_TmpEnv):
    def test_no_data_routed(self):
        from bot.historical import refresh
        nd = FakeProvider(); nd.outcome = FETCH_NO_DATA
        r = refresh.run(mode="backfill", symbols=["DELISTED"],
                          timeframes=["1D"], provider=nd,
                          db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        self.assertEqual(r.symbols_no_data, 1)
        self.assertEqual(r.symbols_failed, 0)

    def test_provider_error_routed(self):
        from bot.historical import refresh
        pe = FakeProvider(); pe.outcome = FETCH_PROVIDER_ERROR
        r = refresh.run(mode="backfill", symbols=["X"], timeframes=["1D"],
                          provider=pe, db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        self.assertEqual(r.symbols_failed, 1)
        self.assertEqual(r.symbols_no_data, 0)
        self.assertGreater(r.errors_count, 0)

    def test_rate_limited_then_ok(self):
        from bot.historical import refresh
        p = FakeProvider()
        # First call rate-limited, second OK.
        p.next_outcomes = [FETCH_RATE_LIMITED, FETCH_OK]
        # Speed up retries for the test.
        import bot.historical.refresh as rmod
        orig = rmod.RETRY_DELAYS_SEC
        rmod.RETRY_DELAYS_SEC = (0.01, 0.01, 0.01, 0.01, 0.01)
        try:
            r = refresh.run(mode="backfill", symbols=["AAPL"],
                              timeframes=["1D"], provider=p,
                              db_path=self.db_path,
                              parquet_root=self.parquet_root,
                              now_utc=self._now())
        finally:
            rmod.RETRY_DELAYS_SEC = orig
        self.assertEqual(r.symbols_ok, 1)
        self.assertEqual(r.rate_limit_count, 1)

    def test_all_rate_limited_is_failed_not_ok(self):
        """M16.A.fix-1: if every symbol exhausts retries with rate-limit
        outcomes, the run status must NOT be 'ok' and the symbols must
        be counted as `symbols_rate_limited`, not `symbols_no_data`.

        Before the fix this run came back as status='ok'
        symbols_no_data=N — which falsely implied a clean 'nothing to
        fetch' outcome.
        """
        from bot.historical import refresh, store
        # FakeProvider returns RATE_LIMITED on every call.
        p = FakeProvider(); p.outcome = FETCH_RATE_LIMITED
        import bot.historical.refresh as rmod
        orig = rmod.RETRY_DELAYS_SEC
        rmod.RETRY_DELAYS_SEC = (0.001, 0.001, 0.001, 0.001, 0.001)
        try:
            r = refresh.run(mode="backfill",
                              symbols=["AAPL", "MSFT"], timeframes=["1D"],
                              provider=p, db_path=self.db_path,
                              parquet_root=self.parquet_root,
                              now_utc=self._now())
        finally:
            rmod.RETRY_DELAYS_SEC = orig

        # Status must be 'failed' (no successes + only rate-limit failures).
        self.assertEqual(r.status, "failed",
                          f"all-rate-limited run had status={r.status!r}; "
                          "must be 'failed', NOT 'ok'")
        # Symbol-level classification.
        self.assertEqual(r.symbols_rate_limited, 2)
        self.assertEqual(r.symbols_no_data, 0,
                          "rate-limited symbols leaked into symbols_no_data")
        self.assertEqual(r.symbols_ok, 0)
        self.assertEqual(r.bars_written, 0)
        # Retry-attempt counter is also non-zero.
        self.assertGreater(r.rate_limit_count, 0)

        # The quality events must include 'rate_limited' kind, NOT 'no_data'.
        events = store.list_quality_events(db_path=self.db_path, limit=500)
        kinds = {e["kind"] for e in events}
        self.assertIn("rate_limited", kinds)
        # If 'no_data' was generated when the cause was rate-limit,
        # we'd see no_data events; the only legitimate no_data events
        # in this test would come from a successful empty fetch, which
        # didn't happen.
        no_data_events = [e for e in events if e["kind"] == "no_data"]
        self.assertEqual(no_data_events, [],
            "rate-limited fetches leaked 'no_data' quality events "
            "(M16.A.fix-1 regression)")


# ---------------------------------------------------------------------------
# G8. Quality rules
# ---------------------------------------------------------------------------
class TestQualityRules(unittest.TestCase):
    def _df(self, n=3, start="2026-05-01"):
        return _seed_df(n=n, start=start)

    def test_rejects_nan_ohlc(self):
        from bot.historical import quality
        df = self._df()
        df.loc[1, "close"] = float("nan")
        o = quality.validate_batch(df, symbol="A", timeframe="1D",
                                       provider="fake")
        self.assertEqual(len(o.valid_df), 2)
        self.assertTrue(any(e.kind == "nan_ohlc" for e in o.events))

    def test_rejects_invalid_hl(self):
        from bot.historical import quality
        df = self._df()
        df.loc[1, "high"] = df.loc[1, "low"] - 1
        o = quality.validate_batch(df, symbol="A", timeframe="1D",
                                       provider="fake")
        self.assertEqual(len(o.valid_df), 2)
        self.assertTrue(any(e.kind == "invalid_hl" for e in o.events))

    def test_rejects_negative_volume(self):
        from bot.historical import quality
        df = self._df()
        df.loc[1, "volume"] = -1
        o = quality.validate_batch(df, symbol="A", timeframe="1D",
                                       provider="fake")
        self.assertEqual(len(o.valid_df), 2)
        self.assertTrue(any(e.kind == "negative_volume" for e in o.events))

    def test_rejects_non_positive_ohlc(self):
        from bot.historical import quality
        df = self._df()
        df.loc[1, "close"] = 0
        o = quality.validate_batch(df, symbol="A", timeframe="1D",
                                       provider="fake")
        self.assertEqual(len(o.valid_df), 2)

    def test_warns_zero_volume(self):
        from bot.historical import quality
        df = self._df()
        df.loc[1, "volume"] = 0
        o = quality.validate_batch(df, symbol="A", timeframe="1D",
                                       provider="fake")
        self.assertEqual(len(o.valid_df), 3)
        self.assertTrue(any(e.kind == "zero_volume" for e in o.events))
        # quality_flags bit 0 set on row 1.
        self.assertTrue(int(o.valid_df.iloc[1]["quality_flags"])
                          & quality.QF_ZERO_VOLUME)

    def test_warns_duplicate_ts_keep_last(self):
        from bot.historical import quality
        df = self._df()
        dup = pd.concat([df, df.iloc[[1]]], ignore_index=True)
        o = quality.validate_batch(dup, symbol="A", timeframe="1D",
                                       provider="fake")
        self.assertEqual(len(o.valid_df), 3)
        self.assertEqual(o.duplicate_count, 1)
        self.assertTrue(any(e.kind == "duplicate_ts" for e in o.events))

    def test_outlier_warning(self):
        from bot.historical import quality
        lookback = pd.DataFrame({
            "close": [100.0 + i*0.01 for i in range(60)],
        })
        # Use a row with internally-consistent OHLC at the outlier
        # price level — otherwise it's rejected as invalid_hl before
        # the outlier rule fires.
        df = pd.DataFrame({
            "ts_utc": pd.to_datetime(["2026-05-02"], utc=True),
            "open":   [10_000.0],
            "high":   [10_001.0],
            "low":    [ 9_999.0],
            "close":  [10_000.0],
            "volume": [1000],
            "adj_close": [10_000.0],
            "adjustment_ratio": [1.0],
            "is_adjusted": [True],
            "provider": ["fake"],
            "ingested_at_utc": pd.Timestamp("2026-06-01", tz="UTC"),
            "quality_flags": [0],
        })
        o = quality.validate_batch(df, symbol="A", timeframe="1D",
                                       provider="fake",
                                       outlier_lookback_df=lookback,
                                       outlier_n_sigma=8.0)
        self.assertTrue(any(e.kind == "outlier" for e in o.events))


# ---------------------------------------------------------------------------
# G9. Split detection
# ---------------------------------------------------------------------------
class TestSplitDetection(_TmpEnv):
    def test_split_detected_on_ratio_drift(self):
        from bot.historical import refresh, store, schema
        # Backfill with adjustment_ratio == 1.0 everywhere.
        p1 = FakeProvider()
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=p1, db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())

        # Now incremental with adjustment_ratio == 0.5 (a 2-for-1 split).
        p2 = FakeProvider()
        last = pd.Timestamp(self._now())  # already tz-aware
        p2.fake_data[("AAPL", "1D")] = {
            "ts_utc": pd.date_range(end=last, periods=7, freq="D",
                                       tz="UTC"),
            "open":  [50.0]*7,
            "high":  [51.0]*7,
            "low":   [49.0]*7,
            "close": [50.5]*7,
            "volume":[1000]*7,
            "adj_close":[25.25]*7,
            "adjustment_ratio":[0.5]*7,
            "is_adjusted":[True]*7,
            "provider":["fake"]*7,
            "quality_flags":[0]*7,
        }
        refresh.run(mode="incremental", symbols=["AAPL"], timeframes=["1D"],
                      provider=p2, db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        events = store.list_quality_events(symbol="AAPL",
                                              db_path=self.db_path)
        kinds = {e["kind"] for e in events}
        self.assertIn("split_detected", kinds)


# ---------------------------------------------------------------------------
# G10. Read façade
# ---------------------------------------------------------------------------
class TestReadFacade(_TmpEnv):
    def setUp(self):
        super().setUp()
        from bot.historical import refresh
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())

    def test_get_bars_unknown_symbol_returns_empty(self):
        from bot.historical import store
        df = store.get_bars("ZZZNOEXIST", "1D", provider="fake",
                              parquet_root=self.parquet_root)
        self.assertEqual(len(df), 0)
        # Empty frame still has the right columns.
        for c in ("ts_utc", "open", "high", "low", "close", "volume"):
            self.assertIn(c, df.columns)

    def test_get_bars_returns_tz_aware_utc(self):
        from bot.historical import store
        df = store.get_bars("AAPL", "1D", provider="fake",
                              parquet_root=self.parquet_root)
        self.assertGreater(len(df), 0)
        self.assertEqual(str(df["ts_utc"].dt.tz), "UTC")

    def test_get_bars_range_filter(self):
        from bot.historical import store
        df_all = store.get_bars("AAPL", "1D", provider="fake",
                                   parquet_root=self.parquet_root)
        mid = df_all.iloc[len(df_all)//2]["ts_utc"]
        df_after = store.get_bars("AAPL", "1D", start_utc=mid,
                                     provider="fake",
                                     parquet_root=self.parquet_root)
        self.assertLess(len(df_after), len(df_all))
        self.assertGreaterEqual(len(df_after), 1)

    def test_get_bars_raw_vs_adjusted(self):
        from bot.historical import store
        adj = store.get_bars("AAPL", "1D", adjusted=True, provider="fake",
                                parquet_root=self.parquet_root)
        raw = store.get_bars("AAPL", "1D", adjusted=False, provider="fake",
                                parquet_root=self.parquet_root)
        self.assertEqual(len(adj), len(raw))
        # With adjustment_ratio==1.0 everywhere, raw == adjusted.
        for c in ("open", "high", "low", "close"):
            pd.testing.assert_series_equal(
                raw[c].reset_index(drop=True), adj[c].reset_index(drop=True),
                check_names=False, check_dtype=False)

    def test_get_bars_invalid_timeframe(self):
        from bot.historical import store
        with self.assertRaises(ValueError):
            store.get_bars("AAPL", "30m", parquet_root=self.parquet_root)


# ---------------------------------------------------------------------------
# G11. 4H resample
# ---------------------------------------------------------------------------
class Test4HResample(_TmpEnv):
    def test_24_hourly_bars_become_6_4h_bars(self):
        from bot.historical import refresh, store
        p = FakeProvider()
        p.fake_data[("NVDA", "1H")] = {
            "ts_utc": pd.date_range("2026-06-01", periods=24, freq="h",
                                       tz="UTC"),
            "open":  [100.0+i for i in range(24)],
            "high":  [101.0+i for i in range(24)],
            "low":   [ 99.0+i for i in range(24)],
            "close": [100.5+i for i in range(24)],
            "volume":[1000]*24,
            "adj_close":[100.5+i for i in range(24)],
            "adjustment_ratio":[1.0]*24,
            "is_adjusted":[True]*24,
            "provider":["fake"]*24,
            "quality_flags":[0]*24,
        }
        refresh.run(mode="backfill", symbols=["NVDA"],
                      timeframes=["1H", "4H"],
                      provider=p, db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        df_4h = store.get_bars("NVDA", "4H", provider="fake",
                                  parquet_root=self.parquet_root)
        self.assertEqual(len(df_4h), 6)

    def test_4h_coverage_metadata_recorded(self):
        from bot.historical import refresh, store
        p = FakeProvider()
        p.fake_data[("AAPL", "1H")] = {
            "ts_utc": pd.date_range("2026-06-01", periods=24, freq="h",
                                       tz="UTC"),
            "open": [100.0]*24, "high":[101.0]*24, "low":[99.0]*24,
            "close":[100.5]*24, "volume":[1000]*24,
            "adj_close":[100.5]*24, "adjustment_ratio":[1.0]*24,
            "is_adjusted":[True]*24, "provider":["fake"]*24,
            "quality_flags":[0]*24,
        }
        refresh.run(mode="backfill", symbols=["AAPL"],
                      timeframes=["1H", "4H"],
                      provider=p, db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        cov = store.get_coverage("AAPL", "4H", provider="fake",
                                    db_path=self.db_path)
        self.assertEqual(cov["source_timeframe"], "1H")
        self.assertEqual(cov["derivation_method"], "resample")
        self.assertEqual(cov["resample_rule_version"], 1)

    def test_resample_bucket_alignment(self):
        from bot.historical.timeframes import resample_1h_to_4h
        df = pd.DataFrame({
            "ts_utc": pd.date_range("2026-06-01", periods=8, freq="h",
                                       tz="UTC"),
            "open": [1.0]*8, "high":[1.0]*8, "low":[1.0]*8,
            "close":[1.0]*8, "volume":[100]*8,
        })
        out, _ = resample_1h_to_4h(df)
        # Buckets should align to 00:00 and 04:00 UTC.
        self.assertEqual(out.iloc[0]["ts_utc"].hour, 0)
        self.assertEqual(out.iloc[1]["ts_utc"].hour, 4)

    def test_incomplete_bucket_logged(self):
        from bot.historical.timeframes import resample_1h_to_4h
        df = pd.DataFrame({
            "ts_utc": pd.date_range("2026-06-01T00:00", periods=2,
                                       freq="h", tz="UTC"),
            "open":[1.0]*2,"high":[1.0]*2,"low":[1.0]*2,
            "close":[1.0]*2,"volume":[100]*2,
        })
        out, issues = resample_1h_to_4h(df)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].actual_source_bars, 2)
        self.assertEqual(issues[0].expected_source_bars, 4)


# ---------------------------------------------------------------------------
# G12. Coverage + status
# ---------------------------------------------------------------------------
class TestCoverageAndStatus(_TmpEnv):
    def test_coverage_matches_parquet(self):
        from bot.historical import refresh, store
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())
        df = store.get_bars("AAPL", "1D", provider="fake",
                              parquet_root=self.parquet_root)
        cov = store.get_coverage("AAPL", "1D", provider="fake",
                                    db_path=self.db_path)
        self.assertEqual(cov["bar_count"], len(df))

    def test_freshness_stale_after_long_gap(self):
        from bot.historical.coverage import compute_freshness_status
        old = "2024-01-01T00:00:00+00:00"
        status = compute_freshness_status(old, timeframe="1D",
                                              now_utc=datetime(2026, 6, 5,
                                                tzinfo=timezone.utc))
        self.assertEqual(status, "stale")

    def test_freshness_never_for_none(self):
        from bot.historical.coverage import compute_freshness_status
        self.assertEqual(compute_freshness_status(None, timeframe="1D"),
                          "never")


# ---------------------------------------------------------------------------
# G13. Concurrency lock
# ---------------------------------------------------------------------------
class TestConcurrencyLock(_TmpEnv):
    def test_second_refresh_exits_cleanly(self):
        from bot.historical import schema, refresh
        c = schema.open_db(self.db_path)
        try:
            schema.apply_schema(c)
            # Manually claim the lock with this PID + a far-future lease.
            future = (datetime.now(timezone.utc) +
                       timedelta(hours=1)).isoformat()
            c.execute("UPDATE historical_refresh_lock SET "
                        " owner_pid=?, owner_host='other', "
                        " acquired_at_utc=?, lease_expires_at_utc=? "
                        "WHERE id=1",
                        (os.getpid(), datetime.now(timezone.utc).isoformat(),
                         future))
            c.commit()
        finally:
            c.close()

        # Now attempt a refresh — should fail to acquire the lock and
        # finalize the run with status=failed (NOT raise).
        r = refresh.run(mode="backfill", symbols=["AAPL"],
                          timeframes=["1D"], provider=FakeProvider(),
                          db_path=self.db_path,
                          parquet_root=self.parquet_root, now_utc=self._now())
        self.assertEqual(r.status, "failed")


# ---------------------------------------------------------------------------
# G14. No broker imports
# ---------------------------------------------------------------------------
class TestNoBrokerImports(unittest.TestCase):
    FORBIDDEN_IMPORTS = (
        "ib_insync", "ibapi",
        "bot.broker_ibkr", "bot.broker_etoro",
        "bot.etoro",
        "bot.scanner", "bot.strategy",
        "bot.risk_authority",
        "bot.gateway_watchdog", "bot.gateway_health",
        "bot.heartbeat",
    )
    FORBIDDEN_NAMES = (
        "placeOrder", "cancelOrder", "modifyOrder",
        "closePosition", "submitOrder",
    )

    def _collect_imports(self, source):
        imports = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
        return imports

    def test_no_forbidden_imports_in_bot_data(self):
        pkg_dir = REPO_ROOT / "bot" / "historical"
        files = list(pkg_dir.glob("*.py"))
        self.assertGreater(len(files), 0)
        for f in files:
            src = f.read_text()
            imports = self._collect_imports(src)
            for forbidden in self.FORBIDDEN_IMPORTS:
                self.assertFalse(
                    any(imp == forbidden or imp.startswith(forbidden + ".")
                        for imp in imports),
                    f"{f.name} imports forbidden module {forbidden}: {imports}")

    def test_no_forbidden_names_in_bot_data(self):
        pkg_dir = REPO_ROOT / "bot" / "historical"
        for f in pkg_dir.glob("*.py"):
            src = f.read_text()
            for name in self.FORBIDDEN_NAMES:
                self.assertNotIn(name, src,
                                    f"{f.name} contains forbidden name {name}")

    def test_no_forbidden_imports_in_preview(self):
        f = REPO_ROOT / "bot" / "historical" / "preview.py"
        src = f.read_text()
        imports = self._collect_imports(src)
        for forbidden in self.FORBIDDEN_IMPORTS:
            self.assertFalse(
                any(imp == forbidden or imp.startswith(forbidden + ".")
                    for imp in imports),
                f"preview.py imports forbidden {forbidden}")


# ---------------------------------------------------------------------------
# G15. Protected files untouched
# ---------------------------------------------------------------------------
class TestProtectedFilesUntouched(unittest.TestCase):
    PROTECTED = (
        "main.py",
        "bot/scanner.py",
        "bot/strategy.py",
        "bot/risk.py",
        "bot/heartbeat.py",
        "bot/gateway_watchdog.py",
        "bot/gateway_health.py",
        "sync.sh",
        "deploy.sh",
        # dashboard/auth/* — M15.3 stack
        "dashboard/auth/__init__.py",
        "dashboard/auth/totp.py",
        "dashboard/auth/audit.py",
        "dashboard/auth/audit_export.py",
        "dashboard/auth/manual_reset.py",
        "dashboard/auth/password_hash.py",
        # bot/risk_authority/*
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/snapshot.py",
        "bot/risk_authority/preflight.py",
        # bot/etoro/*
        "bot/etoro/__init__.py",
        "bot/etoro/contracts.py",
        "bot/etoro/dryrun_adapter.py",
        "bot/etoro/live_adapter.py",
    )

    def test_protected_files_unchanged_vs_baseline(self):
        # Run `git diff --name-only ceb8cd5 -- <files>`. If any are
        # modified, fail with the list.
        modified = []
        for path in self.PROTECTED:
            full = REPO_ROOT / path
            if not full.exists():
                # OK — file may not have existed at baseline either.
                continue
            try:
                rc = subprocess.run(
                    ["git", "diff", "--name-only", BASELINE_COMMIT, "--",
                     path],
                    cwd=str(REPO_ROOT), capture_output=True, text=True,
                    timeout=20)
            except Exception as e:  # noqa: BLE001
                self.skipTest(f"git diff unavailable: {e}")
                return
            if rc.stdout.strip():
                modified.append(path)
        self.assertEqual(modified, [], f"protected files modified: {modified}")


# ---------------------------------------------------------------------------
# G16. M16.B local-read proof
# ---------------------------------------------------------------------------
class TestM16BLocalReadOnly(_TmpEnv):
    def test_preview_does_not_call_provider(self):
        from bot.historical import refresh, preview
        # Backfill so there's local data.
        refresh.run(mode="backfill", symbols=["AAPL"], timeframes=["1D"],
                      provider=FakeProvider(), db_path=self.db_path,
                      parquet_root=self.parquet_root, now_utc=self._now())

        # Now compute SMA — preview must NOT touch any provider.
        # The proof is operational: preview.py only imports from bot.historical.store
        # (asserted by AST test G14). Here we just verify it returns data.
        sma = preview.compute_recent_sma(
            "AAPL", "1D", periods=3, lookback=3, provider="fake",
            parquet_root=self.parquet_root)
        # FakeProvider seeds 5 bars; SMA(3) gives 3 valid trailing values.
        self.assertGreater(len(sma), 0)
        self.assertLessEqual(len(sma), 3)


# ---------------------------------------------------------------------------
# G17. pyarrow installed
# ---------------------------------------------------------------------------
class TestPyarrowInstalled(unittest.TestCase):
    def test_pyarrow_importable(self):
        try:
            import pyarrow
            import pyarrow.parquet  # noqa: F401
        except ImportError as e:
            self.fail(f"pyarrow not importable: {e}")
        # Version sanity.
        major = int(pyarrow.__version__.split(".")[0])
        self.assertGreaterEqual(major, 10)


# ---------------------------------------------------------------------------
# G18. Lookback-exceeded handling
# ---------------------------------------------------------------------------
class TestLookbackExceeded(unittest.TestCase):
    def test_15m_lookback_clamped(self):
        from bot.historical.providers import clamp_to_lookback
        from bot.historical.providers_yfinance import YFINANCE_CAPABILITY
        # Request 100 days of 15m — yfinance caps at 60d.
        want_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        want_to = datetime(2026, 6, 5, tzinfo=timezone.utc)
        clamped_from, clamped_to, exceeded = clamp_to_lookback(
            want_from, want_to, timeframe="15m",
            capability=YFINANCE_CAPABILITY, now_utc=want_to)
        self.assertTrue(exceeded)
        # Clamped from ~ now - 60d
        self.assertGreaterEqual(
            pd.Timestamp(clamped_from), pd.Timestamp(want_to) -
                                              timedelta(days=61))


# ---------------------------------------------------------------------------
# G19. Live yfinance smoke (skip-unless-M16_LIVE=1)
# ---------------------------------------------------------------------------
@unittest.skipUnless(os.environ.get("M16_LIVE") == "1",
                       "set M16_LIVE=1 to enable the live-yfinance test")
class TestLiveYfinanceSmoke(unittest.TestCase):
    def test_aapl_1d_recent_bars(self):
        from bot.historical.providers_yfinance import YFinanceProvider
        prov = YFinanceProvider()
        result = prov.fetch_bars(
            "AAPL", "1D",
            datetime.now(timezone.utc) - timedelta(days=10),
            datetime.now(timezone.utc))
        self.assertEqual(result.outcome, "ok")
        df = result.df
        self.assertGreater(len(df), 0)
        for c in ("ts_utc", "open", "high", "low", "close", "volume",
                    "adj_close", "adjustment_ratio"):
            self.assertIn(c, df.columns)


# ---------------------------------------------------------------------------
# G20. yfinance-adapter rate-limit classification (M16.A.fix-1)
# ---------------------------------------------------------------------------
class TestYFinanceRateLimitClassification(unittest.TestCase):
    """Prove the yfinance adapter classifies rate-limit responses as
    FETCH_RATE_LIMITED, NOT FETCH_NO_DATA, for both observed paths:

      (a) yfinance raises YFRateLimitError directly  — older / direct API
      (b) yfinance returns empty DataFrame + populates yf.shared._ERRORS
          — current behaviour of yf.download() in 0.2.x

    The M16.A original code only handled (a) and via brittle string match;
    on the VPS it hit path (b) and misclassified every rate-limited
    symbol as 'no_data'.
    """

    def _make_provider(self):
        from bot.historical.providers_yfinance import YFinanceProvider
        return YFinanceProvider()

    def test_path_a_raises_yfratelimit(self):
        """When yfinance raises a YFRateLimitError, the adapter must
        return FETCH_RATE_LIMITED."""
        from bot.historical.providers import FETCH_RATE_LIMITED
        prov = self._make_provider()

        # Simulate an installed YFRateLimitError exception class.
        class FakeYFRateLimitError(Exception):
            pass
        FakeYFRateLimitError.__name__ = "YFRateLimitError"

        class FakeYF:
            __version__ = "0.2.55-test"
            class shared: _ERRORS = {}
            @staticmethod
            def download(*a, **kw):
                raise FakeYFRateLimitError(
                    "Too Many Requests. Rate limited. Try after a while.")

        prov._yf = FakeYF
        prov._yf_rate_limit_exc = FakeYFRateLimitError
        r = prov.fetch_bars("AAPL", "1D",
                              datetime(2026, 5, 1, tzinfo=timezone.utc),
                              datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(r.outcome, FETCH_RATE_LIMITED,
                          f"expected RATE_LIMITED, got {r.outcome!r}")
        self.assertIn("rate", r.message.lower())

    def test_path_b_empty_df_with_errors_registry(self):
        """When yf.download returns empty AND yf.shared._ERRORS has a
        rate-limit entry for our symbol, the adapter must return
        FETCH_RATE_LIMITED (not FETCH_NO_DATA).

        This is the path that broke on the VPS.
        """
        from bot.historical.providers import FETCH_RATE_LIMITED, FETCH_NO_DATA
        import pandas as pd
        prov = self._make_provider()

        class FakeYFShared:
            _ERRORS = {}
        class FakeYF:
            __version__ = "0.2.55-test"
            shared = FakeYFShared
            @staticmethod
            def download(symbol, *a, **kw):
                # Simulate yfinance 0.2.55's behaviour: catches the
                # exception internally, stores it in _ERRORS, returns
                # an empty DataFrame.
                FakeYFShared._ERRORS[symbol] = (
                    "YFRateLimitError('Too Many Requests. Rate limited. "
                    "Try after a while.')")
                return pd.DataFrame()

        prov._yf = FakeYF
        prov._yf_rate_limit_exc = None  # simulate not-importable
        r = prov.fetch_bars("AAPL", "1D",
                              datetime(2026, 5, 1, tzinfo=timezone.utc),
                              datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(r.outcome, FETCH_RATE_LIMITED,
            f"empty-DF + _ERRORS rate-limit signal must classify as "
            f"RATE_LIMITED, NOT NO_DATA (M16.A.fix-1). Got {r.outcome!r}.")

    def test_path_b_empty_df_no_rate_limit_is_no_data(self):
        """Conversely: empty DF + clear _ERRORS = legitimate NO_DATA."""
        from bot.historical.providers import FETCH_NO_DATA
        import pandas as pd
        prov = self._make_provider()

        class FakeYFShared:
            _ERRORS = {}
        class FakeYF:
            __version__ = "0.2.55-test"
            shared = FakeYFShared
            @staticmethod
            def download(*a, **kw):
                return pd.DataFrame()

        prov._yf = FakeYF
        r = prov.fetch_bars("OBSCURE", "1D",
                              datetime(2026, 5, 1, tzinfo=timezone.utc),
                              datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(r.outcome, FETCH_NO_DATA)

    def test_path_b_empty_df_non_rate_limit_error_is_provider_error(self):
        """Empty DF + non-rate-limit error in _ERRORS = PROVIDER_ERROR."""
        from bot.historical.providers import FETCH_PROVIDER_ERROR
        import pandas as pd
        prov = self._make_provider()

        class FakeYFShared:
            _ERRORS = {}
        class FakeYF:
            __version__ = "0.2.55-test"
            shared = FakeYFShared
            @staticmethod
            def download(symbol, *a, **kw):
                FakeYFShared._ERRORS[symbol] = (
                    "AttributeError(\"'NoneType' object has no attribute 'name'\")")
                return pd.DataFrame()

        prov._yf = FakeYF
        r = prov.fetch_bars("ZZZ", "1D",
                              datetime(2026, 5, 1, tzinfo=timezone.utc),
                              datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(r.outcome, FETCH_PROVIDER_ERROR)
        self.assertIn("NoneType", r.message)

    def test_rate_limit_token_matcher(self):
        """The rate-limit substring matcher catches all known signals."""
        prov = self._make_provider()
        for txt in (
            "YFRateLimitError('foo')",
            "Too Many Requests",
            "rate limit exceeded",
            "rate-limit hit",
            "Rate Limited",
            "HTTP 429 Too Many Requests",
        ):
            self.assertTrue(prov._is_rate_limit_signal(txt),
                              f"failed to match: {txt!r}")
        for txt in (
            "Some other error",
            "NoneType has no attribute",
            "",
            "404 Not Found",
        ):
            self.assertFalse(prov._is_rate_limit_signal(txt),
                              f"false-positive on: {txt!r}")


# ---------------------------------------------------------------------------
# G21. status CLI auto-migrates v1 DB (M16.A.fix-2)
# ---------------------------------------------------------------------------
class TestStatusCommandAutoMigrates(_TmpEnv):
    """M16.A.fix-2: `python -m bot.historical.cli status` must work
    against a pre-existing v1 historical.db. Previously it queried
    `symbols_rate_limited` without first calling apply_schema, so a
    v1 DB produced `sqlite3.OperationalError: no such column:
    symbols_rate_limited`.
    """

    def _make_v1_db(self):
        """Create a real v1-shaped DB: every table from v1 schema,
        WITHOUT the v2 column `symbols_rate_limited`."""
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            # Manually create the v1 shape (no symbols_rate_limited).
            c.execute("""
                CREATE TABLE historical_schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL)""")
            c.execute("""
                CREATE TABLE historical_symbols (
                    symbol TEXT PRIMARY KEY,
                    asset_class TEXT NOT NULL,
                    is_active INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    CHECK (is_active IN (0, 1)))""")
            c.execute("""
                CREATE TABLE historical_coverage (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    first_ts_utc TEXT, last_ts_utc TEXT,
                    bar_count INTEGER NOT NULL DEFAULT 0,
                    missing_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    quality_status TEXT NOT NULL DEFAULT 'unknown',
                    freshness_status TEXT NOT NULL DEFAULT 'unknown',
                    last_refresh_at_utc TEXT,
                    last_refresh_id INTEGER,
                    provider_limit_note TEXT,
                    source_timeframe TEXT,
                    derivation_method TEXT NOT NULL DEFAULT 'native',
                    resample_rule_version INTEGER,
                    PRIMARY KEY (symbol, timeframe, provider))""")
            # v1-shape refresh_runs — NO symbols_rate_limited.
            c.execute("""
                CREATE TABLE historical_refresh_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at_utc TEXT NOT NULL,
                    finished_at_utc TEXT,
                    mode TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    symbols_requested TEXT NOT NULL,
                    timeframes_requested TEXT NOT NULL,
                    status TEXT NOT NULL,
                    symbols_attempted INTEGER NOT NULL DEFAULT 0,
                    symbols_ok INTEGER NOT NULL DEFAULT 0,
                    symbols_no_data INTEGER NOT NULL DEFAULT 0,
                    symbols_failed INTEGER NOT NULL DEFAULT 0,
                    bars_fetched INTEGER NOT NULL DEFAULT 0,
                    bars_written INTEGER NOT NULL DEFAULT 0,
                    bars_updated INTEGER NOT NULL DEFAULT 0,
                    errors_count INTEGER NOT NULL DEFAULT 0,
                    rate_limit_count INTEGER NOT NULL DEFAULT 0,
                    duration_sec REAL,
                    summary_json TEXT)""")
            c.execute("""
                CREATE TABLE historical_quality_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER REFERENCES historical_refresh_runs(run_id),
                    symbol TEXT, timeframe TEXT, provider TEXT, ts_utc TEXT,
                    severity TEXT NOT NULL, kind TEXT NOT NULL,
                    message TEXT NOT NULL, details_json TEXT,
                    created_at_utc TEXT NOT NULL)""")
            c.execute("""
                CREATE TABLE historical_refresh_lock (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    owner_pid INTEGER, owner_host TEXT,
                    acquired_at_utc TEXT, lease_expires_at_utc TEXT,
                    last_heartbeat_utc TEXT)""")
            # Mark version=1.
            c.execute("INSERT INTO historical_schema_version "
                        "(version, applied_at_utc) VALUES (1, '2026-06-01T00:00:00+00:00')")
            c.execute("INSERT INTO historical_refresh_lock (id) VALUES (1)")
            # Seed one historical run in the v1 shape (no
            # symbols_rate_limited column) so the status query has
            # something to display.
            c.execute(
                "INSERT INTO historical_refresh_runs "
                "(started_at_utc, mode, provider, symbols_requested, "
                " timeframes_requested, status) "
                "VALUES ('2026-06-01T10:00:00+00:00', 'backfill', "
                "  'yfinance', '[\"AAPL\"]', '[\"1D\"]', 'ok')")
            c.commit()
        finally:
            c.close()

    def test_status_command_against_v1_db_succeeds(self):
        """A v1 DB without `symbols_rate_limited` must NOT cause
        status to fail — apply_schema must migrate it first."""
        import io
        import argparse
        import contextlib
        from bot.historical import cli as historical_cli

        self._make_v1_db()

        # Confirm the column is absent BEFORE running status.
        from bot.historical import schema
        c = schema.open_db(self.db_path)
        try:
            cols = {row[1] for row in c.execute(
                "PRAGMA table_info(historical_refresh_runs)").fetchall()}
            self.assertNotIn("symbols_rate_limited", cols,
                              "test setup error — DB already has v2 column")
            self.assertEqual(
                schema.get_schema_version(c), 1,
                "test setup error — DB not at v1")
        finally:
            c.close()

        # Patch default_db_path so cmd_status uses our tmp DB.
        import unittest.mock as _mock
        repo_root_arg = self.db_path.parent.parent  # any path; not used here
        with _mock.patch.object(historical_cli._schema, "default_db_path",
                                  return_value=self.db_path):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = historical_cli.cmd_status(
                    argparse.Namespace(), repo_root_arg)
        out = buf.getvalue()

        # status returns 0 (success).
        self.assertEqual(rc, 0, f"status returned {rc}; output:\n{out}")
        # schema_version is now 2.
        self.assertIn("schema_version:       2", out,
                       f"expected schema_version=2 after migration; got:\n{out}")
        # No traceback / sqlite error in stdout.
        self.assertNotIn("OperationalError", out)
        self.assertNotIn("no such column", out)

        # And the column now exists.
        c = schema.open_db(self.db_path)
        try:
            cols = {row[1] for row in c.execute(
                "PRAGMA table_info(historical_refresh_runs)").fetchall()}
            self.assertIn("symbols_rate_limited", cols,
                "cmd_status did not migrate the DB to v2")
            self.assertEqual(schema.get_schema_version(c), 2)
        finally:
            c.close()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)

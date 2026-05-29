"""M14.D — Exposure / Position / Capital Engine test suite.

Covers the 10 ChatGPT corrections + the user's explicit checklist:

  1.  Batch identifier model (exposure_batch_id shared per-run).
  2.  AST no-write + no-order safety on every adapter and the CLI.
  3.  IBKR adapter discipline: injected reader, no order methods,
      strict per-position validation, FX rule.
  4.  eToro adapter discipline: M13.2 read surface only, strict
      per-position validation, no invented fields.
  5.  Cross-engine separation: M14.C-owned PnL columns and lifecycle
      keys remain byte-identical after an M14.D ingest (and vice versa).
  6.  Dry-run does not write to real `daily_state_per_broker` OR to
      real `broker_positions`, and explicitly inits its in-memory DB.
  7.  Currency / FX strict rule.
  8.  Scanner isolation: scanner/strategy/risk/brokers do NOT import
      any M14.D exposure module.
  9.  Known-zero vs unknown exposure distinguishable in DB.
  10. Malformed same-snapshot position => whole reading UNKNOWN.

No live calls, no eToro write endpoint contact, no order placed.
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

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from bot.flywheel import (
    M14_D_SCHEMA_VERSION,
    M14_D_SENTINEL_KEY,
    ensure_broker_positions_migration,
    init_flywheel_tables,
)
from bot.risk_authority.exposure_reading import (
    BrokerExposureReading,
    ExposureQuality,
    OPPORTUNISTIC_EXPOSURE,
    Position,
    REQUIRED_FOR_FRESH_EXPOSURE,
    _is_real_number,
    make_unknown_exposure,
)
from bot.risk_authority.ingest_etoro_exposure import EtoroExposureAdapter
from bot.risk_authority.ingest_exposure import (
    INGESTIBLE_SCOPES,
    ingest_exposure_all_scopes,
    ingest_exposure_once,
)
from bot.risk_authority.ingest_ibkr_exposure import IBKRExposureAdapter
from bot.risk_authority.ingest import ingest_once as ingest_pnl_once
from bot.risk_authority.reading import BrokerPnLReading

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class _DB:
    """Temp SQLite fixture."""

    def __init__(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        f.close()
        self.path = f.name
        with self.conn() as c:
            init_flywheel_tables(c)

    def conn(self):
        return sqlite3.connect(self.path)

    def cleanup(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _ibkr_pos(symbol="AAPL", side="long", qty=10.0, exposure_usd=2000.0,
              currency=None, mark_price=None, avg_price=None,
              broker_usd=None, unr_pnl=None, opened_at=None,
              instrument_id=None):
    d = {"symbol": symbol, "side": side, "qty": qty}
    if exposure_usd is not None:
        d["exposure_usd"] = exposure_usd
    if currency is not None:
        d["currency"] = currency
    if mark_price is not None:
        d["mark_price"] = mark_price
    if avg_price is not None:
        d["avg_price"] = avg_price
    if broker_usd is not None:
        d["broker_provided_usd_notional"] = broker_usd
    if unr_pnl is not None:
        d["unrealised_pnl_usd"] = unr_pnl
    if opened_at is not None:
        d["opened_at"] = opened_at
    if instrument_id is not None:
        d["instrument_id"] = instrument_id
    return d


def _etoro_pos(units=10.0, rate=200.0, is_buy=True, instrument_id=1000,
               symbol=None, amount=None, broker_usd=None, profit=None,
               open_rate=None, open_dt=None):
    d = {"units": units, "rate": rate, "isBuy": is_buy,
         "instrumentID": instrument_id}
    if symbol is not None:
        d["symbol"] = symbol
    if amount is not None:
        d["amount"] = amount
    if broker_usd is not None:
        d["broker_provided_usd_notional"] = broker_usd
    if profit is not None:
        d["profit"] = profit
    if open_rate is not None:
        d["openRate"] = open_rate
    if open_dt is not None:
        d["openDateTime"] = open_dt
    return d


class _FakePortfolioSnapshot:
    """Mimics EtoroReadAdapter.PortfolioSnapshot for tests."""
    def __init__(self, positions, credit=None, unrealized_pnl=None):
        self.positions = positions
        self.credit = credit
        self.unrealized_pnl = unrealized_pnl


# ─────────────────────────────────────────────────────────────────────────────
# 1. BrokerExposureReading + quality classification
# ─────────────────────────────────────────────────────────────────────────────


class TestExposureReadingQuality(unittest.TestCase):

    def test_required_and_opportunistic_lists_are_disjoint(self):
        self.assertEqual(
            set(REQUIRED_FOR_FRESH_EXPOSURE) & set(OPPORTUNISTIC_EXPOSURE),
            set(),
        )

    def test_is_real_number_rejects_bool_nan_inf(self):
        self.assertFalse(_is_real_number(True))
        self.assertFalse(_is_real_number(False))
        self.assertFalse(_is_real_number(None))
        self.assertFalse(_is_real_number("5"))
        self.assertFalse(_is_real_number(float("nan")))
        self.assertFalse(_is_real_number(float("inf")))
        self.assertFalse(_is_real_number(float("-inf")))
        self.assertTrue(_is_real_number(5))
        self.assertTrue(_is_real_number(5.0))
        self.assertTrue(_is_real_number(-0.0))

    def test_fresh_with_all_required_and_opportunistic(self):
        r = BrokerExposureReading(
            broker_scope="etoro_real", trading_day="2026-05-29",
            fetched_at_utc="2026-05-29T10:00:00Z", data_source_success=True,
            positions=[], open_positions_count=0,
            capital_deployed_usd=0.0,
            unrealised_pnl_usd=0.0, current_equity_usd=1000.0,
            peak_equity_usd=1000.0,
        )
        self.assertEqual(r.quality(), ExposureQuality.FRESH)

    def test_partial_when_only_opportunistic_missing(self):
        r = BrokerExposureReading(
            broker_scope="etoro_real", trading_day="2026-05-29",
            fetched_at_utc="2026-05-29T10:00:00Z", data_source_success=True,
            positions=[], open_positions_count=0,
            capital_deployed_usd=0.0,
        )
        self.assertEqual(r.quality(), ExposureQuality.PARTIAL)
        self.assertTrue(r.has_fresh_exposure())

    def test_unknown_when_required_missing(self):
        r = make_unknown_exposure("etoro_real", trading_day="2026-05-29",
                                  error="boom")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertFalse(r.has_fresh_exposure())

    def test_known_zero_vs_unknown_zero_distinguishable(self):
        # Known zero: success=True, empty list, count=0, capital=0.0.
        kz = BrokerExposureReading(
            broker_scope="etoro_real", trading_day="2026-05-29",
            fetched_at_utc="2026-05-29T10:00:00Z", data_source_success=True,
            positions=[], open_positions_count=0, capital_deployed_usd=0.0,
        )
        # Unknown: numeric defaults all None, success=False.
        uk = make_unknown_exposure("etoro_real", trading_day="2026-05-29",
                                   error="boom")
        self.assertTrue(kz.is_known_zero_exposure())
        self.assertFalse(uk.is_known_zero_exposure())
        self.assertEqual(uk.quality(), ExposureQuality.UNKNOWN)


# ─────────────────────────────────────────────────────────────────────────────
# 2. IBKR exposure adapter
# ─────────────────────────────────────────────────────────────────────────────


class TestIBKRExposureAdapter(unittest.TestCase):

    def test_healthy_day_with_positions_fresh_or_partial(self):
        positions = [
            _ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                      exposure_usd=None),
            _ibkr_pos("MSFT", "long", 5.0, mark_price=400.0,
                      exposure_usd=None),
        ]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r.open_positions_count, 2)
        self.assertEqual(r.capital_deployed_usd, 2000.0 + 2000.0)

    def test_empty_positions_list_is_known_zero(self):
        a = IBKRExposureAdapter("ibkr_paper", positions_reader=lambda: [])
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r.capital_deployed_usd, 0.0)
        self.assertEqual(r.open_positions_count, 0)
        self.assertTrue(r.is_known_zero_exposure())

    def test_reader_failure_returns_unknown_never_raises(self):
        def boom():
            raise RuntimeError("gateway down")
        a = IBKRExposureAdapter("ibkr_live", positions_reader=boom)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("positions_reader_failed", r.error)

    def test_any_position_missing_symbol_makes_whole_reading_unknown(self):
        positions = [
            _ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                      exposure_usd=None),
            {"side": "long", "qty": 5.0, "mark_price": 100.0},   # no symbol
        ]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("missing_symbol", r.error)

    def test_any_position_non_numeric_exposure_makes_whole_reading_unknown(self):
        positions = [
            _ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                      exposure_usd=None),
            _ibkr_pos("XYZ", "long", "nope", exposure_usd=None,
                      mark_price=100.0),
        ]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("qty_non_numeric", r.error)

    def test_position_with_no_mark_or_avg_is_malformed_unknown(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=None,
                               avg_price=None, exposure_usd=None)]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("no_mark_or_avg_price", r.error)

    def test_avg_cost_fallback_when_mark_missing(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=None,
                               avg_price=150.0, exposure_usd=None)]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r.capital_deployed_usd, 1500.0)
        self.assertEqual(
            r.positions[0].raw_evidence["mark_source"], "avg_cost_fallback"
        )

    def test_non_usd_without_broker_usd_is_unknown(self):
        positions = [_ibkr_pos("VOD", "long", 100.0, mark_price=80.0,
                               currency="GBP", exposure_usd=None)]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("non_usd_without_fx", r.error)

    def test_non_usd_with_broker_usd_notional_accepted(self):
        positions = [_ibkr_pos("VOD", "long", 100.0, currency="GBP",
                               broker_usd=10000.0)]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r.capital_deployed_usd, 10000.0)
        self.assertEqual(
            r.positions[0].raw_evidence["mark_source"], "broker_usd_notional"
        )

    def test_bool_qty_rejected(self):
        positions = [_ibkr_pos("AAPL", "long", True, mark_price=100.0,
                               exposure_usd=None)]
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)

    def test_account_reader_failure_is_opportunistic_not_unknown(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        def acct_boom():
            raise RuntimeError("acct down")
        a = IBKRExposureAdapter("ibkr_live",
                                positions_reader=lambda: positions,
                                account_reader=acct_boom)
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIsNone(r.current_equity_usd)

    def test_invalid_scope_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            IBKRExposureAdapter("etoro_real", positions_reader=lambda: [])


# ─────────────────────────────────────────────────────────────────────────────
# 3. eToro exposure adapter
# ─────────────────────────────────────────────────────────────────────────────


class TestEtoroExposureAdapter(unittest.TestCase):

    def test_healthy_day_with_positions(self):
        snap = _FakePortfolioSnapshot(
            positions=[
                _etoro_pos(units=2.0, rate=100.0, is_buy=True,
                           instrument_id=1000, amount=200.0),
                _etoro_pos(units=1.0, rate=50.0, is_buy=False,
                           instrument_id=1001, amount=50.0),
            ],
            credit=1000.0, unrealized_pnl=5.0,
        )
        a = EtoroExposureAdapter("etoro_real",
                                 portfolio_reader=lambda: snap)
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r.open_positions_count, 2)
        self.assertEqual(r.capital_deployed_usd, 250.0)
        self.assertEqual(r.current_equity_usd, 1000.0)
        self.assertEqual(r.unrealised_pnl_usd, 5.0)
        # Position 2 was a short: side='short'.
        sides = sorted(p.side for p in r.positions)
        self.assertEqual(sides, ["long", "short"])

    def test_empty_positions_list_is_known_zero(self):
        snap = _FakePortfolioSnapshot(positions=[])
        a = EtoroExposureAdapter("etoro_real",
                                 portfolio_reader=lambda: snap)
        r = a.read(today="2026-05-29")
        self.assertNotEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertTrue(r.is_known_zero_exposure())

    def test_reader_failure_classified(self):
        def boom_keys():
            raise RuntimeError("keys missing for etoro: keys_absent")
        a = EtoroExposureAdapter(
            "etoro_real", portfolio_reader=boom_keys)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r.error, "keys_absent")

        def boom_401():
            raise RuntimeError("401 Unauthorized")
        a2 = EtoroExposureAdapter(
            "etoro_real", portfolio_reader=boom_401)
        r2 = a2.read(today="2026-05-29")
        self.assertEqual(r2.quality(), ExposureQuality.UNKNOWN)
        self.assertEqual(r2.error, "auth_unavailable")

    def test_any_position_missing_units_is_unknown(self):
        snap = _FakePortfolioSnapshot(positions=[
            _etoro_pos(units=2.0, rate=100.0, instrument_id=1000),
            {"isBuy": True, "rate": 100.0, "instrumentID": 1001},  # no units
        ])
        a = EtoroExposureAdapter("etoro_real",
                                 portfolio_reader=lambda: snap)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("units_non_numeric", r.error)

    def test_position_with_no_rate_and_no_amount_is_unknown(self):
        snap = _FakePortfolioSnapshot(positions=[
            {"units": 2.0, "isBuy": True, "instrumentID": 1000},
            # No rate, no amount → no way to derive USD.
        ])
        a = EtoroExposureAdapter("etoro_real",
                                 portfolio_reader=lambda: snap)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("no_usd_notional_or_rate", r.error)

    def test_position_missing_isBuy_is_unknown(self):
        snap = _FakePortfolioSnapshot(positions=[
            {"units": 2.0, "rate": 100.0, "instrumentID": 1000},
        ])
        a = EtoroExposureAdapter("etoro_real",
                                 portfolio_reader=lambda: snap)
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)
        self.assertIn("missing_isBuy", r.error)

    def test_snapshot_without_positions_field_is_unknown(self):
        class Empty:
            pass
        a = EtoroExposureAdapter("etoro_real",
                                 portfolio_reader=lambda: Empty())
        r = a.read(today="2026-05-29")
        self.assertEqual(r.quality(), ExposureQuality.UNKNOWN)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError):
            EtoroExposureAdapter("ibkr_live", portfolio_reader=lambda: None)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Schema / migration
# ─────────────────────────────────────────────────────────────────────────────


class TestSchema(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_broker_positions_table_exists_with_expected_columns(self):
        with self.fx.conn() as c:
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(broker_positions)"
            ).fetchall()}
        expected = {
            "position_row_id", "exposure_batch_id", "broker_scope",
            "date", "fetched_at_utc", "symbol", "side", "qty",
            "exposure_usd", "avg_price", "mark_price",
            "unrealised_pnl_usd", "opened_at", "instrument_id",
            "raw_evidence", "created_at",
        }
        self.assertTrue(expected.issubset(cols),
                        f"missing columns: {expected - cols}")

    def test_position_row_id_is_pk(self):
        with self.fx.conn() as c:
            info = c.execute(
                "PRAGMA table_info(broker_positions)"
            ).fetchall()
        pk = [(r[1], r[5]) for r in info if r[5] > 0]
        self.assertEqual(len(pk), 1)
        self.assertEqual(pk[0][0], "position_row_id")

    def test_expected_indexes_present(self):
        with self.fx.conn() as c:
            idx = {r[1] for r in c.execute(
                "PRAGMA index_list(broker_positions)"
            ).fetchall()}
        for needed in ("ix_bp_scope_date_fetched",
                       "ix_bp_scope_batch", "ix_bp_symbol"):
            self.assertIn(needed, idx)

    def test_invalid_broker_scope_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO broker_positions ("
                    " exposure_batch_id, broker_scope, date, "
                    " fetched_at_utc, symbol, side, qty, exposure_usd, "
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("b1", "fake_scope", "2026-05-29",
                     "2026-05-29T10:00:00Z", "AAPL", "long",
                     10.0, 1000.0, "now"),
                )

    def test_invalid_side_rejected(self):
        with self.fx.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO broker_positions ("
                    " exposure_batch_id, broker_scope, date, "
                    " fetched_at_utc, symbol, side, qty, exposure_usd, "
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("b1", "ibkr_live", "2026-05-29",
                     "2026-05-29T10:00:00Z", "AAPL", "sideways",
                     10.0, 1000.0, "now"),
                )

    def test_migration_idempotent(self):
        with self.fx.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM broker_positions"
            ).fetchone()[0]
            ensure_broker_positions_migration(c)
            ensure_broker_positions_migration(c)
            after = c.execute(
                "SELECT COUNT(*) FROM broker_positions"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_sentinel_written(self):
        with self.fx.conn() as c:
            row = c.execute(
                "SELECT value FROM portfolio_risk_state WHERE key=?",
                (M14_D_SENTINEL_KEY,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], str(M14_D_SCHEMA_VERSION))

    def test_sentinel_does_not_hide_missing_ddl(self):
        with self.fx.conn() as c:
            c.execute("DROP TABLE broker_positions")
            ensure_broker_positions_migration(c)
            row = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='broker_positions'"
            ).fetchone()
        self.assertIsNotNone(row)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Orchestrator UPSERT + batch ID + hysteresis + drawdown
# ─────────────────────────────────────────────────────────────────────────────


class TestOrchestrator(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def _ibkr_adapter(self, positions, equity=None):
        def pr():
            return positions
        if equity is None:
            return IBKRExposureAdapter("ibkr_live", positions_reader=pr)
        def ar():
            return {"equity_usd": equity}
        return IBKRExposureAdapter("ibkr_live",
                                    positions_reader=pr, account_reader=ar)

    def _unknown_adapter(self):
        def pr():
            raise RuntimeError("gateway down")
        return IBKRExposureAdapter("ibkr_live", positions_reader=pr)

    def _row(self, today, scope):
        with self.fx.conn() as c:
            r = c.execute(
                "SELECT open_positions, capital_deployed, peak_equity, "
                "       drawdown_from_peak, lifecycle_json "
                "FROM daily_state_per_broker "
                "WHERE date=? AND broker_scope=?",
                (today, scope),
            ).fetchone()
        if not r:
            return None
        return {
            "open_positions": r[0],
            "capital_deployed": r[1],
            "peak_equity": r[2],
            "drawdown_from_peak": r[3],
            "lifecycle": json.loads(r[4]) if r[4] else {},
        }

    def test_fresh_insert_populates_columns_and_batch_id(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        with self.fx.conn() as c:
            res = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=5000.0),
            )
            row = self._row("2026-05-29", "ibkr_live")
            bp = c.execute(
                "SELECT COUNT(*), exposure_batch_id FROM broker_positions "
                "WHERE broker_scope=? AND date=? "
                "GROUP BY exposure_batch_id",
                ("ibkr_live", "2026-05-29"),
            ).fetchone()
        self.assertEqual(res["status"], "inserted")
        self.assertEqual(res["positions_written"], 1)
        self.assertEqual(row["open_positions"], 1)
        self.assertEqual(row["capital_deployed"], 2000.0)
        self.assertEqual(row["lifecycle"]["exposure_status"],
                         ExposureQuality.PARTIAL.value)
        # Batch ID is a UUID-looking string, shared between row and table.
        self.assertEqual(bp[0], 1)
        self.assertEqual(bp[1], res["exposure_batch_id"])
        self.assertEqual(row["lifecycle"]["exposure_batch_id"],
                         res["exposure_batch_id"])

    def test_batch_id_shared_across_positions_in_one_run(self):
        positions = [
            _ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                      exposure_usd=None),
            _ibkr_pos("MSFT", "long", 5.0, mark_price=400.0,
                      exposure_usd=None),
            _ibkr_pos("GOOG", "long", 2.0, mark_price=150.0,
                      exposure_usd=None),
        ]
        with self.fx.conn() as c:
            res = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions),
            )
            batch_ids = c.execute(
                "SELECT DISTINCT exposure_batch_id FROM broker_positions "
                "WHERE broker_scope=? AND date=?",
                ("ibkr_live", "2026-05-29"),
            ).fetchall()
            n_rows = c.execute(
                "SELECT COUNT(*) FROM broker_positions "
                "WHERE broker_scope=? AND date=? AND exposure_batch_id=?",
                ("ibkr_live", "2026-05-29", res["exposure_batch_id"]),
            ).fetchone()[0]
        self.assertEqual(len(batch_ids), 1)
        self.assertEqual(batch_ids[0][0], res["exposure_batch_id"])
        self.assertEqual(n_rows, 3)

    def test_two_runs_produce_two_distinct_batches_append_only(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        with self.fx.conn() as c:
            r1 = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions),
            )
            r2 = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions),
            )
            batch_ids = [b[0] for b in c.execute(
                "SELECT DISTINCT exposure_batch_id FROM broker_positions "
                "WHERE broker_scope=? AND date=?",
                ("ibkr_live", "2026-05-29"),
            ).fetchall()]
            total = c.execute(
                "SELECT COUNT(*) FROM broker_positions "
                "WHERE broker_scope=? AND date=?",
                ("ibkr_live", "2026-05-29"),
            ).fetchone()[0]
        self.assertNotEqual(r1["exposure_batch_id"], r2["exposure_batch_id"])
        self.assertEqual(set(batch_ids), {r1["exposure_batch_id"],
                                          r2["exposure_batch_id"]})
        # Append-only: 1 + 1 = 2 rows total.
        self.assertEqual(total, 2)

    def test_latest_batch_per_scope_query_pattern_works(self):
        """Engine in M14.E will fetch latest batch per scope via:
            SELECT exposure_batch_id FROM broker_positions
            WHERE broker_scope=? AND date=?
            ORDER BY fetched_at_utc DESC LIMIT 1
        Verify the query returns the most recent batch.
        """
        import time
        positions_v1 = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                                  exposure_usd=None)]
        positions_v2 = [_ibkr_pos("AAPL", "long", 20.0, mark_price=200.0,
                                  exposure_usd=None)]
        with self.fx.conn() as c:
            r1 = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions_v1),
            )
            time.sleep(0.01)  # ensure fetched_at_utc differs
            r2 = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions_v2),
            )
            latest_batch = c.execute(
                "SELECT exposure_batch_id FROM broker_positions "
                "WHERE broker_scope=? AND date=? "
                "ORDER BY fetched_at_utc DESC LIMIT 1",
                ("ibkr_live", "2026-05-29"),
            ).fetchone()
        self.assertEqual(latest_batch[0], r2["exposure_batch_id"])
        # And fetching all rows from latest batch produces v2's qty.
        with self.fx.conn() as c:
            rows = c.execute(
                "SELECT qty FROM broker_positions "
                "WHERE broker_scope=? AND exposure_batch_id=?",
                ("ibkr_live", r2["exposure_batch_id"]),
            ).fetchall()
        self.assertEqual([r[0] for r in rows], [20.0])

    def test_hysteresis_fresh_partial_increment_unknown_resets(self):
        with self.fx.conn() as c:
            # Two PARTIAL reads (positions but no equity).
            positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                                   exposure_usd=None)]
            ingest_exposure_once(c, scope="ibkr_live", today="2026-05-29",
                                 adapter=self._ibkr_adapter(positions))
            ingest_exposure_once(c, scope="ibkr_live", today="2026-05-29",
                                 adapter=self._ibkr_adapter(positions))
            row = self._row("2026-05-29", "ibkr_live")
        self.assertEqual(row["lifecycle"]["exposure_fresh_reads_count"], 2)

        # FRESH reading bumps to 3.
        with self.fx.conn() as c:
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=10000.0),
            )
            row = self._row("2026-05-29", "ibkr_live")
        self.assertEqual(row["lifecycle"]["exposure_fresh_reads_count"], 3)

        # UNKNOWN reading resets to 0.
        with self.fx.conn() as c:
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._unknown_adapter(),
            )
            row = self._row("2026-05-29", "ibkr_live")
        self.assertEqual(row["lifecycle"]["exposure_fresh_reads_count"], 0)
        # Numeric columns get reset to 0 on UNKNOWN (engine MUST consult
        # lifecycle.exposure_status, not the bare number).
        self.assertEqual(row["open_positions"], 0)
        self.assertEqual(row["capital_deployed"], 0.0)
        self.assertEqual(row["lifecycle"]["exposure_status"],
                         ExposureQuality.UNKNOWN.value)

    def test_peak_equity_ratchets_up_only(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        with self.fx.conn() as c:
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=5000.0),
            )
            row1 = self._row("2026-05-29", "ibkr_live")
            # Equity drops; peak must not lower.
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=4000.0),
            )
            row2 = self._row("2026-05-29", "ibkr_live")
            # Equity rises; peak updates.
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=6000.0),
            )
            row3 = self._row("2026-05-29", "ibkr_live")
        self.assertEqual(row1["peak_equity"], 5000.0)
        self.assertEqual(row2["peak_equity"], 5000.0)
        self.assertEqual(row3["peak_equity"], 6000.0)

    def test_drawdown_from_peak_correct(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        with self.fx.conn() as c:
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=10000.0),
            )
            # 10% drawdown.
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions, equity=9000.0),
            )
            row = self._row("2026-05-29", "ibkr_live")
        self.assertAlmostEqual(row["drawdown_from_peak"], 0.10, places=4)

    def test_invalid_scope_raises(self):
        with self.fx.conn() as c:
            with self.assertRaises(ValueError):
                ingest_exposure_once(
                    c, scope="GLOBAL", today="2026-05-29",
                    adapter=self._ibkr_adapter([]),
                )

    def test_known_zero_writes_zeros_and_partial_status(self):
        with self.fx.conn() as c:
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter([], equity=5000.0),
            )
            row = self._row("2026-05-29", "ibkr_live")
        # FRESH (positions+count+capital+equity all known) since equity supplied.
        self.assertEqual(row["lifecycle"]["exposure_status"],
                         ExposureQuality.FRESH.value)
        self.assertEqual(row["open_positions"], 0)
        self.assertEqual(row["capital_deployed"], 0.0)

    def test_unknown_writes_zeros_but_status_unknown(self):
        with self.fx.conn() as c:
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._unknown_adapter(),
            )
            row = self._row("2026-05-29", "ibkr_live")
        self.assertEqual(row["lifecycle"]["exposure_status"],
                         ExposureQuality.UNKNOWN.value)
        # Distinguishable from known-zero: status=='unknown' AND count=0.
        self.assertEqual(row["lifecycle"]["exposure_fresh_reads_count"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Dry-run behaviour
# ─────────────────────────────────────────────────────────────────────────────


class TestDryRun(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def _ibkr_adapter(self, positions):
        return IBKRExposureAdapter("ibkr_live",
                                    positions_reader=lambda: positions)

    def test_dry_run_writes_no_daily_state_row(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        with self.fx.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            res = ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions), dry_run=True,
            )
            after = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(res["status"], "dry_run")
        self.assertFalse(res["would_write"])

    def test_dry_run_writes_no_broker_positions_rows(self):
        positions = [_ibkr_pos("AAPL", "long", 10.0, mark_price=200.0,
                               exposure_usd=None)]
        with self.fx.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM broker_positions"
            ).fetchone()[0]
            ingest_exposure_once(
                c, scope="ibkr_live", today="2026-05-29",
                adapter=self._ibkr_adapter(positions), dry_run=True,
            )
            after = c.execute(
                "SELECT COUNT(*) FROM broker_positions"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_cli_dry_run_inits_memory_db_first(self):
        """Per ChatGPT correction #6: the CLI dry-run uses :memory:, and
        MUST call init_flywheel_tables on it FIRST so a missing table
        doesn't mask real dry-run behaviour. We exercise this by running
        the CLI with --dry-run --all against a fresh real DB and
        asserting (a) no writes to the real DB, (b) the CLI doesn't
        crash with "no such table" on the in-memory conn."""
        env = dict(os.environ)
        env["SIGNALS_DB_PATH"] = self.fx.path
        # Snapshot before.
        with self.fx.conn() as c:
            before_ds = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            before_bp = c.execute(
                "SELECT COUNT(*) FROM broker_positions"
            ).fetchone()[0]
        res = subprocess.run(
            [sys.executable, "tools/ingest_exposure_state.py",
             "--all", "--dry-run"],
            capture_output=True, text=True, env=env, cwd=_REPO,
        )
        with self.fx.conn() as c:
            after_ds = c.execute(
                "SELECT COUNT(*) FROM daily_state_per_broker"
            ).fetchone()[0]
            after_bp = c.execute(
                "SELECT COUNT(*) FROM broker_positions"
            ).fetchone()[0]
        self.assertEqual(before_ds, after_ds)
        self.assertEqual(before_bp, after_bp)
        # Adapter resolution will produce UNKNOWN readings (no IBKR
        # gateway, no eToro keys) but the CLI must not crash with a
        # missing-table SQL error. "no such table" must not appear.
        combined = (res.stdout or "") + "\n" + (res.stderr or "")
        self.assertNotIn("no such table", combined.lower())


# ─────────────────────────────────────────────────────────────────────────────
# 7. M14.C ↔ M14.D separation
# ─────────────────────────────────────────────────────────────────────────────


class TestCrossEngineSeparation(unittest.TestCase):
    """Run M14.C then M14.D (and vice versa) and verify each engine's
    owned fields are byte-identical after the other engine ran."""

    M14C_OWNED_COLUMNS = (
        "realised_pnl_usd", "realised_pnl_pct", "realised_daily_loss",
        "daily_pnl_source", "daily_pnl_available",
        "daily_loss_block_active", "daily_loss_alert_sent",
        "fresh_reads_count",
    )
    M14C_LIFECYCLE_KEYS = (
        "status", "reading_quality", "known_fields", "missing_fields",
        "latest_reading", "events",
    )
    M14D_OWNED_COLUMNS = (
        "open_positions", "capital_deployed",
        "peak_equity", "drawdown_from_peak",
    )
    M14D_LIFECYCLE_KEYS = (
        "exposure_status", "exposure_missing_fields",
        "exposure_known_fields", "exposure_fresh_reads_count",
        "exposure_latest_reading", "exposure_events",
        "exposure_batch_id",
    )

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def _row_dict(self, c, today, scope):
        cols = ",".join(self.M14C_OWNED_COLUMNS + self.M14D_OWNED_COLUMNS +
                        ("lifecycle_json",))
        r = c.execute(
            f"SELECT {cols} FROM daily_state_per_broker "
            f"WHERE date=? AND broker_scope=?", (today, scope),
        ).fetchone()
        if not r:
            return None
        keys = cols.split(",")
        d = dict(zip(keys, r))
        d["__lifecycle"] = json.loads(d.pop("lifecycle_json")) if d["lifecycle_json"] else {}
        return d

    def _pnl_adapter(self, scope, pnl):
        from bot.risk_authority.reading import BrokerPnLReading

        class _A:
            name = scope
            def read(self, *, today):
                # Field name in M14.C BrokerPnLReading is `success`,
                # not `data_source_success`.
                return BrokerPnLReading(
                    broker_scope=scope, trading_day=today,
                    fetched_at_utc="2026-05-29T08:00:00Z",
                    success=True,
                    realised_pnl_usd=pnl,
                    realised_pnl_pct=pnl / 1000.0,
                    realised_daily_loss=max(0.0, -pnl),
                    source="ingested",
                )
        return _A()

    def _exp_adapter(self, scope, qty=10.0, mark=200.0, equity=None):
        positions = [_ibkr_pos("AAPL", "long", qty, mark_price=mark,
                               exposure_usd=None)]
        def pr():
            return positions
        if equity is None:
            return IBKRExposureAdapter(scope, positions_reader=pr)
        def ar():
            return {"equity_usd": equity}
        return IBKRExposureAdapter(scope, positions_reader=pr,
                                    account_reader=ar)

    def test_pnl_then_exposure_preserves_pnl(self):
        scope = "ibkr_live"
        today = "2026-05-29"
        with self.fx.conn() as c:
            # 1. M14.C ingest first.
            ingest_pnl_once(c, scope=scope, today=today,
                            adapter=self._pnl_adapter(scope, pnl=-25.0))
            before = self._row_dict(c, today, scope)
            # 2. M14.D ingest on top.
            ingest_exposure_once(c, scope=scope, today=today,
                                  adapter=self._exp_adapter(scope))
            after = self._row_dict(c, today, scope)

        # Every M14.C-owned column unchanged.
        for col in self.M14C_OWNED_COLUMNS:
            self.assertEqual(before[col], after[col],
                f"M14.D mutated M14.C column {col!r}: "
                f"{before[col]!r} -> {after[col]!r}")
        # Every M14.C lifecycle key unchanged.
        for k in self.M14C_LIFECYCLE_KEYS:
            self.assertEqual(before["__lifecycle"].get(k),
                             after["__lifecycle"].get(k),
                f"M14.D mutated M14.C lifecycle key {k!r}")
        # M14.D-owned fields DID change (sanity: separation isn't trivial).
        self.assertNotEqual(before["open_positions"], after["open_positions"])
        self.assertIn("exposure_status", after["__lifecycle"])
        self.assertNotIn("exposure_status", before["__lifecycle"])

    def test_exposure_then_pnl_preserves_exposure(self):
        scope = "ibkr_live"
        today = "2026-05-29"
        with self.fx.conn() as c:
            # 1. M14.D ingest first.
            ingest_exposure_once(c, scope=scope, today=today,
                                  adapter=self._exp_adapter(scope, equity=5000.0))
            before = self._row_dict(c, today, scope)
            # 2. M14.C ingest on top.
            ingest_pnl_once(c, scope=scope, today=today,
                            adapter=self._pnl_adapter(scope, pnl=10.0))
            after = self._row_dict(c, today, scope)

        for col in self.M14D_OWNED_COLUMNS:
            self.assertEqual(before[col], after[col],
                f"M14.C mutated M14.D column {col!r}: "
                f"{before[col]!r} -> {after[col]!r}")
        for k in self.M14D_LIFECYCLE_KEYS:
            self.assertEqual(before["__lifecycle"].get(k),
                             after["__lifecycle"].get(k),
                f"M14.C mutated M14.D lifecycle key {k!r}")
        # M14.C-owned fields DID change (sanity).
        self.assertNotEqual(before["realised_pnl_usd"],
                             after["realised_pnl_usd"])

    def test_interleaved_writes_keep_both_owners_intact(self):
        scope = "ibkr_live"
        today = "2026-05-29"
        with self.fx.conn() as c:
            ingest_pnl_once(c, scope=scope, today=today,
                            adapter=self._pnl_adapter(scope, pnl=5.0))
            after_c1 = self._row_dict(c, today, scope)
            ingest_exposure_once(c, scope=scope, today=today,
                                  adapter=self._exp_adapter(scope))
            after_d1 = self._row_dict(c, today, scope)
            ingest_pnl_once(c, scope=scope, today=today,
                            adapter=self._pnl_adapter(scope, pnl=-3.0))
            after_c2 = self._row_dict(c, today, scope)
            ingest_exposure_once(c, scope=scope, today=today,
                                  adapter=self._exp_adapter(scope, equity=7000.0))
            after_d2 = self._row_dict(c, today, scope)
        # M14.D-owned fields after the second M14.C ingest are still
        # equal to what they were after the FIRST M14.D ingest.
        for col in self.M14D_OWNED_COLUMNS:
            self.assertEqual(after_d1[col], after_c2[col],
                f"interleaved M14.C mutated M14.D column {col!r}")
        # M14.C-owned fields after second M14.D ingest equal what they
        # were after the second M14.C ingest.
        for col in self.M14C_OWNED_COLUMNS:
            self.assertEqual(after_c2[col], after_d2[col],
                f"interleaved M14.D mutated M14.C column {col!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 8. AST safety: no write endpoints + no order methods
# ─────────────────────────────────────────────────────────────────────────────


class TestNoWriteNoOrder(unittest.TestCase):
    """AST scan: exposure adapters + CLI contain no executable
    references to:
      * HTTP write verbs (POST/DELETE/PUT/PATCH) as Call args or
        method= kwargs;
      * .post/.delete/.put/.patch attribute calls;
      * IBKR order methods placeOrder/cancelOrder/modifyOrder/reqGlobalCancel;
      * EtoroLiveBroker name/import or tools.etoro_live_write import.
    Docstrings/comments that simply describe these prohibitions are
    ignored (matches the M14.C / M13.2 pattern)."""

    FORBIDDEN_HTTP_LITERALS = {"POST", "DELETE", "PUT", "PATCH"}
    FORBIDDEN_HTTP_METHODS  = {"post", "delete", "put", "patch"}
    FORBIDDEN_ORDER_METHODS = {"placeOrder", "cancelOrder",
                                "modifyOrder", "reqGlobalCancel"}
    FORBIDDEN_NAMES         = {"EtoroLiveBroker"}
    FORBIDDEN_MODULES       = {"bot.etoro.live_broker",
                                "tools.etoro_live_write"}

    TARGETS = (
        "bot/risk_authority/exposure_reading.py",
        "bot/risk_authority/ingest_exposure.py",
        "bot/risk_authority/ingest_etoro_exposure.py",
        "bot/risk_authority/ingest_ibkr_exposure.py",
        "tools/ingest_exposure_state.py",
    )

    def _scan(self, path):
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        offenders = []
        for node in ast.walk(tree):
            # Imports
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if m in self.FORBIDDEN_MODULES:
                    offenders.append(f"ImportFrom {m!r} @{node.lineno}")
                for alias in node.names:
                    if alias.name in self.FORBIDDEN_NAMES:
                        offenders.append(
                            f"ImportFrom name {alias.name!r} @{node.lineno}"
                        )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self.FORBIDDEN_MODULES:
                        offenders.append(
                            f"Import {alias.name!r} @{node.lineno}"
                        )
            # Code references to forbidden names
            if isinstance(node, ast.Name) and node.id in self.FORBIDDEN_NAMES:
                offenders.append(f"Name {node.id!r} @{node.lineno}")
            # Call nodes — string-literal args and method= kwargs
            if isinstance(node, ast.Call):
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        if a.value.upper() in self.FORBIDDEN_HTTP_LITERALS:
                            offenders.append(
                                f"call arg {a.value!r} @{a.lineno}"
                            )
                for kw in node.keywords:
                    if (kw.arg == "method"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value.upper()
                            in self.FORBIDDEN_HTTP_LITERALS):
                        offenders.append(
                            f"method={kw.value.value!r} @{kw.value.lineno}"
                        )
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    if fn.attr in self.FORBIDDEN_HTTP_METHODS:
                        offenders.append(f".{fn.attr}(…) @{fn.lineno}")
                    if fn.attr in self.FORBIDDEN_ORDER_METHODS:
                        offenders.append(
                            f".{fn.attr}(…) (order verb) @{fn.lineno}"
                        )
        return offenders

    def test_no_writes_or_orders_in_targets(self):
        for path in self.TARGETS:
            full = os.path.join(_REPO, path)
            offenders = self._scan(full)
            self.assertEqual(offenders, [],
                f"{path} has forbidden code references: {offenders}")

    def test_scan_catches_synthetic_order_call(self):
        synthetic = (
            "def f(ib):\n"
            "    return ib.placeOrder('x', 'y')\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                          delete=False) as tf:
            tf.write(synthetic)
            tmp = tf.name
        try:
            offenders = self._scan(tmp)
            self.assertTrue(any("placeOrder" in o for o in offenders),
                            f"AST scan missed real placeOrder: {offenders}")
        finally:
            os.unlink(tmp)

    def test_scan_ignores_docstring_mentions(self):
        synthetic = (
            '"""This module never calls placeOrder / cancelOrder, '
            'never issues POST/DELETE, never imports EtoroLiveBroker."""\n'
            "def f(): return 0\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                          delete=False) as tf:
            tf.write(synthetic)
            tmp = tf.name
        try:
            offenders = self._scan(tmp)
            self.assertEqual(offenders, [],
                f"AST scan false-positived on docstring: {offenders}")
        finally:
            os.unlink(tmp)

    def test_scan_catches_synthetic_http_post_call(self):
        synthetic = (
            "import requests\n"
            "def f():\n"
            "    return requests.post('http://x')\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                          delete=False) as tf:
            tf.write(synthetic)
            tmp = tf.name
        try:
            offenders = self._scan(tmp)
            self.assertTrue(any(".post" in o for o in offenders),
                            f"AST scan missed .post(...): {offenders}")
        finally:
            os.unlink(tmp)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Scanner isolation (subprocess)
# ─────────────────────────────────────────────────────────────────────────────


class TestScannerIsolation(unittest.TestCase):

    def test_scanner_imports_do_not_load_exposure_modules(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk, bot.brokers\n"
            "forbidden = [m for m in (\n"
            "    'bot.risk_authority.ingest_exposure',\n"
            "    'bot.risk_authority.ingest_etoro_exposure',\n"
            "    'bot.risk_authority.ingest_ibkr_exposure',\n"
            "    'bot.risk_authority.exposure_reading',\n"
            "    'bot.etoro.live_broker',\n"
            ") if m in sys.modules]\n"
            "print('loaded_forbidden:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", check],
            capture_output=True, text=True, cwd=_REPO,
        )
        self.assertEqual(r.returncode, 0,
            f"scanner-isolation violated. stdout={r.stdout!r} stderr={r.stderr!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 10. CLI flag absence
# ─────────────────────────────────────────────────────────────────────────────


class TestCLIForbiddenFlags(unittest.TestCase):

    def _run(self, *flags):
        env = dict(os.environ)
        return subprocess.run(
            [sys.executable, "tools/ingest_exposure_state.py", *flags],
            capture_output=True, text=True, env=env, cwd=_REPO,
        )

    def test_demo_flag_rejected(self):
        r = self._run("--demo", "--scope", "etoro_real")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)

    def test_base_url_flag_rejected(self):
        r = self._run("--base-url=https://evil.example", "--scope", "etoro_real")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)

    def test_override_realised_flag_rejected(self):
        r = self._run("--override-realised-pnl=999", "--scope", "etoro_real")
        self.assertEqual(r.returncode, 2)

    def test_help_does_not_mention_forbidden_flags(self):
        r = self._run("--help")
        for f in ("--demo", "--base-url", "--override-realised", "--override"):
            self.assertNotIn(f, r.stdout,
                f"forbidden flag {f!r} appeared in --help")


# ─────────────────────────────────────────────────────────────────────────────
# 11. ingest_exposure_all_scopes
# ─────────────────────────────────────────────────────────────────────────────


class TestIngestAllScopes(unittest.TestCase):

    def setUp(self):
        self.fx = _DB()

    def tearDown(self):
        self.fx.cleanup()

    def test_any_unknown_reports_true(self):
        def factory(scope):
            if scope == "etoro_real":
                # UNKNOWN.
                def boom():
                    raise RuntimeError("keys missing for etoro: keys_absent")
                return EtoroExposureAdapter("etoro_real",
                                             portfolio_reader=boom)
            return IBKRExposureAdapter(scope, positions_reader=lambda: [])
        with self.fx.conn() as c:
            out = ingest_exposure_all_scopes(
                c, adapter_factory=factory, today="2026-05-29",
            )
        self.assertTrue(out["any_unknown"])
        # Should have results for all four scopes.
        self.assertEqual({r["scope"] for r in out["results"]},
                         INGESTIBLE_SCOPES)


if __name__ == "__main__":
    unittest.main(verbosity=2)

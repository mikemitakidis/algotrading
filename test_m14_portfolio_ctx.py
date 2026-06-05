"""test_m14_portfolio_ctx.py — P0-4 PortfolioRiskContext population tests.

Verifies bot.portfolio_ctx.gather() populates positions, open_orders,
local_open_intents, and kill_switch_active for both the live-IBKR
path (reusing RiskManager's stashed reconcile — Correction B) and
the paper paths (derived from execution_intents).

Audit P0-4 (M1-M16 audit, 2026-06-05): before this fix,
PortfolioRiskContext was constructed with those four fields at
their empty defaults. PortfolioRiskPolicy gates that read them
(_count_open_trades, _calc_symbol_exposure, _calc_sector_exposure)
ran blind. The smoking-gun regression test below documents the
exact behaviour change.

Correction B (NO duplicate IBKR reconcile per signal): the live
path MUST reuse intent.risk_checks['_recon'] populated by
RiskManager.evaluate. test_live_path_does_not_call_ibkr asserts
no IBKRBroker import or instantiation happens inside gather().
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.portfolio_ctx import (
    gather,
    RECON_STASH_KEY,
    _local_intents_from_db,
    _paper_positions_from_intents,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_intent(*, risk_checks=None, symbol="AAPL", direction="long"):
    return SimpleNamespace(
        symbol=symbol,
        direction=direction,
        risk_checks=risk_checks if risk_checks is not None else {},
    )


def _make_db_with_intents(intents):
    """Build a temp signals.db with execution_intents rows.
    `intents` is a list of dicts with keys: signal_id, symbol,
    direction, position_size, entry_price, status."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE execution_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            symbol TEXT,
            direction TEXT,
            position_size REAL,
            entry_price REAL,
            status TEXT
        )
    """)
    for it in intents:
        conn.execute(
            "INSERT INTO execution_intents "
            "(signal_id, symbol, direction, position_size, entry_price, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (it["signal_id"], it["symbol"], it["direction"],
             it["position_size"], it["entry_price"], it["status"]),
        )
    conn.commit()
    return conn, path


# ─────────────────────────────────────────────────────────────────────────────
# G1. Live-path: reuse the reconcile stash, no second network call
# ─────────────────────────────────────────────────────────────────────────────

class TestGatherLivePath(unittest.TestCase):

    def test_live_path_reuses_stashed_recon(self):
        """When intent.risk_checks contains '_recon', gather() must
        use it directly without any broker import."""
        positions_in = [
            {"symbol": "AAPL", "position": 10, "avg_cost": 150.0},
            {"symbol": "MSFT", "position": 5,  "avg_cost": 300.0},
        ]
        open_orders_in = [
            {"symbol": "TSLA", "orderId": 42, "totalQuantity": 100,
             "lmtPrice": 200.0},
        ]
        intent = _make_intent(risk_checks={
            RECON_STASH_KEY: {
                "positions":   positions_in,
                "open_orders": open_orders_in,
                "warnings":    [],
            }
        })
        # Empty conn — gather should not need it for the live path
        # positions / open_orders. local_open_intents will be [].
        conn, db_path = _make_db_with_intents([])
        try:
            out = gather("ibkr_live", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)

        self.assertEqual(out["positions"], positions_in)
        self.assertEqual(out["open_orders"], open_orders_in)
        self.assertEqual(out["local_open_intents"], [])

    def test_live_path_does_not_import_ibkrbroker(self):
        """Correction B: gather() in live mode must not touch the
        broker. Test by patching the import location and asserting
        no instantiation."""
        intent = _make_intent(risk_checks={
            RECON_STASH_KEY: {"positions": [], "open_orders": [],
                                "warnings": []}
        })
        conn, db_path = _make_db_with_intents([])
        try:
            with patch("bot.brokers.ibkr_broker.IBKRBroker") as mock_b:
                gather("ibkr_live", intent, conn)
                mock_b.assert_not_called()
        finally:
            conn.close()
            os.unlink(db_path)

    def test_live_path_empty_recon_returns_empty(self):
        """A stashed recon with empty lists yields empty out — not
        a fallback to paper derivation."""
        intent = _make_intent(risk_checks={
            RECON_STASH_KEY: {"positions": [], "open_orders": [],
                                "warnings": []}
        })
        # Seed DB with one accepted intent — must NOT leak into
        # positions when recon is stashed (even if empty).
        conn, db_path = _make_db_with_intents([{
            "signal_id": 1, "symbol": "XYZ", "direction": "long",
            "position_size": 10, "entry_price": 50.0,
            "status": "accepted",
        }])
        try:
            out = gather("ibkr_live", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertEqual(out["positions"], [])
        self.assertEqual(out["open_orders"], [])
        # local_open_intents still populated from DB.
        self.assertEqual(len(out["local_open_intents"]), 1)


# ─────────────────────────────────────────────────────────────────────────────
# G2. Paper path: derive positions from execution_intents
# ─────────────────────────────────────────────────────────────────────────────

class TestGatherPaperPath(unittest.TestCase):

    def test_paper_path_derives_positions_from_intents(self):
        intent = _make_intent(risk_checks={})  # NO recon stash
        seed = [
            {"signal_id": 1, "symbol": "AAPL", "direction": "long",
             "position_size": 10, "entry_price": 150.0,
             "status": "paper_logged"},
            {"signal_id": 2, "symbol": "MSFT", "direction": "long",
             "position_size": 5, "entry_price": 300.0,
             "status": "accepted"},
            # Risk-rejected — must NOT appear.
            {"signal_id": 3, "symbol": "REJ", "direction": "long",
             "position_size": 1, "entry_price": 1.0,
             "status": "risk_rejected"},
            # Test signal_id — must NOT appear.
            {"signal_id": 888888, "symbol": "TEST", "direction": "long",
             "position_size": 1, "entry_price": 1.0,
             "status": "accepted"},
        ]
        conn, db_path = _make_db_with_intents(seed)
        try:
            out = gather("paper", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)

        symbols = sorted(p["symbol"] for p in out["positions"])
        self.assertEqual(symbols, ["AAPL", "MSFT"])
        self.assertEqual(out["open_orders"], [])

        # Each derived position has the right shape.
        aapl = next(p for p in out["positions"] if p["symbol"] == "AAPL")
        self.assertEqual(aapl["position"], 10)
        self.assertEqual(aapl["avg_cost"], 150.0)
        self.assertIsNone(aapl["market_value"])

        # local_open_intents excludes risk_rejected and test IDs.
        loi_symbols = sorted(it["symbol"] for it in out["local_open_intents"])
        self.assertEqual(loi_symbols, ["AAPL", "MSFT"])

    def test_paper_path_with_no_intents_returns_empty(self):
        intent = _make_intent(risk_checks={})
        conn, db_path = _make_db_with_intents([])
        try:
            out = gather("paper", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertEqual(out["positions"], [])
        self.assertEqual(out["open_orders"], [])
        self.assertEqual(out["local_open_intents"], [])

    def test_paper_path_skips_zero_size_positions(self):
        intent = _make_intent(risk_checks={})
        conn, db_path = _make_db_with_intents([
            {"signal_id": 1, "symbol": "ZERO", "direction": "long",
             "position_size": 0, "entry_price": 50.0,
             "status": "accepted"},
        ])
        try:
            out = gather("paper", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertEqual(out["positions"], [])
        # local_open_intents still includes it (the policy decides
        # whether to skip zero-size).
        self.assertEqual(len(out["local_open_intents"]), 1)


# ─────────────────────────────────────────────────────────────────────────────
# G3. kill_switch_active propagation
# ─────────────────────────────────────────────────────────────────────────────

class TestKillSwitchActivePropagation(unittest.TestCase):

    def test_kill_switch_inactive_propagates(self):
        intent = _make_intent(risk_checks={
            RECON_STASH_KEY: {"positions": [], "open_orders": [],
                                "warnings": []}
        })
        conn, db_path = _make_db_with_intents([])
        try:
            with patch("bot.kill_switch.is_kill_switch_active",
                         return_value=False):
                out = gather("ibkr_live", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertFalse(out["kill_switch_active"])

    def test_kill_switch_active_propagates(self):
        intent = _make_intent(risk_checks={
            RECON_STASH_KEY: {"positions": [], "open_orders": [],
                                "warnings": []}
        })
        conn, db_path = _make_db_with_intents([])
        try:
            with patch("bot.kill_switch.is_kill_switch_active",
                         return_value=True):
                out = gather("ibkr_live", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertTrue(out["kill_switch_active"])

    def test_kill_switch_read_failure_fails_safe_active(self):
        """If kill_switch read raises, gather treats it as ACTIVE —
        same fail-safe policy as bot.kill_switch itself."""
        intent = _make_intent(risk_checks={
            RECON_STASH_KEY: {"positions": [], "open_orders": [],
                                "warnings": []}
        })
        conn, db_path = _make_db_with_intents([])
        try:
            with patch("bot.kill_switch.is_kill_switch_active",
                         side_effect=RuntimeError("disk gone")):
                out = gather("ibkr_live", intent, conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertTrue(out["kill_switch_active"])


# ─────────────────────────────────────────────────────────────────────────────
# G4. Local-DB helper edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestLocalIntentsFromDB(unittest.TestCase):

    def test_none_conn_returns_empty(self):
        self.assertEqual(_local_intents_from_db(None), [])

    def test_missing_table_returns_empty(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            # No execution_intents table created.
            result = _local_intents_from_db(conn)
            conn.close()
            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_excludes_synthetic_test_ids(self):
        conn, db_path = _make_db_with_intents([
            {"signal_id": 999999, "symbol": "T", "direction": "long",
             "position_size": 1, "entry_price": 1.0,
             "status": "accepted"},
            {"signal_id": 1, "symbol": "REAL", "direction": "long",
             "position_size": 1, "entry_price": 1.0,
             "status": "accepted"},
        ])
        try:
            result = _local_intents_from_db(conn)
        finally:
            conn.close()
            os.unlink(db_path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "REAL")


class TestPaperPositionsHelper(unittest.TestCase):

    def test_empty_input(self):
        self.assertEqual(_paper_positions_from_intents([]), [])

    def test_dedup_not_done_by_helper(self):
        # The helper is intentionally non-deduplicating; that's
        # PortfolioRiskPolicy._count_open_trades's job.
        result = _paper_positions_from_intents([
            {"symbol": "X", "position_size": 1, "entry_price": 50},
            {"symbol": "X", "position_size": 2, "entry_price": 60},
        ])
        self.assertEqual(len(result), 2)


# ─────────────────────────────────────────────────────────────────────────────
# G5. Smoking-gun regression: PortfolioRiskPolicy now sees the data
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolioRiskPolicySeesPopulatedData(unittest.TestCase):
    """Before P0-4: ctx.positions was empty → _count_open_trades
    returned 0 → max_open_trades gate never blocked.

    After P0-4: ctx.positions reflects real state → gate works.

    This test is the smoking-gun proof of the bug + fix: it builds
    an empty ctx and a populated ctx with identical other inputs,
    confirms _count_open_trades sees different totals, and asserts
    the populated case reports the intended-to-be-gated count.
    """

    def test_empty_ctx_sees_zero_open_trades(self):
        from bot.risk import PortfolioRiskPolicy, PortfolioRiskContext
        policy = PortfolioRiskPolicy()
        ctx = PortfolioRiskContext()  # all defaults — the bug
        count, detail = policy._count_open_trades(ctx)
        self.assertEqual(count, 0)
        self.assertEqual(detail["broker_positions"], 0)
        self.assertEqual(detail["local_intents"], 0)

    def test_populated_ctx_sees_real_open_trades(self):
        from bot.risk import PortfolioRiskPolicy, PortfolioRiskContext
        policy = PortfolioRiskPolicy()
        # Three positions + one matching-symbol open order + one
        # local intent in a distinct symbol → expected count = 3
        # (one position dedup against the order, one position
        # standalone, one local intent).
        ctx = PortfolioRiskContext(
            positions=[
                {"symbol": "AAPL", "position": 10, "avg_cost": 150},
                {"symbol": "MSFT", "position": 5,  "avg_cost": 300},
            ],
            open_orders=[
                # parent order for AAPL — counts as one bracket
                {"symbol": "AAPL", "order_id": 42, "parent_id": 0,
                 "qty": 10, "lmt_price": 150},
            ],
            local_open_intents=[
                {"symbol": "TSLA", "position_size": 3,
                 "entry_price": 200},
            ],
        )
        count, detail = policy._count_open_trades(ctx)
        self.assertGreater(count, 0,
                           "populated ctx must see > 0 open trades — "
                           "this is the smoking-gun assertion")
        # Three distinct trades: AAPL bracket (from order), MSFT
        # position, TSLA local intent.
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

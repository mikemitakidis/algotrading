"""M15.5 — IBKR paper exposure reader wiring tests.

Covers every proof the M15.5 plan requires:
  * function contract (signature, scope validation, NotImplementedError preservation)
  * read-only IB API session (readonly=True on connect; no order verbs;
    disconnect always; paper port only)
  * health check gating (M15.4 readiness before any IB API call)
  * dry-run path (no DB writes, structured summary, all readiness gates fire)
  * no fake exposure (the existing M14.D adapter's known-zero vs unknown
    distinction is preserved when M15.5 reader produces empty/partial data)
  * AST scan: no forbidden surface (no order methods, no order imports,
    no HTTP write methods, no mutating systemctl)
  * scanner isolation carry-forward
  * client ID 15 does not conflict with existing IDs
  * ibkr_live stays NotImplementedError-blocked
  * existing CLI flags preserved
  * protected files untouched (M14 engine/governor/snapshot/audit/preflight)

No live IB API call. No order. No broker write.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

_REPO = str(Path(__file__).resolve().parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from bot.risk_authority.ibkr_paper_reader import (
    DEFAULT_API_TIMEOUT_SEC,
    DEFAULT_CONNECT_TIMEOUT_SEC,
    GatewayNotReadyError,
    IBKR_PAPER_HOST,
    IBKR_PAPER_PORT,
    IBPaperReadError,
    M15_5_CLIENT_ID,
    _check_gateway_ready,
    _position_dict_from_portfolio_item,
    make_ibkr_paper_positions_reader,
    run_paper_dryrun,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: a fake portfolio item that mimics ib_insync.PortfolioItem
# ─────────────────────────────────────────────────────────────────────────────


class _FakeContract:
    def __init__(self, symbol="AAPL", currency="USD"):
        self.symbol = symbol
        self.currency = currency


class _FakePortfolioItem:
    """Mimics ib_insync.PortfolioItem (a NamedTuple in real ib_insync).
    Attribute names must match what _position_dict_from_portfolio_item
    reads: contract, position, averageCost, marketPrice, marketValue,
    unrealizedPNL, realizedPNL, account."""
    def __init__(self, *, symbol="AAPL", currency="USD",
                 position=10.0, averageCost=180.0, marketPrice=190.0,
                 marketValue=1900.0, unrealizedPNL=100.0):
        self.contract = _FakeContract(symbol=symbol, currency=currency)
        self.position = position
        self.averageCost = averageCost
        self.marketPrice = marketPrice
        self.marketValue = marketValue
        self.unrealizedPNL = unrealizedPNL


def _healthy_paper_health():
    return {
        "ready_for_ibkr_trading": True,
        "mode": "paper",
        "expected_port": IBKR_PAPER_PORT,
        "status": "service_active_api_port_open",
        "systemd_active": True,
        "tcp_reachable": True,
        "login_error_detected": False,
    }


def _unhealthy_login_error_health():
    return {
        "ready_for_ibkr_trading": False,
        "mode": "paper",
        "expected_port": IBKR_PAPER_PORT,
        "status": "service_active_login_error",
        "systemd_active": True,
        "tcp_reachable": False,
        "login_error_detected": True,
    }


def _factory_result(*, portfolio_items=None, position_records=None,
                    snapshot_ready=True, account_values_count=5):
    """Build the dict shape the new _read_portfolio_via_ib returns.
    Tests inject a fake `ib_session_factory` that returns this dict."""
    return {
        "portfolio_items":      list(portfolio_items or []),
        "position_records":     list(position_records or []),
        "snapshot_ready":       snapshot_ready,
        "account_values_count": account_values_count,
        "snapshot_waited_sec":  0.2,
    }


class _FakePosition:
    """Mimics ib_insync.Position for cross-confirm tests. Position has
    a `contract` attribute (which has .symbol) and a `position`
    attribute (the quantity)."""
    def __init__(self, *, symbol="AAPL", position=10.0):
        self.contract = _FakeContract(symbol=symbol)
        self.position = position


# ─────────────────────────────────────────────────────────────────────────────
# Group 1 — Client ID is reserved + non-conflicting
# ─────────────────────────────────────────────────────────────────────────────


class TestClientIdNoConflict(unittest.TestCase):
    """The user's corrections explicitly required proof that the chosen
    client ID does not conflict with existing usage."""

    def test_m15_5_client_id_is_15(self):
        self.assertEqual(M15_5_CLIENT_ID, 15)

    def test_m15_5_client_id_does_not_match_known_existing_ids(self):
        existing = self._collect_existing_client_ids()
        self.assertNotIn(M15_5_CLIENT_ID, existing,
            f"M15_5_CLIENT_ID={M15_5_CLIENT_ID} conflicts with "
            f"existing IDs {sorted(existing)}")

    def test_paper_broker_id_is_11(self):
        ids = self._collect_existing_client_ids()
        self.assertIn(11, ids, "expected PAPER_CLIENT_ID=11 in ibkr_broker.py")

    def test_live_broker_id_is_12(self):
        ids = self._collect_existing_client_ids()
        self.assertIn(12, ids, "expected LIVE_CLIENT_ID=12 in ibkr_broker.py")

    def test_watchdog_id_is_99(self):
        with open(os.path.join(_REPO, "bot/gateway_watchdog.py")) as f:
            src = f.read()
        self.assertIn("WATCHDOG_CLIENT_ID", src)
        m = re.search(r"WATCHDOG_CLIENT_ID\s*=.*?'99'", src)
        self.assertIsNotNone(m,
            "expected watchdog client ID default '99' in bot/gateway_watchdog.py")

    def _collect_existing_client_ids(self):
        ids = set()
        with open(os.path.join(_REPO, "bot/brokers/ibkr_broker.py")) as f:
            for line in f:
                m = re.match(r"\s*(PAPER|LIVE)_CLIENT_ID\s*=\s*(\d+)", line)
                if m:
                    ids.add(int(m.group(2)))
        with open(os.path.join(_REPO, "bot/gateway_watchdog.py")) as f:
            src = f.read()
        for m in re.finditer(r"WATCHDOG_CLIENT_ID.*?'(\d+)'", src):
            ids.add(int(m.group(1)))
        return ids


# ─────────────────────────────────────────────────────────────────────────────
# Group 2 — Health check gating: refuse to connect when not ready
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthGate(unittest.TestCase):

    def test_healthy_paper_passes(self):
        # Should not raise.
        _check_gateway_ready(scope="ibkr_paper",
                              health_checker=_healthy_paper_health)

    def test_unhealthy_login_error_raises(self):
        with self.assertRaises(GatewayNotReadyError) as ctx:
            _check_gateway_ready(scope="ibkr_paper",
                                  health_checker=_unhealthy_login_error_health)
        self.assertIn("gateway_not_ready", str(ctx.exception))

    def test_live_mode_refused_even_when_healthy(self):
        def live_health():
            h = _healthy_paper_health()
            h["mode"] = "live"
            h["expected_port"] = 4001
            return h
        with self.assertRaises(GatewayNotReadyError) as ctx:
            _check_gateway_ready(scope="ibkr_paper",
                                  health_checker=live_health)
        self.assertIn("gateway_mode_not_paper", str(ctx.exception))

    def test_unknown_mode_refused(self):
        def unknown_health():
            h = _healthy_paper_health()
            h["mode"] = "unknown"
            return h
        with self.assertRaises(GatewayNotReadyError):
            _check_gateway_ready(scope="ibkr_paper",
                                  health_checker=unknown_health)

    def test_unexpected_port_refused(self):
        def wrong_port_health():
            h = _healthy_paper_health()
            h["expected_port"] = 4001
            return h
        with self.assertRaises(GatewayNotReadyError) as ctx:
            _check_gateway_ready(scope="ibkr_paper",
                                  health_checker=wrong_port_health)
        self.assertIn("gateway_unexpected_port", str(ctx.exception))

    def test_health_checker_returns_non_dict_raises(self):
        with self.assertRaises(GatewayNotReadyError):
            _check_gateway_ready(scope="ibkr_paper",
                                  health_checker=lambda: None)

    def test_scope_must_be_ibkr_paper_in_gate(self):
        with self.assertRaises(NotImplementedError):
            _check_gateway_ready(scope="ibkr_live",
                                  health_checker=_healthy_paper_health)


# ─────────────────────────────────────────────────────────────────────────────
# Group 3 — Reader: connects to 4002, readonly=True, disconnects always
# ─────────────────────────────────────────────────────────────────────────────


class TestReaderReadOnlySession(unittest.TestCase):

    def _make_reader(self, *, factory_result=None,
                     factory_side_effect=None):
        calls = {"factory_kwargs": None}

        def fake_factory(**kw):
            calls["factory_kwargs"] = kw
            if factory_side_effect is not None:
                raise factory_side_effect
            return factory_result if factory_result is not None \
                else _factory_result()

        reader = make_ibkr_paper_positions_reader(
            health_checker=_healthy_paper_health,
            ib_session_factory=fake_factory,
        )
        return reader, calls

    def test_reader_returns_empty_list_when_snapshot_ready_and_both_empty(self):
        # Critical: snapshot_ready=True + both empty → []. Adapter
        # treats this as known-zero.
        reader, _ = self._make_reader(
            factory_result=_factory_result(snapshot_ready=True))
        self.assertEqual(reader(), [])

    def test_reader_forwards_port_4002(self):
        reader, calls = self._make_reader()
        reader()
        self.assertEqual(calls["factory_kwargs"]["port"], 4002)
        self.assertEqual(calls["factory_kwargs"]["host"], "127.0.0.1")
        self.assertEqual(calls["factory_kwargs"]["client_id"], M15_5_CLIENT_ID)

    def test_reader_does_not_route_to_4001(self):
        reader, calls = self._make_reader()
        reader()
        self.assertNotEqual(calls["factory_kwargs"]["port"], 4001)

    def test_factory_failure_becomes_ib_paper_read_error(self):
        reader, _ = self._make_reader(
            factory_side_effect=OSError("connection refused"))
        with self.assertRaises(IBPaperReadError) as ctx:
            reader()
        self.assertIn("ib_portfolio_read_failed", str(ctx.exception))

    def test_factory_returning_non_dict_raises(self):
        """If a factory returns a list (old shape), the reader must
        reject it rather than silently report known-zero."""
        reader, _ = self._make_reader(factory_result=[])  # legacy shape
        with self.assertRaises(IBPaperReadError) as ctx:
            reader()
        self.assertIn("ib_factory_returned_non_dict", str(ctx.exception))

    def test_snapshot_not_ready_raises_ib_paper_read_error(self):
        """Empty portfolio + snapshot_ready=False MUST raise; the
        adapter then classifies the reading as UNKNOWN. NOT known-zero."""
        reader, _ = self._make_reader(
            factory_result=_factory_result(snapshot_ready=False,
                                            account_values_count=0))
        with self.assertRaises(IBPaperReadError) as ctx:
            reader()
        self.assertIn("account_snapshot_not_ready_within_timeout",
                       str(ctx.exception))

    def test_gateway_not_ready_propagates_before_factory(self):
        # Health checker fails the gate → factory NEVER invoked.
        called = {"factory_invoked": False}

        def fake_factory(**kw):
            called["factory_invoked"] = True
            return _factory_result()

        reader = make_ibkr_paper_positions_reader(
            health_checker=_unhealthy_login_error_health,
            ib_session_factory=fake_factory,
        )
        with self.assertRaises(GatewayNotReadyError):
            reader()
        self.assertFalse(called["factory_invoked"],
            "IB factory was invoked despite unhealthy gateway — gate broken")

    def test_reader_with_mocked_ib_call_uses_readonly_true(self):
        """Patch ib_insync.IB at sys.modules and confirm the real
        _read_portfolio_via_ib calls connect(readonly=True), reads BOTH
        portfolio() and positions(), waits for accountValues to be
        non-empty, and never touches order methods."""
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.portfolio.return_value = []
        mock_ib.positions.return_value = []
        # accountValues non-empty on the first poll — proves snapshot
        # readiness without us needing to spin.
        mock_ib.accountValues.return_value = [object(), object(), object()]

        class _FakeIB:
            def __new__(cls):
                return mock_ib

        fake_module = type(sys)("ib_insync")
        fake_module.IB = _FakeIB
        with patch.dict(sys.modules, {"ib_insync": fake_module}):
            reader = make_ibkr_paper_positions_reader(
                health_checker=_healthy_paper_health,
            )
            reader()

        # Inspect the connect call.
        self.assertTrue(mock_ib.connect.called,
            "ib.connect was never called")
        ca = mock_ib.connect.call_args
        self.assertEqual(ca.args[0], IBKR_PAPER_HOST)
        self.assertEqual(ca.args[1], IBKR_PAPER_PORT)
        self.assertIn("readonly", ca.kwargs,
            f"ib.connect was called WITHOUT readonly= kwarg: {ca}")
        self.assertTrue(ca.kwargs["readonly"],
            f"ib.connect was called with readonly={ca.kwargs.get('readonly')!r} "
            f"— must be True")
        self.assertEqual(ca.kwargs["clientId"], M15_5_CLIENT_ID)
        # BOTH portfolio() AND positions() must have been called.
        self.assertTrue(mock_ib.portfolio.called,
            "ib.portfolio() was not called — cross-confirm requires both reads")
        self.assertTrue(mock_ib.positions.called,
            "ib.positions() was not called — cross-confirm requires both reads")
        # accountValues must have been called (snapshot-ready check).
        self.assertTrue(mock_ib.accountValues.called,
            "ib.accountValues() was not called — snapshot-ready gate missing")
        # Forbidden methods MUST NOT be called.
        for forbidden in ("placeOrder", "cancelOrder", "modifyOrder",
                           "reqGlobalCancel", "reqMktData",
                           "reqHistoricalData", "reqOpenOrders",
                           "reqExecutions"):
            self.assertFalse(
                getattr(mock_ib, forbidden).called,
                f"ib.{forbidden}() was called — that is an order/write/heavy "
                f"path forbidden by the M15.5 contract")
        # Disconnect must have been called exactly once.
        self.assertEqual(mock_ib.disconnect.call_count, 1)

    def test_reader_disconnects_when_portfolio_raises(self):
        """The `finally` branch must call disconnect even when
        portfolio() raises."""
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.accountValues.return_value = [object()]
        mock_ib.portfolio.side_effect = TimeoutError("api timeout")

        class _FakeIB:
            def __new__(cls):
                return mock_ib

        fake_module = type(sys)("ib_insync")
        fake_module.IB = _FakeIB
        with patch.dict(sys.modules, {"ib_insync": fake_module}):
            reader = make_ibkr_paper_positions_reader(
                health_checker=_healthy_paper_health,
            )
            with self.assertRaises(IBPaperReadError):
                reader()
        self.assertEqual(mock_ib.disconnect.call_count, 1,
            "disconnect must be called from finally even when "
            "portfolio() raises")


# ─────────────────────────────────────────────────────────────────────────────
# Group 4 — Portfolio item translation does not invent exposure
# ─────────────────────────────────────────────────────────────────────────────


class TestPortfolioItemTranslation(unittest.TestCase):

    def test_long_usd_item_translates(self):
        item = _FakePortfolioItem(symbol="AAPL", currency="USD",
                                    position=10.0, averageCost=180.0,
                                    marketPrice=190.0, marketValue=1900.0,
                                    unrealizedPNL=100.0)
        d = _position_dict_from_portfolio_item(item)
        self.assertEqual(d["symbol"], "AAPL")
        self.assertEqual(d["side"], "long")
        self.assertEqual(d["qty"], 10.0)
        self.assertEqual(d["currency"], "USD")
        self.assertEqual(d["exposure_usd"], 1900.0)
        self.assertEqual(d["broker_provided_usd_notional"], 1900.0)

    def test_short_position_classified_as_short(self):
        item = _FakePortfolioItem(symbol="TSLA", position=-5.0,
                                    marketValue=-500.0)
        d = _position_dict_from_portfolio_item(item)
        self.assertEqual(d["side"], "short")
        self.assertEqual(d["qty"], 5.0)   # abs

    def test_zero_position_side_is_none_so_adapter_rejects(self):
        item = _FakePortfolioItem(position=0.0)
        d = _position_dict_from_portfolio_item(item)
        self.assertIsNone(d["side"])

    def test_non_usd_does_not_get_broker_provided_usd_notional(self):
        item = _FakePortfolioItem(symbol="VOD", currency="GBP",
                                    position=100.0, marketValue=20000.0)
        d = _position_dict_from_portfolio_item(item)
        self.assertEqual(d["currency"], "GBP")
        self.assertNotIn("broker_provided_usd_notional", d,
            "Non-USD positions must NOT carry broker_provided_usd_notional "
            "— we don't invent FX. Adapter will fail closed.")

    def test_missing_market_value_passes_through_as_none(self):
        item = _FakePortfolioItem(marketValue=None, marketPrice=None)
        d = _position_dict_from_portfolio_item(item)
        self.assertIsNone(d["exposure_usd"])
        self.assertIsNone(d["mark_price"])

    def test_bool_position_quantity_is_not_classified_as_long_short(self):
        """Python booleans are ints; defensive guard prevents
        classifying them as long/short."""
        class _Item:
            contract = _FakeContract()
            position = True
            averageCost = 1.0
            marketPrice = 1.0
            marketValue = 1.0
            unrealizedPNL = 0.0
        d = _position_dict_from_portfolio_item(_Item())
        self.assertIsNone(d["side"])


# ─────────────────────────────────────────────────────────────────────────────
# Group 5 — Known-zero vs unknown preserved by adapter (integration)
# ─────────────────────────────────────────────────────────────────────────────


class TestKnownZeroVsUnknown(unittest.TestCase):
    """The user's correction B: do not fake exposure. If IBKR returns
    confirmed empty positions, that is known-zero exposure (the adapter
    produces an OK reading with capital_deployed_usd=0). If IBKR returns
    malformed/non-USD positions, the whole reading must be UNKNOWN.

    Plus the cross-confirm cases the user added in the post-138df9e
    review:
      * portfolio empty + positions empty + snapshot_ready=True → known-zero
      * portfolio empty + positions non-empty                   → UNKNOWN
      * portfolio non-empty + positions empty                   → UNKNOWN
      * portfolio non-empty + positions non-empty same symbols  → fresh
      * snapshot_ready=False                                    → UNKNOWN

    Both classifications are produced by the existing M14.D
    IBKRExposureAdapter via the IBPaperReadError → adapter UNKNOWN
    conversion path; M15.5 supplies the upstream signal."""

    def setUp(self):
        from bot.risk_authority.ingest_ibkr_exposure import IBKRExposureAdapter
        self.Adapter = IBKRExposureAdapter

    def _build_adapter(self, *, factory_result):
        reader = make_ibkr_paper_positions_reader(
            health_checker=_healthy_paper_health,
            ib_session_factory=lambda **kw: factory_result,
        )
        return self.Adapter(broker_scope="ibkr_paper",
                             positions_reader=reader)

    def test_portfolio_empty_AND_positions_empty_AND_snapshot_ready_is_known_zero(self):
        """The headline acceptance case from the user's correction:
        empty exposure marked known-zero ONLY when (a) portfolio empty,
        (b) positions empty, (c) snapshot_ready=True."""
        adapter = self._build_adapter(
            factory_result=_factory_result(
                portfolio_items=[],
                position_records=[],
                snapshot_ready=True,
                account_values_count=7,
            ))
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_known_zero_exposure(),
            f"empty + empty + snapshot_ready must be known-zero. "
            f"quality={reading.quality()}, error={reading.error!r}")
        self.assertFalse(reading.is_exposure_unknown())
        self.assertEqual(reading.open_positions_count, 0)
        self.assertEqual(reading.capital_deployed_usd, 0.0)

    def test_portfolio_empty_BUT_positions_nonempty_is_UNKNOWN(self):
        """Disagreement case A: portfolio() empty but positions() non-empty.
        Must NOT be reported as known-zero. Must be UNKNOWN."""
        adapter = self._build_adapter(
            factory_result=_factory_result(
                portfolio_items=[],
                position_records=[_FakePosition(symbol="AAPL", position=10.0)],
                snapshot_ready=True,
            ))
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_exposure_unknown(),
            "portfolio empty + positions non-empty MUST be UNKNOWN, "
            "never known-zero")
        # capital_deployed_usd MUST NOT be a fake 0.0.
        self.assertIsNone(reading.capital_deployed_usd,
            f"capital_deployed_usd must be None on disagreement; "
            f"got {reading.capital_deployed_usd!r}")
        # Error must mention disagreement so engine sees a real reason.
        self.assertIn("portfolio_positions_disagreement",
                       (reading.error or "").lower())

    def test_portfolio_nonempty_BUT_positions_empty_is_UNKNOWN(self):
        """Disagreement case B: portfolio() has data but positions()
        is empty. Must be UNKNOWN."""
        adapter = self._build_adapter(
            factory_result=_factory_result(
                portfolio_items=[_FakePortfolioItem(symbol="AAPL",
                                                     position=10.0,
                                                     marketValue=1900.0)],
                position_records=[],
                snapshot_ready=True,
            ))
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_exposure_unknown())
        self.assertIsNone(reading.capital_deployed_usd)
        self.assertIn("portfolio_positions_disagreement",
                       (reading.error or "").lower())

    def test_portfolio_nonempty_AND_positions_agree_is_fresh(self):
        """Cross-confirm pass: both sources see the same symbol set.
        Adapter produces a fresh/partial reading."""
        adapter = self._build_adapter(
            factory_result=_factory_result(
                portfolio_items=[_FakePortfolioItem(symbol="AAPL",
                                                     position=10.0,
                                                     marketValue=1900.0)],
                position_records=[_FakePosition(symbol="AAPL", position=10.0)],
                snapshot_ready=True,
            ))
        reading = adapter.read(today="2026-06-02")
        self.assertFalse(reading.is_exposure_unknown(),
            "agreeing reads must NOT be UNKNOWN")
        self.assertTrue(reading.has_fresh_exposure())
        self.assertEqual(reading.open_positions_count, 1)
        self.assertEqual(reading.capital_deployed_usd, 1900.0)

    def test_snapshot_not_ready_is_UNKNOWN_even_with_empty_lists(self):
        """The critical user-mandated case: empty + empty must NOT be
        known-zero when the snapshot is not ready. The account-update
        subscription may simply not have delivered data yet."""
        adapter = self._build_adapter(
            factory_result=_factory_result(
                portfolio_items=[],
                position_records=[],
                snapshot_ready=False,
                account_values_count=0,
            ))
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_exposure_unknown(),
            "snapshot_ready=False MUST be UNKNOWN, never known-zero, "
            "even when both lists are empty")
        self.assertIsNone(reading.capital_deployed_usd)
        self.assertIn("account_snapshot_not_ready",
                       (reading.error or "").lower())

    def test_non_usd_position_makes_reading_unknown(self):
        """Adapter must reject non-USD without broker USD notional.
        (The reader passes cross-confirm because both lists have AAPL/VOD
        with non-zero size, but the adapter rejects the GBP currency.)"""
        adapter = self._build_adapter(
            factory_result=_factory_result(
                portfolio_items=[_FakePortfolioItem(symbol="VOD",
                                                     currency="GBP",
                                                     position=100.0,
                                                     marketValue=20000.0)],
                position_records=[_FakePosition(symbol="VOD", position=100.0)],
                snapshot_ready=True,
            ))
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_exposure_unknown(),
            "Non-USD without broker-provided USD notional must classify "
            "the WHOLE reading as UNKNOWN (no fake FX)")
        self.assertIsNone(reading.capital_deployed_usd,
            f"capital_deployed_usd must be None on UNKNOWN; "
            f"got {reading.capital_deployed_usd!r}")
        self.assertIsNone(reading.open_positions_count)

    def test_gateway_not_ready_makes_reading_unknown(self):
        """If the M15.4 gate fails, the reader raises and the adapter
        produces UNKNOWN with the error reason. Engine then fails closed."""
        reader = make_ibkr_paper_positions_reader(
            health_checker=_unhealthy_login_error_health,
            ib_session_factory=lambda **kw: _factory_result(
                portfolio_items=[_FakePortfolioItem(symbol="AAPL",
                                                     position=10.0,
                                                     marketValue=1900.0)],
                position_records=[_FakePosition(symbol="AAPL", position=10.0)],
            ),
        )
        adapter = self.Adapter(broker_scope="ibkr_paper",
                                positions_reader=reader)
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_exposure_unknown(),
            "gateway-not-ready reading must be UNKNOWN")
        err = (reading.error or "").lower()
        self.assertIn("gateway_not_ready", err,
            f"unknown reading must carry the gateway_not_ready reason; "
            f"got error={reading.error!r}")
        self.assertIsNone(reading.capital_deployed_usd)

    def test_failed_positions_read_is_UNKNOWN_not_zero(self):
        """Factory raises after a successful gateway-ready check.
        Adapter must classify as UNKNOWN with the error reason,
        capital_deployed_usd=None — NOT zero."""
        reader = make_ibkr_paper_positions_reader(
            health_checker=_healthy_paper_health,
            ib_session_factory=MagicMock(
                side_effect=TimeoutError("portfolio() timed out")),
        )
        adapter = self.Adapter(broker_scope="ibkr_paper",
                                positions_reader=reader)
        reading = adapter.read(today="2026-06-02")
        self.assertTrue(reading.is_exposure_unknown())
        self.assertIsNone(reading.capital_deployed_usd,
            "timeout must NOT yield a fake 0.0 exposure")
        self.assertIn("ib_portfolio_read_failed",
                       (reading.error or "").lower())


# ─────────────────────────────────────────────────────────────────────────────
# Group 6 — Dry-run path exists and proves preconditions without DB writes
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotReadyAndSymbolHelpers(unittest.TestCase):
    """Direct tests for the two new helpers added in the cross-confirm
    patch: _wait_for_snapshot_ready (snapshot gate) and
    _symbols_with_position (cross-confirm set extractor)."""

    def test_wait_for_snapshot_ready_returns_immediately_when_populated(self):
        from bot.risk_authority.ibkr_paper_reader import _wait_for_snapshot_ready
        mock_ib = MagicMock()
        # Already populated — the helper should not enter the poll loop
        # at all (or should exit on the first iteration).
        mock_ib.accountValues.return_value = [object(), object()]
        out = _wait_for_snapshot_ready(mock_ib, timeout=5.0)
        self.assertTrue(out["ready"])
        self.assertEqual(out["account_values_count"], 2)

    def test_wait_for_snapshot_ready_times_out_when_empty(self):
        from bot.risk_authority.ibkr_paper_reader import _wait_for_snapshot_ready
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = []
        # Very short timeout so the test finishes quickly.
        out = _wait_for_snapshot_ready(mock_ib, timeout=0.4)
        self.assertFalse(out["ready"])
        self.assertEqual(out["account_values_count"], 0)

    def test_wait_for_snapshot_ready_returns_true_when_values_arrive_late(self):
        from bot.risk_authority.ibkr_paper_reader import _wait_for_snapshot_ready
        mock_ib = MagicMock()
        # Start empty, then turn non-empty after one poll iteration.
        sequence = [[], [object(), object()]]
        mock_ib.accountValues.side_effect = lambda: sequence.pop(0) \
            if sequence else [object()]
        # waitOnUpdate is a no-op for this test; we just need the
        # polling loop to call accountValues twice.
        mock_ib.waitOnUpdate.return_value = None
        out = _wait_for_snapshot_ready(mock_ib, timeout=2.0)
        self.assertTrue(out["ready"])
        self.assertEqual(out["account_values_count"], 2)

    def test_wait_for_snapshot_ready_handles_account_values_exception(self):
        """If accountValues() raises, we treat it as empty (defensive)
        and continue polling until timeout."""
        from bot.risk_authority.ibkr_paper_reader import _wait_for_snapshot_ready
        mock_ib = MagicMock()
        mock_ib.accountValues.side_effect = RuntimeError("transient")
        out = _wait_for_snapshot_ready(mock_ib, timeout=0.4)
        self.assertFalse(out["ready"])

    def test_symbols_with_position_extracts_nonzero(self):
        from bot.risk_authority.ibkr_paper_reader import _symbols_with_position
        items = [
            _FakePortfolioItem(symbol="AAPL", position=10.0),
            _FakePortfolioItem(symbol="MSFT", position=-5.0),
            _FakePortfolioItem(symbol="GOOG", position=0.0),  # zero → skip
        ]
        syms = _symbols_with_position(items)
        self.assertEqual(syms, {"AAPL", "MSFT"})

    def test_symbols_with_position_works_for_position_records_too(self):
        from bot.risk_authority.ibkr_paper_reader import _symbols_with_position
        records = [
            _FakePosition(symbol="AAPL", position=10.0),
            _FakePosition(symbol="VOD",  position=100.0),
        ]
        self.assertEqual(_symbols_with_position(records), {"AAPL", "VOD"})

    def test_symbols_with_position_skips_malformed_items(self):
        from bot.risk_authority.ibkr_paper_reader import _symbols_with_position
        # Item missing .contract entirely.
        class _Broken: pass
        broken = _Broken()
        items = [
            _FakePortfolioItem(symbol="AAPL", position=10.0),
            broken,
        ]
        # Malformed items must NOT cause the set to grow OR raise.
        syms = _symbols_with_position(items)
        self.assertEqual(syms, {"AAPL"})

    def test_symbols_with_position_handles_bool_quantity(self):
        from bot.risk_authority.ibkr_paper_reader import _symbols_with_position
        class _Item:
            contract = _FakeContract("AAPL")
            position = True   # Python: bool ⊂ int; must be rejected
        # bool MUST be treated as malformed → skipped.
        self.assertEqual(_symbols_with_position([_Item()]), set())



    def test_dryrun_happy_path(self):
        items = [
            _FakePortfolioItem(symbol="AAPL", position=10.0,
                                marketValue=1900.0),
            _FakePortfolioItem(symbol="MSFT", position=5.0,
                                marketValue=2100.0),
        ]
        positions_records = [
            _FakePosition(symbol="AAPL", position=10.0),
            _FakePosition(symbol="MSFT", position=5.0),
        ]
        summary = run_paper_dryrun(
            health_checker=_healthy_paper_health,
            ib_session_factory=lambda **kw: _factory_result(
                portfolio_items=items,
                position_records=positions_records,
                snapshot_ready=True,
                account_values_count=7,
            ),
        )
        self.assertTrue(summary["dry_run"])
        self.assertTrue(summary["gateway_ready"])
        self.assertEqual(summary["mode"], "paper")
        self.assertEqual(summary["expected_port"], IBKR_PAPER_PORT)
        self.assertTrue(summary["ib_connect_ok"])
        self.assertTrue(summary["snapshot_ready"])
        self.assertEqual(summary["account_values_count"], 7)
        self.assertTrue(summary["positions_read_ok"])
        self.assertEqual(summary["portfolio_count"], 2)
        self.assertEqual(summary["positions_count"], 2)
        self.assertTrue(summary["cross_confirm_ok"])
        self.assertIsNone(summary["error"])

    def test_dryrun_empty_paper_account_reports_cross_confirm_ok(self):
        summary = run_paper_dryrun(
            health_checker=_healthy_paper_health,
            ib_session_factory=lambda **kw: _factory_result(
                portfolio_items=[],
                position_records=[],
                snapshot_ready=True,
                account_values_count=5,
            ),
        )
        self.assertTrue(summary["snapshot_ready"])
        self.assertEqual(summary["portfolio_count"], 0)
        self.assertEqual(summary["positions_count"], 0)
        self.assertTrue(summary["cross_confirm_ok"],
            "empty + empty + snapshot_ready must agree → cross_confirm_ok=True")
        self.assertIsNone(summary["error"])

    def test_dryrun_reports_snapshot_not_ready(self):
        summary = run_paper_dryrun(
            health_checker=_healthy_paper_health,
            ib_session_factory=lambda **kw: _factory_result(
                portfolio_items=[],
                position_records=[],
                snapshot_ready=False,
                account_values_count=0,
            ),
        )
        self.assertTrue(summary["ib_connect_ok"])
        self.assertFalse(summary["snapshot_ready"])
        self.assertFalse(summary["positions_read_ok"])
        self.assertIn("account_snapshot_not_ready_within_timeout",
                       summary["error"])

    def test_dryrun_reports_cross_confirm_failure(self):
        summary = run_paper_dryrun(
            health_checker=_healthy_paper_health,
            ib_session_factory=lambda **kw: _factory_result(
                portfolio_items=[_FakePortfolioItem(symbol="AAPL",
                                                     position=10.0)],
                position_records=[],
                snapshot_ready=True,
                account_values_count=5,
            ),
        )
        self.assertTrue(summary["snapshot_ready"])
        self.assertTrue(summary["positions_read_ok"])
        self.assertFalse(summary["cross_confirm_ok"])
        self.assertIn("portfolio_positions_disagreement", summary["error"])

    def test_dryrun_refuses_unhealthy_gateway(self):
        summary = run_paper_dryrun(
            health_checker=_unhealthy_login_error_health,
            ib_session_factory=lambda **kw: _factory_result(),
        )
        self.assertFalse(summary["gateway_ready"])
        self.assertFalse(summary["ib_connect_ok"])
        self.assertIsNotNone(summary["error"])
        self.assertIn("GatewayNotReadyError", summary["error"])

    def test_dryrun_reports_factory_failure(self):
        summary = run_paper_dryrun(
            health_checker=_healthy_paper_health,
            ib_session_factory=MagicMock(side_effect=TimeoutError("api timeout")),
        )
        self.assertTrue(summary["gateway_ready"])
        self.assertFalse(summary["ib_connect_ok"])
        self.assertIsNotNone(summary["error"])
        self.assertIn("TimeoutError", summary["error"])

    def test_dryrun_does_not_write_db(self):
        """run_paper_dryrun must NOT open any DB connection. Mock
        sqlite3.connect and assert it is never called."""
        with patch("sqlite3.connect") as mock_connect:
            run_paper_dryrun(
                health_checker=_healthy_paper_health,
                ib_session_factory=lambda **kw: _factory_result(),
            )
        self.assertFalse(mock_connect.called,
            "dry-run touched sqlite3.connect — must not write to DB")


# ─────────────────────────────────────────────────────────────────────────────
# Group 7 — AST: no forbidden surface
# ─────────────────────────────────────────────────────────────────────────────


class TestNoForbiddenSurface(unittest.TestCase):

    MODULE = os.path.join(_REPO, "bot/risk_authority/ibkr_paper_reader.py")
    CLI    = os.path.join(_REPO, "tools/ingest_exposure_state.py")

    FORBIDDEN_IB_METHODS = {
        "placeOrder", "cancelOrder", "modifyOrder", "reqGlobalCancel",
        "reqMktData", "reqHistoricalData", "reqOpenOrders",
        "reqExecutions", "reqMatchingSymbols",
    }
    FORBIDDEN_NAMES_FROM_IB_INSYNC = {
        "Order", "Trade", "MarketOrder", "LimitOrder", "StopOrder",
        "BracketOrder", "ComboLeg",
    }
    FORBIDDEN_HTTP_METHODS = {"post", "delete", "put", "patch"}
    FORBIDDEN_SYSTEMCTL_VERBS = {
        "start", "stop", "restart", "enable", "disable", "mask",
        "unmask", "daemon-reload", "reset-failed", "reload",
    }

    def _load(self, path):
        with open(path) as f:
            return ast.parse(f.read(), filename=path)

    def test_module_does_not_import_order_symbols_from_ib_insync(self):
        tree = self._load(self.MODULE)
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and (node.module or "") == "ib_insync"):
                for a in node.names:
                    if a.name in self.FORBIDDEN_NAMES_FROM_IB_INSYNC:
                        offenders.append(
                            f"ImportFrom ib_insync.{a.name} @{node.lineno}")
        self.assertEqual(offenders, [],
            f"M15.5 module imports order symbols from ib_insync: {offenders}")

    def test_module_does_not_call_forbidden_ib_methods(self):
        tree = self._load(self.MODULE)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                          ast.Attribute):
                if node.func.attr in self.FORBIDDEN_IB_METHODS:
                    offenders.append(
                        f"call .{node.func.attr}() @{node.lineno}")
        self.assertEqual(offenders, [],
            f"M15.5 module calls forbidden IB methods: {offenders}")

    def test_module_does_not_call_http_write_methods(self):
        tree = self._load(self.MODULE)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                          ast.Attribute):
                if node.func.attr in self.FORBIDDEN_HTTP_METHODS:
                    offenders.append(
                        f".{node.func.attr}() @{node.lineno}")
        self.assertEqual(offenders, [],
            f"M15.5 module calls HTTP write methods: {offenders}")

    def test_every_ib_connect_call_has_readonly_true(self):
        """AST-walk every `ib.connect(...)` call in the M15.5 module
        and assert each one passes `readonly=True` as a constant kwarg."""
        tree = self._load(self.MODULE)
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect"):
                readonly_kw = None
                for kw in node.keywords:
                    if kw.arg == "readonly":
                        readonly_kw = kw
                        break
                if readonly_kw is None:
                    offenders.append(
                        f".connect() WITHOUT readonly= kwarg @{node.lineno}")
                else:
                    if not (isinstance(readonly_kw.value, ast.Constant)
                             and readonly_kw.value.value is True):
                        offenders.append(
                            f".connect(readonly=<not True>) @{node.lineno}")
        self.assertEqual(offenders, [],
            f"M15.5 module has .connect() calls without readonly=True: "
            f"{offenders}")

    def test_module_does_not_call_mutating_systemctl(self):
        tree = self._load(self.MODULE)
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and node.args
                    and isinstance(node.args[0], ast.List)):
                els = [e.value for e in node.args[0].elts
                       if isinstance(e, ast.Constant)]
                if els and els[0] == "systemctl" and len(els) > 1 \
                        and els[1] in self.FORBIDDEN_SYSTEMCTL_VERBS:
                    offenders.append(
                        f"systemctl {els[1]} @{node.lineno}")
        self.assertEqual(offenders, [],
            f"M15.5 module calls mutating systemctl: {offenders}")

    def test_cli_ibkr_live_still_raises_not_implemented(self):
        """tools/ingest_exposure_state.py _build_ibkr_exposure_adapter
        for ibkr_live must still raise NotImplementedError."""
        tree = self._load(self.CLI)
        found_target = False
        live_raises_nie = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_build_ibkr_exposure_adapter"):
                found_target = True
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Raise):
                        exc = sub.exc
                        # Either `raise NotImplementedError(...)` or
                        # `raise NotImplementedError`.
                        if isinstance(exc, ast.Call):
                            f = exc.func
                            if isinstance(f, ast.Name) and f.id == "NotImplementedError":
                                live_raises_nie = True
                        elif isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                            live_raises_nie = True
        self.assertTrue(found_target,
            "_build_ibkr_exposure_adapter not found in CLI")
        self.assertTrue(live_raises_nie,
            "ibkr_live path must still raise NotImplementedError")

    def test_cli_only_imports_paper_reader_for_paper_scope(self):
        """Defensive: the CLI must NOT import ibkr_paper_reader at module
        scope (lazy import inside the paper branch only)."""
        with open(self.CLI) as f:
            src = f.read()
        # Module-level imports are at column 0; lazy imports inside
        # functions are indented. The lazy import inside the paper
        # branch is fine; a top-level one would pull the M15.5 module
        # into scanner-isolation subprocess test failures.
        for line in src.splitlines():
            if line.startswith("from bot.risk_authority.ibkr_paper_reader"):
                self.fail(
                    "tools/ingest_exposure_state.py has a TOP-LEVEL "
                    "import of bot.risk_authority.ibkr_paper_reader; "
                    "must be lazy inside _build_ibkr_exposure_adapter")
            if line.startswith("import bot.risk_authority.ibkr_paper_reader"):
                self.fail("same as above, with `import` form")


# ─────────────────────────────────────────────────────────────────────────────
# Group 8 — Scanner isolation carry-forward
# ─────────────────────────────────────────────────────────────────────────────


class TestScannerIsolation(unittest.TestCase):

    def test_importing_scanner_does_not_load_ibkr_paper_reader(self):
        check = (
            "import sys\n"
            "import bot.scanner, bot.strategy, bot.risk, bot.brokers\n"
            "forbidden = [m for m in (\n"
            "    'bot.risk_authority.ibkr_paper_reader',\n"
            "    'ib_insync',\n"
            "    'dashboard.app',\n"
            "    'tools.etoro_live_write',\n"
            "    'bot.risk_authority.preflight',\n"
            ") if m in sys.modules]\n"
            "print('loaded_forbidden:', forbidden)\n"
            "sys.exit(0 if not forbidden else 1)\n"
        )
        r = subprocess.run([sys.executable, "-c", check],
                            capture_output=True, text=True, cwd=_REPO)
        self.assertEqual(r.returncode, 0,
            f"scanner-isolation violated. stdout={r.stdout!r} stderr={r.stderr!r}")

    def test_importing_ibkr_paper_reader_does_not_load_ib_insync(self):
        """Lazy import discipline: importing the M15.5 module without
        calling its factories must NOT pull ib_insync into sys.modules."""
        check = (
            "import sys\n"
            "import bot.risk_authority.ibkr_paper_reader\n"
            "loaded = 'ib_insync' in sys.modules\n"
            "print('ib_insync_loaded:', loaded)\n"
            "sys.exit(0 if not loaded else 1)\n"
        )
        r = subprocess.run([sys.executable, "-c", check],
                            capture_output=True, text=True, cwd=_REPO)
        self.assertEqual(r.returncode, 0,
            f"importing ibkr_paper_reader pulled ib_insync: "
            f"stdout={r.stdout!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 9 — Protected files unchanged + M14.D adapter contract untouched
# ─────────────────────────────────────────────────────────────────────────────


class TestProtectedFilesUntouched(unittest.TestCase):

    BASE_REV = "d73a04a"  # pre-M15.5 HEAD (M15.4 docs-closeout)

    PROTECTED = (
        "main.py",
        "bot/scanner.py",
        "bot/strategy.py",
        "bot/risk.py",
        "bot/risk_authority/engine.py",
        "bot/risk_authority/governor.py",
        "bot/risk_authority/authority.py",
        "bot/risk_authority/snapshot.py",
        "bot/risk_authority/audit_decisions.py",
        "bot/risk_authority/preflight.py",
        "bot/risk_authority/ingest_ibkr_exposure.py",  # M14.D adapter frozen
        "bot/etoro/live_broker.py",
        "tools/etoro_live_write.py",
        "bot/gateway_watchdog.py",
        "bot/gateway_health.py",
        "infra/systemd/algo-trader.service",
        "infra/systemd/algo-trader-dashboard.service",
        "infra/systemd/ibgateway.service.documented",
        "sync.sh",
        "deploy.sh",
    )

    def test_no_protected_file_modified(self):
        offenders = []
        for f in self.PROTECTED:
            r = subprocess.run(
                ["git", "diff", "--stat", self.BASE_REV, "--", f],
                capture_output=True, text=True, cwd=_REPO)
            if r.stdout.strip():
                offenders.append(f"{f}: {r.stdout.strip()}")
        self.assertEqual(offenders, [],
            f"M15.5 modified protected files: {offenders}")


# ─────────────────────────────────────────────────────────────────────────────
# Group 10 — CLI preserved
# ─────────────────────────────────────────────────────────────────────────────


class TestCLIPreserved(unittest.TestCase):

    def test_existing_cli_flags_still_present(self):
        with open(os.path.join(_REPO, "tools/ingest_exposure_state.py")) as f:
            src = f.read()
        for flag in ("--db", "--scope", "--all", "--today",
                      "--dry-run", "--fail-on-unknown"):
            self.assertIn(f'"{flag}"', src,
                f"existing CLI flag {flag} was removed")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)

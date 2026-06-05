"""test_m12_cancel.py — P0-2 IBKR cancel() format-aware tests.

Verifies bot.brokers.ibkr_broker.IBKRBroker.cancel() supports both
the canonical 'IB-PERM-{permId}' format (preferred, broker-assigned
permId) and the legacy 'IB-{orderId}-{tp}-{sl}' fallback format.

Recorded at the M1-M16 audit pass (P0-2); previous implementation
did int(broker_order_id.split('-')[1]) which raised ValueError on
canonical 'IB-PERM-...' IDs ('PERM' is not an int), silently
swallowed by `except Exception`, returning False with no actionable
log line. Result: cancel was broken for the preferred format used
in production audit trails.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.brokers.ibkr_broker import IBKRBroker


def _fake_order(*, order_id=None, perm_id=None):
    """Build a minimal ib_insync-Order-shaped namespace with the
    attributes our cancel() reads (orderId, permId)."""
    return SimpleNamespace(orderId=order_id, permId=perm_id)


def _fake_ib_with_open_orders(open_orders):
    """Build a MagicMock IB session that .openOrders() returns the
    supplied list. .cancelOrder is mocked so tests can assert on it.
    .isConnected() returns True so the finally-block's disconnect
    path runs."""
    ib = MagicMock()
    ib.openOrders.return_value = list(open_orders)
    ib.isConnected.return_value = True
    return ib


class TestCancelPermFormat(unittest.TestCase):
    """Canonical 'IB-PERM-{permId}' branch."""

    def test_cancel_perm_format_succeeds(self):
        """IB-PERM-12345 matches the order whose .permId == 12345."""
        target_order = _fake_order(order_id=7, perm_id=12345)
        other_order = _fake_order(order_id=9, perm_id=99999)
        fake_ib = _fake_ib_with_open_orders([other_order, target_order])

        broker = IBKRBroker()
        with patch.object(broker, "_connect", return_value=fake_ib):
            result = broker.cancel("IB-PERM-12345")

        self.assertTrue(result)
        fake_ib.cancelOrder.assert_called_once_with(target_order)
        fake_ib.disconnect.assert_called_once()

    def test_cancel_perm_format_not_found_returns_false(self):
        """openOrders has no order with the requested permId → False."""
        unrelated = _fake_order(order_id=1, perm_id=2)
        fake_ib = _fake_ib_with_open_orders([unrelated])

        broker = IBKRBroker()
        with patch.object(broker, "_connect", return_value=fake_ib):
            result = broker.cancel("IB-PERM-99999")

        self.assertFalse(result)
        fake_ib.cancelOrder.assert_not_called()
        fake_ib.disconnect.assert_called_once()

    def test_cancel_malformed_perm_returns_false_no_connect(self):
        """'IB-PERM-not-a-number' must NOT raise and must NOT even
        attempt a connection — fail fast before any I/O."""
        broker = IBKRBroker()
        with patch.object(broker, "_connect") as mock_connect:
            result = broker.cancel("IB-PERM-not-a-number")

        self.assertFalse(result)
        mock_connect.assert_not_called()


class TestCancelLegacyFormat(unittest.TestCase):
    """Legacy 'IB-{orderId}-{tp}-{sl}' fallback branch — unchanged
    behaviour from pre-P0-2."""

    def test_cancel_legacy_format_still_works(self):
        """IB-42-43-44 matches the order whose .orderId == 42."""
        target_order = _fake_order(order_id=42, perm_id=0)
        other_order = _fake_order(order_id=99, perm_id=0)
        fake_ib = _fake_ib_with_open_orders([other_order, target_order])

        broker = IBKRBroker()
        with patch.object(broker, "_connect", return_value=fake_ib):
            result = broker.cancel("IB-42-43-44")

        self.assertTrue(result)
        fake_ib.cancelOrder.assert_called_once_with(target_order)

    def test_cancel_legacy_format_orderid_not_int_returns_false(self):
        """'IB-x-43-44' — parent segment not int → False, no connect."""
        broker = IBKRBroker()
        with patch.object(broker, "_connect") as mock_connect:
            result = broker.cancel("IB-x-43-44")

        self.assertFalse(result)
        mock_connect.assert_not_called()


class TestCancelUnknownFormat(unittest.TestCase):
    """Anything that doesn't start with 'IB-' or 'IB-PERM-' is
    rejected before any I/O."""

    def test_cancel_unknown_prefix_returns_false_no_connect(self):
        broker = IBKRBroker()
        with patch.object(broker, "_connect") as mock_connect:
            result = broker.cancel("GARBAGE-FORMAT")
        self.assertFalse(result)
        mock_connect.assert_not_called()

    def test_cancel_empty_string_returns_false(self):
        broker = IBKRBroker()
        with patch.object(broker, "_connect") as mock_connect:
            result = broker.cancel("")
        self.assertFalse(result)
        mock_connect.assert_not_called()

    def test_cancel_non_string_returns_false(self):
        broker = IBKRBroker()
        with patch.object(broker, "_connect") as mock_connect:
            result = broker.cancel(12345)  # type: ignore[arg-type]
        self.assertFalse(result)
        mock_connect.assert_not_called()


class TestCancelBrokerExceptionSwallowed(unittest.TestCase):
    """Even with a valid format, broker I/O exceptions must not
    propagate. This preserves the broker-base contract that
    cancel() returns bool, never raises."""

    def test_cancel_perm_format_with_broker_exception_returns_false(self):
        broker = IBKRBroker()
        with patch.object(broker, "_connect",
                            side_effect=ConnectionError("Gateway down")):
            result = broker.cancel("IB-PERM-12345")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)

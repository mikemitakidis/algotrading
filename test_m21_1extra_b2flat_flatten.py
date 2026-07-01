#!/usr/bin/env python3
"""M21.1extra-B2flat — tests for the paper-only flatten primitive + harness.

Gateway interaction mocked; no IB Gateway required. Proves the 12 gates:
  1. live mode refused
  2. wrong account refused
  3. wrong port refused
  4. kill switch blocks
  5. explicit target (symbol) required
  6. no "flatten everything" default (confirm required; no symbol -> refuse)
  7. mock position flatten cancels target orders THEN places one offsetting close
  8. post-flatten reconcile success -> flatten_confirmed=True
  9. post-flatten reconcile warning/failure -> flatten_confirmed=False
 10. existing submit/reconcile/cancel signatures unchanged
 11. existing submit/reconcile/cancel bodies not modified (purely additive diff)
 12. AST/source guard bans eToro/Telegram/scheduler/dashboard/live-origination
"""
from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import types
import unittest

# The adapter method does `from ib_insync import MarketOrder`. ib_insync is not
# installed in CI/sandbox (only on the VPS), so inject a minimal stub BEFORE
# importing anything that triggers that import path. The stub records action +
# totalQuantity exactly like the real MarketOrder for assertion purposes.
if "ib_insync" not in sys.modules:
    _stub = types.ModuleType("ib_insync")

    class _StubMarketOrder:
        def __init__(self, action, totalQuantity):
            self.action = action
            self.totalQuantity = totalQuantity
            self.account = None
            self.orderId = 0

    _stub.MarketOrder = _StubMarketOrder
    sys.modules["ib_insync"] = _stub

from bot.brokers.ibkr_broker import IBKRBroker
import tools.paper_loop.m21_1extra_b2flat_flatten_dryrun as B2FLAT

_HARNESS_PATH = "tools/paper_loop/m21_1extra_b2flat_flatten_dryrun.py"


# ---- mock ib_insync surface for the adapter method -------------------------

class _MockContract:
    def __init__(self, symbol):
        self.symbol = symbol


class _MockOrder:
    def __init__(self, symbol, order_id=1, perm_id=None):
        self.contract = _MockContract(symbol)
        self.orderId = order_id
        self.permId = perm_id


class _MockPosition:
    def __init__(self, symbol, position):
        self.contract = _MockContract(symbol)
        self.position = position


class _MockTrade:
    def __init__(self, order):
        self.order = order


class _MockOpenTrade:
    """ib_insync Trade-like: carries BOTH contract and order (the reliable
    contract-aware source the flatten primitive uses)."""
    def __init__(self, symbol, order):
        self.contract = _MockContract(symbol) if symbol is not None else None
        self.order = order


class _MockIB:
    """Enough of ib_insync.IB for flatten_paper_position."""

    def __init__(self, *, open_orders, positions, positions_after=None,
                 cancel_clears=True, open_trades=None, ambiguous_trade=False,
                 trades_after_close=None):
        self._open_orders = list(open_orders)
        self._positions = list(positions)
        self._positions_after = (positions_after if positions_after is not None
                                 else [])
        self._cancel_clears = cancel_clears
        self.cancelled = []
        self.placed = []
        self._flattened = False
        self.call_order = []          # records sequence of broker ops
        # Optional explicit open-trades state to return AFTER the close order is
        # placed (simulates a leg that survived / an ambiguous trade appearing).
        self._trades_after_close = trades_after_close
        # Contract-aware open trades. If not supplied, derive from open_orders.
        if open_trades is not None:
            self._open_trades = list(open_trades)
        else:
            self._open_trades = [
                _MockOpenTrade(getattr(o.contract, 'symbol', None), o)
                for o in self._open_orders]
        if ambiguous_trade:
            # a trade whose contract/order cannot be resolved
            self._open_trades.append(_MockOpenTrade(None, None))

    def isConnected(self):
        return True

    def openOrders(self):
        self.call_order.append("openOrders")
        return list(self._open_orders)

    def openTrades(self):
        self.call_order.append("openTrades")
        if self._flattened and self._trades_after_close is not None:
            return list(self._trades_after_close)
        return list(self._open_trades)

    def cancelOrder(self, order):
        self.call_order.append("cancelOrder")
        self.cancelled.append(order)
        if self._cancel_clears:
            self._open_orders = [o for o in self._open_orders if o is not order]
            self._open_trades = [t for t in self._open_trades
                                 if getattr(t, 'order', None) is not order]

    def positions(self, account=None):
        self.call_order.append("positions")
        # before the close order is placed -> original; after -> post state
        return list(self._positions_after if self._flattened
                    else self._positions)

    def placeOrder(self, contract, order):
        self.call_order.append("placeOrder")
        self.placed.append((contract, order))
        self._flattened = True          # simulate the close filling
        return _MockTrade(order)

    def sleep(self, *_a):
        pass

    def disconnect(self):
        pass


class _FlattenBroker(IBKRBroker):
    """IBKRBroker with _connect(), reconcile(), and _verify_account() stubbed,
    so flatten_paper_position runs against the mock IB without a real gateway.
    is_live and name are read-only properties on IBKRBroker (env-driven), so we
    do not set them — in paper test env is_live is already False."""

    def __init__(self, mock_ib, recon_after, account_ok=True):
        # deliberately bypass IBKRBroker.__init__ side effects
        self._mock_ib = mock_ib
        self._recon_after = recon_after
        self._account_ok = account_ok

    def _connect(self):
        return self._mock_ib

    def _verify_account(self, ib):
        return (self._account_ok,
                "ok" if self._account_ok else "account mismatch")

    def reconcile(self):
        return self._recon_after


def _clean_recon():
    return {"open_orders": [], "positions": [], "warnings": []}


class TestPaperModeGates(unittest.TestCase):
    def test_1_live_mode_refused(self):
        prev = os.environ.get("BROKER")
        os.environ["BROKER"] = "ibkr_live"
        try:
            with self.assertRaises(B2FLAT.PaperModeRefused):
                B2FLAT.run_flatten("AAA", confirm=True,
                                   kill_switch_active=False)
        finally:
            if prev is None:
                os.environ.pop("BROKER", None)
            else:
                os.environ["BROKER"] = prev

    def test_2_wrong_account_refused(self):
        prev = os.environ.get("IBKR_ACCOUNT")
        os.environ["IBKR_ACCOUNT"] = "OTHER"
        try:
            with self.assertRaises(B2FLAT.PaperModeRefused):
                B2FLAT.run_flatten("AAA", confirm=True,
                                   kill_switch_active=False)
        finally:
            if prev is None:
                os.environ.pop("IBKR_ACCOUNT", None)
            else:
                os.environ["IBKR_ACCOUNT"] = prev

    def test_3_wrong_port_refused(self):
        prev = os.environ.get("IBKR_PORT")
        os.environ["IBKR_PORT"] = "4001"
        try:
            with self.assertRaises(B2FLAT.PaperModeRefused):
                B2FLAT.run_flatten("AAA", confirm=True,
                                   kill_switch_active=False)
        finally:
            if prev is None:
                os.environ.pop("IBKR_PORT", None)
            else:
                os.environ["IBKR_PORT"] = prev

    def test_4_kill_switch_blocks(self):
        d = B2FLAT.run_flatten("AAA", confirm=True, kill_switch_active=True)
        self.assertTrue(d["kill_switch_active"])
        self.assertFalse(d["flatten_confirmed"])


class TestAdapterFlattenGates(unittest.TestCase):
    """Direct tests of IBKRBroker.flatten_paper_position via a stub broker."""

    def _broker(self, *, open_orders, positions, recon_after,
                cancel_clears=True, account_ok=True, open_trades=None,
                ambiguous_trade=False, trades_after_close=None):
        mock_ib = _MockIB(open_orders=open_orders, positions=positions,
                          cancel_clears=cancel_clears, open_trades=open_trades,
                          ambiguous_trade=ambiguous_trade,
                          trades_after_close=trades_after_close)
        return _FlattenBroker(mock_ib, recon_after, account_ok=account_ok), \
            mock_ib

    def test_5_explicit_symbol_required(self):
        b, _ = self._broker(open_orders=[], positions=[],
                            recon_after=_clean_recon())
        d = b.flatten_paper_position("", confirm=True)
        self.assertFalse(d["flatten_confirmed"])
        self.assertTrue(any("symbol required" in w for w in d["warnings"]))

    def test_6_confirm_required_no_default_flatten(self):
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA")],
            positions=[_MockPosition("AAA", 10)], recon_after=_clean_recon())
        d = b.flatten_paper_position("AAA", confirm=False)
        self.assertFalse(d["flatten_confirmed"])
        self.assertFalse(d["close_order_placed"])
        self.assertEqual(mock_ib.placed, [])       # no broker action
        self.assertEqual(mock_ib.cancelled, [])

    def test_7_cancels_orders_then_places_single_close(self):
        pos = _MockPosition("AAA", 10)              # long 10
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA", order_id=1),
                         _MockOrder("BBB", order_id=2)],  # BBB must be ignored
            positions=[pos], recon_after=_clean_recon())
        d = b.flatten_paper_position("AAA", confirm=True)
        # cancelled only the AAA order, not BBB
        self.assertEqual(len(mock_ib.cancelled), 1)
        self.assertEqual(mock_ib.cancelled[0].contract.symbol, "AAA")
        # exactly ONE close order, SELL (to offset long), qty 10
        self.assertEqual(len(mock_ib.placed), 1)
        _contract, close = mock_ib.placed[0]
        self.assertEqual(close.action, "SELL")
        self.assertEqual(close.totalQuantity, 10)
        self.assertTrue(d["close_order_placed"])

    def test_7_short_position_closes_with_buy(self):
        b, mock_ib = self._broker(
            open_orders=[], positions=[_MockPosition("AAA", -5)],
            recon_after=_clean_recon())
        b.flatten_paper_position("AAA", confirm=True)
        _c, close = mock_ib.placed[0]
        self.assertEqual(close.action, "BUY")
        self.assertEqual(close.totalQuantity, 5)

    def test_7_cancel_happens_before_close_order(self):
        # Ordering proof: every cancelOrder precedes the placeOrder in the
        # recorded broker-op sequence.
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA", order_id=1)],
            positions=[_MockPosition("AAA", 10)], recon_after=_clean_recon())
        b.flatten_paper_position("AAA", confirm=True)
        seq = mock_ib.call_order
        self.assertIn("cancelOrder", seq)
        self.assertIn("placeOrder", seq)
        self.assertLess(seq.index("cancelOrder"), seq.index("placeOrder"))

    def test_11_already_flat_no_position_no_orders(self):
        # No position, no target orders -> already_flat true, confirmed true,
        # nothing placed.
        b, mock_ib = self._broker(
            open_orders=[], positions=[], recon_after=_clean_recon())
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertTrue(d["already_flat"])
        self.assertTrue(d["flatten_confirmed"])
        self.assertFalse(d["close_order_placed"])
        self.assertEqual(mock_ib.placed, [])

    def test_12_post_cancel_orders_remaining_blocks_close(self):
        # cancel does NOT clear the order (silent failure) -> primitive must
        # refuse to place a close and report flatten_confirmed=false.
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA", order_id=1)],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(), cancel_clears=False)
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["post_cancel_open_orders_cleared"])
        self.assertFalse(d["flatten_confirmed"])
        self.assertEqual(mock_ib.placed, [])     # no close placed over live legs
        self.assertTrue(any("could not prove" in w or "refusing" in w
                            for w in d["warnings"]))

    def test_contract_aware_openTrades_used_for_cancel(self):
        # Target legs are cancelled via openTrades (contract-aware), and BBB is
        # left alone.
        aaa = _MockOrder("AAA", order_id=1)
        bbb = _MockOrder("BBB", order_id=2)
        trades = [_MockOpenTrade("AAA", aaa), _MockOpenTrade("BBB", bbb)]
        b, mock_ib = self._broker(
            open_orders=[aaa, bbb], positions=[_MockPosition("AAA", 4)],
            recon_after=_clean_recon(), open_trades=trades)
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertEqual([o for o in mock_ib.cancelled], [aaa])  # only AAA
        self.assertIn("openTrades", mock_ib.call_order)
        self.assertTrue(d["flatten_confirmed"])

    def test_order_without_contract_does_not_false_clear(self):
        # An ambiguous open trade (no contract/order) must NOT be read as
        # "cleared". Primitive fails closed: no close, flatten_confirmed=false.
        b, mock_ib = self._broker(
            open_orders=[], positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(), ambiguous_trade=True)
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["post_cancel_open_orders_cleared"])
        self.assertFalse(d["flatten_confirmed"])
        self.assertEqual(mock_ib.placed, [])
        self.assertTrue(any("could not prove" in w for w in d["warnings"]))

    def test_account_verification_failure_refuses(self):
        # _verify_account fails after connect -> no cancel, no close.
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA")],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(), account_ok=False)
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["account_verified"])
        self.assertFalse(d["flatten_confirmed"])
        self.assertEqual(mock_ib.cancelled, [])
        self.assertEqual(mock_ib.placed, [])
        self.assertTrue(any("account verification failed" in w
                            for w in d["warnings"]))

    def test_account_verified_proceeds_normally(self):
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA")],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(), account_ok=True)
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertTrue(d["account_verified"])
        self.assertTrue(d["flatten_confirmed"])
        self.assertEqual(len(mock_ib.placed), 1)

    def test_final_openTrades_target_remains_not_confirmed(self):
        # After the close, a target trade still appears in openTrades ->
        # flatten_confirmed must be False even if reconcile looks clean.
        leftover = _MockOrder("AAA", order_id=9)
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA", order_id=1)],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(),
            trades_after_close=[_MockOpenTrade("AAA", leftover)])
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["flatten_confirmed"])
        self.assertTrue(any("post-flatten open trades not cleared" in w
                            for w in d["warnings"]))

    def test_final_openTrades_ambiguous_not_confirmed(self):
        # After the close, an ambiguous trade appears -> not confirmed.
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA", order_id=1)],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(),
            trades_after_close=[_MockOpenTrade(None, None)])
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["flatten_confirmed"])
        self.assertTrue(any("not cleared / ambiguous" in w
                            for w in d["warnings"]))

    def test_final_openTrades_clear_and_reconcile_clean_confirmed(self):
        # After the close, openTrades clear AND reconcile clean -> confirmed.
        b, mock_ib = self._broker(
            open_orders=[_MockOrder("AAA", order_id=1)],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon(),
            trades_after_close=[])          # nothing left
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertTrue(d["flatten_confirmed"])
        self.assertEqual(len(mock_ib.placed), 1)

    def test_8_reconcile_clean_sets_confirmed_true(self):
        b, _ = self._broker(
            open_orders=[_MockOrder("AAA")],
            positions=[_MockPosition("AAA", 10)],
            recon_after=_clean_recon())
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertTrue(d["flatten_confirmed"])

    def test_9_reconcile_failure_sets_confirmed_false(self):
        b, _ = self._broker(
            open_orders=[_MockOrder("AAA")],
            positions=[_MockPosition("AAA", 10)],
            recon_after={"open_orders": [], "positions": [],
                         "warnings": ["reconcile failed: lost conn"]})
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["flatten_confirmed"])
        self.assertTrue(any("NOT verified" in w for w in d["warnings"]))

    def test_9_residual_position_sets_confirmed_false(self):
        b, _ = self._broker(
            open_orders=[_MockOrder("AAA")],
            positions=[_MockPosition("AAA", 10)],
            recon_after={"open_orders": [],
                         "positions": [{"symbol": "AAA", "position": 10}],
                         "warnings": []})
        d = b.flatten_paper_position("AAA", confirm=True)
        self.assertFalse(d["flatten_confirmed"])


class TestExistingBehaviourUnchanged(unittest.TestCase):
    def test_10_existing_signatures_unchanged(self):
        self.assertEqual(str(inspect.signature(IBKRBroker.submit)),
                         "(self, intent: bot.brokers.base.OrderIntent) -> "
                         "bot.brokers.base.OrderResult")
        self.assertEqual(str(inspect.signature(IBKRBroker.reconcile)),
                         "(self) -> dict")
        self.assertEqual(str(inspect.signature(IBKRBroker.cancel)),
                         "(self, broker_order_id: str) -> bool")

    def test_11_diff_is_purely_additive_for_adapter(self):
        # The only change to bot/brokers/ibkr_broker.py must be additions
        # (the new method); no existing line removed/modified.
        r = subprocess.run(
            ["git", "diff", "origin/main", "--", "bot/brokers/ibkr_broker.py"],
            capture_output=True, text=True)
        removed = [ln for ln in r.stdout.splitlines()
                   if ln.startswith("-") and not ln.startswith("---")]
        self.assertEqual(removed, [], "adapter diff must be purely additive")


class TestOutputAndSourceGuard(unittest.TestCase):
    def test_harness_writes_only_tmp(self):
        env = {**os.environ, "PYTHONPATH": os.getcwd()}
        r = subprocess.run(
            [sys.executable, "-m",
             "tools.paper_loop.m21_1extra_b2flat_flatten_dryrun",
             "--symbol", "AAA",
             "--i-understand-this-closes-a-paper-position",
             "--report", "reports/should_refuse.md"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/tmp/", (r.stderr + r.stdout))

    def test_12_harness_source_guard(self):
        import tokenize
        with open(_HARNESS_PATH, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
        names = {t.string for t in toks if t.type == tokenize.NAME}
        forbidden = {"etoro", "telegram", "notifier", "scheduler",
                     "apscheduler", "dashboard", "OrderIntent", "submit",
                     "_make_bracket"}
        self.assertEqual(names & forbidden, set(),
                         "forbidden tokens in harness code")

    def test_harness_imports_constrained(self):
        with open(_HARNESS_PATH) as fh:
            tree = ast.parse(fh.read())
        mods = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                mods.append(n.module or "")
        for m in mods:
            self.assertFalse(m.startswith("bot.etoro"))
            self.assertNotEqual(m, "main")
            self.assertFalse(m.startswith("dashboard"))

    def test_harness_does_not_originate_entry(self):
        # The harness must never call submit / build brackets.
        with open(_HARNESS_PATH) as fh:
            src = fh.read()
        self.assertNotIn(".submit(", src)
        self.assertNotIn("_make_bracket", src)
        self.assertNotIn("OrderIntent", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

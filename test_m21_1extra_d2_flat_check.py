#!/usr/bin/env python3
"""M21.1extra-D2 — flat verifier tests. Proves NO false-green is possible:
any ambiguous/unattributable open trade or position, any disconnect, any wrong
account, or any warning yields UNKNOWN — never YES.
"""
from __future__ import annotations

import ast
import unittest

from tools.paper_loop.m21_1extra_d2_flat_check import (
    decide, classify_open_trades, classify_positions, verify_flat,
    _EXPECTED_ACCOUNT,
)

SRC = "tools/paper_loop/m21_1extra_d2_flat_check.py"


class _Contract:
    def __init__(self, symbol):
        self.symbol = symbol


class _Order:
    def __init__(self, oid=1):
        self.orderId = oid


class _Trade:
    def __init__(self, symbol=None, has_order=True, has_contract=True):
        self.contract = _Contract(symbol) if has_contract else None
        self.order = _Order() if has_order else None


class _Pos:
    def __init__(self, symbol=None, position=0, has_contract=True):
        self.contract = _Contract(symbol) if has_contract else None
        self.position = position


def _ok(**kw):
    base = dict(connected=True, account_ok=True, open_trades=[], positions=[],
                symbol="AAPL", account=_EXPECTED_ACCOUNT, warnings=[])
    base.update(kw)
    return decide(**base)


class TestDecideFlat(unittest.TestCase):
    def test_clean_flat_yes(self):
        self.assertEqual(_ok(), "YES")

    def test_other_symbol_only_yes(self):
        self.assertEqual(_ok(open_trades=[_Trade("MSFT")],
                             positions=[_Pos("MSFT", 5)]), "YES")


class TestDecideNo(unittest.TestCase):
    def test_target_position_no(self):
        self.assertEqual(_ok(positions=[_Pos("AAPL", 1)]), "NO")

    def test_target_open_trade_no(self):
        self.assertEqual(_ok(open_trades=[_Trade("AAPL")]), "NO")

    def test_target_short_position_no(self):
        self.assertEqual(_ok(positions=[_Pos("AAPL", -1)]), "NO")


class TestDecideUnknownFailClosed(unittest.TestCase):
    def test_not_connected_unknown(self):
        self.assertEqual(_ok(connected=False), "UNKNOWN")

    def test_account_not_ok_unknown(self):
        self.assertEqual(_ok(account_ok=False), "UNKNOWN")

    def test_wrong_account_unknown(self):
        self.assertEqual(_ok(account="WRONG123"), "UNKNOWN")

    def test_any_warning_unknown(self):
        self.assertEqual(_ok(warnings=["reconcile failed: timeout"]), "UNKNOWN")

    def test_ambiguous_trade_missing_contract_unknown(self):
        # THE blocker case: an order we cannot attribute to a symbol.
        self.assertEqual(_ok(open_trades=[_Trade(None, has_contract=False)]),
                         "UNKNOWN")

    def test_ambiguous_trade_missing_order_unknown(self):
        self.assertEqual(_ok(open_trades=[_Trade("AAPL", has_order=False)]),
                         "UNKNOWN")

    def test_ambiguous_trade_none_symbol_unknown(self):
        self.assertEqual(_ok(open_trades=[_Trade(None)]), "UNKNOWN")

    def test_ambiguous_position_missing_contract_unknown(self):
        self.assertEqual(_ok(positions=[_Pos(None, 1, has_contract=False)]),
                         "UNKNOWN")

    def test_ambiguous_wins_even_with_clean_target(self):
        # a clean non-target trade AND an ambiguous one -> UNKNOWN, not YES
        self.assertEqual(
            _ok(open_trades=[_Trade("MSFT"), _Trade(None, has_contract=False)]),
            "UNKNOWN")

    def test_ambiguous_position_and_target_absent_unknown(self):
        # ambiguous position but no proven target -> still UNKNOWN (can't prove)
        self.assertEqual(_ok(positions=[_Pos(None, 3, has_contract=False)]),
                         "UNKNOWN")


class TestClassifiers(unittest.TestCase):
    def test_open_trades_ambiguous_flag(self):
        has, amb = classify_open_trades([_Trade(None, has_contract=False)],
                                        "AAPL")
        self.assertFalse(has)
        self.assertTrue(amb)

    def test_open_trades_target(self):
        has, amb = classify_open_trades([_Trade("AAPL")], "AAPL")
        self.assertTrue(has)
        self.assertFalse(amb)

    def test_positions_ambiguous(self):
        has, amb = classify_positions([_Pos(None, 1, has_contract=False)],
                                      "AAPL", _EXPECTED_ACCOUNT)
        self.assertTrue(amb)

    def test_positions_zero_size_not_target(self):
        has, amb = classify_positions([_Pos("AAPL", 0)], "AAPL",
                                      _EXPECTED_ACCOUNT)
        self.assertFalse(has)
        self.assertFalse(amb)


class TestVerifyFlatFailClosed(unittest.TestCase):
    def test_broker_factory_raises_unknown(self):
        def boom():
            raise RuntimeError("no gateway")
        self.assertEqual(verify_flat("AAPL", broker_factory=boom), "UNKNOWN")

    def test_live_broker_unknown(self):
        class Live:
            is_live = True
        self.assertEqual(verify_flat("AAPL", broker_factory=Live), "UNKNOWN")

    def test_connect_raises_unknown(self):
        class B:
            is_live = False
            def _connect(self):
                raise ConnectionError("gateway down")
        self.assertEqual(verify_flat("AAPL", broker_factory=B), "UNKNOWN")


class TestHygiene(unittest.TestCase):
    def _read(self):
        with open(SRC) as fh:
            return fh.read()

    def _code_only(self, src):
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
        return " ".join(out)

    def test_read_only_no_mutation_calls(self):
        # the verifier must NEVER cancel/close/submit — read-only.
        code = self._code_only(self._read())
        for bad in ("cancelOrder", "placeOrder", "flatten_paper_position",
                    "submit", "cancelAllOrders"):
            self.assertNotIn(bad, code, "flat check must be read-only (%s)" % bad)

    def test_no_safety_assert(self):
        tree = ast.parse(self._read())
        self.assertEqual(
            [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

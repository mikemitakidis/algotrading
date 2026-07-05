#!/usr/bin/env python3
"""M21.1extra-D2 — read-only, fail-closed account flat verifier.

Independently verifies whether a target symbol is genuinely FLAT after the
supervised paper lifecycle proof — with NO false-green risk. It mirrors the
contract-aware final-proof semantics of
IBKRBroker.flatten_paper_position (openTrades() + positions() on one
connection), but is strictly READ-ONLY: it never cancels, closes, or submits.

verify_flat(...) returns one of:
    "YES"      -> connection ok, account == DUP623346, no target position,
                  no target open trade, NO ambiguous open trade, no warnings.
    "NO"       -> a target position or a target open trade is present.
    "UNKNOWN"  -> anything that prevents PROVING flatness: not connected,
                  wrong/failed account, ANY ambiguous open trade whose
                  symbol/order cannot be attributed, or any broker/query error.

Rationale: ib.openOrders() Order entries may lack a contract, so an AAPL order
could appear as symbol '?' and slip past a naive match. ib.openTrades() yields
Trade objects that reliably carry BOTH trade.contract and trade.order; a trade
missing either is AMBIGUOUS and forces UNKNOWN (never YES).
"""
from __future__ import annotations

from typing import Any, Callable, List, Tuple

_EXPECTED_ACCOUNT = "DUP623346"


def classify_open_trades(open_trades: List[Any], symbol: str
                         ) -> Tuple[bool, bool]:
    """Return (has_target_open, ambiguous).

    ambiguous is True if ANY trade lacks a readable order or contract symbol —
    i.e. we cannot prove it is NOT the target. has_target_open is True if a
    trade's contract symbol equals the target symbol."""
    has_target = False
    ambiguous = False
    for tr in open_trades:
        c = getattr(tr, "contract", None)
        o = getattr(tr, "order", None)
        csym = getattr(c, "symbol", None) if c is not None else None
        if o is None or csym is None:
            # cannot attribute this order to a symbol -> ambiguous, fail closed
            ambiguous = True
            continue
        if csym == symbol:
            has_target = True
    return has_target, ambiguous


def classify_positions(positions: List[Any], symbol: str, account: str
                       ) -> Tuple[bool, bool]:
    """Return (has_target_position, ambiguous).

    A position row must carry a readable contract symbol; one that doesn't is
    ambiguous. A target position with non-zero size counts as present."""
    has_target = False
    ambiguous = False
    for p in positions:
        c = getattr(p, "contract", None)
        csym = getattr(c, "symbol", None) if c is not None else None
        size = getattr(p, "position", None)
        if csym is None or size is None:
            ambiguous = True
            continue
        if csym == symbol and abs(size) > 0:
            has_target = True
    return has_target, ambiguous


def decide(*, connected: bool, account_ok: bool,
           open_trades: List[Any], positions: List[Any],
           symbol: str, account: str, warnings: List[str]) -> str:
    """Pure decision function (unit-tested). Fail closed on anything that
    prevents proving flatness."""
    if warnings:
        return "UNKNOWN"
    if not connected:
        return "UNKNOWN"
    if not account_ok or str(account).strip() != _EXPECTED_ACCOUNT:
        return "UNKNOWN"
    ot_target, ot_ambiguous = classify_open_trades(open_trades, symbol)
    pos_target, pos_ambiguous = classify_positions(positions, symbol, account)
    if ot_ambiguous or pos_ambiguous:
        # cannot prove the ambiguous entry isn't the target -> not flat
        return "UNKNOWN"
    if ot_target or pos_target:
        return "NO"
    return "YES"


def verify_flat(symbol: str, *, broker_factory: Callable[[], Any] = None
                ) -> str:
    """Connect once (read-only), gather openTrades() + positions() + account,
    and classify. Any exception -> UNKNOWN. Never cancels/closes/submits."""
    warnings: List[str] = []
    try:
        if broker_factory is None:
            from bot.brokers.ibkr_broker import IBKRBroker
            broker_factory = IBKRBroker
        broker = broker_factory()

        # refuse to even try if the broker reports live mode.
        if getattr(broker, "is_live", False):
            return "UNKNOWN"

        ib = broker._connect()   # raises on failure -> caught below
        from bot.brokers.ibkr_broker import _get_connection_params
        _, port, account, _ = _get_connection_params()

        # account must verify AND equal the paper account.
        acct_ok, acct_msg = broker._verify_account(ib)
        if not acct_ok:
            warnings.append("account verification failed: %s" % acct_msg)

        open_trades = list(ib.openTrades())
        positions = list(ib.positions(account=account))

        result = decide(connected=True, account_ok=bool(acct_ok),
                        open_trades=open_trades, positions=positions,
                        symbol=symbol, account=account, warnings=warnings)
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
        return result
    except Exception:
        # any connection/query failure -> cannot prove flat -> UNKNOWN
        return "UNKNOWN"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Read-only fail-closed flat check.")
    ap.add_argument("--symbol", required=True)
    args = ap.parse_args(argv)
    print("ACCOUNT_FLAT=%s" % verify_flat(args.symbol))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

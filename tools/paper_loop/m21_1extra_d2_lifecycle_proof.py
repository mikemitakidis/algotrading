#!/usr/bin/env python3
"""M21.1extra-D2 — supervised paper lifecycle proof runner.

Runs ONE supervised paper lifecycle on the VPS, during real US regular market
open, with D0 enabled and D1 returning market_open, IBKR PAPER only, human
watching. It reuses the real B2b run_lifecycle + C persist_lifecycle via
run_supervised_cycle; it creates NO new broker path and places no live order.

Prices are OPERATOR-SUPPLIED and validated here (no live price fetch):
  --symbol, --entry-price, --stop-loss, --target-price, and an ugly confirm
  flag. Refuses unless: all prices > 0, stop_loss < entry_price < target_price.
  B2b enforces qty/notional caps, paper mode, account, and port.

Fail-closed: without the ugly confirm flag, or on any invalid price, it refuses
and does NOT touch the broker. It prints RC markers for the wrapper.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

OK_MARKER = "D2_LIFECYCLE_VERIFY_OK"
RC_MARKER = "M21_1EXTRA_D2_LIFECYCLE_VERIFY_RC"
JSON_MARKER = "D2_LIFECYCLE_PROOF_JSON"
_UGLY_CONFIRM = "yes-run-one-real-paper-lifecycle"


def validate_prices(entry_price: float, stop_loss: float,
                    target_price: float) -> None:
    """Raise ValueError unless all prices are positive and ordered
    stop_loss < entry_price < target_price."""
    if not (entry_price > 0 and stop_loss > 0 and target_price > 0):
        raise ValueError("all prices must be > 0")
    if not (stop_loss < entry_price):
        raise ValueError("stop_loss must be < entry_price")
    if not (target_price > entry_price):
        raise ValueError("target_price must be > entry_price")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="D2 supervised paper lifecycle proof (paper only).")
    p.add_argument("--symbol", required=True)
    p.add_argument("--entry-price", required=True, type=float)
    p.add_argument("--stop-loss", required=True, type=float)
    p.add_argument("--target-price", required=True, type=float)
    p.add_argument("--db-path", default=None,
                   help="lifecycle DB path (defaults to C's resolution)")
    p.add_argument("--confirm", default="",
                   help="must equal %r to actually run" % _UGLY_CONFIRM)
    return p


def run(args) -> int:
    proof = {"symbol": args.symbol, "checked_at_utc":
             datetime.now(timezone.utc).isoformat()}

    # price validation (fail-closed BEFORE touching anything).
    try:
        validate_prices(args.entry_price, args.stop_loss, args.target_price)
    except ValueError as exc:
        proof["refused"] = "invalid_prices:%s" % exc
        _emit(proof, rc=1)
        return 1

    if args.confirm != _UGLY_CONFIRM:
        proof["refused"] = "missing_or_wrong_confirm_flag"
        _emit(proof, rc=1)
        return 1

    # Import the REAL primitives + gates here (runtime), not at module import,
    # so the module can be unit-tested without a broker/gateway present.
    from bot.paper_loop_d2 import (
        run_supervised_cycle, make_d0_gate, make_d1_gate,
    )
    from bot.market_clock import AlpacaClockSource
    from tools.paper_loop.m21_1extra_b2b_single_submit import (
        run_lifecycle as b2b_run_lifecycle,
    )
    from tools.paper_loop.m21_1extra_c_outcome_persistence import (
        persist_lifecycle as c_persist_lifecycle,
    )
    # The real IBKR PAPER broker is constructed by B2b's own connection path;
    # run_lifecycle accepts broker=None and builds its paper broker internally
    # under its paper-mode/account/port guards. We pass broker=None so we do
    # NOT introduce a new broker path here.
    result = run_supervised_cycle(
        d0_gate=make_d0_gate(db_path=args.db_path),
        d1_gate=make_d1_gate(sources=[AlpacaClockSource()]),
        candidate_provider=lambda: [(args.symbol, 1.0)],
        threshold=0.0,
        entry_price=args.entry_price, stop_loss=args.stop_loss,
        target_price=args.target_price,
        b2b_run_lifecycle=b2b_run_lifecycle,
        c_persist_lifecycle=c_persist_lifecycle,
        broker=None, db_path=args.db_path)

    proof.update({
        "state": result.state,
        "d0_allowed": result.d0_allowed, "d0_reason": result.d0_reason,
        "d1_may_submit": result.d1_may_submit, "d1_reason": result.d1_reason,
        "submit_blocked_reason": result.submit_blocked_reason,
        "errors": result.errors,
        "persisted": bool(getattr(result, "persist_result", None)),
    })
    # RC=0 only if the supervised lifecycle reached PERSISTED (a full, confirmed,
    # persisted paper lifecycle). Any blocked/failed terminal -> RC=1 (honest).
    rc = 0 if result.state == "PERSISTED" else 1
    _emit(proof, rc=rc)
    return rc


def _emit(proof, *, rc: int) -> None:
    print(JSON_MARKER + " " + json.dumps(proof, sort_keys=True))
    if rc == 0:
        print(OK_MARKER)
    else:
        print("D2_LIFECYCLE_PROOF_INCOMPLETE " + json.dumps(
            proof.get("refused") or proof.get("submit_blocked_reason")
            or proof.get("state")))
    print("%s=%d" % (RC_MARKER, rc))


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

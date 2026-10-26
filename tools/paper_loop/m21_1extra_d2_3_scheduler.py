#!/usr/bin/env python3
"""M21.1extra-D2.3 — run the unattended paper learning loop.

Modes:
  --broker simulated   (default) deterministic simulated broker, synthetic
                       prices, fixture signals scored by the real M19 scoring
                       bridge. Writes ONLY under /tmp. Outcomes are tagged
                       data_source='simulated' and never count on the D3
                       dashboard. Gates are explicit always-allow stubs so the
                       loop mechanics can be exercised offline.
  --broker ibkr-paper  the real IBKR paper account via the loop's IBKR adapter
                       (orderRef on every leg, contract/account-scoped reads,
                       ownership-checked time-stop close). Requires --i-understand-this-runs-unattended-ibkr-paper-orders,
                       a time-boxed D0 authorization, an open market (D1), the
                       kill switch off and a Risk Authority allow for every
                       entry. Not exercised in this repository's tests beyond
                       its refusals; VPS verification needs explicit approval.

Scheduling: --ticks N runs N slots (sleeping until each next slot); --once
runs one tick (for a systemd timer). Nothing here installs a service.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[2]
REAL_LOOP_FLAG = "--i-understand-this-runs-unattended-ibkr-paper-orders"


def _tmp_only(path: str, what: str) -> str:
    if not str(Path(path).resolve()).startswith("/tmp/"):
        raise SystemExit("simulated mode writes only under /tmp/ (%s=%s)" % (what, path))
    return path


def _simulated_parts(args):
    from bot.paper_loop_d2.sim_broker import SimulatedPaperBroker
    from tools.signal_scoring.score_rank_harness import fixture_signals
    from tools.signal_scoring.scanner_bridge import score_signal
    from bot.paper_loop_d2.candidates import candidate_from_scored_signal
    t0 = datetime.now(timezone.utc)
    signals = fixture_signals()
    base = {s["symbol"]: float(s["entry_price"]) for s in signals}

    def price_fn(symbol: str, now: datetime) -> float:
        # deterministic synthetic path: +-6% sine wave, ~1 cycle per 6 hours
        minutes = (now - t0).total_seconds() / 60.0
        return round(base.get(symbol, 100.0) * (1 + 0.06 * math.sin(minutes / 57.3)), 4)

    broker = SimulatedPaperBroker(price_fn=price_fn, clock=lambda: datetime.now(timezone.utc),
                                  state_path=_tmp_only(args.sim_state, "--sim-state"))

    def provider(now):
        out = []
        for sig in signals:
            sig = dict(sig, timestamp=now.isoformat(),          # synthetic: fresh each slot
                       entry_price=price_fn(sig["symbol"], now))
            sc = score_signal(sig)
            row = {"final_score_100": float(sc.final_score_100)}
            out.append(candidate_from_scored_signal(sig, row, strategy_prefix="fixture"))
        return out

    allow = lambda: (True, "simulated_stub_allow")          # noqa: E731
    return dict(broker=broker, d0_gate=allow, d1_gate=allow,
                kill_switch_active=lambda: False,
                risk_gate=lambda **kw: (True, "simulated_stub_allow", None),
                candidate_provider=provider)


def _scanner_provider(focus_size: int):
    """Real candidates: scanner signals scored by the M19 bridge (read-only)."""
    def provider(now):
        from bot.scanner import scan_cycle
        from bot.universe.active_selection import get_scan_ready_symbols
        from tools.signal_scoring.scanner_bridge import score_signal
        from bot.paper_loop_d2.candidates import candidate_from_scored_signal, InvalidCandidate
        focus = get_scan_ready_symbols()[:focus_size]
        config = {"strategy": "default",
                  "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2, "min_valid_tfs": 1}}
        signals, _meta = scan_cycle(focus, config, conn=None, cycle_id=0)
        out = []
        for sig in signals:
            sc = score_signal(sig)
            if sc.execution_eligible is not False:
                raise AssertionError("scoring must stay research-grade")
            try:
                out.append(candidate_from_scored_signal(sig, {"final_score_100": float(sc.final_score_100)}))
            except InvalidCandidate:
                continue
        return out
    return provider


def _ibkr_parts(args):
    from tools.paper_loop.m21_1extra_a_run_once import _load_env_for_live
    _load_env_for_live()
    from bot.brokers.ibkr_broker import _get_connection_params
    from bot.paper_loop_d2.ibkr_adapter import IbkrLoopBroker
    from bot.paper_loop_d2 import make_d0_gate, make_d1_gate
    from bot.paper_loop_d2.risk_gate import make_risk_authority_gate
    from bot.kill_switch import is_kill_switch_active

    host, port, _account, cid = _get_connection_params()

    def ib_factory():
        # dedicated client id for the loop; orders carry orderRef, so ownership
        # does not depend on the client id that placed them
        from ib_insync import IB
        ib = IB()
        ib.connect(host, port, clientId=int(cid) + 7, readonly=False, timeout=15)
        return ib

    broker = IbkrLoopBroker(ib_factory=ib_factory)
    signals_db = str(REPO / "data" / "signals.db")
    return dict(broker=broker, d0_gate=make_d0_gate(), d1_gate=make_d1_gate(),
                kill_switch_active=is_kill_switch_active,
                risk_gate=make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(signals_db)),
                candidate_provider=_scanner_provider(args.focus_size))


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", choices=("simulated", "ibkr-paper"), default="simulated")
    ap.add_argument(REAL_LOOP_FLAG, dest="confirm_real", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--ticks", type=int, default=1)
    ap.add_argument("--interval-minutes", type=int, default=15)
    ap.add_argument("--min-score", type=float, default=60.0)
    ap.add_argument("--focus-size", type=int, default=150)
    ap.add_argument("--db", default=None)
    ap.add_argument("--lock", default=None)
    ap.add_argument("--sim-state", default="/tmp/m21_d2_3_sim_broker.json")
    ap.add_argument("--json-out", default="/tmp/m21_d2_3_ticks.json")
    args = ap.parse_args(argv)
    _tmp_only(args.json_out, "--json-out")

    from bot.paper_loop_d2.journal import LoopJournal, default_db_path
    from bot.paper_loop_d2.scheduler import LoopConfig, PaperLearningLoop

    if args.broker == "simulated":
        db = _tmp_only(args.db or "/tmp/m21_d2_3_sim_loop.db", "--db")
        lock = _tmp_only(args.lock or "/tmp/m21_d2_3_sim_loop.lock", "--lock")
        parts = _simulated_parts(args)
    else:
        if not args.confirm_real:
            print("REFUSED: --broker ibkr-paper runs the unattended loop against the real IBKR "
                  "paper account; pass %s only with explicit approval." % REAL_LOOP_FLAG,
                  file=sys.stderr)
            return 2
        db = args.db or default_db_path()
        lock = args.lock or str(REPO / "data" / "d2_loop.lock")
        parts = _ibkr_parts(args)

    loop = PaperLearningLoop(journal=LoopJournal(db), lock_path=lock,
                             config=LoopConfig(interval_minutes=args.interval_minutes,
                                               min_score=args.min_score),
                             **parts)
    results = loop.run(max_ticks=1 if args.once else max(1, args.ticks), sleep=time.sleep)
    Path(args.json_out).write_text(json.dumps(results, indent=2, default=str))
    for r in results:
        print("%s skipped=%s entry=%s block=%s opened=%d closed=%d errors=%d" % (
            r["cycle_key"], r["skipped"], bool(r["entry"]), r["entry_block"],
            len(r["opened"]), len(r["closed"]), len(r["errors"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())

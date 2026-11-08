"""tools/monitoring/run_checks.py — M21 PR-d (BLK-5): read-only paper-loop monitoring run.

One run = one round of the WP10 checks plus the PR-d risk-data check, applied to the
persistent alarm state machine (bot.monitoring.alarms.Monitor). Intended to be run by
algo-paper-monitor.timer every 5 minutes; installed DISABLED (see
infra/systemd/install_paper_units.sh).

Inputs (all read-only):
  * the loop journal (SQLite ``mode=ro``);
  * gateway health (bot.gateway_health.assemble_health: systemctl / port probe / log tail);
  * the market clock (the same trusted D1 Alpaca source the loop uses; GET only);
  * optionally a WP5 observations JSON (``--observations``). Without it the WP10
    data-freshness check is NOT evaluated (reported as such), rather than raising a
    permanent "cannot verify" alarm; the loop-relevant data check is risk_data_blocks.

Writes ONLY the alarm database (``--alarms-db``) and the log (``--log``); LogSink only,
no messaging. Never connects to a broker, never places or cancels an order.

Known gap (stated, not hidden): the broker side of the duplicate-orders check needs the
broker's open orders, which this runner does not read. It reports the journal-side
check as ``duplicate_orders_journal`` and says the broker side was not checked. The
loop itself reconciles with the broker every tick.

Exit codes: 0 no open alarm; 1 at least one open alarm (ACTIVE or CLEARED_PENDING_ACK);
2 the run itself failed (nothing evaluated).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

log = logging.getLogger("paper_monitor")

DEFAULT_JOURNAL = REPO / "data" / "paper_lifecycles.db"
DEFAULT_ALARMS = REPO / "data" / "monitor_alarms.db"
DEFAULT_LOG = REPO / "logs" / "monitoring.log"


def market_open_now(now: datetime, market_state: Optional[Callable[[], Any]] = None
                    ) -> Optional[bool]:
    """True for a verified open session, False for a verified closed one, None when the
    clock cannot be verified (the checks then treat the market state as unknown)."""
    from bot.paper_loop_d2.risk_gate import make_market_state_provider, verify_market_open
    try:
        provider = market_state or make_market_state_provider()
        ok, why = verify_market_open(provider, now=now)
    except Exception:
        return None
    if ok is True:
        return True
    return False if why == "market_not_open:market_closed" else None


def journal_only_duplicates(result):
    """Rename the duplicate check to its journal-only form when the only reason it is
    UNKNOWN is that the broker's open orders were not read."""
    from bot.monitoring.checks import CheckResult, OK, UNKNOWN
    if (result.check == "duplicate_orders" and result.status == UNKNOWN
            and result.detail == "broker open orders unavailable"):
        return CheckResult("duplicate_orders_journal", OK, result.severity,
                           "journal: no duplicate or reused order_ref; broker side NOT "
                           "checked by this runner")
    return result


def collect(*, journal_db: str, now: datetime, interval: timedelta,
            market_open: Optional[bool], gateway_health: Optional[Dict[str, Any]],
            observations: Optional[Dict[str, Dict[str, str]]]):
    """The check results for one run. ``observations=None`` = no observations source:
    the data-freshness check is left out (see NOT_EVALUATED in main)."""
    from bot.monitoring.checks import CheckResult, CRITICAL, UNKNOWN
    from bot.monitoring.probe import read_journal, run_checks
    from bot.monitoring.risk_data import check_risk_data_blocks
    extra: List[Any] = []
    if os.path.exists(journal_db):
        try:
            journal = read_journal(journal_db)
        except Exception as e:
            journal = {"cycles": [], "orders": []}
            extra.append(CheckResult("journal_readable", UNKNOWN, CRITICAL,
                                     "loop journal unreadable: %s" % type(e).__name__))
    else:
        journal = {"cycles": [], "orders": []}
    results = run_checks(journal=journal, now=now, interval=interval, market_open=market_open,
                         gateway_health=gateway_health, observations=observations or {},
                         broker_open_orders=None)
    results = [journal_only_duplicates(r) for r in results
               if not (observations is None and r.check == "data_freshness")]
    results.append(check_risk_data_blocks(journal["cycles"], market_open=market_open))
    return results + extra


def main(argv: Optional[List[str]] = None, *, now: Optional[datetime] = None,
         health: Optional[Callable[[], Dict[str, Any]]] = None,
         market_state: Optional[Callable[[], Any]] = None) -> int:
    ap = argparse.ArgumentParser(description="read-only paper-loop monitoring run")
    ap.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    ap.add_argument("--alarms-db", default=str(DEFAULT_ALARMS))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--observations", default=None)
    ap.add_argument("--interval-minutes", type=int, default=15)
    args = ap.parse_args(argv)

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(args.log)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        from bot.monitoring.alarms import LogSink, Monitor
        t = now or datetime.now(timezone.utc)
        try:
            if health is not None:
                gh = health()
            else:
                from bot.gateway_health import assemble_health
                gh = assemble_health()
        except Exception as e:
            log.warning("gateway health unavailable: %s", type(e).__name__)
            gh = None
        observations: Optional[Dict[str, Dict[str, str]]] = None
        if args.observations:
            with open(args.observations) as f:
                observations = json.load(f)
        results = collect(journal_db=args.journal, now=t,
                          interval=timedelta(minutes=args.interval_minutes),
                          market_open=market_open_now(t, market_state),
                          gateway_health=gh, observations=observations)
        mon = Monitor(args.alarms_db, sinks=(LogSink(),), clock=lambda: t)
        transitions = mon.evaluate(results)
        open_alarms = mon.open_alarms()
        for r in results:
            log.info("check %s %s/%s: %s", r.check, r.status, r.severity, r.detail)
        not_evaluated = ["data_freshness"] if observations is None else []
        if not_evaluated:
            log.info("not evaluated: %s (no observations source)", ",".join(not_evaluated))
        log.info("run done: %d transitions, %d open alarms", len(transitions), len(open_alarms))
        print(json.dumps({"checks": [[r.check, r.status, r.severity] for r in results],
                          "not_evaluated": not_evaluated,
                          "transitions": transitions, "open_alarms": len(open_alarms)}))
        return 1 if open_alarms else 0
    except Exception as e:
        log.error("monitoring run failed: %s: %s", type(e).__name__, e)
        print(json.dumps({"error": type(e).__name__}))
        return 2
    finally:
        root.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    sys.exit(main())

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
broker's open orders, which this runner does not read. The journal half is reported as
``duplicate_orders_journal``; the broker half as ``duplicate_orders_broker`` =
UNKNOWN / CRITICAL / NOT_EVALUATED, which keeps a critical alarm open and makes the
rollup ``coverage_complete: false, healthy: false`` until a broker-side reader exists
(a pre-activation item). The loop itself reconciles with the broker every tick.

Exit codes: 0 no open alarm and complete coverage (not reachable while the broker side is
NOT_EVALUATED); 1 at least one open alarm (ACTIVE or CLEARED_PENDING_ACK) or incomplete
coverage; 2 the run itself failed (nothing evaluated).
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


BROKER_DUPLICATES = "duplicate_orders_broker"
NOT_EVALUATED = ("NOT_EVALUATED: this runner does not read the broker's open orders; "
                 "broker-side duplicate orders are unverified")


def split_duplicates(result):
    """The WP10 duplicate check needs the journal AND the broker's open orders; this
    runner reads only the journal. Report it as two results with stable names, so
    no rollup can present duplicate coverage as complete (review finding #39-1):

      * ``duplicate_orders_journal``: the journal half, accurately scoped: OK when the
        journal shows no duplicate or reused order_ref, ALARM (with the WP10 detail)
        when it does;
      * ``duplicate_orders_broker``: always UNKNOWN / CRITICAL with NOT_EVALUATED, so a
        critical "cannot verify" alarm stays open until a broker-side reader exists.
    Any other check passes through unchanged."""
    from bot.monitoring.checks import ALARM, CheckResult, CRITICAL, OK, UNKNOWN
    if result.check != "duplicate_orders":
        return [result]
    if result.status == ALARM:
        journal = CheckResult("duplicate_orders_journal", ALARM, result.severity, result.detail)
    elif result.status == UNKNOWN and result.detail == "broker open orders unavailable":
        journal = CheckResult("duplicate_orders_journal", OK, result.severity,
                              "journal only: no duplicate or reused order_ref in the journal")
    else:
        journal = CheckResult("duplicate_orders_journal", UNKNOWN, result.severity,
                              "journal check gave an unexpected result: %s" % result.detail)
    return [journal, CheckResult(BROKER_DUPLICATES, UNKNOWN, CRITICAL, NOT_EVALUATED)]


def collect(*, journal_db: str, now: datetime, interval: timedelta,
            market_open: Optional[bool], gateway_health: Optional[Dict[str, Any]],
            observations: Optional[Dict[str, Dict[str, str]]]):
    """The check results for one run. ``observations=None`` = no observations source:
    the WP10 data-freshness check then runs on no observations and reports its own
    UNKNOWN ("cannot verify") while the market is open."""
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
    results = [x for r in results for x in split_duplicates(r)]
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
        not_evaluated = sorted({r.check for r in results if r.detail.startswith("NOT_EVALUATED")}
                               | ({"data_freshness"} if observations is None else set()))
        coverage_complete = not not_evaluated
        log.info("coverage %s; not evaluated: %s", "complete" if coverage_complete else "INCOMPLETE",
                 ",".join(not_evaluated) or "none")
        log.info("run done: %d transitions, %d open alarms", len(transitions), len(open_alarms))
        print(json.dumps({"checks": [[r.check, r.status, r.severity] for r in results],
                          "coverage_complete": coverage_complete,
                          "not_evaluated": not_evaluated,
                          "healthy": coverage_complete and not open_alarms,
                          "transitions": transitions, "open_alarms": len(open_alarms)}))
        return 0 if (coverage_complete and not open_alarms) else 1
    except Exception as e:
        log.error("monitoring run failed: %s: %s", type(e).__name__, e)
        print(json.dumps({"error": type(e).__name__}))
        return 2
    finally:
        root.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    sys.exit(main())

"""M21 WP10 — read-only collection of monitoring inputs from the loop journal.

Opens the WS2 loop journal READ-ONLY (SQLite ``mode=ro``) and assembles the
inputs for ``bot.monitoring.checks``; never writes, never contacts a broker.
The gateway health dict comes from ``bot.gateway_health.assemble_health`` (or
the dashboard's ``/api/gateway/health``) and the observation states from the
WP5 research report; both are passed in by the caller.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from bot.monitoring.checks import (
    CheckResult, check_data_freshness, check_duplicate_orders, check_gateway,
    check_loop_heartbeat, check_scheduler_liveness, check_unresolved_execution,
)


def read_journal(journal_db: str) -> Dict[str, List[Dict[str, Any]]]:
    path = str(journal_db).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    c.row_factory = sqlite3.Row
    try:
        cycles = [dict(r) for r in c.execute(
            "SELECT cycle_key, started_at_utc, finished_at_utc, state FROM loop_cycles "
            "ORDER BY started_at_utc")]
        orders = [dict(r) for r in c.execute("SELECT * FROM loop_orders")]
    finally:
        c.close()
    return {"cycles": cycles, "orders": orders}


def run_checks(*, journal: Mapping[str, List[Mapping[str, Any]]], now: datetime,
               interval: timedelta, market_open: Optional[bool],
               gateway_health: Optional[Mapping[str, Any]],
               observations: Mapping[str, Mapping[str, str]],
               broker_open_orders: Optional[List[Mapping[str, Any]]] = None
               ) -> List[CheckResult]:
    cycles = list(journal.get("cycles", []))
    orders = list(journal.get("orders", []))
    last = max((c["started_at_utc"] for c in cycles if c.get("started_at_utc")), default=None)
    return [
        check_loop_heartbeat(last, now, max_age=2 * interval + timedelta(minutes=2))
        if market_open is not False else
        CheckResult("loop_heartbeat", "OK", "CRITICAL", "market closed"),
        check_scheduler_liveness(cycles, now, interval=interval, market_open=market_open),
        check_data_freshness(observations, market_open=market_open),
        check_gateway(gateway_health, now),
        check_unresolved_execution(orders, now),
        check_duplicate_orders(orders, broker_open_orders),
    ]

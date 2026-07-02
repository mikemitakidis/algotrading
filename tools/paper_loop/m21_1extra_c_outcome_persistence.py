#!/usr/bin/env python3
"""M21.1extra-C — paper-lifecycle outcome persistence.

Persists B2b-style paper-order lifecycle records into a dedicated, append-only
`paper_lifecycles` table, plus a read-only summary reader. These are MECHANICAL
paper-lifecycle records (immediate-flatten), NOT hold-to-exit P&L edge outcomes:
every row is tagged record_kind='mechanical_paper_lifecycle' and is_edge_outcome=0.

C does NOT schedule anything, does NOT hold trades open to SL/TP, does NOT touch
the dashboard or the broker/adapter. It consumes a B2b result dict and writes it.

Timestamp honesty (per review): B2b does not currently expose true per-stage
event timestamps, so C must NOT claim to know exact submit/observe/flatten
instants. C stamps persist-time only:
  persisted_at_utc  = UTC datetime when C wrote the record
  created_at_utc    = same as persisted_at_utc for now
  submitted_at_utc / observed_at_utc / flattened_at_utc = null unless the source
      JSON actually provides a real event timestamp
  timestamp_source  = 'c_persist_time_only'
  event_timestamps_available = False

DST safety: the exchange session date is derived by converting the UTC instant
to America/New_York via zoneinfo (stdlib, DST-correct) — never a fixed UK / VPS /
Greece / UTC offset, never a hardcoded session window.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

_EXCHANGE_TZ = "America/New_York"
_TABLE = "paper_lifecycles"


def _default_db_path() -> str:
    """Resolve C's paper-lifecycles DB path. Isolated from signals.db by
    default; overridable via env for tests/ops. Never couples to the live
    signal/outcome tables."""
    env = os.environ.get("PAPER_LIFECYCLES_DB_PATH")
    if env:
        return env
    # sit alongside the repo data dir but as a distinct file
    base = Path(__file__).resolve().parents[2]
    return str(base / "data" / "paper_lifecycles.db")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def market_session_date_for(instant_utc: datetime) -> str:
    """DST-correct exchange session date: convert a timezone-aware UTC instant
    to America/New_York and take the calendar date. Uses zoneinfo so US DST
    transitions are handled correctly and independently of UK/VPS local time."""
    if instant_utc.tzinfo is None:
        raise ValueError("instant_utc must be timezone-aware UTC")
    et = instant_utc.astimezone(ZoneInfo(_EXCHANGE_TZ))
    return et.date().isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        " lifecycle_id TEXT PRIMARY KEY,"          # idempotency key
        " symbol TEXT,"
        " account TEXT,"
        " port INTEGER,"
        " entry_order_id TEXT,"
        " entry_result_status TEXT,"
        " entry_order_originated INTEGER,"
        " entry_filled INTEGER,"
        " position_observed INTEGER,"
        " observation_attempts INTEGER,"
        " observation_seconds REAL,"
        " observation_timeout INTEGER,"
        " flatten_called INTEGER,"
        " flatten_confirmed INTEGER,"
        " close_order_placed INTEGER,"
        " remaining_positions_json TEXT,"
        " remaining_open_orders_json TEXT,"
        " lifecycle_confirmed INTEGER,"
        " data_source TEXT,"
        " record_kind TEXT,"
        " is_edge_outcome INTEGER,"
        # time fields (honest persist-time semantics)
        " persisted_at_utc TEXT,"
        " created_at_utc TEXT,"
        " submitted_at_utc TEXT,"
        " observed_at_utc TEXT,"
        " flattened_at_utc TEXT,"
        " timestamp_source TEXT,"
        " event_timestamps_available INTEGER,"
        " exchange_timezone TEXT,"
        " market_session_date TEXT,"
        " market_session_date_source TEXT,"
        " market_clock_checked INTEGER,"
        " market_clock_reason TEXT,"
        " raw_json TEXT"
        ")" % _TABLE)
    conn.commit()


def compute_lifecycle_id(b2b: Dict[str, Any], persisted_at_utc: datetime) -> str:
    """Deterministic idempotency key. Prefer the real broker order id (stable,
    unique per order); fall back to a hash of symbol + persist instant for
    records that never got an order id (e.g. policy-blocked lifecycles), so
    those are still de-duplicated per persist but never collide with real ones."""
    oid = b2b.get("entry_order_id")
    if oid:
        return "oid:%s" % oid
    basis = "%s|%s|%s" % (
        b2b.get("symbol"), b2b.get("entry_result_status"),
        persisted_at_utc.isoformat())
    return "noid:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _b(v: Any) -> int:
    return 1 if v else 0


def persist_lifecycle(b2b: Dict[str, Any], *, db_path: Optional[str] = None,
                      persisted_at_utc: Optional[datetime] = None
                      ) -> Dict[str, Any]:
    """Append one B2b lifecycle record. Idempotent on lifecycle_id: a second
    persist of the same lifecycle does NOT duplicate or overwrite. Failed
    lifecycles (lifecycle_confirmed=false) are persisted truthfully too.

    Returns a small dict describing what happened (inserted vs duplicate + the
    lifecycle_id and derived time fields)."""
    if db_path is None:
        db_path = _default_db_path()
    if persisted_at_utc is None:
        persisted_at_utc = _utc_now()
    if persisted_at_utc.tzinfo is None:
        raise ValueError("persisted_at_utc must be timezone-aware UTC")

    lifecycle_id = compute_lifecycle_id(b2b, persisted_at_utc)

    # honest event timestamps: only use them if the source JSON actually has them
    submitted = b2b.get("submitted_at_utc")
    observed = b2b.get("observed_at_utc")
    flattened = b2b.get("flattened_at_utc")
    event_available = any(x is not None for x in (submitted, observed, flattened))

    session_date = market_session_date_for(persisted_at_utc)

    row = {
        "lifecycle_id": lifecycle_id,
        "symbol": b2b.get("symbol"),
        "account": b2b.get("account"),
        "port": b2b.get("port"),
        "entry_order_id": b2b.get("entry_order_id"),
        "entry_result_status": b2b.get("entry_result_status"),
        "entry_order_originated": _b(b2b.get("entry_order_originated")),
        "entry_filled": _b(b2b.get("entry_filled")),
        "position_observed": _b(b2b.get("position_observed")),
        "observation_attempts": b2b.get("observation_attempts"),
        "observation_seconds": b2b.get("observation_seconds"),
        "observation_timeout": _b(b2b.get("observation_timeout")),
        "flatten_called": _b(b2b.get("flatten_called")),
        "flatten_confirmed": _b(b2b.get("flatten_confirmed")),
        "close_order_placed": _b(b2b.get("close_order_placed")),
        "remaining_positions_json": json.dumps(
            b2b.get("remaining_positions", [])),
        "remaining_open_orders_json": json.dumps(
            b2b.get("remaining_open_orders", [])),
        "lifecycle_confirmed": _b(b2b.get("lifecycle_confirmed")),
        "data_source": b2b.get("data_source"),
        "record_kind": "mechanical_paper_lifecycle",
        "is_edge_outcome": 0,
        "persisted_at_utc": _iso(persisted_at_utc),
        "created_at_utc": _iso(persisted_at_utc),
        "submitted_at_utc": submitted,
        "observed_at_utc": observed,
        "flattened_at_utc": flattened,
        "timestamp_source": "c_persist_time_only",
        "event_timestamps_available": _b(event_available),
        "exchange_timezone": _EXCHANGE_TZ,
        "market_session_date": session_date,
        "market_session_date_source": "persisted_at_utc_not_execution_time",
        "market_clock_checked": 0,
        "market_clock_reason": "not_checked_in_C_deferred_to_D",
        "raw_json": json.dumps(b2b, sort_keys=True),
    }

    conn = _connect(db_path)
    try:
        _ensure_table(conn)
        cols = ",".join(row.keys())
        placeholders = ",".join("?" for _ in row)
        # INSERT OR IGNORE => append-only + idempotent on the PRIMARY KEY.
        cur = conn.execute(
            "INSERT OR IGNORE INTO %s (%s) VALUES (%s)"
            % (_TABLE, cols, placeholders), list(row.values()))
        conn.commit()
        inserted = cur.rowcount == 1
    finally:
        conn.close()

    return {
        "lifecycle_id": lifecycle_id,
        "inserted": inserted,
        "duplicate": not inserted,
        "market_session_date": session_date,
        "exchange_timezone": _EXCHANGE_TZ,
        "persisted_at_utc": _iso(persisted_at_utc),
        "record_kind": "mechanical_paper_lifecycle",
        "is_edge_outcome": 0,
    }


def read_lifecycles(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read-only: return all persisted lifecycle rows as dicts (oldest first)."""
    if db_path is None:
        db_path = _default_db_path()
    if not Path(db_path).exists():
        return []
    conn = _connect(db_path)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT * FROM %s ORDER BY persisted_at_utc ASC" % _TABLE
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def summarize(*, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Read-only summary counts. Explicitly notes these are mechanical
    lifecycle records, not edge outcomes."""
    rows = read_lifecycles(db_path=db_path)
    total = len(rows)
    confirmed = sum(1 for r in rows if r.get("lifecycle_confirmed"))
    filled = sum(1 for r in rows if r.get("entry_filled"))
    return {
        "total_lifecycles": total,
        "lifecycle_confirmed_count": confirmed,
        "entry_filled_count": filled,
        "record_kind": "mechanical_paper_lifecycle",
        "is_edge_outcome": 0,
        "note": ("mechanical immediate-flatten paper lifecycles; NOT "
                 "hold-to-exit P&L edge outcomes"),
    }


def _render_report(summary: Dict[str, Any], data_source: str) -> str:
    L = ["# M21.1extra-C — paper-lifecycle outcome persistence", ""]
    L.append("- data_source: **%s**" % data_source)
    L.append("- table: **%s** (append-only, idempotent)" % _TABLE)
    L.append("- record_kind: **mechanical_paper_lifecycle**")
    L.append("- is_edge_outcome: **0**")
    L.append("- exchange_timezone: **%s**" % _EXCHANGE_TZ)
    L.append("- total_lifecycles: **%s**" % summary.get("total_lifecycles"))
    L.append("- lifecycle_confirmed_count: **%s**"
             % summary.get("lifecycle_confirmed_count"))
    L.append("")
    L.append("> **C persists B2b-style MECHANICAL paper-lifecycle records "
             "(immediate-flatten) into a dedicated append-only table. These are "
             "NOT hold-to-exit P&L edge outcomes. Timestamps are persist-time "
             "only (timestamp_source=c_persist_time_only, "
             "event_timestamps_available=false); C does not claim to know exact "
             "broker submit/observe/flatten instants. The exchange session date "
             "is derived from the UTC instant via America/New_York (zoneinfo, "
             "DST-correct), never a fixed offset. C does not schedule, hold "
             "trades open, or touch the dashboard — the market-clock guard is "
             "deferred to D (market_clock_checked=false).**")
    L.append("")
    return "\n".join(L)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", required=True,
                    help="Path to a B2b result JSON to persist.")
    ap.add_argument("--db", default=None,
                    help="paper_lifecycles DB path (default: repo data dir).")
    ap.add_argument("--report", default="/tmp/m21_1extra_c.md")
    ap.add_argument("--summary-out", default="/tmp/m21_1extra_c_summary.json")
    args = ap.parse_args()

    for p in (args.report, args.summary_out):
        if p and not str(Path(p).resolve()).startswith("/tmp/"):
            raise SystemExit("M21.1extra-C writes reports only under /tmp/")

    b2b = json.loads(Path(args.from_json).read_text())
    res = persist_lifecycle(b2b, db_path=args.db)
    summary = summarize(db_path=args.db)
    Path(args.report).write_text(
        _render_report(summary, b2b.get("data_source", "unknown")),
        encoding="utf-8")
    Path(args.summary_out).write_text(json.dumps(summary, indent=2),
                                      encoding="utf-8")
    print("persisted lifecycle_id=%s inserted=%s duplicate=%s "
          "market_session_date=%s"
          % (res["lifecycle_id"], res["inserted"], res["duplicate"],
             res["market_session_date"]))
    print("summary: total=%s confirmed=%s"
          % (summary["total_lifecycles"],
             summary["lifecycle_confirmed_count"]))


if __name__ == "__main__":
    main()

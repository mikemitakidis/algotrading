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
_DEFAULT_CALENDAR_ID = "US_EQ"
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


def _as_utc(dt: datetime) -> datetime:
    """Normalise a timezone-aware datetime to UTC. Refuses naive datetimes so a
    field named *_utc is always genuinely UTC (offset +00:00), never merely
    'timezone-aware in some other zone'."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _normalise_utc_str(value: Any) -> Optional[str]:
    """Normalise a source-provided event timestamp to a UTC ISO string. Accepts
    an ISO-8601 string or a datetime; returns None if absent or unparseable, so
    a field named *_utc never holds a non-UTC value. A naive string/datetime is
    treated as unusable (None) rather than silently assumed to be UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return None  # do not assume naive == UTC; leave the *_utc field null
    return dt.astimezone(timezone.utc).isoformat()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def market_session_date_for(instant_utc: datetime,
                            exchange_timezone: str = _EXCHANGE_TZ) -> str:
    """DST-correct exchange session date: convert a UTC instant to the given
    exchange timezone (default America/New_York) and take the calendar date.
    Uses zoneinfo so DST transitions are handled correctly and independently of
    UK/VPS/UTC local time. The timezone MUST match the record's
    exchange_timezone so the stored session date and stored timezone are
    consistent (D-readiness for non-US exchanges). An invalid timezone raises
    rather than silently falling back."""
    instant_utc = _as_utc(instant_utc)   # refuses naive; normalises to UTC
    try:
        tz = ZoneInfo(exchange_timezone)
    except Exception as e:
        raise ValueError(
            "invalid exchange_timezone %r" % exchange_timezone) from e
    return instant_utc.astimezone(tz).date().isoformat()


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
        " market_calendar_id TEXT,"
        " market_session_date TEXT,"
        " market_session_date_source TEXT,"
        " market_clock_checked INTEGER,"
        " market_clock_reason TEXT,"
        " raw_json TEXT"
        ")" % _TABLE)
    conn.commit()


def compute_lifecycle_id(b2b: Dict[str, Any]) -> str:
    """Deterministic idempotency key.

    - Real accepted lifecycles use oid:<entry_order_id> (stable, unique per
      order).
    - Order-less lifecycles (e.g. policy-blocked) hash the STABLE source
      payload, NOT the persist instant — so replaying the same saved B2b JSON
      later is idempotent (one row), while two genuinely different order-less
      payloads produce two rows. If future B2b adds real event timestamps or
      attempt ids, those naturally live in the payload and distinguish separate
      attempts."""
    oid = b2b.get("entry_order_id")
    if oid:
        return "oid:%s" % oid
    basis = json.dumps(b2b, sort_keys=True)
    return "noid:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


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
    # normalise to genuine UTC (refuses naive); a field named *_utc must be UTC
    persisted_at_utc = _as_utc(persisted_at_utc)

    lifecycle_id = compute_lifecycle_id(b2b)

    # honest event timestamps: only stored if the source JSON provides a REAL
    # value, and normalised to UTC — never store a non-UTC string in a _utc
    # field, and never assume a naive value is UTC (those become null).
    submitted = _normalise_utc_str(b2b.get("submitted_at_utc"))
    observed = _normalise_utc_str(b2b.get("observed_at_utc"))
    flattened = _normalise_utc_str(b2b.get("flattened_at_utc"))
    event_available = any(x is not None for x in (submitted, observed, flattened))

    # resolve the record's market identity BEFORE deriving the session date, so
    # the stored session date is computed in the SAME timezone that is stored in
    # exchange_timezone (consistency for non-US records). Invalid tz raises.
    exchange_timezone = b2b.get("exchange_timezone") or _EXCHANGE_TZ
    market_calendar_id = b2b.get("market_calendar_id") or _DEFAULT_CALENDAR_ID
    session_date = market_session_date_for(persisted_at_utc, exchange_timezone)

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
        "exchange_timezone": exchange_timezone,
        # D-readiness identity only: D maps this to a real exchange calendar.
        # C does NOT check holidays / early closes / weekends / open status.
        "market_calendar_id": market_calendar_id,
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
        "exchange_timezone": exchange_timezone,
        "market_calendar_id": market_calendar_id,
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
    L.append("- market_calendar_id: **%s** (identity only; D maps to a calendar)"
             % _DEFAULT_CALENDAR_ID)
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
    L.append("> **C does NOT check public holidays, early closes, weekends, "
             "lunch breaks, or market-open status. Those checks are explicitly "
             "deferred to D. C only stores DST-safe UTC timestamps, an "
             "America/New_York session date, and market-identity fields "
             "(market_calendar_id, exchange_timezone) so D can enforce a real "
             "per-exchange market-open guard.**")
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

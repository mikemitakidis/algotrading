"""bot/risk_authority/ingest_exposure.py — M14.D exposure orchestrator.

Independent of M14.C (bot/risk_authority/ingest.py). Same per-row UPSERT
target (`daily_state_per_broker`) but writes ONLY exposure-owned columns
and ONLY exposure-namespaced `lifecycle_json` keys. Reads M14.C-owned
data only to preserve it byte-identical on the same row.

Column ownership (M14.D writes these and only these):
  open_positions, capital_deployed, peak_equity, drawdown_from_peak,
  source, last_ingested_at, updated_at,
  lifecycle_json.exposure_*  (status, missing_fields, known_fields,
                              fresh_reads_count, latest_reading, events)

M14.C-owned columns NEVER touched:
  realised_pnl_usd, realised_pnl_pct, realised_daily_loss,
  daily_pnl_source, daily_pnl_available,
  daily_loss_block_active, daily_loss_alert_sent,
  fresh_reads_count,
  lifecycle_json.{status, reading_quality, known_fields,
                  missing_fields, latest_reading, events}

The orchestrator never raises on adapter failure — adapters return
UNKNOWN readings instead. Dry-run path makes ZERO writes to both
`daily_state_per_broker` and `broker_positions`.

Also writes per-position rows to `broker_positions` (append-only) sharing
a single `exposure_batch_id` (UUID) per ingest run; M14.E can fetch the
latest snapshot per scope via MAX(fetched_at_utc) and then SELECT WHERE
exposure_batch_id = that.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from bot.risk_authority.exposure_reading import (
    BrokerExposureReading,
    ExposureQuality,
    Position,
)

log = logging.getLogger(__name__)

INGESTIBLE_SCOPES = {"ibkr_live", "ibkr_paper", "etoro_real", "etoro_paper"}

# Columns M14.D writes. NEVER add an M14.C-owned column to this list.
_M14D_OWNED_COLUMNS = (
    "open_positions",
    "capital_deployed",
    "peak_equity",
    "drawdown_from_peak",
    "source",
    "last_ingested_at",
    "updated_at",
    "lifecycle_json",
)

# M14.C-owned lifecycle_json keys — M14.D MUST preserve these.
_M14C_OWNED_LIFECYCLE_KEYS = (
    "status", "reading_quality", "known_fields", "missing_fields",
    "latest_reading", "events",
)

# M14.D-owned lifecycle_json keys — only these may be written by M14.D.
_M14D_OWNED_LIFECYCLE_KEYS = (
    "exposure_status",
    "exposure_missing_fields",
    "exposure_known_fields",
    "exposure_fresh_reads_count",
    "exposure_latest_reading",
    "exposure_events",
    "exposure_batch_id",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _validate_scope(scope: str) -> None:
    if scope not in INGESTIBLE_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(INGESTIBLE_SCOPES)}, got {scope!r}"
        )


def _read_existing_row(conn: sqlite3.Connection, today: str,
                        scope: str) -> Optional[dict]:
    cols = ("date,broker_scope,realised_pnl_usd,realised_pnl_pct,"
            "daily_pnl_source,daily_pnl_available,"
            "daily_loss_block_active,daily_loss_alert_sent,"
            "realised_daily_loss,open_positions,capital_deployed,"
            "peak_equity,drawdown_from_peak,source,last_ingested_at,"
            "fresh_reads_count,lifecycle_json,updated_at")
    cur = conn.execute(
        f"SELECT {cols} FROM daily_state_per_broker "
        "WHERE date=? AND broker_scope=?",
        (today, scope),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = [c.strip() for c in cols.split(",")]
    return dict(zip(keys, row))


def _load_lifecycle(blob: Optional[str]) -> dict:
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _compact_latest_summary(reading: BrokerExposureReading) -> dict:
    """Compact redacted per-row summary suitable for DB storage. The
    full per-position detail goes to broker_positions; this is just a
    one-line snapshot for the engine and dashboard."""
    return {
        "fetched_at_utc": reading.fetched_at_utc,
        "data_source_success": reading.data_source_success,
        "open_positions_count": reading.open_positions_count,
        "capital_deployed_usd": reading.capital_deployed_usd,
        "unrealised_pnl_usd": reading.unrealised_pnl_usd,
        "current_equity_usd": reading.current_equity_usd,
        "error": reading.error,
        "quality": reading.quality().value,
    }


def _insert_position_rows(
    conn: sqlite3.Connection,
    scope: str,
    today: str,
    fetched_at_utc: str,
    exposure_batch_id: str,
    positions: list,
) -> int:
    """Append per-position rows under one exposure_batch_id. Returns
    the count of rows inserted."""
    if not positions:
        return 0
    rows = []
    now = _now_iso()
    for p in positions:
        rows.append((
            exposure_batch_id, scope, today, fetched_at_utc,
            p.symbol, p.side, float(p.qty), float(p.exposure_usd),
            p.avg_price, p.mark_price, p.unrealised_pnl_usd,
            p.opened_at, p.instrument_id,
            json.dumps(p.raw_evidence, separators=(",", ":")),
            now,
        ))
    conn.executemany(
        "INSERT INTO broker_positions ("
        " exposure_batch_id, broker_scope, date, fetched_at_utc,"
        " symbol, side, qty, exposure_usd, avg_price, mark_price,"
        " unrealised_pnl_usd, opened_at, instrument_id,"
        " raw_evidence, created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def ingest_exposure_once(
    conn: sqlite3.Connection,
    *,
    scope: str,
    today: Optional[str] = None,
    adapter,
    dry_run: bool = False,
    audit_logger=None,
) -> dict:
    """Run one exposure adapter and UPSERT the M14.D-owned slice.

    Returns a result dict describing what was (or would be) written:
      {scope, today, exposure_batch_id, quality, status,
       open_positions, capital_deployed_usd, exposure_fresh_reads_count,
       positions_written, would_write, error?}
    """
    _validate_scope(scope)
    day = today or _today_utc()

    reading: BrokerExposureReading = adapter.read(today=day)

    # Diagnostic check that adapters honour the contract (no raise; if
    # quality says UNKNOWN, required fields ARE missing, etc).
    quality = reading.quality()

    # Generate the batch ID up-front so it's part of the result even
    # on dry-run paths.
    exposure_batch_id = str(uuid.uuid4())

    result = {
        "scope": scope,
        "today": day,
        "exposure_batch_id": exposure_batch_id,
        "quality": quality.value,
        "status": "dry_run" if dry_run else "pending",
        "open_positions": reading.open_positions_count,
        "capital_deployed_usd": reading.capital_deployed_usd,
        "would_write": not dry_run,
    }
    if reading.error:
        result["error"] = reading.error

    # Audit BEFORE write — never blocks the orchestrator.
    if audit_logger is not None:
        try:
            audit_logger.event(
                "exposure_ingest_attempt",
                broker_scope=scope, today=day, quality=quality.value,
                error=reading.error,
                open_positions=reading.open_positions_count,
            )
        except Exception as e:
            log.warning("[ingest_exposure] audit failed: %s", e)

    if dry_run:
        return result

    # UPSERT logic ---------------------------------------------------------
    prev = _read_existing_row(conn, day, scope)
    lifecycle = _load_lifecycle(prev["lifecycle_json"] if prev else None)
    prev_exposure_count = int(lifecycle.get("exposure_fresh_reads_count", 0))

    # Hysteresis: FRESH or PARTIAL increments, UNKNOWN resets.
    if reading.has_fresh_exposure():
        exposure_count = min(prev_exposure_count + 1, 9999)
    else:
        exposure_count = 0

    # Peak equity ratchet — only on known equity values; never lowers.
    new_peak = None
    prev_peak = prev["peak_equity"] if prev else None
    if reading.current_equity_usd is not None and reading.has_fresh_exposure():
        new_peak = reading.current_equity_usd
        if prev_peak is not None:
            new_peak = max(float(prev_peak), float(reading.current_equity_usd))
    elif prev_peak is not None:
        new_peak = prev_peak

    # Hydrate reading.peak_equity_usd from the computed/carry-forward
    # value so the quality classification (used for lifecycle.exposure_status)
    # reflects what we are actually persisting. Without this, a reading
    # with current_equity but no per-call peak_equity would classify as
    # PARTIAL even when the orchestrator just ratcheted peak from it.
    if new_peak is not None and reading.peak_equity_usd is None:
        reading.peak_equity_usd = float(new_peak)

    # Re-classify after hydration (cheap; quality() is pure).
    quality = reading.quality()

    # Drawdown derived only when both inputs known. Column is NOT NULL
    # DEFAULT 0 in the schema; on first INSERT with no equity inputs we
    # must supply a concrete number (0.0). The "unknown" semantic lives
    # in lifecycle.exposure_status, not in this numeric column —
    # downstream engine MUST consult lifecycle.exposure_status (mirrors
    # M14.C's daily_pnl_available / unknown-zero discipline).
    new_drawdown = 0.0
    if (reading.current_equity_usd is not None and new_peak is not None
            and new_peak > 0):
        new_drawdown = 1.0 - (float(reading.current_equity_usd) / float(new_peak))
    elif prev is not None and prev["drawdown_from_peak"] is not None:
        new_drawdown = prev["drawdown_from_peak"]

    # Build the NEW lifecycle_json by merging M14.D-owned keys into the
    # existing dict — explicitly preserving every M14.C-owned key
    # untouched (key-namespacing discipline).
    new_lifecycle = dict(lifecycle)  # preserves M14.C keys verbatim
    new_lifecycle["exposure_status"] = quality.value
    new_lifecycle["exposure_missing_fields"] = (
        reading.missing_required() + reading.missing_opportunistic()
    )
    new_lifecycle["exposure_known_fields"] = reading.known_fields()
    new_lifecycle["exposure_fresh_reads_count"] = exposure_count
    new_lifecycle["exposure_latest_reading"] = _compact_latest_summary(reading)
    new_lifecycle["exposure_batch_id"] = exposure_batch_id
    events = list(new_lifecycle.get("exposure_events", []))
    events.append({
        "ts": _now_iso(),
        "quality": quality.value,
        "error": reading.error,
        "open_positions": reading.open_positions_count,
        "exposure_batch_id": exposure_batch_id,
    })
    # Cap event log at a reasonable size to bound row growth.
    new_lifecycle["exposure_events"] = events[-32:]

    open_positions = reading.open_positions_count if reading.has_fresh_exposure() else 0
    capital_deployed = (reading.capital_deployed_usd
                        if reading.has_fresh_exposure() else 0.0)

    if prev is None:
        # INSERT — fill only M14.D-owned columns + lifecycle; M14.C
        # columns stay at their DEFAULT 0 / 'unavailable' since this
        # row never had a PnL writer.
        conn.execute(
            "INSERT INTO daily_state_per_broker "
            "(date, broker_scope, open_positions, capital_deployed, "
            " peak_equity, drawdown_from_peak, source, "
            " last_ingested_at, updated_at, lifecycle_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day, scope, open_positions, capital_deployed,
             new_peak, new_drawdown,
             reading.source if reading.has_fresh_exposure() else (
                 prev["source"] if prev else "ingested"),
             _now_iso(), _now_iso(),
             json.dumps(new_lifecycle, separators=(",", ":"))),
        )
        result["status"] = "inserted"
    else:
        # UPDATE — only M14.D-owned columns. M14.C-owned columns are
        # NEVER referenced in this statement.
        conn.execute(
            "UPDATE daily_state_per_broker SET "
            " open_positions=?, capital_deployed=?, "
            " peak_equity=?, drawdown_from_peak=?, "
            " source=?, last_ingested_at=?, updated_at=?, "
            " lifecycle_json=? "
            "WHERE date=? AND broker_scope=?",
            (open_positions, capital_deployed,
             new_peak, new_drawdown,
             reading.source if reading.has_fresh_exposure() else prev["source"],
             _now_iso(), _now_iso(),
             json.dumps(new_lifecycle, separators=(",", ":")),
             day, scope),
        )
        result["status"] = "updated"

    # Append per-position rows under the shared exposure_batch_id.
    rows_written = 0
    if reading.has_fresh_exposure() and reading.positions:
        rows_written = _insert_position_rows(
            conn, scope, day, reading.fetched_at_utc, exposure_batch_id,
            reading.positions,
        )
    result["positions_written"] = rows_written
    result["exposure_fresh_reads_count"] = exposure_count

    conn.commit()
    return result


def ingest_exposure_all_scopes(
    conn: sqlite3.Connection,
    *,
    scopes: Optional[list] = None,
    adapter_factory=None,
    today: Optional[str] = None,
    dry_run: bool = False,
    audit_logger=None,
) -> dict:
    """Run all scopes and report. Returns
    {results: [...], any_unknown: bool}.
    `adapter_factory(scope) -> adapter` is required."""
    if adapter_factory is None:
        raise ValueError("adapter_factory is required")
    scopes_to_run = scopes or sorted(INGESTIBLE_SCOPES)
    out = []
    any_unknown = False
    for s in scopes_to_run:
        if s not in INGESTIBLE_SCOPES:
            raise ValueError(
                f"scope {s!r} not in {sorted(INGESTIBLE_SCOPES)}"
            )
        try:
            adapter = adapter_factory(s)
        except Exception as e:
            out.append({"scope": s, "status": "factory_error",
                        "error": f"{type(e).__name__}:{e}",
                        "quality": ExposureQuality.UNKNOWN.value})
            any_unknown = True
            continue
        r = ingest_exposure_once(
            conn, scope=s, today=today, adapter=adapter,
            dry_run=dry_run, audit_logger=audit_logger,
        )
        out.append(r)
        if r["quality"] == ExposureQuality.UNKNOWN.value:
            any_unknown = True
    return {"results": out, "any_unknown": any_unknown}


__all__ = [
    "INGESTIBLE_SCOPES",
    "ingest_exposure_once",
    "ingest_exposure_all_scopes",
]

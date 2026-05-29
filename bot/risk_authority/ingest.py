"""
bot/risk_authority/ingest.py — M14.C orchestrator.

Calls one adapter (or all), then UPSERTs one row into
daily_state_per_broker keyed (today_utc, broker_scope).

Per ChatGPT M14.C corrections:
  * Unknown ≠ zero. A UNKNOWN reading sets `daily_pnl_available=0` and
    records status='unknown' in lifecycle_json; numeric columns remain
    at their DEFAULT (which is 0 for NOT NULL columns), but downstream
    callers MUST consult lifecycle_json.status to distinguish
    known-zero from unknown.
  * Hysteresis: FRESH or PARTIAL (PnL fresh, opportunistic optional) → +1;
    UNKNOWN (required PnL missing) → reset 0. Correction #2: PARTIAL must
    not penalise PnL freshness just because exposure data was missing.
  * `peak_equity` ratchets only when peak_equity is known on a
    FRESH/PARTIAL reading; otherwise carried forward.
  * Dry-run never writes to DB.
  * Adapters NEVER raise to the orchestrator. Failures arrive as
    UNKNOWN readings.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from .reading import (
    BrokerPnLReading,
    ReadingQuality,
    has_fresh_pnl,
    is_unknown,
)

log = logging.getLogger(__name__)


# Valid broker scopes — must match the M14.B CHECK constraint exactly.
VALID_BROKER_SCOPES = {"ibkr_live", "ibkr_paper", "etoro_real",
                       "etoro_paper", "GLOBAL"}

# "GLOBAL" is a derived view and is NEVER written by ingestion.
INGESTIBLE_SCOPES = {"ibkr_live", "ibkr_paper", "etoro_real", "etoro_paper"}


class BrokerPnLAdapter(Protocol):
    name: str
    def read(self, *, today: str) -> BrokerPnLReading: ...


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Registry — DI for tests; production wiring happens in tools/ingest_risk_state.py.
_ADAPTER_FACTORIES: Dict[str, Callable[[], BrokerPnLAdapter]] = {}


def register_adapter_factory(scope: str,
                              factory: Callable[[], BrokerPnLAdapter]) -> None:
    if scope not in INGESTIBLE_SCOPES:
        raise ValueError(f"refusing to register adapter for non-ingestible scope "
                         f"{scope!r}; allowed={sorted(INGESTIBLE_SCOPES)}")
    _ADAPTER_FACTORIES[scope] = factory


def _resolve_adapter(scope: str,
                     adapter: Optional[BrokerPnLAdapter]) -> BrokerPnLAdapter:
    if adapter is not None:
        return adapter
    factory = _ADAPTER_FACTORIES.get(scope)
    if factory is None:
        raise LookupError(f"no adapter factory registered for scope {scope!r}")
    return factory()


def _read_existing_row(conn: sqlite3.Connection,
                        today: str, scope: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT realised_pnl_usd, realised_pnl_pct, daily_pnl_source, "
        "       daily_pnl_available, daily_loss_block_active, "
        "       daily_loss_alert_sent, realised_daily_loss, open_positions, "
        "       capital_deployed, peak_equity, drawdown_from_peak, source, "
        "       last_ingested_at, fresh_reads_count, lifecycle_json "
        "FROM daily_state_per_broker WHERE date=? AND broker_scope=?",
        (today, scope),
    ).fetchone()
    if row is None:
        return None
    cols = ["realised_pnl_usd", "realised_pnl_pct", "daily_pnl_source",
            "daily_pnl_available", "daily_loss_block_active",
            "daily_loss_alert_sent", "realised_daily_loss", "open_positions",
            "capital_deployed", "peak_equity", "drawdown_from_peak", "source",
            "last_ingested_at", "fresh_reads_count", "lifecycle_json"]
    return dict(zip(cols, row))


def _load_lifecycle(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def _compact_summary(r: BrokerPnLReading) -> dict:
    """Compact, redacted summary suitable for DB lifecycle_json.latest_reading.
    Per M14.C correction #5: do NOT store full raw broker responses in DB.
    Drop bulky/raw evidence; keep only the small status fields.
    """
    return {
        "fetched_at_utc":      r.fetched_at_utc,
        "success":             r.success,
        "quality":             r.quality.value,
        "realised_pnl_usd":    r.realised_pnl_usd,
        "realised_daily_loss": r.realised_daily_loss,
        "open_positions":      r.open_positions,
        "capital_deployed":    r.capital_deployed,
        "known_fields":        list(r.known_fields),
        "missing_fields":      list(r.missing_fields),
        "error":               r.error,
        # NB: r.evidence_summary intentionally NOT included here; it goes
        # to the rotating risk_ingest.log instead.
    }


def ingest_once(
    conn: sqlite3.Connection,
    *,
    scope: str,
    today: Optional[str] = None,
    adapter: Optional[BrokerPnLAdapter] = None,
    dry_run: bool = False,
    audit_logger: Optional[Any] = None,
) -> dict:
    """Perform one ingestion for one scope.

    Args:
      conn: SQLite connection. NOT written to when dry_run=True.
      scope: one of INGESTIBLE_SCOPES.
      today: 'YYYY-MM-DD' UTC; defaults to today.
      adapter: optional explicit adapter (DI); else resolved from registry.
      dry_run: when True, runs the adapter + computes the would-be UPSERT
        but performs no SQL writes. (M14.C correction #6.)
      audit_logger: optional bot.etoro.audit.AuditLogger.

    Returns a small dict suitable for logging/CLI output. Never raises
    on adapter failure — adapter failures arrive as UNKNOWN readings.
    """
    if scope not in INGESTIBLE_SCOPES:
        raise ValueError(f"scope {scope!r} not in {sorted(INGESTIBLE_SCOPES)}")
    today = today or _today_utc()

    adapter_obj = _resolve_adapter(scope, adapter)
    # Adapter must not raise; we still guard defensively here.
    try:
        reading = adapter_obj.read(today=today)
    except Exception as e:                          # pragma: no cover - defensive
        from .reading import make_unknown
        reading = make_unknown(scope, trading_day=today,
                                error=f"adapter_raised:{type(e).__name__}:{e}")

    summary = _compact_summary(reading)
    result = {
        "scope":             scope,
        "today":             today,
        "quality":           reading.quality.value,
        "status":            "dry_run" if dry_run else "pending",
        "known_fields":      list(reading.known_fields),
        "missing_fields":    list(reading.missing_fields),
        "error":             reading.error,
        "would_write":       not dry_run,
    }

    # Audit log — pre-write. Never raises. Compact entry; richer evidence
    # also recorded if the adapter supplied evidence_summary.
    if audit_logger is not None:
        audit_logger.event(
            "ingest_" + (
                "dry_run" if dry_run else ("unknown" if is_unknown(reading)
                                            else "ok")
            ),
            scope=scope,
            today=today,
            quality=reading.quality.value,
            known_fields=list(reading.known_fields),
            missing_fields=list(reading.missing_fields),
            error=reading.error,
            evidence_summary=reading.evidence_summary,
        )

    if dry_run:
        result["status"] = "dry_run"
        return result

    # UPSERT logic --------------------------------------------------------
    prev = _read_existing_row(conn, today, scope)
    lifecycle = _load_lifecycle(prev["lifecycle_json"] if prev else None)
    prev_fresh_count = int(prev["fresh_reads_count"]) if prev else 0

    # Hysteresis — per ChatGPT M14.C correction #2, opportunistic-missing
    # fields (PARTIAL) must NOT fail a fresh PnL read. So FRESH and PARTIAL
    # both increment the counter; only UNKNOWN (PnL missing or read failed)
    # resets to 0.
    if has_fresh_pnl(reading):                  # FRESH or PARTIAL
        fresh_count = min(prev_fresh_count + 1, 9999)
    else:                                       # UNKNOWN
        fresh_count = 0

    # daily_pnl_available: 1 only when PnL fields are known and read succeeded.
    daily_pnl_available = 1 if has_fresh_pnl(reading) else 0
    daily_pnl_source = scope if has_fresh_pnl(reading) else "unavailable"

    # peak_equity ratchets only on known values.
    if reading.peak_equity is not None and has_fresh_pnl(reading):
        new_peak = reading.peak_equity
        if prev and prev["peak_equity"] is not None:
            new_peak = max(float(prev["peak_equity"]),
                           float(reading.peak_equity))
    elif prev and prev["peak_equity"] is not None:
        new_peak = float(prev["peak_equity"])
    else:
        new_peak = None

    # Update lifecycle_json
    events = lifecycle.get("events")
    if not isinstance(events, list):
        events = []
    events.append({
        "ts":                _utc_now_iso(),
        "quality":           reading.quality.value,
        "source":            reading.source,
        "missing_fields":    list(reading.missing_fields),
        "known_fields":      list(reading.known_fields),
        "error":             reading.error,
    })
    # Cap event log size so lifecycle_json doesn't grow unbounded.
    events = events[-50:]
    lifecycle["events"]          = events
    lifecycle["status"]          = reading.quality.value
    lifecycle["reading_quality"] = reading.quality.value
    lifecycle["known_fields"]    = list(reading.known_fields)
    lifecycle["missing_fields"]  = list(reading.missing_fields)
    lifecycle["latest_reading"]  = summary
    lifecycle["last_quality_at"] = _utc_now_iso()

    # Numeric columns: write known values; for unknown leave column at
    # whatever the existing row holds, or DEFAULT (0) for fresh inserts.
    # CRITICAL: daily_pnl_available=0 + lifecycle.status='unknown' is the
    # *only* truthful signal of unknown when the numeric column reads 0.
    rpnl_usd = (reading.realised_pnl_usd if reading.realised_pnl_usd is not None
                else (prev["realised_pnl_usd"] if prev else 0.0))
    rpnl_pct = (reading.realised_pnl_pct if reading.realised_pnl_pct is not None
                else (prev["realised_pnl_pct"] if prev else 0.0))
    rdl      = (reading.realised_daily_loss if reading.realised_daily_loss is not None
                else (prev["realised_daily_loss"] if prev else 0.0))
    op       = (reading.open_positions if reading.open_positions is not None
                else (prev["open_positions"] if prev else 0))
    cap_d    = (reading.capital_deployed if reading.capital_deployed is not None
                else (prev["capital_deployed"] if prev else 0.0))
    dd_peak  = (reading.drawdown_from_peak if reading.drawdown_from_peak is not None
                else (prev["drawdown_from_peak"] if prev else 0.0))

    src_col = reading.source if has_fresh_pnl(reading) else (
        prev["source"] if prev else "ingested"
    )
    last_ingested = _utc_now_iso()

    now = _utc_now_iso()
    try:
        if prev is None:
            conn.execute(
                "INSERT INTO daily_state_per_broker "
                "(date, broker_scope, realised_pnl_usd, realised_pnl_pct, "
                " daily_pnl_source, daily_pnl_available, "
                " daily_loss_block_active, daily_loss_alert_sent, "
                " realised_daily_loss, open_positions, capital_deployed, "
                " peak_equity, drawdown_from_peak, source, "
                " last_ingested_at, fresh_reads_count, lifecycle_json, "
                " updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (today, scope,
                 float(rpnl_usd), float(rpnl_pct),
                 daily_pnl_source, int(daily_pnl_available),
                 0, 0,
                 float(rdl), int(op), float(cap_d),
                 new_peak, float(dd_peak), src_col,
                 last_ingested, int(fresh_count),
                 json.dumps(lifecycle), now),
            )
            status = "inserted"
        else:
            conn.execute(
                "UPDATE daily_state_per_broker SET "
                " realised_pnl_usd=?, realised_pnl_pct=?, "
                " daily_pnl_source=?, daily_pnl_available=?, "
                " realised_daily_loss=?, open_positions=?, "
                " capital_deployed=?, peak_equity=?, drawdown_from_peak=?, "
                " source=?, last_ingested_at=?, fresh_reads_count=?, "
                " lifecycle_json=?, updated_at=? "
                "WHERE date=? AND broker_scope=?",
                (float(rpnl_usd), float(rpnl_pct),
                 daily_pnl_source, int(daily_pnl_available),
                 float(rdl), int(op), float(cap_d),
                 new_peak, float(dd_peak),
                 src_col, last_ingested, int(fresh_count),
                 json.dumps(lifecycle), now,
                 today, scope),
            )
            status = "updated"
        conn.commit()
    except sqlite3.Error as e:
        log.error("[ingest] UPSERT failed scope=%s today=%s: %s",
                  scope, today, e)
        result["status"] = "db_error"
        result["error"]  = f"{type(e).__name__}: {e}"
        return result

    result["status"]            = status
    result["fresh_reads_count"] = fresh_count
    result["daily_pnl_available"] = daily_pnl_available
    return result


def ingest_all_scopes(
    conn: sqlite3.Connection,
    *,
    scopes: Optional[List[str]] = None,
    today: Optional[str] = None,
    dry_run: bool = False,
    audit_logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """Iterate ingestion over all (or selected) ingestible scopes.

    Continues through every scope even if individual scopes return
    UNKNOWN readings (M14.C correction #6). The overall summary reports
    whether any required scope was unknown so CLI callers can exit
    non-zero.
    """
    scopes_to_run = scopes or sorted(INGESTIBLE_SCOPES)
    for s in scopes_to_run:
        if s not in INGESTIBLE_SCOPES:
            raise ValueError(f"scope {s!r} not in {sorted(INGESTIBLE_SCOPES)}")

    results: Dict[str, Any] = {}
    any_unknown = False
    any_db_error = False
    for s in scopes_to_run:
        try:
            r = ingest_once(conn, scope=s, today=today, dry_run=dry_run,
                            audit_logger=audit_logger)
        except LookupError as e:
            # Adapter not registered — count as unknown but continue.
            r = {"scope": s, "today": today or _today_utc(),
                 "quality": "unknown", "status": "no_adapter",
                 "error": str(e), "would_write": False}
        results[s] = r
        if r.get("quality") == "unknown" or r.get("status") in ("no_adapter",
                                                                  "db_error"):
            any_unknown = True
        if r.get("status") == "db_error":
            any_db_error = True

    return {
        "today":        today or _today_utc(),
        "dry_run":      dry_run,
        "results":      results,
        "any_unknown":  any_unknown,
        "any_db_error": any_db_error,
    }


__all__ = [
    "BrokerPnLAdapter",
    "VALID_BROKER_SCOPES",
    "INGESTIBLE_SCOPES",
    "register_adapter_factory",
    "ingest_once",
    "ingest_all_scopes",
]

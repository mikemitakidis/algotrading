"""bot/risk_authority/dashboard_read.py — M14.G read-only query helpers.

Provides four narrow SELECT-only helpers for the dashboard API:

  * list_recent_decisions(conn, limit, scope) — risk_decisions tail.
  * get_scope_status(conn)                    — per-scope view for all 4 scopes.
  * get_latest_snapshot(conn)                 — most recent risk_snapshots row.
  * get_authority_view(conn)                  — derived per-scope authority.

Hard contract per ChatGPT M14.G plan:
  * READ-ONLY: no INSERT/UPDATE/DELETE, no commit(), no executemany.
    AST-enforced. Runtime-enforced via a NOT-WRITE connection wrapper
    in test_m14_g_dashboard.py.
  * No broker imports. No live-write imports. No ingestion imports.
    No imports of bot.etoro.live_broker / tools.etoro_live_write /
    bot.brokers / bot.risk_authority.preflight / bot.risk_authority.ingest_*.
  * Known-zero vs unknown is explicit at the JSON boundary —
    `pnl_known_zero` and `exposure_known_zero` booleans are emitted in
    addition to the status string so the UI cannot silently collapse
    them.
  * Four canonical scopes always appear in the result, even when no
    daily_state_per_broker row exists (status 'absent' / known=False).

This module never imports from the dashboard either — the dashboard
calls in, never the other way around. That keeps scanner-isolation
intact: importing bot.scanner does not load this module.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.risk_authority.snapshot import ALL_BROKER_SCOPES

log = logging.getLogger(__name__)

# Reasons that require manual_reset to clear (cribbed from
# bot.risk_authority.governor._MANUAL_RESET_ONLY_REASONS). Repeated
# here as a literal so this module has zero imports from governor —
# governor's vocabulary is the constitution, ours is the dashboard
# projection of it. Mismatch is caught by a cross-test.
_MANUAL_RESET_ONLY_REASONS = frozenset({
    "global_kill",
    "broker_kill",
    "drawdown_throttle_hit",
})

# Maximum decisions per request — server-side cap per M14.G plan.
DECISIONS_MAX_LIMIT = 100
DECISIONS_DEFAULT_LIMIT = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_json_loads(blob: Optional[str], default):
    if not blob:
        return default
    try:
        parsed = json.loads(blob)
        return parsed if parsed is not None else default
    except (TypeError, ValueError):
        return default


def list_recent_decisions(
    conn: sqlite3.Connection,
    *,
    limit: int = DECISIONS_DEFAULT_LIMIT,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the latest decisions ordered by taken_at descending.

    `limit` is clamped to [1, DECISIONS_MAX_LIMIT]. `scope`, if given,
    must be one of ALL_BROKER_SCOPES, else ValueError.

    Returns {decisions: [...], total_count: int, as_of_utc: str}.
    """
    if not isinstance(limit, int) or limit < 1:
        limit = DECISIONS_DEFAULT_LIMIT
    limit = min(limit, DECISIONS_MAX_LIMIT)

    if scope is not None and scope not in ALL_BROKER_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(ALL_BROKER_SCOPES)}, got {scope!r}"
        )

    where = "WHERE broker_scope=?" if scope else ""
    params = (scope,) if scope else ()
    rows = conn.execute(
        f"SELECT decision_id, taken_at, broker_scope, requested_action, "
        f"       request_json, result, authority_before, authority_after, "
        f"       reason_codes, recovery_paths, snapshot_id, source, "
        f"       actor, explainer "
        f"FROM risk_decisions {where} "
        f"ORDER BY taken_at DESC, decision_id DESC LIMIT ?",
        params + (limit,),
    ).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) FROM risk_decisions {where}",
        params,
    ).fetchone()[0]

    decisions = []
    for r in rows:
        decisions.append({
            "decision_id":      r[0],
            "taken_at":         r[1],
            "broker_scope":     r[2],
            "requested_action": r[3],
            "request_payload":  _safe_json_loads(r[4], None),
            "result":           r[5],
            "authority_before": r[6],
            "authority_after":  r[7],
            "reason_codes":     _safe_json_loads(r[8], []),
            "recovery_paths":   _safe_json_loads(r[9], {}),
            "snapshot_id":      r[10],
            "source":           r[11],
            "actor":            r[12],
            "explainer":        r[13],
        })
    return {
        "decisions":    decisions,
        "total_count":  total,
        "as_of_utc":    _now_iso(),
    }


def _classify_warnings(*, pnl_status: str, exposure_status: str,
                       exposure_fresh_reads_count: int,
                       daily_loss_block_active: bool) -> List[str]:
    """Build the small enumerated warnings list for one scope. Empty
    means green badge in the UI."""
    out = []
    if pnl_status in ("unknown", "unavailable", "absent"):
        out.append("pnl_unknown")
    if exposure_status in ("exposure_unknown", "absent"):
        out.append("exposure_unknown")
    elif (exposure_status in ("exposure_fresh", "exposure_partial")
            and exposure_fresh_reads_count < 3):
        out.append("exposure_stale")
    if daily_loss_block_active:
        out.append("daily_loss_block_active")
    return out


def get_scope_status(
    conn: sqlite3.Connection,
    *,
    trading_day: Optional[str] = None,
) -> Dict[str, Any]:
    """Per-scope view for all 4 canonical scopes.

    Always returns all four scopes, even when no row exists (status
    'absent'). Distinguishes known-zero from unknown via explicit
    `pnl_known_zero` and `exposure_known_zero` booleans.
    """
    day = trading_day or _today_utc()
    out: Dict[str, Any] = {}
    for scope in ALL_BROKER_SCOPES:
        row = conn.execute(
            "SELECT realised_pnl_usd, realised_daily_loss, "
            "       daily_pnl_available, daily_pnl_source, "
            "       daily_loss_block_active, "
            "       open_positions, capital_deployed, peak_equity, "
            "       drawdown_from_peak, fresh_reads_count, "
            "       last_ingested_at, lifecycle_json "
            "FROM daily_state_per_broker "
            "WHERE date=? AND broker_scope=?",
            (day, scope),
        ).fetchone()

        if row is None:
            out[scope] = {
                "scope":                       scope,
                "pnl_status":                  "absent",
                "pnl_known":                   False,
                "pnl_known_zero":              False,
                "realised_pnl_usd":            0.0,
                "realised_daily_loss":         0.0,
                "exposure_status":             "absent",
                "exposure_known":              False,
                "exposure_known_zero":         False,
                "open_positions":              0,
                "capital_deployed":            0.0,
                "peak_equity":                 None,
                "drawdown_from_peak":          0.0,
                "exposure_fresh_reads_count":  0,
                "daily_loss_block_active":     False,
                "last_ingested_at":            None,
                "warnings":                    ["pnl_unknown", "exposure_unknown"],
            }
            continue

        lifecycle = _safe_json_loads(row[11], {})
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        pnl_status      = lifecycle.get("status") or row[3] or "unavailable"
        exposure_status = lifecycle.get("exposure_status") or "absent"
        exp_count = int(lifecycle.get("exposure_fresh_reads_count", 0) or 0)
        pnl_known = bool(row[2]) and pnl_status in ("fresh", "partial")
        exposure_known = exposure_status in ("exposure_fresh",
                                              "exposure_partial")
        realised_pnl = float(row[0] or 0.0)
        realised_loss = float(row[1] or 0.0)
        open_pos = int(row[5] or 0)
        capital = float(row[6] or 0.0)

        out[scope] = {
            "scope":                       scope,
            "pnl_status":                  str(pnl_status),
            "pnl_known":                   pnl_known,
            "pnl_known_zero": (pnl_known
                               and realised_pnl == 0.0
                               and realised_loss == 0.0),
            "realised_pnl_usd":            realised_pnl,
            "realised_daily_loss":         realised_loss,
            "exposure_status":             str(exposure_status),
            "exposure_known":              exposure_known,
            "exposure_known_zero": (exposure_known
                                     and open_pos == 0
                                     and capital == 0.0),
            "open_positions":              open_pos,
            "capital_deployed":            capital,
            "peak_equity":                 row[7],
            "drawdown_from_peak":          float(row[8] or 0.0),
            "exposure_fresh_reads_count":  exp_count,
            "daily_loss_block_active":     bool(row[4]),
            "last_ingested_at":            row[10],
            "warnings": _classify_warnings(
                pnl_status=str(pnl_status),
                exposure_status=str(exposure_status),
                exposure_fresh_reads_count=exp_count,
                daily_loss_block_active=bool(row[4]),
            ),
        }
    return {
        "scopes":          out,
        "as_of_utc":       _now_iso(),
        "trading_day_utc": day,
    }


def get_latest_snapshot(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Return the most recent risk_snapshots row, parsed. None on empty
    table."""
    row = conn.execute(
        "SELECT id, taken_at, policy_version, snapshot_json, "
        "       freshness_summary, source "
        "FROM risk_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    snap = _safe_json_loads(row[3], {})
    if not isinstance(snap, dict):
        snap = {}
    fresh = _safe_json_loads(row[4], {})
    if not isinstance(fresh, dict):
        fresh = {}
    global_view = snap.get("global_view", {}) if isinstance(snap, dict) else {}
    if not isinstance(global_view, dict):
        global_view = {}
    return {
        "snapshot_id":      row[0],
        "taken_at":         row[1],
        "policy_version":   row[2],
        "trading_day_utc":  snap.get("trading_day_utc"),
        "source":           row[5],
        "combined": {
            "combined_capital_deployed":
                global_view.get("combined_capital_deployed"),
            "combined_open_positions":
                global_view.get("combined_open_positions"),
            "combined_realised_daily_loss":
                global_view.get("combined_realised_daily_loss"),
            "per_symbol_exposure":
                global_view.get("per_symbol_exposure", {}),
            "any_pnl_unknown":
                bool(global_view.get("any_pnl_unknown", False)),
            "any_exposure_unknown":
                bool(global_view.get("any_exposure_unknown", False)),
            "unknown_pnl_scopes":
                list(global_view.get("unknown_pnl_scopes", [])),
            "unknown_exposure_scopes":
                list(global_view.get("unknown_exposure_scopes", [])),
        },
        "freshness_summary": fresh,
    }


def get_authority_view(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Per-scope effective authority derived from the latest
    risk_decisions row for each scope.

    M14.G is read-only: there is NO persistent governor state. We
    report whatever the most recent decision recorded as
    authority_after. The `manual_reset_would_be_required` flag is
    derived from the reason set (no DB write to compute it).

    Scopes without any recorded decision report None values + a
    `latest_authority_after=null` so the UI can render "no decisions
    yet" rather than fabricating a default level.
    """
    out: Dict[str, Any] = {}
    for scope in ALL_BROKER_SCOPES:
        row = conn.execute(
            "SELECT decision_id, taken_at, authority_after, reason_codes, "
            "       result "
            "FROM risk_decisions "
            "WHERE broker_scope=? "
            "ORDER BY taken_at DESC, decision_id DESC LIMIT 1",
            (scope,),
        ).fetchone()
        if row is None:
            out[scope] = {
                "latest_authority_after":           None,
                "latest_downgrade_reason":          None,
                "latest_decision_id":               None,
                "latest_taken_at":                  None,
                "latest_result":                    None,
                "manual_reset_would_be_required":   False,
            }
            continue
        reasons = _safe_json_loads(row[3], [])
        first_reason = reasons[0] if isinstance(reasons, list) and reasons else None
        needs_reset = (
            first_reason is not None
            and first_reason in _MANUAL_RESET_ONLY_REASONS
        )
        out[scope] = {
            "latest_authority_after":           row[2],
            "latest_downgrade_reason":          first_reason,
            "latest_decision_id":               row[0],
            "latest_taken_at":                  row[1],
            "latest_result":                    row[4],
            "manual_reset_would_be_required":   needs_reset,
        }
    return {
        "scopes":    out,
        "as_of_utc": _now_iso(),
    }


__all__ = [
    "list_recent_decisions",
    "get_scope_status",
    "get_latest_snapshot",
    "get_authority_view",
    "DECISIONS_DEFAULT_LIMIT",
    "DECISIONS_MAX_LIMIT",
]

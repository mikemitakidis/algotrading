"""bot/risk_authority/audit_decisions.py — M14.E audit-row writer.

The pure engine (`engine.decide`) does NOT touch the DB. This module is
the thin wrapper that:

  1. Persists the RiskSnapshot to `risk_snapshots`.
  2. Persists the RiskDecision to `risk_decisions`, linked by `snapshot_id`.

Per ChatGPT M14.E correction #1:
    Keep `decide(...)` pure. It must not write to the DB. Add a
    separate thin wrapper such as `decide_and_audit(...)` for writing
    `risk_snapshots` and `risk_decisions`.

Read-only with respect to broker-state tables: this module NEVER writes
to `daily_state_per_broker`, `broker_positions`, or any other M14.B/C/D
table. It writes only to the two audit tables that M14.B already
shipped, exactly as the M14.A design specified.

NO broker call. NO HTTP write verb. NO order method. NO live broker
construction. AST-enforced in tests.
"""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from bot.risk_authority.engine import (
    RiskContext,
    RiskDecision,
    RiskPolicyView,
    TradeRequest,
    decide,
)
from bot.risk_authority.snapshot import RiskSnapshot

log = logging.getLogger(__name__)


# ── Redaction (carry-forward of M13.5.B audit discipline) ────────────────────

_REDACT_KEYS = {
    "api_key", "user_key", "api-key", "user-key",
    "x-api-key", "x-user-key", "authorization", "cookie", "set-cookie",
    "token", "bearer", "etoro_api_key", "etoro_user_key",
    "etoro_real_api_key", "etoro_real_user_key",
    "telegram_token", "telegram_bot_token",
}
_TRUNC_KEYS = {"cid", "CID", "GCID", "userId", "user_id",
               "accountId", "account_id"}


def _redact(v: Any) -> Any:
    if isinstance(v, dict):
        out = {}
        for k, sub in v.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                out[k] = "<REDACTED>"
            elif isinstance(k, str) and k in _TRUNC_KEYS:
                s = str(sub) if sub is not None else ""
                out[k] = "<REDACTED>" if len(s) <= 4 else f"***{s[-4:]}"
            else:
                out[k] = _redact(sub)
        return out
    if isinstance(v, list):
        return [_redact(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_redact(x) for x in v)
    return v


def _scope_view_to_dict(sv) -> dict:
    if is_dataclass(sv):
        d = {f.name: getattr(sv, f.name) for f in fields(sv)}
    else:
        d = dict(sv) if isinstance(sv, dict) else {}
    # positions is a tuple of dicts; pass through.
    return d


def _serialize_snapshot(snap: RiskSnapshot) -> str:
    """Compact, redacted JSON of the snapshot for risk_snapshots.snapshot_json."""
    payload = {
        "taken_at_utc":    snap.taken_at_utc,
        "trading_day_utc": snap.trading_day_utc,
        "policy_version":  snap.policy_version,
        "scopes":          {k: _scope_view_to_dict(v) for k, v in snap.scopes.items()},
        "global_view":     _scope_view_to_dict(snap.global_view),
        "raw_evidence":    dict(snap.raw_evidence or {}),
    }
    return json.dumps(_redact(payload), separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _freshness_summary(snap: RiskSnapshot) -> str:
    summary = {
        "any_pnl_unknown":         snap.global_view.any_pnl_unknown,
        "any_exposure_unknown":    snap.global_view.any_exposure_unknown,
        "unknown_pnl_scopes":      list(snap.global_view.unknown_pnl_scopes),
        "unknown_exposure_scopes": list(snap.global_view.unknown_exposure_scopes),
    }
    return json.dumps(summary, separators=(",", ":"))


def write_snapshot(conn: sqlite3.Connection, snap: RiskSnapshot, *,
                   source: str = "pre_decision") -> int:
    """Insert one risk_snapshots row. Returns the inserted id."""
    if source not in ("scheduled", "on_demand", "pre_decision"):
        raise ValueError(f"invalid snapshot source: {source!r}")
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO risk_snapshots "
        "(taken_at, policy_version, snapshot_json, freshness_summary, "
        " source, created_at) VALUES (?,?,?,?,?,?)",
        (snap.taken_at_utc, snap.policy_version,
         _serialize_snapshot(snap), _freshness_summary(snap),
         source, now),
    )
    return cur.lastrowid


def write_decision(conn: sqlite3.Connection, decision: RiskDecision,
                   *, snapshot_id: int,
                   source: str = "auto",
                   actor: str = "system") -> str:
    """Insert one risk_decisions row. Returns the decision_id."""
    if source not in ("auto", "manual", "reconciled", "manual_reset"):
        raise ValueError(f"invalid decision source: {source!r}")
    now = datetime.now(timezone.utc).isoformat()
    req_json = (json.dumps(_redact(decision.request_payload),
                           separators=(",", ":"))
                if decision.request_payload is not None else None)
    conn.execute(
        "INSERT INTO risk_decisions "
        "(decision_id, taken_at, broker_scope, requested_action, "
        " request_json, result, authority_before, authority_after, "
        " reason_codes, recovery_paths, snapshot_id, source, actor, "
        " explainer, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision.decision_id, decision.taken_at_utc,
         decision.broker_scope, decision.requested_action,
         req_json, decision.result,
         decision.authority_before.name, decision.authority_after.name,
         json.dumps(list(decision.reason_codes)),
         json.dumps(decision.recovery_paths),
         snapshot_id, source, actor,
         decision.explainer, now),
    )
    return decision.decision_id


def decide_and_audit(
    conn: sqlite3.Connection,
    context: RiskContext,
    snapshot: RiskSnapshot,
    request: Optional[TradeRequest] = None,
    *,
    policy: Optional[RiskPolicyView] = None,
    audit_source: str = "auto",
    actor: str = "system",
) -> RiskDecision:
    """The auditing wrapper around the pure engine.

    Order of operations:
      1. Run the PURE engine: `decide(context, snapshot, request, policy)`.
      2. Persist the snapshot to `risk_snapshots`.
      3. Persist the decision to `risk_decisions`, linking by snapshot id.
      4. Return the RiskDecision (with `snapshot_ref` populated).

    The DB is committed AFTER both rows are written; either both rows
    persist or neither does.
    """
    decision = decide(context, snapshot, request, policy=policy)
    started_tx = not conn.in_transaction
    if started_tx:
        conn.execute("BEGIN")
    try:
        snapshot_id = write_snapshot(conn, snapshot, source="pre_decision")
        # Mutate the immutable decision by building a copy with snapshot_ref.
        decision_with_ref = RiskDecision(
            decision_id=decision.decision_id,
            taken_at_utc=decision.taken_at_utc,
            broker_scope=decision.broker_scope,
            requested_action=decision.requested_action,
            result=decision.result,
            authority_before=decision.authority_before,
            authority_after=decision.authority_after,
            reason_codes=decision.reason_codes,
            recovery_paths=decision.recovery_paths,
            explainer=decision.explainer,
            snapshot_ref=snapshot_id,
            request_payload=decision.request_payload,
        )
        write_decision(conn, decision_with_ref, snapshot_id=snapshot_id,
                       source=audit_source, actor=actor)
        if started_tx:
            conn.commit()
        return decision_with_ref
    except Exception:
        if started_tx:
            conn.rollback()
        raise


__all__ = [
    "decide_and_audit",
    "write_snapshot",
    "write_decision",
]

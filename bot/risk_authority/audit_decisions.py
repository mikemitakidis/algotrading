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
import uuid
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
    "write_manual_reset_decision",
]


# ─────────────────────────────────────────────────────────────────────────────
# M15.3.B — manual_reset operator audit
# ─────────────────────────────────────────────────────────────────────────────
#
# Per the M14.A design (docs/M14_A_design.md §390, M14_FINAL_AUDIT.md §226)
# every operator-initiated manual_reset must write a risk_decisions row with
# source='manual_reset'. The vocabulary has existed since M14 (the source
# enum already accepts 'manual_reset' — see write_decision() above and the
# CHECK constraint in bot/flywheel.py); only the writer was missing.
#
# This function is the audit writer. It is intentionally:
#   * NOT calling decide() or any other engine function — manual_reset is
#     not a trade evaluation; the operator is asserting "I cleared the
#     locks", not asking the engine for a decision.
#   * NOT requiring a RiskDecision dataclass or a RiskSnapshot — those
#     are for trade-decision audit rows.
#   * NOT committing — the caller commits as part of its atomic write.
#
# It writes a single risk_decisions row with:
#   * broker_scope='GLOBAL'          (manual_reset is always a global action;
#                                     per-broker kill-switch state is captured
#                                     via switches_cleared+explainer)
#   * requested_action='query_authority'
#                                    (best-fit from the closed action set;
#                                     manual_reset is not 'trade_open' or
#                                     'trade_close')
#   * result='allow'                 (the operation completed)
#   * authority_before/after='OFF'   (kill_switch=true forces OFF per M14.F
#                                     preflight; engine will re-evaluate
#                                     authority on next cycle)
#   * source='manual_reset'
#   * snapshot_id=NULL               (no engine snapshot; manual_reset is
#                                     an operator action, not an engine
#                                     evaluation)
#
# The row schema is in bot/flywheel.py RISK_DECISIONS_SCHEMA. snapshot_id
# is INTEGER (nullable) — NULL is valid per the schema (it's a soft FK
# only; cf. comment in flywheel.py above the schema definition).
#
# This function is callable WITHOUT importing dashboard code — it stays
# inside the bot/risk_authority package and uses only stdlib + the
# already-imported `bot.risk_authority.engine` types (implicit; no new
# imports added).
#
# The operator's reason text is stored in the `explainer` column. It is
# plain text; the dashboard renders explainer values with HTML escaping
# (existing M14.G audit display behaviour) so XSS is not a concern.

def write_manual_reset_decision(
    conn: sqlite3.Connection,
    *,
    switches_cleared: list,
    reason_text: str,
    actor: str = "operator",
    now_iso: Optional[str] = None,
) -> str:
    """Insert one risk_decisions row recording an operator manual_reset.

    Returns the new decision_id (a short ULID-ish string).

    Does NOT call conn.commit() — caller manages the transaction.

    Arguments:
      conn               : sqlite3 connection. Must have risk_decisions
                           table available (M14.B migration).
      switches_cleared   : list of scope names whose kill_switch went
                           True->False. May be empty (idempotent no-op).
      reason_text        : operator-supplied reason. Stored in explainer.
                           Caller is responsible for length validation.
      actor              : short identifier for the operator (e.g.
                           'operator', or 'operator:<client_ip>'). Stored
                           in the actor column. NEVER contains the raw
                           session id or any secret material.
      now_iso            : UTC ISO timestamp. Defaults to now if None.
                           Tests inject for determinism.

    No engine call. No broker call. No HTTP call. No live broker
    construction. AST-enforced in test_m15_3_b_manual_reset.py.
    """
    if not isinstance(switches_cleared, list):
        raise TypeError(
            f"switches_cleared must be a list, got {type(switches_cleared)!r}"
        )
    if not isinstance(reason_text, str):
        raise TypeError(
            f"reason_text must be a string, got {type(reason_text)!r}"
        )
    if not isinstance(actor, str) or not actor:
        raise ValueError("actor must be a non-empty string")

    decision_id = f"mr-{uuid.uuid4().hex[:16]}"
    ts = now_iso or datetime.now(timezone.utc).isoformat()

    cleared_sorted = sorted(str(s) for s in switches_cleared)
    noop = len(cleared_sorted) == 0
    if noop:
        explainer = (
            "manual_reset (no-op): all kill switches were already cleared; "
            "operator confirmed reset intent. "
            f"Operator reason: {reason_text}"
        )
    else:
        explainer = (
            f"manual_reset: cleared kill switches {cleared_sorted!r}; "
            "engine will re-evaluate authority on next cycle. "
            f"Operator reason: {reason_text}"
        )

    reason_codes = json.dumps(["manual_reset"])
    recovery_paths = json.dumps(
        {"manual_reset": "operator cleared kill switches"}
    )

    conn.execute(
        "INSERT INTO risk_decisions "
        "(decision_id, taken_at, broker_scope, requested_action, "
        " request_json, result, authority_before, authority_after, "
        " reason_codes, recovery_paths, snapshot_id, source, actor, "
        " explainer, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, ts, "GLOBAL", "query_authority",
         None, "allow",
         "OFF", "OFF",
         reason_codes, recovery_paths,
         None,  # snapshot_id — manual_reset is operator action, not engine eval
         "manual_reset", actor,
         explainer, ts),
    )
    return decision_id

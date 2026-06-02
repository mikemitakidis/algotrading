"""bot/risk_authority/preflight.py — M14.F operator preflight bridge.

Connects the existing M13.5 eToro live-write operator path to the M14.E
Risk Authority Engine. Pure orchestration:

    1. snapshot = assemble_snapshot(conn)                  [M14.E read-only]
    2. policy   = policy_view_from_allocation_policy(
                     load_policy(conn))                    [M13.4A source of truth]
    3. ctx      = RiskContext(broker_scope, 'trade_open', current_authority, ...)
    4. decision = decide_and_audit(conn, ctx, snapshot, request, ...)
    5. return PreflightResult(allowed=..., decision=..., ...)

This module NEVER:
  * contacts a broker
  * places an order
  * makes any HTTP write (POST/DELETE/PUT/PATCH)
  * imports bot.etoro.live_broker, tools.etoro_live_write, or any broker adapter
  * writes to the DB directly — every write goes through
    `bot.risk_authority.audit_decisions.decide_and_audit`, the single
    audit surface approved in M14.E.

The CALLER decides what to do with the PreflightResult. The intended
caller is `tools/etoro_live_write.py`, which exits with code 4 on
`allowed=False` BEFORE constructing any broker, minting any nonce, or
contacting transport.

Per ChatGPT M14.F corrections / hard rules:
  * Risk Authority must run before transport/env/nonce.
  * Risk Authority must be testable even when ETORO_LIVE_ENABLED is
    false/absent — this module has no env-flag dependency.
  * audit_source defaults to 'manual' (operator-typed CLI invocation);
    'manual_reset' is reserved for the future M14.G upgrade path.
  * --authority AUTO_ALLOWED still does not bypass Risk Authority; the
    engine consults every gate regardless of the supplied level.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from bot.broker_allocation import load_policy
from bot.risk_authority.audit_decisions import decide_and_audit
from bot.risk_authority.authority import Authority
from bot.risk_authority.engine import (
    RiskContext,
    RiskDecision,
    TradeRequest,
    policy_view_from_allocation_policy,
)
from bot.risk_authority.snapshot import assemble_snapshot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightResult:
    """Frozen result of one preflight call. Everything the caller
    (operator CLI) needs to render a denial cleanly, exit with the
    correct code, or proceed into the existing M13.5 envelope."""
    allowed: bool
    decision: RiskDecision
    reason_codes: Tuple[str, ...]
    recovery_paths: Dict[str, str]
    decision_id: str
    authority_before: Authority
    authority_after: Authority

    @property
    def explainer(self) -> str:
        return self.decision.explainer


def run_risk_preflight(
    conn: sqlite3.Connection,
    *,
    broker_scope: str,
    request: TradeRequest,
    current_authority: Authority,
    actor: str = "operator",
    audit_source: str = "manual",
    market_open: bool = True,
    quote_age_sec: Optional[float] = None,
    quote_max_age_sec: float = 30.0,
    spread_bps: Optional[float] = None,
    spread_max_bps: float = 50.0,
) -> PreflightResult:
    """Run M14.E Risk Authority on a proposed live-write request.

    PURE ORCHESTRATION. NEVER contacts a broker. NEVER places an order.
    NEVER touches transport. The only DB writes are the audit-table
    inserts performed by `decide_and_audit` (M14.E-approved single
    audit surface): one risk_snapshots row + one risk_decisions row.

    The caller (operator CLI) is responsible for:
      * deciding whether to honour the result (exit 4 on block,
        proceed into M13.5 envelope on allow),
      * NEVER bypassing this call,
      * NEVER weakening any existing gate (env flag, nonce, schema
        validate, operator confirmation) downstream of an allow.
    """
    if not isinstance(current_authority, Authority):
        raise TypeError(
            f"current_authority must be Authority, got "
            f"{type(current_authority).__name__}"
        )
    if audit_source not in ("auto", "manual", "reconciled"):
        # 'manual_reset' is deliberately excluded — that vocabulary is
        # reserved for the explicit authority-upgrade carrier in M14.G.
        raise ValueError(
            f"invalid audit_source {audit_source!r}; M14.F allows "
            f"only 'auto'|'manual'|'reconciled'"
        )

    # 1. Assemble a read-only snapshot of M14.B/C/D state.
    snapshot = assemble_snapshot(conn)

    # 2. Load M13.4A policy (the dashboard / broker-allocation source
    #    of truth) and bridge to the engine's view. The bridge is pure;
    #    it takes a dict, not a conn.
    policy_dict = load_policy(conn)
    policy_view = policy_view_from_allocation_policy(policy_dict)

    # 3. Build the engine context. trade_open is the live-write action.
    ctx = RiskContext(
        broker_scope=broker_scope,
        requested_action="trade_open",
        current_authority=current_authority,
        market_open=market_open,
        quote_age_sec=quote_age_sec,
        quote_max_age_sec=quote_max_age_sec,
        spread_bps=spread_bps,
        spread_max_bps=spread_max_bps,
    )

    # 4. Decide + audit. This is the only DB-writing surface; preflight
    #    itself does NO direct conn.execute / INSERT / UPDATE.
    decision = decide_and_audit(
        conn, ctx, snapshot, request,
        policy=policy_view,
        audit_source=audit_source,
        actor=actor,
    )

    return PreflightResult(
        allowed=(decision.result == "allow"),
        decision=decision,
        reason_codes=tuple(decision.reason_codes),
        recovery_paths=dict(decision.recovery_paths),
        decision_id=decision.decision_id,
        authority_before=decision.authority_before,
        authority_after=decision.authority_after,
    )


__all__ = ["PreflightResult", "run_risk_preflight"]

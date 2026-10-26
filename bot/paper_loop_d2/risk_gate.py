"""M21.1extra-D2.3 — Risk Authority gate for the paper learning loop.

Every new paper entry must pass the M14 Risk Authority (the same engine the
eToro operator path uses) before the broker is called:

    run_risk_preflight(conn, broker_scope='ibkr_paper', request=TradeRequest(...),
                       current_authority=Authority.AUTO_ALLOWED, actor='paper_learning_loop',
                       audit_source='auto')

The loop is the unattended (auto) lane, so it presents AUTO_ALLOWED; the
engine still blocks unless policy, kill switches, caps and state freshness
allow (trade_open needs >= ONE_SHOT_MANUAL; lower levels are always blocked).
The operator's time-boxed D0 authorization is checked separately, earlier.

The engine writes its own audit rows (risk_snapshots / risk_decisions) through
its single approved audit surface; this adapter writes nothing itself. Any
exception or unusable result is a BLOCK (fail closed). The decision id is
returned so the journal can tie each order to the decision that allowed it.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional, Tuple

GateResult = Tuple[bool, str, Optional[str]]
RiskGate = Callable[..., GateResult]

PAPER_SCOPE = "ibkr_paper"
ACTOR = "paper_learning_loop"


def make_risk_authority_gate(*, conn_factory: Callable[[], sqlite3.Connection],
                             authority_name: str = "AUTO_ALLOWED",
                             broker_scope: str = PAPER_SCOPE) -> RiskGate:
    if broker_scope != PAPER_SCOPE:
        raise ValueError("the paper learning loop only ever uses the ibkr_paper scope")

    def gate(*, symbol: str, side: str, notional_usd: float, stop_loss: float,
             take_profit: float) -> GateResult:
        from bot.risk_authority import Authority, run_risk_preflight
        from bot.risk_authority.engine import TradeRequest
        conn = None
        try:
            conn = conn_factory()
            res = run_risk_preflight(
                conn, broker_scope=broker_scope,
                request=TradeRequest(symbol=symbol, amount_usd=float(notional_usd), side=side,
                                     sl=float(stop_loss), tp=float(take_profit)),
                current_authority=Authority.from_string(authority_name),
                actor=ACTOR, audit_source="auto", market_open=True)
        except Exception as exc:
            return False, "risk_authority_error:%s" % type(exc).__name__, None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if res.allowed is not True:
            return False, "risk_authority_block:%s" % (",".join(res.reason_codes) or "unspecified"), \
                res.decision_id
        return True, "risk_authority_allow", res.decision_id

    return gate

"""bot/risk_authority/ — M14 Risk Intelligence Layer.

M14.B scope: schema/migration helpers only. The decision engine, governor,
ingestion, exposure logic, and dashboard are deferred to M14.C–G per the
approved M14.A design (docs/M14_A_design.md).

This package must NEVER be imported from bot.scanner, bot.strategy,
bot.risk, or main.py. M14.E introduces the pure decide() core; the
public re-exports below are deliberately minimal (Authority + decide).
"""
from bot.risk_authority.authority import Authority  # re-export
from bot.risk_authority.engine import decide        # re-export
from bot.risk_authority.preflight import (
    PreflightResult,
    run_risk_preflight,
)  # re-export (M14.F)
from bot.risk_authority.dashboard_read import (
    get_authority_view,
    get_latest_snapshot,
    get_scope_status,
    list_recent_decisions,
)  # re-export (M14.G — read-only)

__all__ = [
    "Authority", "decide", "PreflightResult", "run_risk_preflight",
    "list_recent_decisions", "get_scope_status",
    "get_latest_snapshot", "get_authority_view",
]

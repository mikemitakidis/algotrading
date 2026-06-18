"""M20.F paper position closing + realised PnL.

Pure, in-memory: close an OPEN LONG PaperPosition at a caller-supplied exit price
and commissions, producing a CLOSED PaperPosition copy and realised-PnL metrics.
Reuses the frozen M20.A PaperPosition contract (no schema change). Full close
only. Invalid inputs reject safely via PaperCloseResult (no exception, no closed
position constructed). NO cash ledger, portfolio ledger, snapshot, storage, or
broker/live logic. The input position is never mutated. No wall-clock, no RNG.

Realised PnL (LONG):
    entry_notional     = quantity * average_entry_price
    exit_notional      = quantity * exit_price
    gross_realized_pnl = exit_notional - entry_notional
    total_commission   = entry_commission + exit_commission
    net_realized_pnl   = gross_realized_pnl - total_commission
    realized_pnl_pct   = net_realized_pnl / entry_notional * 100
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from bot.paper.schema import PaperPosition, PaperPositionStatus, PaperSide


@dataclass(frozen=True)
class PaperCloseResult:
    ok: bool
    closed_position: Optional[PaperPosition] = None
    rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    derived_metrics: Dict[str, Any] = field(default_factory=dict)


def _parse_utc(ts) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    if parsed.utcoffset().total_seconds() != 0:
        return None
    return parsed


def _finite_positive(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _finite_non_negative(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _reject(reason: str) -> PaperCloseResult:
    return PaperCloseResult(ok=False, closed_position=None,
                            rejection_reason=reason, reason_codes=[reason])


def close_paper_position(
    position: PaperPosition,
    *,
    exit_price: float,
    closed_at_utc: str,
    entry_commission: float = 0.0,
    exit_commission: float = 0.0,
) -> PaperCloseResult:
    """Close an OPEN LONG position (full close only). Mechanical: rejects safely
    on invalid input, never raises for normal bad inputs, never mutates the
    input position. Does not touch cash/equity (realised PnL only)."""
    if not isinstance(position, PaperPosition):
        return _reject("invalid_position")
    if position.status != PaperPositionStatus.OPEN:
        return _reject("position_not_open")
    if position.side != PaperSide.LONG:
        return _reject("non_long_not_closed")
    if not _finite_positive(position.quantity):
        return _reject("non_positive_quantity")
    if not _finite_positive(position.average_entry_price):
        return _reject("non_positive_entry_price")
    if not _finite_positive(exit_price):
        return _reject("invalid_exit_price")
    if not _finite_non_negative(entry_commission):
        return _reject("negative_entry_commission")
    if not _finite_non_negative(exit_commission):
        return _reject("negative_exit_commission")

    closed_dt = _parse_utc(closed_at_utc)
    if closed_dt is None:
        return _reject("invalid_timestamp")
    opened_dt = _parse_utc(position.opened_at_utc)
    if opened_dt is not None and closed_dt < opened_dt:
        return _reject("close_before_open")

    # ── realised PnL (LONG) ──
    entry_notional = position.quantity * position.average_entry_price
    exit_notional = position.quantity * exit_price
    gross_realized_pnl = exit_notional - entry_notional
    total_commission = entry_commission + exit_commission
    net_realized_pnl = gross_realized_pnl - total_commission
    realized_pnl_pct = net_realized_pnl / entry_notional * 100.0

    try:
        # closed copy: keep id/symbol/side/quantity/avg_entry/opened_at for
        # audit history; only status/closed_at/pnl change. Original untouched.
        closed_position = replace(
            position,
            status=PaperPositionStatus.CLOSED,
            closed_at_utc=closed_at_utc,
            unrealized_pnl=0.0,
            realized_pnl=net_realized_pnl,
        )
    except (ValueError, TypeError) as e:
        return PaperCloseResult(ok=False, closed_position=None,
                                rejection_reason="invalid_close_inputs",
                                reason_codes=["invalid_close_inputs"],
                                warnings=[str(e)])

    derived_metrics = {
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "gross_realized_pnl": gross_realized_pnl,
        "entry_commission": entry_commission,
        "exit_commission": exit_commission,
        "total_commission": total_commission,
        "net_realized_pnl": net_realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
        "exit_price": exit_price,
    }

    return PaperCloseResult(ok=True, closed_position=closed_position,
                            derived_metrics=derived_metrics)

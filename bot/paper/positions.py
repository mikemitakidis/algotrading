"""M20.E paper position builder.

Pure, in-memory: convert a frozen M20.A PaperOrder + PaperFill into an OPEN
PaperPosition. Reuses the frozen PaperPosition contract (no schema change).
Invalid inputs reject safely via PaperPositionResult (no exception, no
PaperPosition constructed). NO position closing, realised PnL, cash ledger,
portfolio accounting, storage, or broker/live logic. No mutation of inputs, no
wall-clock, no RNG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from bot.paper.schema import (
    PaperOrder, PaperFill, PaperPosition, PaperPositionStatus, PaperSide,
)
from bot.paper import provenance


@dataclass(frozen=True)
class PaperPositionResult:
    ok: bool
    position: Optional[PaperPosition] = None
    rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _valid_utc(ts) -> bool:
    if not isinstance(ts, str) or not ts.strip():
        return False
    s = ts.strip()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    return parsed.utcoffset().total_seconds() == 0


def _finite_positive(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _reject(reason: str) -> PaperPositionResult:
    return PaperPositionResult(ok=False, position=None,
                               rejection_reason=reason, reason_codes=[reason])


def build_paper_position(
    order: PaperOrder,
    fill: PaperFill,
    *,
    opened_at_utc: str,
) -> PaperPositionResult:
    """Build an OPEN PaperPosition from a matching order + full fill. Mechanical:
    rejects safely on invalid input, never raises for normal bad inputs, never
    mutates the inputs."""
    if not isinstance(order, PaperOrder):
        return _reject("invalid_order")
    if not isinstance(fill, PaperFill):
        return _reject("invalid_fill")
    if order.paper_order_id != fill.paper_order_id:
        return _reject("order_fill_id_mismatch")
    if order.side != PaperSide.LONG:
        return _reject("non_long_not_positioned")
    if not _finite_positive(order.quantity):
        return _reject("non_positive_order_quantity")
    if not _finite_positive(fill.fill_quantity):
        return _reject("non_positive_fill_quantity")
    if not _finite_positive(fill.fill_price):
        return _reject("non_positive_fill_price")
    if not math.isclose(fill.fill_quantity, order.quantity,
                        rel_tol=1e-9, abs_tol=1e-9):
        return _reject("fill_quantity_mismatch")
    if not _valid_utc(opened_at_utc):
        return _reject("invalid_timestamp")

    position_id = provenance.paper_position_id({
        "paper_order_id": order.paper_order_id,
        "paper_fill_id": fill.paper_fill_id,
        "symbol": order.symbol,
        "side": PaperSide.LONG.value,
        "quantity": repr(fill.fill_quantity),
        "average_entry_price": repr(fill.fill_price),
        "opened_at_utc": opened_at_utc,
    })

    try:
        position = PaperPosition(
            paper_position_id=position_id,
            symbol=order.symbol,
            side=PaperSide.LONG,
            quantity=fill.fill_quantity,
            average_entry_price=fill.fill_price,
            status=PaperPositionStatus.OPEN,
            opened_at_utc=opened_at_utc,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            closed_at_utc=None,
        )
    except (ValueError, TypeError) as e:
        return PaperPositionResult(ok=False, position=None,
                                   rejection_reason="invalid_position_inputs",
                                   reason_codes=["invalid_position_inputs"],
                                   warnings=[str(e)])

    return PaperPositionResult(ok=True, position=position)

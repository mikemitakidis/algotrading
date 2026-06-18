"""M20.D simulated paper fill.

Pure, in-memory: simulate a deterministic LONG fill from a frozen M20.A
PaperOrder and caller-supplied execution assumptions, producing a frozen M20.A
PaperFill. Reuses the frozen PaperFill contract (no schema change). Invalid
inputs reject safely via PaperFillResult (no exception, no PaperFill
constructed). NO position/PnL/ledger/storage/broker/live logic, no order-state
mutation, no wall-clock, no RNG.

Fill model (LONG):
    fill_price       = simulated_market_price * (1 + slippage_bps / 10000)
    fill_notional    = fill_price * order.quantity
    commission       = max(flat_commission, fill_notional * commission_bps/10000)
    assumed_slippage = (fill_price - simulated_market_price) * order.quantity
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from bot.paper.schema import PaperOrder, PaperFill, PaperSide
from bot.paper import provenance


@dataclass(frozen=True)
class PaperFillResult:
    ok: bool
    fill: Optional[PaperFill] = None
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


def _finite_non_negative(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _reject(reason: str) -> PaperFillResult:
    return PaperFillResult(ok=False, fill=None, rejection_reason=reason,
                           reason_codes=[reason])


def simulate_paper_fill(
    order: PaperOrder,
    *,
    simulated_market_price: float,
    fill_time_utc: str,
    slippage_bps: float = 0.0,
    commission_bps: float = 0.0,
    flat_commission: float = 0.0,
) -> PaperFillResult:
    """Simulate a deterministic LONG fill. Mechanical: rejects safely on invalid
    input, never raises for normal bad inputs, never mutates the order."""
    if not isinstance(order, PaperOrder):
        return _reject("invalid_order")
    if order.side != PaperSide.LONG:
        return _reject("non_long_not_filled")
    if not _finite_positive(order.quantity):
        return _reject("non_positive_quantity")
    if not _finite_positive(simulated_market_price):
        return _reject("invalid_market_price")
    if not _finite_non_negative(slippage_bps):
        return _reject("negative_slippage")
    if not _finite_non_negative(commission_bps):
        return _reject("negative_commission")
    if not _finite_non_negative(flat_commission):
        return _reject("negative_flat_commission")
    if not _valid_utc(fill_time_utc):
        return _reject("invalid_timestamp")

    # ── deterministic LONG fill model ──
    fill_price = simulated_market_price * (1 + slippage_bps / 10000.0)
    fill_notional = fill_price * order.quantity
    bps_commission = fill_notional * commission_bps / 10000.0
    commission = max(flat_commission, bps_commission)
    assumed_slippage = (fill_price - simulated_market_price) * order.quantity

    reason_codes = [
        f"slippage_applied_bps:{slippage_bps}",
        "commission_model:flat" if flat_commission >= bps_commission
        else "commission_model:bps",
    ]

    fill_id = provenance.paper_fill_id({
        "paper_order_id": order.paper_order_id,
        "fill_price": repr(fill_price),
        "fill_quantity": repr(order.quantity),
        "fill_time_utc": fill_time_utc,
        "commission": repr(commission),
    })

    try:
        fill = PaperFill(
            paper_fill_id=fill_id,
            paper_order_id=order.paper_order_id,
            fill_price=fill_price,
            fill_quantity=order.quantity,
            fill_time_utc=fill_time_utc,
            assumed_slippage=assumed_slippage,
            assumed_commission=commission,
        )
    except (ValueError, TypeError) as e:
        return PaperFillResult(ok=False, fill=None,
                               rejection_reason="invalid_fill_inputs",
                               reason_codes=["invalid_fill_inputs"],
                               warnings=[str(e)])

    return PaperFillResult(ok=True, fill=fill, rejection_reason=None,
                           reason_codes=sorted(reason_codes))

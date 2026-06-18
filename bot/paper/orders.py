"""M20.D paper order building.

Pure, in-memory: convert a paper_routing_eligible PaperRoutingDecision plus a
valid PaperSizingPreview into a frozen M20.A PaperOrder (status
PENDING_SIMULATION). Reuses the frozen PaperOrder contract (no schema change).
Invalid inputs reject safely via PaperOrderResult (no exception, no PaperOrder
constructed). NO position/PnL/ledger/storage/broker/live logic. No lifecycle
mutation beyond creating the order at PENDING_SIMULATION. No wall-clock, no RNG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from bot.paper.schema import (
    PaperOrder, PaperOrderType, PaperOrderStatus, PaperSide,
    PaperRoutingDecision,
)
from bot.paper.sizing import PaperSizingPreview
from bot.paper import provenance


@dataclass(frozen=True)
class PaperOrderResult:
    ok: bool
    order: Optional[PaperOrder] = None
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


def _reject(reason: str, warnings: Optional[List[str]] = None
            ) -> PaperOrderResult:
    return PaperOrderResult(ok=False, order=None, rejection_reason=reason,
                            reason_codes=[reason], warnings=warnings or [])


def build_paper_order(
    decision: PaperRoutingDecision,
    sizing: PaperSizingPreview,
    *,
    reference_price: float,
    created_at_utc: str,
    order_type: PaperOrderType = PaperOrderType.MARKET,
) -> PaperOrderResult:
    """Build a PaperOrder from a routable decision + valid sizing preview.
    Mechanical: rejects safely on invalid input, never raises for normal bad
    inputs, never mutates the inputs."""
    # ── eligibility / shape preconditions ──
    if getattr(decision, "paper_routing_eligible", None) is not True:
        return _reject("not_paper_routable")
    if getattr(sizing, "sizing_eligible", None) is not True:
        return _reject("sizing_not_eligible")
    if getattr(sizing, "side", None) != PaperSide.LONG:
        return _reject("non_long_not_ordered")
    if not _finite_positive(getattr(sizing, "paper_quantity", None)):
        return _reject("non_positive_quantity")
    if not _finite_positive(getattr(sizing, "paper_notional", None)):
        return _reject("non_positive_notional")
    if not _finite_positive(reference_price):
        return _reject("invalid_reference_price")

    cid = getattr(decision, "m19_candidate_id", None)
    symbol = getattr(decision, "symbol", None)
    if not isinstance(cid, str) or not cid:
        return _reject("invalid_candidate_shape")
    if not isinstance(symbol, str) or not symbol:
        return _reject("invalid_candidate_shape")
    if not _valid_utc(created_at_utc):
        return _reject("invalid_timestamp")

    # ── advisory: reference price differs from the sizing basis ──
    warnings: List[str] = []
    reason_codes: List[str] = []
    sizing_basis = None
    if sizing.paper_quantity:
        sizing_basis = sizing.paper_notional / sizing.paper_quantity
    if sizing_basis is not None and not math.isclose(
            reference_price, sizing_basis, rel_tol=1e-9, abs_tol=1e-9):
        warnings.append("reference_price_differs_from_sizing_basis")

    # ── deterministic id + order construction ──
    order_id = provenance.paper_order_id({
        "m19_candidate_id": cid,
        "symbol": symbol,
        "side": PaperSide.LONG.value,
        "quantity": repr(sizing.paper_quantity),
        "reference_price": repr(reference_price),
        "created_at_utc": created_at_utc,
        "order_type": order_type.value if isinstance(order_type, PaperOrderType)
        else str(order_type),
    })

    try:
        order = PaperOrder(
            paper_order_id=order_id,
            m19_candidate_id=cid,
            symbol=symbol,
            side=PaperSide.LONG,
            order_type=order_type,
            quantity=sizing.paper_quantity,
            reference_price=reference_price,
            paper_routing_eligible=True,
            status=PaperOrderStatus.PENDING_SIMULATION,
            created_at_utc=created_at_utc,
            reason_codes=sorted(reason_codes),
        )
    except (ValueError, TypeError) as e:
        # defensive: any residual schema rejection becomes a safe reject
        return _reject("invalid_order_inputs", warnings=[str(e)])

    return PaperOrderResult(ok=True, order=order, rejection_reason=None,
                            reason_codes=sorted(reason_codes),
                            warnings=sorted(warnings))

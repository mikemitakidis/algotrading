"""M20.C clean-room paper risk sizing.

Pure, in-memory: take a paper_routing_eligible PaperRoutingDecision plus caller-
supplied capital/price/risk inputs and produce a PaperSizingPreview (quantity,
notional, risk, stop distance, capital/cash) or a safe rejection. Fractional
quantity (preview only, NOT an order). NO PaperOrder/Fill/Position/PnL creation,
NO storage, NO broker/live/risk_authority/account/market calls, NO wall-clock,
NO RNG. Never mutates the input decision.

Cash model: cash_required = capital_used = paper_notional (no leverage/margin).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from bot.paper.schema import PaperRoutingDecision, PaperSide

SCHEMA_VERSION = "m20_paper_sizing_v1"


def _require_utc(ts: Any, field_name: str) -> str:
    if not isinstance(ts, str) or not ts.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO UTC string")
    s = ts.strip()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{field_name} invalid ISO timestamp: {ts!r} ({e})")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC: {ts!r}")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (+00:00/Z): {ts!r}")
    return s


def _finite_number(value: Any) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


@dataclass(frozen=True)
class PaperSizingPreview:
    m19_candidate_id: str
    symbol: str
    side: PaperSide
    sizing_eligible: bool
    evaluated_at_utc: str
    paper_quantity: float = 0.0
    paper_notional: float = 0.0
    paper_risk_amount: float = 0.0
    paper_risk_pct: float = 0.0
    stop_distance: float = 0.0
    capital_used: float = 0.0
    cash_required: float = 0.0
    binding_constraint: Optional[str] = None
    sizing_rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        if isinstance(self.side, str):
            object.__setattr__(self, "side", PaperSide(self.side))
        elif not isinstance(self.side, PaperSide):
            raise ValueError(f"side: unknown PaperSide {self.side!r}")
        _require_utc(self.evaluated_at_utc, "evaluated_at_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperSizingPreview":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


def _reject(decision, evaluated_at_utc, reason) -> PaperSizingPreview:
    """A safe, fully-zeroed rejection preview."""
    side = decision.side if isinstance(getattr(decision, "side", None),
                                       PaperSide) else PaperSide.LONG
    return PaperSizingPreview(
        m19_candidate_id=getattr(decision, "m19_candidate_id", "") or "",
        symbol=getattr(decision, "symbol", "") or "",
        side=side,
        sizing_eligible=False,
        evaluated_at_utc=evaluated_at_utc,
        sizing_rejection_reason=reason,
        reason_codes=[reason],
    )


def compute_paper_sizing(
    decision: PaperRoutingDecision,
    *,
    paper_equity: float,
    available_paper_cash: float,
    reference_price: float,
    evaluated_at_utc: str,
    stop_loss_price: Optional[float] = None,
    stop_distance: Optional[float] = None,
    max_risk_pct: float = 1.0,
    max_position_notional_pct: float = 0.20,
) -> PaperSizingPreview:
    """Produce a PaperSizingPreview. Pure, in-memory, fractional quantity. Never
    mutates `decision`. Bad inputs reject safely (sizing_eligible=False,
    paper_quantity=0.0, specific sizing_rejection_reason)."""
    # evaluated_at_utc is validated by the preview dataclass on construction.

    # ── eligibility preconditions (safe rejects) ──
    if getattr(decision, "paper_routing_eligible", None) is not True:
        return _reject(decision, evaluated_at_utc, "not_paper_routable")
    if getattr(decision, "side", None) != PaperSide.LONG:
        return _reject(decision, evaluated_at_utc, "non_long_not_sized")

    if not _finite_number(reference_price) or reference_price <= 0:
        return _reject(decision, evaluated_at_utc, "invalid_reference_price")

    if not _finite_number(paper_equity) or paper_equity <= 0 \
            or not _finite_number(available_paper_cash):
        return _reject(decision, evaluated_at_utc, "invalid_capital_inputs")
    if available_paper_cash <= 0:
        return _reject(decision, evaluated_at_utc, "invalid_capital_inputs")

    if not _finite_number(max_risk_pct) or max_risk_pct <= 0 \
            or max_risk_pct > 100:
        return _reject(decision, evaluated_at_utc, "invalid_sizing_limits")
    if not _finite_number(max_position_notional_pct) \
            or max_position_notional_pct <= 0 or max_position_notional_pct > 1:
        return _reject(decision, evaluated_at_utc, "invalid_sizing_limits")

    # ── stop handling ──
    resolved_stop = _resolve_stop(reference_price, stop_loss_price,
                                  stop_distance)
    if resolved_stop is None:
        # distinguish "missing" from "invalid"
        if stop_loss_price is None and stop_distance is None:
            return _reject(decision, evaluated_at_utc, "missing_stop")
        return _reject(decision, evaluated_at_utc, "invalid_stop")

    # ── sizing math (clean-room, deterministic) ──
    risk_budget = paper_equity * (max_risk_pct / 100.0)
    raw_quantity = risk_budget / resolved_stop
    notional_cap_quantity = (
        paper_equity * max_position_notional_pct) / reference_price
    cash_cap_quantity = available_paper_cash / reference_price

    candidates = {
        "risk_pct": raw_quantity,
        "position_notional_cap": notional_cap_quantity,
        "cash_cap": cash_cap_quantity,
    }
    binding_constraint = min(candidates, key=candidates.get)
    paper_quantity = candidates[binding_constraint]

    if not math.isfinite(paper_quantity) or paper_quantity <= 0:
        return _reject(decision, evaluated_at_utc, "invalid_capital_inputs")

    paper_notional = paper_quantity * reference_price
    paper_risk_amount = paper_quantity * resolved_stop
    paper_risk_pct = paper_risk_amount / paper_equity * 100.0

    return PaperSizingPreview(
        m19_candidate_id=decision.m19_candidate_id,
        symbol=decision.symbol,
        side=PaperSide.LONG,
        sizing_eligible=True,
        evaluated_at_utc=evaluated_at_utc,
        paper_quantity=paper_quantity,
        paper_notional=paper_notional,
        paper_risk_amount=paper_risk_amount,
        paper_risk_pct=paper_risk_pct,
        stop_distance=resolved_stop,
        capital_used=paper_notional,
        cash_required=paper_notional,
        binding_constraint=binding_constraint,
        reason_codes=[f"binding_constraint:{binding_constraint}"],
    )


def _resolve_stop(reference_price, stop_loss_price, stop_distance
                  ) -> Optional[float]:
    """Resolve the stop distance for a LONG. Returns a positive finite distance,
    or None if missing/invalid/inconsistent."""
    have_price = stop_loss_price is not None
    have_dist = stop_distance is not None
    if not have_price and not have_dist:
        return None  # caller maps to missing_stop

    expected = None
    if have_price:
        if not _finite_number(stop_loss_price):
            return None
        expected = reference_price - stop_loss_price
        if not math.isfinite(expected) or expected <= 0:
            return None  # stop not below entry for a long

    if have_dist:
        if not _finite_number(stop_distance) or stop_distance <= 0:
            return None
        if have_price and not math.isclose(
                stop_distance, expected, rel_tol=1e-9, abs_tol=1e-9):
            return None  # inconsistent -> invalid_stop
        return float(stop_distance)

    return float(expected)

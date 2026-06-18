"""M20.E paper PnL marking.

Pure, in-memory: mark an OPEN LONG PaperPosition to market, producing a frozen
M20.A PaperPnLSnapshot plus a marked_position copy with unrealized_pnl updated.
Reuses the frozen PaperPnLSnapshot / PaperPosition contracts (no schema change).
Per-position numeric metrics that the portfolio-level snapshot does not carry
(mark_price, entry/market notional, unrealized_pnl_pct) are returned in
PaperPnLResult.derived_metrics, NOT in the schema.

NO realised PnL calculation (daily_realized_pnl is caller-supplied/0.0), NO
position closing, NO cash ledger (cash values are caller-supplied), NO storage,
NO broker/live logic. The input position is never mutated. No wall-clock, no RNG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from bot.paper.schema import (
    PaperPosition, PaperPnLSnapshot, PaperPositionStatus, PaperSide,
)


@dataclass(frozen=True)
class PaperPnLResult:
    ok: bool
    snapshot: Optional[PaperPnLSnapshot] = None
    marked_position: Optional[PaperPosition] = None
    rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    derived_metrics: Dict[str, Any] = field(default_factory=dict)


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


def _finite_number(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _reject(reason: str) -> PaperPnLResult:
    return PaperPnLResult(ok=False, rejection_reason=reason,
                          reason_codes=[reason])


def mark_paper_position(
    position: PaperPosition,
    *,
    mark_price: float,
    paper_equity: float,
    available_paper_cash: float,
    evaluated_at_utc: str,
    locked_paper_margin: float = 0.0,
    daily_realized_pnl: float = 0.0,
    drawdown_pct: float = 0.0,
) -> PaperPnLResult:
    """Mark an OPEN LONG position to market. Mechanical: rejects safely on
    invalid input, never raises for normal bad inputs, never mutates the input
    position. Cash values are caller-supplied (no cash ledger in M20.E)."""
    if not isinstance(position, PaperPosition):
        return _reject("invalid_position")
    if position.status != PaperPositionStatus.OPEN:
        return _reject("position_not_open")
    if not _finite_positive(position.quantity):
        return _reject("non_positive_quantity")
    if not _finite_positive(position.average_entry_price):
        return _reject("non_positive_entry_price")
    if position.side != PaperSide.LONG:
        return _reject("non_long_not_marked")
    if not _finite_positive(mark_price):
        return _reject("invalid_mark_price")
    if not _finite_positive(paper_equity):
        return _reject("invalid_paper_equity")
    if not _finite_non_negative(available_paper_cash):
        return _reject("invalid_available_paper_cash")
    if not _finite_non_negative(locked_paper_margin):
        return _reject("invalid_locked_paper_margin")
    if not _finite_non_negative(drawdown_pct):
        return _reject("invalid_drawdown_pct")
    if not _finite_number(daily_realized_pnl):
        return _reject("invalid_daily_realized_pnl")
    if not _valid_utc(evaluated_at_utc):
        return _reject("invalid_timestamp")

    # ── LONG mark-to-market ──
    entry_notional = position.quantity * position.average_entry_price
    market_notional = position.quantity * mark_price
    unrealized_pnl = market_notional - entry_notional
    unrealized_pnl_pct = unrealized_pnl / entry_notional * 100.0

    warnings = ["cash_ledger_not_modeled", "cash_values_caller_supplied"]
    reason_codes: List[str] = []

    try:
        snapshot = PaperPnLSnapshot(
            timestamp_utc=evaluated_at_utc,
            total_paper_equity=paper_equity,
            available_paper_cash=available_paper_cash,
            locked_paper_margin=locked_paper_margin,
            daily_realized_pnl=daily_realized_pnl,
            unrealized_pnl=unrealized_pnl,
            drawdown_pct=drawdown_pct,
        )
        # marked copy: only unrealized_pnl changes; original is untouched
        marked_position = replace(position, unrealized_pnl=unrealized_pnl)
    except (ValueError, TypeError) as e:
        return PaperPnLResult(ok=False, rejection_reason="invalid_snapshot_inputs",
                              reason_codes=["invalid_snapshot_inputs"],
                              warnings=[str(e)])

    derived_metrics = {
        "mark_price": mark_price,
        "entry_notional": entry_notional,
        "market_notional": market_notional,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
    }

    return PaperPnLResult(ok=True, snapshot=snapshot,
                          marked_position=marked_position,
                          reason_codes=sorted(reason_codes),
                          warnings=sorted(warnings),
                          derived_metrics=derived_metrics)

"""M20.G paper account state + pure accounting transitions.

Pure, in-memory paper-accounting layer over the existing paper objects. Tracks
available cash, locked margin (always 0 in M20.G), cumulative realised PnL, open
positions, and processed close ids; computes equity/unrealised PnL from caller-
supplied marks; emits ledger events (reusing the frozen PaperEvent) and a close-
time PaperPnLSnapshot (reusing the frozen schema).

Model (fully cash-funded; no margin/leverage/short/multi-currency):
  open  : available_paper_cash -= fill_notional + entry_commission
  close : available_paper_cash += exit_notional - exit_commission
  equity: available_paper_cash + sum(quantity * mark_price for open positions)

Realised PnL is absorbed from PaperCloseResult ONLY (never recomputed from
prices). Duplicate closes are blocked via processed_close_ids. Operations that
would drive cash or equity below zero are rejected (no clamping). All transitions
return new copies; inputs are never mutated. No storage, no wall-clock, no RNG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bot.paper.schema import (
    PaperPosition, PaperPositionStatus, PaperSide, PaperPnLSnapshot,
    PaperEvent,
)
from bot.paper.closing import PaperCloseResult
from bot.paper.ledger import (
    build_account_event, PaperLedgerResult,  # noqa: F401
)

SCHEMA_VERSION = "m20_paper_account_v1"
_TOL = 1e-6


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


def _valid_utc(ts) -> bool:
    try:
        _require_utc(ts, "ts")
        return True
    except ValueError:
        return False


def _finite_positive(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _finite_non_negative(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


@dataclass(frozen=True)
class PaperAccountState:
    starting_equity: float
    available_paper_cash: float
    as_of_utc: str
    locked_paper_margin: float = 0.0
    realized_pnl_cumulative: float = 0.0
    total_commissions_paid: float = 0.0
    open_positions: Tuple[PaperPosition, ...] = ()
    processed_close_ids: Tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        if not _finite_non_negative(self.starting_equity):
            raise ValueError("starting_equity must be a non-negative number")
        if not _finite_non_negative(self.available_paper_cash):
            raise ValueError("available_paper_cash must be >= 0")
        if not _finite_non_negative(self.locked_paper_margin):
            raise ValueError("locked_paper_margin must be >= 0")
        _require_utc(self.as_of_utc, "as_of_utc")
        # deterministic ordering of open positions
        object.__setattr__(
            self, "open_positions",
            tuple(sorted(self.open_positions,
                         key=lambda p: p.paper_position_id)))
        object.__setattr__(
            self, "processed_close_ids",
            tuple(sorted(self.processed_close_ids)))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["open_positions"] = [p.to_dict() for p in self.open_positions]
        d["processed_close_ids"] = list(self.processed_close_ids)
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperAccountState":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        payload = {k: v for k, v in d.items() if k in allowed}
        if "open_positions" in payload:
            payload["open_positions"] = tuple(
                p if isinstance(p, PaperPosition) else PaperPosition.from_dict(p)
                for p in payload["open_positions"])
        if "processed_close_ids" in payload:
            payload["processed_close_ids"] = tuple(payload["processed_close_ids"])
        return cls(**payload)


@dataclass(frozen=True)
class PaperAccountResult:
    ok: bool
    account_state: Optional[PaperAccountState] = None
    events: List[PaperEvent] = field(default_factory=list)
    snapshot: Optional[PaperPnLSnapshot] = None
    rejection_reason: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    derived_metrics: Dict[str, Any] = field(default_factory=dict)


def _reject(reason: str) -> PaperAccountResult:
    return PaperAccountResult(ok=False, rejection_reason=reason,
                              reason_codes=[reason])


def new_account(*, starting_equity: float, as_of_utc: str) -> PaperAccountResult:
    """Create a fresh account: cash == starting_equity, no positions."""
    if not _finite_positive(starting_equity):
        return _reject("invalid_starting_equity")
    if not _valid_utc(as_of_utc):
        return _reject("invalid_timestamp")
    state = PaperAccountState(starting_equity=starting_equity,
                              available_paper_cash=starting_equity,
                              as_of_utc=as_of_utc)
    return PaperAccountResult(ok=True, account_state=state)


def _open_ids(state: PaperAccountState):
    return {p.paper_position_id for p in state.open_positions}


def open_position_in_account(
    state: PaperAccountState,
    position: PaperPosition,
    *,
    fill_notional: float,
    entry_commission: float = 0.0,
    event_time_utc: str,
) -> PaperAccountResult:
    """Open a position: consume cash = fill_notional + entry_commission. Reject
    on insufficient cash, duplicate id, or invalid input. Returns a new state."""
    if not isinstance(state, PaperAccountState):
        return _reject("invalid_account_state")
    if not isinstance(position, PaperPosition):
        return _reject("invalid_position")
    if position.status != PaperPositionStatus.OPEN:
        return _reject("position_not_open")
    if position.side != PaperSide.LONG:
        return _reject("non_long_not_supported")
    if not _finite_positive(fill_notional):
        return _reject("invalid_fill_notional")
    if not _finite_non_negative(entry_commission):
        return _reject("negative_entry_commission")
    if not _valid_utc(event_time_utc):
        return _reject("invalid_timestamp")
    if position.paper_position_id in _open_ids(state):
        return _reject("duplicate_position_id")
    if position.paper_position_id in state.processed_close_ids:
        return _reject("already_closed_in_account")

    cost = fill_notional + entry_commission
    new_cash = state.available_paper_cash - cost
    if new_cash < -_TOL:
        return _reject("insufficient_cash")

    new_state = PaperAccountState(
        starting_equity=state.starting_equity,
        available_paper_cash=max(0.0, new_cash),
        as_of_utc=event_time_utc,
        locked_paper_margin=state.locked_paper_margin,
        realized_pnl_cumulative=state.realized_pnl_cumulative,
        total_commissions_paid=state.total_commissions_paid + entry_commission,
        open_positions=state.open_positions + (position,),
        processed_close_ids=state.processed_close_ids,
    )
    event = build_account_event(
        event_type="POSITION_OPENED", event_time_utc=event_time_utc,
        paper_position_id=position.paper_position_id, symbol=position.symbol,
        detail={"cash_delta": -cost, "fill_notional": fill_notional,
                "entry_commission": entry_commission,
                "available_paper_cash": new_state.available_paper_cash})
    return PaperAccountResult(
        ok=True, account_state=new_state,
        events=[event.event] if event.ok else [],
        derived_metrics={"cash_delta": -cost,
                         "available_paper_cash": new_state.available_paper_cash})


def _equity_components(state: PaperAccountState, marks: Dict[str, float]):
    open_market_value = 0.0
    unrealized_pnl = 0.0
    for p in state.open_positions:
        mp = marks[p.paper_position_id]
        open_market_value += p.quantity * mp
        unrealized_pnl += p.quantity * (mp - p.average_entry_price)
    total_equity = state.available_paper_cash + open_market_value
    return open_market_value, unrealized_pnl, total_equity


def mark_account(
    state: PaperAccountState,
    marks: Dict[str, float],
    *,
    evaluated_at_utc: str,
) -> PaperAccountResult:
    """Recompute unrealised PnL + equity from caller-supplied marks (one per
    open position). Does not change cash. Returns a snapshot + metrics."""
    if not isinstance(state, PaperAccountState):
        return _reject("invalid_account_state")
    if not isinstance(marks, dict):
        return _reject("invalid_marks")
    if not _valid_utc(evaluated_at_utc):
        return _reject("invalid_timestamp")
    ids = _open_ids(state)
    if set(marks) != ids:
        return _reject("marks_mismatch_open_positions")
    for mp in marks.values():
        if not _finite_positive(mp):
            return _reject("invalid_mark_price")

    open_market_value, unrealized_pnl, total_equity = _equity_components(
        state, marks)
    if total_equity < -_TOL:
        return _reject("negative_equity")

    snapshot = PaperPnLSnapshot(
        timestamp_utc=evaluated_at_utc,
        total_paper_equity=max(0.0, total_equity),
        available_paper_cash=state.available_paper_cash,
        locked_paper_margin=state.locked_paper_margin,
        daily_realized_pnl=0.0,
        unrealized_pnl=unrealized_pnl,
        drawdown_pct=0.0,
    )
    event = build_account_event(
        event_type="POSITION_UPDATED", event_time_utc=evaluated_at_utc,
        detail={"open_market_value": open_market_value,
                "unrealized_pnl": unrealized_pnl,
                "total_paper_equity": snapshot.total_paper_equity})
    return PaperAccountResult(
        ok=True, account_state=state, snapshot=snapshot,
        events=[event.event] if event.ok else [],
        derived_metrics={"open_market_value": open_market_value,
                         "unrealized_pnl": unrealized_pnl,
                         "total_paper_equity": snapshot.total_paper_equity})


def close_position_in_account(
    state: PaperAccountState,
    close_result: PaperCloseResult,
    *,
    event_time_utc: str,
) -> PaperAccountResult:
    """Close a position in the account. Realised PnL is sourced from the
    PaperCloseResult ONLY (cross-checked closed_position.realized_pnl vs
    derived_metrics['net_realized_pnl']). Cash += exit_notional - exit_commission.
    Duplicate closes are rejected. Returns a new state + close-time snapshot."""
    if not isinstance(state, PaperAccountState):
        return _reject("invalid_account_state")
    if not isinstance(close_result, PaperCloseResult) or not close_result.ok \
            or close_result.closed_position is None:
        return _reject("invalid_close_result")
    if not _valid_utc(event_time_utc):
        return _reject("invalid_timestamp")

    closed = close_result.closed_position
    pid = closed.paper_position_id

    if pid in state.processed_close_ids:
        return _reject("already_closed_in_account")
    if pid not in _open_ids(state):
        return _reject("position_not_in_account")

    dm = close_result.derived_metrics
    try:
        net_realized = float(dm["net_realized_pnl"])
        exit_notional = float(dm["exit_notional"])
        exit_commission = float(dm["exit_commission"])
        entry_commission = float(dm["entry_commission"])
    except (KeyError, TypeError, ValueError):
        return _reject("invalid_close_metrics")

    # single source of truth: closed_position.realized_pnl must match metrics
    if not math.isclose(closed.realized_pnl, net_realized,
                        rel_tol=1e-9, abs_tol=1e-6):
        return _reject("realized_pnl_inconsistent")

    cash_in = exit_notional - exit_commission
    new_cash = state.available_paper_cash + cash_in
    if new_cash < -_TOL:
        return _reject("would_overdraw_cash")

    remaining = tuple(p for p in state.open_positions
                      if p.paper_position_id != pid)

    new_state = PaperAccountState(
        starting_equity=state.starting_equity,
        available_paper_cash=max(0.0, new_cash),
        as_of_utc=event_time_utc,
        locked_paper_margin=state.locked_paper_margin,
        realized_pnl_cumulative=state.realized_pnl_cumulative + net_realized,
        total_commissions_paid=state.total_commissions_paid + exit_commission,
        open_positions=remaining,
        processed_close_ids=state.processed_close_ids + (pid,),
    )

    # close-time snapshot: only remaining open positions contribute unrealised
    # PnL; with none, equity == cash. (Marks for remaining positions are not
    # provided here; close-time equity uses cash + entry-valued remaining.)
    open_market_value = sum(p.quantity * p.average_entry_price
                            for p in remaining)
    total_equity = new_state.available_paper_cash + open_market_value
    if total_equity < -_TOL:
        return _reject("negative_equity")

    snapshot = PaperPnLSnapshot(
        timestamp_utc=event_time_utc,
        total_paper_equity=max(0.0, total_equity),
        available_paper_cash=new_state.available_paper_cash,
        locked_paper_margin=new_state.locked_paper_margin,
        daily_realized_pnl=net_realized,
        unrealized_pnl=0.0,
        drawdown_pct=0.0,
    )
    event = build_account_event(
        event_type="POSITION_CLOSED", event_time_utc=event_time_utc,
        paper_position_id=pid, symbol=closed.symbol,
        detail={"cash_delta": cash_in, "exit_notional": exit_notional,
                "exit_commission": exit_commission,
                "net_realized_pnl": net_realized,
                "realized_pnl_cumulative": new_state.realized_pnl_cumulative,
                "available_paper_cash": new_state.available_paper_cash})
    return PaperAccountResult(
        ok=True, account_state=new_state, snapshot=snapshot,
        events=[event.event] if event.ok else [],
        derived_metrics={"cash_delta": cash_in,
                         "net_realized_pnl": net_realized,
                         "realized_pnl_cumulative":
                         new_state.realized_pnl_cumulative,
                         "entry_commission_recorded": entry_commission})

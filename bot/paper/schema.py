"""M20.A paper-trading contracts.

Frozen, standard-library dataclasses only (no Pydantic). Every paper schema
exposes IS_LIVE = False. M20 owns its own enums (PaperSide etc.) and does NOT
reuse M19 enums inside paper schemas; mapping from M19 SignalSide happens at
ingestion (later phase). Paper IDs use deterministic PPR-/PFL-/PPS-/PEV-
prefixes from provenance.

This module also defines the M19 ingestion contract guard: because M19
guarantees execution_eligible is ALWAYS False, an inbound candidate with
execution_eligible == True is a contract violation (corrupt/tampered input) and
must raise PaperContractViolation. This is NOT a routing gate.

No live/broker terminology. No shared base class with broker/live order objects.
No I/O. No network. No persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from bot.paper import provenance

SCHEMA_VERSION = "m20_paper_v1"


# ──────────────────────────── exceptions ────────────────────────────
class PaperContractViolation(Exception):
    """Raised when an inbound M19 artifact violates the frozen M19 contract
    (e.g. execution_eligible == True, which M19 guarantees is always False)."""


# ──────────────────────────── enums ────────────────────────────
class PaperSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PaperOrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class PaperPositionStatus(str, Enum):
    """Position status is distinct from order lifecycle status: a position is
    only ever OPEN or CLOSED (it is never FILLED/PARTIAL_FILL/etc.)."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PaperEventType(str, Enum):
    ROUTING_DECIDED = "ROUTING_DECIDED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_REJECTED = "ORDER_REJECTED"
    FILL_SIMULATED = "FILL_SIMULATED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"
    PNL_COMPUTED = "PNL_COMPUTED"


# re-export lifecycle status so callers have one import point
from bot.paper.lifecycle import PaperOrderStatus  # noqa: E402,F401


# ──────────────────────────── helpers ────────────────────────────
def _require_utc(ts: Any, field_name: str) -> str:
    """Validate a caller-supplied UTC ISO timestamp string. Reject naive /
    non-UTC / non-string. M20 never generates timestamps itself."""
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


def _coerce_enum(value, enum_cls, field_name):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise ValueError(f"{field_name}: unknown {enum_cls.__name__} "
                             f"{value!r}")
    raise ValueError(f"{field_name}: unknown {enum_cls.__name__} {value!r}")


def _require_positive(value, field_name: str) -> None:
    """Reject non-numeric, bool, NaN/inf, and <= 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive number, got "
                         f"{value!r}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0, got {value!r}")


def _require_non_negative(value, field_name: str) -> None:
    """Reject non-numeric, bool, NaN/inf, and < 0 (zero allowed)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative number, got "
                         f"{value!r}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0, got {value!r}")


# ──────────────────────────── ingestion guard ────────────────────────────
def assert_m19_candidate_contract(candidate: Any) -> None:
    """Raise PaperContractViolation if the inbound M19 candidate violates the
    frozen contract. M19 guarantees execution_eligible is always False; True is
    corrupt/tampered input and must be refused loudly. This is a precondition,
    not a routing gate."""
    elig = getattr(candidate, "execution_eligible", None)
    if elig is True:
        raise PaperContractViolation(
            "M19 contract violation: execution_eligible is True, but M19 "
            "guarantees it is always False (refusing corrupt/tampered input)")


# ──────────────────────────── schemas ────────────────────────────
@dataclass(frozen=True)
class PaperRoutingDecision:
    m19_candidate_id: str
    symbol: str
    side: PaperSide
    decision_bucket: str
    confidence_bucket: str
    paper_routing_eligible: bool
    evaluated_at_utc: str
    m19_input_digest: Optional[str] = None
    calibration_applied: bool = False
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        object.__setattr__(self, "side",
                           _coerce_enum(self.side, PaperSide, "side"))
        if not isinstance(self.m19_candidate_id, str) or \
                not self.m19_candidate_id:
            raise ValueError("m19_candidate_id must be a non-empty string")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        _require_utc(self.evaluated_at_utc, "evaluated_at_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperRoutingDecision":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass(frozen=True)
class PaperOrder:
    paper_order_id: str
    m19_candidate_id: str
    symbol: str
    side: PaperSide
    order_type: PaperOrderType
    quantity: float
    reference_price: float
    paper_routing_eligible: bool
    status: PaperOrderStatus
    created_at_utc: str
    m19_input_digest: Optional[str] = None
    limit_price: Optional[float] = None
    simulated_stop_loss: Optional[float] = None
    simulated_take_profit: Optional[float] = None
    reason_codes: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        object.__setattr__(self, "side",
                           _coerce_enum(self.side, PaperSide, "side"))
        object.__setattr__(self, "order_type",
                           _coerce_enum(self.order_type, PaperOrderType,
                                        "order_type"))
        object.__setattr__(self, "status",
                           _coerce_enum(self.status, PaperOrderStatus,
                                        "status"))
        if not isinstance(self.paper_order_id, str) or \
                not self.paper_order_id.startswith(provenance.PPR_PREFIX):
            raise ValueError("paper_order_id must start with 'PPR-'")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        _require_positive(self.quantity, "quantity")
        _require_positive(self.reference_price, "reference_price")
        if self.limit_price is not None:
            _require_positive(self.limit_price, "limit_price")
        if self.simulated_stop_loss is not None:
            _require_positive(self.simulated_stop_loss, "simulated_stop_loss")
        if self.simulated_take_profit is not None:
            _require_positive(self.simulated_take_profit,
                              "simulated_take_profit")
        _require_utc(self.created_at_utc, "created_at_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        d["status"] = self.status.value
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperOrder":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass(frozen=True)
class PaperFill:
    """Defined in M20.A; exercised in a later phase (M20.D)."""
    paper_fill_id: str
    paper_order_id: str
    fill_price: float
    fill_quantity: float
    fill_time_utc: str
    assumed_slippage: float = 0.0
    assumed_commission: float = 0.0
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        if not isinstance(self.paper_fill_id, str) or \
                not self.paper_fill_id.startswith(provenance.PFL_PREFIX):
            raise ValueError("paper_fill_id must start with 'PFL-'")
        if not isinstance(self.paper_order_id, str) or \
                not self.paper_order_id.startswith(provenance.PPR_PREFIX):
            raise ValueError("paper_order_id must start with 'PPR-'")
        _require_positive(self.fill_price, "fill_price")
        _require_positive(self.fill_quantity, "fill_quantity")
        _require_non_negative(self.assumed_slippage, "assumed_slippage")
        _require_non_negative(self.assumed_commission, "assumed_commission")
        _require_utc(self.fill_time_utc, "fill_time_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperFill":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass(frozen=True)
class PaperPosition:
    """Defined in M20.A; exercised in a later phase (M20.D)."""
    paper_position_id: str
    symbol: str
    side: PaperSide
    quantity: float
    average_entry_price: float
    status: PaperPositionStatus
    opened_at_utc: str
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    closed_at_utc: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        object.__setattr__(self, "side",
                           _coerce_enum(self.side, PaperSide, "side"))
        object.__setattr__(self, "status",
                           _coerce_enum(self.status, PaperPositionStatus,
                                        "status"))
        if not isinstance(self.paper_position_id, str) or \
                not self.paper_position_id.startswith(provenance.PPS_PREFIX):
            raise ValueError("paper_position_id must start with 'PPS-'")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        _require_non_negative(self.quantity, "quantity")
        if self.quantity > 0:
            _require_positive(self.average_entry_price, "average_entry_price")
        _require_utc(self.opened_at_utc, "opened_at_utc")
        if self.closed_at_utc is not None:
            _require_utc(self.closed_at_utc, "closed_at_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["status"] = self.status.value
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperPosition":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass(frozen=True)
class PaperPnLSnapshot:
    """Defined in M20.A; exercised in a later phase (M20.D)."""
    timestamp_utc: str
    total_paper_equity: float
    available_paper_cash: float
    locked_paper_margin: float = 0.0
    daily_realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    drawdown_pct: float = 0.0
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        _require_non_negative(self.total_paper_equity, "total_paper_equity")
        _require_non_negative(self.available_paper_cash, "available_paper_cash")
        _require_non_negative(self.locked_paper_margin, "locked_paper_margin")
        _require_non_negative(self.drawdown_pct, "drawdown_pct")
        # daily_realized_pnl / unrealized_pnl / realized_pnl MAY be negative.
        _require_utc(self.timestamp_utc, "timestamp_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperPnLSnapshot":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass(frozen=True)
class PaperEvent:
    paper_event_id: str
    event_time_utc: str
    event_type: PaperEventType
    m19_candidate_id: str
    paper_order_id: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    reason_codes: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    IS_LIVE: bool = field(default=False, init=False)

    def __post_init__(self):
        object.__setattr__(self, "event_type",
                           _coerce_enum(self.event_type, PaperEventType,
                                        "event_type"))
        if not isinstance(self.paper_event_id, str) or \
                not self.paper_event_id.startswith(provenance.PEV_PREFIX):
            raise ValueError("paper_event_id must start with 'PEV-'")
        _require_utc(self.event_time_utc, "event_time_utc")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["IS_LIVE"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PaperEvent":
        allowed = {f for f in cls.__dataclass_fields__ if f != "IS_LIVE"}
        unknown = set(d) - allowed - {"IS_LIVE"}
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})

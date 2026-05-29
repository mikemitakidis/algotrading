"""bot/risk_authority/exposure_reading.py — M14.D exposure data model.

Deliberately separate from bot/risk_authority/reading.py (M14.C) so the
PnL and exposure subsystems remain decoupled in code and in tests.

Quality:
  EXPOSURE_FRESH    — every required field present, all positions have
                      symbol + numeric exposure_usd.
  EXPOSURE_PARTIAL  — required fields present, some opportunistic data
                      missing (equity, mark prices, etc).
  EXPOSURE_UNKNOWN  — any required field missing, OR any same-snapshot
                      position malformed (no symbol / non-numeric
                      exposure / no derivable USD notional), OR
                      data_source_success=False.

Required for EXPOSURE_FRESH:
    positions, open_positions_count, capital_deployed_usd,
    fetched_at_utc, data_source_success.

Opportunistic (don't fail freshness on missing):
    unrealised_pnl_usd, current_equity_usd, peak_equity_usd,
    plus per-position avg_price, mark_price, opened_at,
    unrealised_pnl_usd.

Honesty rules carried from M14.C:
  * None means unknown. Never substitute 0.0.
  * Bool is a subclass of int in Python; numeric fields reject bool.
  * Date filter applied BEFORE field validation, so previous-day junk
    cannot poison today.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ExposureQuality(str, Enum):
    FRESH = "exposure_fresh"
    PARTIAL = "exposure_partial"
    UNKNOWN = "exposure_unknown"


REQUIRED_FOR_FRESH_EXPOSURE = (
    "positions",
    "open_positions_count",
    "capital_deployed_usd",
    "fetched_at_utc",
    "data_source_success",
)

OPPORTUNISTIC_EXPOSURE = (
    "unrealised_pnl_usd",
    "current_equity_usd",
    "peak_equity_usd",
)


@dataclass
class Position:
    """One open position in a single broker scope.

    Required (validated by exposure_reading.validate_position()):
      symbol, side, qty, exposure_usd.
    Opportunistic: everything else.
    """
    symbol: str
    side: str               # 'long' | 'short'
    qty: float
    exposure_usd: float
    avg_price: Optional[float] = None
    mark_price: Optional[float] = None
    unrealised_pnl_usd: Optional[float] = None
    opened_at: Optional[str] = None
    instrument_id: Optional[int] = None
    raw_evidence: dict = field(default_factory=dict)


def _is_real_number(v) -> bool:
    """Numeric, not bool, not NaN, not inf. (Bool is a subclass of int.)"""
    if v is None or isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    # Reject NaN/inf — they would silently propagate through sums.
    return v == v and v not in (float("inf"), float("-inf"))


def validate_position(raw: dict) -> Optional[str]:
    """Return None if the dict can produce a usable Position; otherwise
    a short error string explaining why the *whole reading* must be
    UNKNOWN. Called by adapters per-position before any aggregation."""
    sym = raw.get("symbol")
    if not isinstance(sym, str) or not sym:
        return f"position_missing_symbol:type={type(sym).__name__}"
    side = raw.get("side")
    if side not in ("long", "short"):
        return f"position_invalid_side:{side!r}"
    qty = raw.get("qty")
    if not _is_real_number(qty):
        return f"position_qty_non_numeric:type={type(qty).__name__}"
    exp = raw.get("exposure_usd")
    if not _is_real_number(exp):
        return f"position_exposure_non_numeric:type={type(exp).__name__}"
    return None


@dataclass
class BrokerExposureReading:
    """One adapter's exposure read for one (broker_scope, trading_day).

    Adapters MUST return this dataclass populated honestly. They MUST
    NOT substitute 0.0/0/[] for missing data, and MUST NOT raise to
    the orchestrator on transport/auth/parser failure — set
    data_source_success=False and `error` to a short reason instead.
    """
    broker_scope: str
    trading_day: str
    fetched_at_utc: str
    data_source_success: bool
    positions: Optional[List[Position]] = None
    open_positions_count: Optional[int] = None
    capital_deployed_usd: Optional[float] = None
    unrealised_pnl_usd: Optional[float] = None
    current_equity_usd: Optional[float] = None
    peak_equity_usd: Optional[float] = None
    source: str = "ingested"
    error: Optional[str] = None
    raw_evidence: dict = field(default_factory=dict)

    # ── classification ────────────────────────────────────────────────

    def missing_required(self) -> List[str]:
        miss = []
        if self.positions is None:
            miss.append("positions")
        if self.open_positions_count is None:
            miss.append("open_positions_count")
        if self.capital_deployed_usd is None:
            miss.append("capital_deployed_usd")
        if not self.fetched_at_utc:
            miss.append("fetched_at_utc")
        if not self.data_source_success:
            miss.append("data_source_success")
        return miss

    def missing_opportunistic(self) -> List[str]:
        miss = []
        if self.unrealised_pnl_usd is None:
            miss.append("unrealised_pnl_usd")
        if self.current_equity_usd is None:
            miss.append("current_equity_usd")
        if self.peak_equity_usd is None:
            miss.append("peak_equity_usd")
        return miss

    def known_fields(self) -> List[str]:
        present = []
        for name in REQUIRED_FOR_FRESH_EXPOSURE + OPPORTUNISTIC_EXPOSURE:
            v = getattr(self, name, None)
            if name == "data_source_success":
                if v is True:
                    present.append(name)
            elif name == "fetched_at_utc":
                if isinstance(v, str) and v:
                    present.append(name)
            elif v is not None:
                present.append(name)
        return present

    def quality(self) -> ExposureQuality:
        if self.missing_required():
            return ExposureQuality.UNKNOWN
        if self.missing_opportunistic():
            return ExposureQuality.PARTIAL
        return ExposureQuality.FRESH

    # ── helpful predicates ────────────────────────────────────────────

    def is_known_zero_exposure(self) -> bool:
        """Successful read with zero open positions and zero capital
        deployed — clearly distinguished from UNKNOWN where DB defaults
        happen to be 0."""
        return (
            self.data_source_success
            and self.positions == []
            and self.open_positions_count == 0
            and self.capital_deployed_usd == 0.0
            and self.error is None
        )

    def is_exposure_unknown(self) -> bool:
        return self.quality() == ExposureQuality.UNKNOWN

    def has_fresh_exposure(self) -> bool:
        """True iff required-fields are present (FRESH or PARTIAL).
        Engine in M14.E uses this to gate concentration / combined-
        exposure decisions."""
        return not self.missing_required()


def make_unknown_exposure(scope: str, *, trading_day: str,
                          error: str) -> BrokerExposureReading:
    """Helper for adapters: build a fail-closed UNKNOWN reading without
    substituting any zero or empty list for the required fields."""
    from datetime import datetime, timezone
    return BrokerExposureReading(
        broker_scope=scope,
        trading_day=trading_day,
        fetched_at_utc=datetime.now(timezone.utc).isoformat(),
        data_source_success=False,
        positions=None,
        open_positions_count=None,
        capital_deployed_usd=None,
        error=error,
    )


__all__ = [
    "ExposureQuality",
    "Position",
    "BrokerExposureReading",
    "REQUIRED_FOR_FRESH_EXPOSURE",
    "OPPORTUNISTIC_EXPOSURE",
    "make_unknown_exposure",
    "validate_position",
    "_is_real_number",
]

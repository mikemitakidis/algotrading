"""
bot/risk_authority/reading.py — M14.C data types.

A `BrokerPnLReading` is the pure-data result of one read attempt against
one broker. It carries enough metadata for the orchestrator and the
M14.E engine to distinguish *known-zero* (successful read, no trades)
from *unknown* (failed/stale read), per ChatGPT M14.C correction #1.

Required fields for a "fresh PnL reading" (M14.C correction #2):
  - realised_pnl_usd
  - realised_daily_loss
  - fetched_at_utc
  - success

Opportunistic fields (NOT required for a fresh reading):
  - open_positions
  - capital_deployed
  - peak_equity
  - drawdown_from_peak
  - realised_pnl_pct

`ReadingQuality`:
  - FRESH    — success AND all required fields populated.
  - PARTIAL  — success AND all required PnL fields populated; some
               opportunistic fields missing.
  - UNKNOWN  — success=False, OR any required field is None.

A numeric `0` is only known-safe if `quality in {FRESH, PARTIAL}` AND
`realised_pnl_usd is not None and == 0.0`. Downstream M14.E must treat
any missing required field as unknown even if the DB numeric column
defaults to 0.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional


class ReadingQuality(str, enum.Enum):
    FRESH   = "fresh"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


# Required fields for a fresh / partial reading (per M14.C correction #2).
REQUIRED_FOR_FRESH = ("realised_pnl_usd", "realised_daily_loss",
                      "fetched_at_utc", "success")

# Opportunistic fields — captured if available but never gating freshness.
OPPORTUNISTIC = ("open_positions", "capital_deployed", "peak_equity",
                 "drawdown_from_peak", "realised_pnl_pct")


# Valid sources for the daily_state_per_broker.source column (matches M14.B CHECK).
VALID_SOURCES = {"backfill", "ingested", "reconciled", "manual_fallback", "rollup"}


@dataclass
class BrokerPnLReading:
    """One attempt's worth of broker PnL data. Pure value, no I/O."""
    # Identity
    broker_scope:           str
    trading_day:            str          # YYYY-MM-DD (UTC)
    fetched_at_utc:         str          # ISO-8601
    success:                bool
    # Required (for fresh)
    realised_pnl_usd:       Optional[float] = None
    realised_daily_loss:    Optional[float] = None
    # Opportunistic
    realised_pnl_pct:       Optional[float] = None
    open_positions:         Optional[int]   = None
    capital_deployed:       Optional[float] = None
    peak_equity:            Optional[float] = None
    drawdown_from_peak:     Optional[float] = None
    # Metadata
    source:                 str = "ingested"
    known_fields:           List[str] = field(default_factory=list)
    missing_fields:         List[str] = field(default_factory=list)
    quality:                ReadingQuality = ReadingQuality.UNKNOWN
    error:                  Optional[str]  = None
    # Compact redacted summary suitable for DB lifecycle_json.latest_reading.
    # The richer evidence goes to the rotating risk_ingest.log only.
    evidence_summary:       dict = field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def make_unknown(broker_scope: str,
                 *,
                 trading_day: Optional[str] = None,
                 error: str,
                 evidence_summary: Optional[dict] = None) -> BrokerPnLReading:
    """Construct an UNKNOWN reading. Used by adapters on any failure path —
    they MUST NOT raise to the orchestrator (M14.C correction #3)."""
    r = BrokerPnLReading(
        broker_scope=broker_scope,
        trading_day=trading_day or _today_utc(),
        fetched_at_utc=_utc_now_iso(),
        success=False,
        error=error,
        evidence_summary=dict(evidence_summary or {}),
    )
    finalize_quality(r)
    return r


def finalize_quality(r: BrokerPnLReading) -> ReadingQuality:
    """Populate `known_fields`, `missing_fields`, and `quality`.

    Idempotent. Called by adapters at the end of read().
    """
    all_fields = REQUIRED_FOR_FRESH + OPPORTUNISTIC
    known: List[str] = []
    missing: List[str] = []
    for f in all_fields:
        v = getattr(r, f, None) if f != "success" else r.success
        if f == "success":
            if v is True:
                known.append(f)
            else:
                missing.append(f)
            continue
        if f == "fetched_at_utc":
            if isinstance(v, str) and v:
                known.append(f)
            else:
                missing.append(f)
            continue
        if v is None:
            missing.append(f)
        else:
            known.append(f)

    required_missing = [f for f in REQUIRED_FOR_FRESH if f in missing]
    opportunistic_missing = [f for f in OPPORTUNISTIC if f in missing]

    if required_missing or not r.success:
        q = ReadingQuality.UNKNOWN
    elif opportunistic_missing:
        q = ReadingQuality.PARTIAL
    else:
        q = ReadingQuality.FRESH

    r.known_fields = known
    r.missing_fields = missing
    r.quality = q
    return q


def is_known_zero(r: BrokerPnLReading) -> bool:
    """True iff this is a *successful* zero PnL (no trades today, broker
    reported cleanly). Critical distinction from `is_unknown(r)` — a row
    with DB numeric 0 may be either.
    """
    if r.quality == ReadingQuality.UNKNOWN:
        return False
    if not r.success:
        return False
    if r.realised_pnl_usd is None or r.realised_daily_loss is None:
        return False
    return r.realised_pnl_usd == 0.0 and r.realised_daily_loss == 0.0


def is_unknown(r: BrokerPnLReading) -> bool:
    return r.quality == ReadingQuality.UNKNOWN


def has_fresh_pnl(r: BrokerPnLReading) -> bool:
    """True iff the required PnL fields are present and the read succeeded.
    Equivalent to quality in {FRESH, PARTIAL}."""
    return r.quality in (ReadingQuality.FRESH, ReadingQuality.PARTIAL)


__all__ = [
    "ReadingQuality",
    "REQUIRED_FOR_FRESH",
    "OPPORTUNISTIC",
    "VALID_SOURCES",
    "BrokerPnLReading",
    "make_unknown",
    "finalize_quality",
    "is_known_zero",
    "is_unknown",
    "has_fresh_pnl",
]

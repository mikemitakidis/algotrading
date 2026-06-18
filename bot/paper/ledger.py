"""M20.G ledger events.

Builds deterministic ledger/account events by REUSING the frozen M20.A
PaperEvent (PEV- id, event_type, event_time_utc, paper_order_id, detail,
reason_codes). No new PaperLedgerEvent schema is introduced. The cash/PnL/equity
deltas are carried in PaperEvent.detail. In-memory only (persistence is M20.H).
No wall-clock, no RNG, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from bot.paper.schema import PaperEvent, PaperEventType
from bot.paper import provenance


@dataclass(frozen=True)
class PaperLedgerResult:
    ok: bool
    event: Optional[PaperEvent] = None
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


def build_account_event(
    *,
    event_type,
    event_time_utc: str,
    detail: Optional[Dict[str, Any]] = None,
    paper_position_id: Optional[str] = None,
    symbol: Optional[str] = None,
    paper_order_id: Optional[str] = None,
    reason_codes: Optional[List[str]] = None,
) -> PaperLedgerResult:
    """Build a deterministic PaperEvent for an account/ledger transition. Safe-
    rejects on invalid input rather than raising."""
    if not _valid_utc(event_time_utc):
        return PaperLedgerResult(ok=False, rejection_reason="invalid_timestamp",
                                 reason_codes=["invalid_timestamp"])
    try:
        et = (event_type if isinstance(event_type, PaperEventType)
              else PaperEventType(event_type))
    except ValueError:
        return PaperLedgerResult(ok=False, rejection_reason="invalid_event_type",
                                 reason_codes=["invalid_event_type"])

    full_detail: Dict[str, Any] = dict(detail or {})
    if paper_position_id is not None:
        full_detail.setdefault("paper_position_id", paper_position_id)
    if symbol is not None:
        full_detail.setdefault("symbol", symbol)

    event_id = provenance.paper_event_id({
        "event_type": et.value,
        "event_time_utc": event_time_utc,
        "paper_position_id": paper_position_id or "",
        "paper_order_id": paper_order_id or "",
        "detail": full_detail,
    })

    try:
        event = PaperEvent(
            paper_event_id=event_id,
            event_time_utc=event_time_utc,
            event_type=et,
            m19_candidate_id=full_detail.get("m19_candidate_id", "account"),
            paper_order_id=paper_order_id,
            detail=full_detail,
            reason_codes=sorted(reason_codes or []),
        )
    except (ValueError, TypeError) as e:
        return PaperLedgerResult(ok=False, rejection_reason="invalid_event",
                                 reason_codes=["invalid_event"],
                                 warnings=[str(e)])
    return PaperLedgerResult(ok=True, event=event)

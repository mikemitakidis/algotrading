#!/usr/bin/env python3
"""M21.1extra-D1 — market-clock decision type and stable reason codes.

The market-clock guard answers ONE question, fail-closed: "for this
market_calendar_id, at this instant, may an order be submitted?" The default
answer is always "no / do not submit". `may_submit` is True only on a fresh,
supported, regular-session, affirmative-open decision from a trusted source.

This module defines the structured result (`MarketClockDecision`) and the
stable reason-code strings. It has NO side effects: no network, no DB, no
imports of order/scheduler/dashboard/persistence code.

The decision carries C-compatible fields (`market_calendar_id`,
`market_clock_checked`, `market_clock_reason`, `as_of_utc`) so a later D2/C
wiring step can PERSIST them. D1 itself never writes them anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


# ── Stable reason codes (do not rename without a milestone + report update) ──
REASON_MARKET_OPEN                 = "market_open"
REASON_MARKET_CLOSED               = "market_closed"
REASON_CALENDAR_UNSUPPORTED        = "market_calendar_unsupported"
REASON_NO_CREDENTIALS              = "market_status_no_credentials"
REASON_SOURCE_UNAVAILABLE          = "market_status_source_unavailable"
REASON_SOURCE_STALE                = "market_status_source_stale"
REASON_SOURCE_MALFORMED            = "market_status_source_malformed"
REASON_SOURCE_INCONSISTENT         = "market_status_source_inconsistent"
REASON_EXTENDED_HOURS_REFUSED      = "market_extended_hours_refused"
REASON_INTERNAL_ERROR              = "market_clock_internal_error"

VALID_REASONS = frozenset({
    REASON_MARKET_OPEN,
    REASON_MARKET_CLOSED,
    REASON_CALENDAR_UNSUPPORTED,
    REASON_NO_CREDENTIALS,
    REASON_SOURCE_UNAVAILABLE,
    REASON_SOURCE_STALE,
    REASON_SOURCE_MALFORMED,
    REASON_SOURCE_INCONSISTENT,
    REASON_EXTENDED_HOURS_REFUSED,
    REASON_INTERNAL_ERROR,
})

# The ONLY reason that is allowed to accompany may_submit=True.
_ALLOW_REASON = REASON_MARKET_OPEN

# Supported market calendars for THIS D1 slice. Anything else -> unsupported.
SUPPORTED_CALENDAR_IDS = frozenset({"US_EQ"})


@dataclass(frozen=True)
class MarketClockDecision:
    """Immutable, structured market-clock verdict.

    may_submit is True ONLY when reason == market_open. Any other reason is a
    do-not-submit. `__post_init__` enforces that invariant so a decision can
    never claim may_submit=True with a non-open reason (fail-closed by type).
    """
    may_submit: bool
    reason: str
    market_calendar_id: str
    symbol: Optional[str] = None
    source: str = "none"
    source_status: str = "unknown"
    as_of_utc: Optional[str] = None
    fetched_at_utc: Optional[str] = None
    next_open_utc: Optional[str] = None
    next_close_utc: Optional[str] = None
    regular_session_only: bool = True
    stale: bool = False
    unsupported: bool = False
    inconsistent: bool = False
    raw_source_summary: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Explicit runtime checks (NOT assert — assert is stripped under -O and
        # must never guard a safety invariant).
        if self.reason not in VALID_REASONS:
            raise ValueError("unknown market-clock reason: %r" % (self.reason,))
        if self.may_submit is not True and self.may_submit is not False:
            raise ValueError("may_submit must be a bool, got %r"
                             % (self.may_submit,))
        # The core fail-closed invariant: may_submit=True <=> reason is open.
        if self.may_submit is True and self.reason != _ALLOW_REASON:
            raise ValueError(
                "may_submit=True is only valid with reason=%s (got %r)"
                % (_ALLOW_REASON, self.reason))
        if self.may_submit is False and self.reason == _ALLOW_REASON:
            raise ValueError(
                "reason=%s must not accompany may_submit=False" % _ALLOW_REASON)

    # C-compatible view — RETURNED for a later persister, never written here.
    @property
    def market_clock_checked(self) -> bool:
        """True once the guard has produced a verdict (always True for a real
        decision object). D2/C may persist this as INTEGER; D1 never writes it."""
        return True

    @property
    def market_clock_reason(self) -> str:
        return self.reason

    def c_fields(self) -> Dict[str, Any]:
        """The exact C-compatible fields a later D2/C step may persist. D1
        returns these; it does NOT write them to any DB."""
        return {
            "market_calendar_id": self.market_calendar_id,
            "market_clock_checked": self.market_clock_checked,
            "market_clock_reason": self.market_clock_reason,
            "as_of_utc": self.as_of_utc,
        }


def do_not_submit(
    *,
    reason: str,
    market_calendar_id: str,
    symbol: Optional[str] = None,
    source: str = "none",
    source_status: str = "unknown",
    as_of_utc: Optional[str] = None,
    fetched_at_utc: Optional[str] = None,
    next_open_utc: Optional[str] = None,
    next_close_utc: Optional[str] = None,
    stale: bool = False,
    unsupported: bool = False,
    inconsistent: bool = False,
    raw_source_summary: Optional[Dict[str, Any]] = None,
) -> MarketClockDecision:
    """Construct a fail-closed (may_submit=False) decision. `reason` must be a
    non-open VALID_REASON."""
    if reason == _ALLOW_REASON:
        # Guard against a caller accidentally routing an "open" through the
        # do-not-submit builder.
        raise ValueError("do_not_submit cannot use the open reason")
    return MarketClockDecision(
        may_submit=False, reason=reason,
        market_calendar_id=market_calendar_id, symbol=symbol,
        source=source, source_status=source_status, as_of_utc=as_of_utc,
        fetched_at_utc=fetched_at_utc, next_open_utc=next_open_utc,
        next_close_utc=next_close_utc, stale=stale, unsupported=unsupported,
        inconsistent=inconsistent,
        raw_source_summary=raw_source_summary or {})


def market_open(
    *,
    market_calendar_id: str,
    symbol: Optional[str],
    source: str,
    source_status: str,
    as_of_utc: Optional[str],
    fetched_at_utc: str,
    next_open_utc: Optional[str],
    next_close_utc: Optional[str],
    raw_source_summary: Optional[Dict[str, Any]] = None,
) -> MarketClockDecision:
    """Construct the ONLY may_submit=True decision. Reserved for the guard's
    single affirmative-open path."""
    return MarketClockDecision(
        may_submit=True, reason=REASON_MARKET_OPEN,
        market_calendar_id=market_calendar_id, symbol=symbol,
        source=source, source_status=source_status, as_of_utc=as_of_utc,
        fetched_at_utc=fetched_at_utc, next_open_utc=next_open_utc,
        next_close_utc=next_close_utc, stale=False, unsupported=False,
        inconsistent=False, raw_source_summary=raw_source_summary or {})

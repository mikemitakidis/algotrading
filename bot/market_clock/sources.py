#!/usr/bin/env python3
"""M21.1extra-D1 — market-status source protocol and mock sources.

A market-status source answers "is US_EQ open right now, and when does the
regular session open/close?" and reports its own freshness/health. Sources are
INJECTED into the guard; the guard applies the fail-closed policy. Sources
never make an authorization decision themselves.

This module (Commit 1) provides:
  - SourceResult: the normalized shape every source returns
  - MarketStatusSource: the protocol the guard consumes
  - MockMarketStatusSource: a fully controllable source for tests
  - IbkrMarketStatusSource: a MOCK/stub only — D1 never opens a gateway
    connection. It exists so an IBKR cross-check can be added later (D1b/D2)
    behind an already-existing connection context.

No network, no DB, no secrets. The real Alpaca source arrives in Commit 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, Protocol, runtime_checkable


# Source health/status strings (diagnostic; distinct from guard reason codes).
STATUS_OK              = "ok"
STATUS_NO_CREDENTIALS  = "no_credentials"
STATUS_UNAVAILABLE     = "unavailable"
STATUS_MALFORMED       = "malformed"


@dataclass(frozen=True)
class SourceResult:
    """Normalized result from a market-status source.

    A source reports what it observed; it does NOT decide may_submit. The guard
    interprets these fields fail-closed.

    Fields:
      status            : one of the STATUS_* strings.
      is_open           : source's regular-session open/closed flag, or None if
                          it could not be determined (malformed/unavailable).
      session_open_utc  : regular-session open for the current day (from the
                          calendar), or None. Used to enforce regular-session.
      session_close_utc : regular-session close for the current day (encodes
                          early closes), or None.
      next_open_utc / next_close_utc : informational upcoming boundaries.
      as_of_utc         : the source's own timestamp for the reading, if any.
      fetched_at_utc    : when WE obtained the reading (always set by a real
                          source; freshness is measured on this).
      calendar_present  : whether the source could supply the session
                          open/close for the current day. Missing calendar +
                          is_open=True must fail closed (see guard).
      source_name       : "alpaca" | "mock" | "ibkr_mock" | ...
      raw_summary       : SMALL, secret-free snapshot for logging/audit.
    """
    status: str
    is_open: Optional[bool] = None
    session_open_utc: Optional[datetime] = None
    session_close_utc: Optional[datetime] = None
    next_open_utc: Optional[datetime] = None
    next_close_utc: Optional[datetime] = None
    as_of_utc: Optional[datetime] = None
    fetched_at_utc: Optional[datetime] = None
    calendar_present: bool = False
    source_name: str = "unknown"
    raw_summary: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MarketStatusSource(Protocol):
    """Protocol for a market-status source. Implementations MUST NOT raise for
    ordinary failure modes (timeout/auth/rate-limit/malformed) — they return a
    SourceResult with the appropriate STATUS_* so the guard stays fail-closed.
    They MUST NOT place orders, write DBs, or hold long-lived broker
    connections at guard-eval time."""

    @property
    def name(self) -> str: ...

    def get_status(self, *, market_calendar_id: str,
                   now_utc: datetime) -> SourceResult: ...


class MockMarketStatusSource:
    """A fully controllable source for tests. Returns whatever SourceResult it
    is constructed with (optionally deriving fetched_at from now_utc)."""

    def __init__(self, result: SourceResult, *, name: str = "mock",
                 stamp_fetched_at: bool = True):
        self._result = result
        self._name = name
        self._stamp_fetched_at = stamp_fetched_at

    @property
    def name(self) -> str:
        return self._name

    def get_status(self, *, market_calendar_id: str,
                   now_utc: datetime) -> SourceResult:
        r = self._result
        if self._stamp_fetched_at and r.fetched_at_utc is None:
            # emulate a real source stamping its own fetch time = now
            r = SourceResult(
                status=r.status, is_open=r.is_open,
                session_open_utc=r.session_open_utc,
                session_close_utc=r.session_close_utc,
                next_open_utc=r.next_open_utc, next_close_utc=r.next_close_utc,
                as_of_utc=r.as_of_utc if r.as_of_utc is not None else now_utc,
                fetched_at_utc=now_utc, calendar_present=r.calendar_present,
                source_name=self._name, raw_summary=dict(r.raw_summary))
        return r


class IbkrMarketStatusSource:
    """MOCK/stub IBKR source. D1 NEVER opens a gateway connection; this is a
    placeholder so an IBKR cross-check can be wired in later (D1b/D2) using an
    already-existing connection context.

    Calling get_status here does NOT connect to anything. Without an injected
    reader it reports UNAVAILABLE (fail-closed). A future connection-backed
    reader can be injected as `status_reader(now_utc) -> SourceResult`.
    """

    def __init__(self, status_reader=None):
        # status_reader is an optional injectable callable for future use / tests.
        # It must NOT open a connection inside D1.
        self._status_reader = status_reader

    @property
    def name(self) -> str:
        return "ibkr_mock"

    def get_status(self, *, market_calendar_id: str,
                   now_utc: datetime) -> SourceResult:
        if self._status_reader is None:
            return SourceResult(status=STATUS_UNAVAILABLE, is_open=None,
                                fetched_at_utc=now_utc, calendar_present=False,
                                source_name=self.name,
                                raw_summary={"note": "ibkr live cross-check "
                                             "deferred; no connection in D1"})
        # Even with a reader injected, this stays a pure function call — the
        # reader itself is responsible for using an EXISTING connection context.
        return self._status_reader(now_utc)

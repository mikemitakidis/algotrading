#!/usr/bin/env python3
"""M21.1extra-D1 — the fail-closed market-clock guard.

`evaluate_market_clock(...)` returns a MarketClockDecision. The default answer
is always do-not-submit. may_submit=True is reached ONLY when, for a supported
calendar (US_EQ this slice), a source reports a FRESH, affirmative, regular-
session open — and, when a second source is present, the sources AGREE.

Design invariants (mirroring the D0 gate's discipline):
  - fail-closed on every error/uncertain path (never "open" on doubt);
  - freshness measured on our own fetch time; a stale reading (incl. a stale
    cached OPEN) is do-not-submit;
  - the injectable cache may serve an OPEN only within freshness and must never
    hide a source failure once freshness expires (no D0-style stale-allow bug);
  - no order/scheduler/dashboard imports, no DB writes, no secrets, no assert
    guarding safety, no hardcoded 09:30/16:00 as decision authority — the
    session open/close comes from the source's calendar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence, Dict, Any

from bot.market_clock.decision import (
    MarketClockDecision, do_not_submit, market_open,
    SUPPORTED_CALENDAR_IDS,
    REASON_CALENDAR_UNSUPPORTED, REASON_NO_CREDENTIALS,
    REASON_SOURCE_UNAVAILABLE, REASON_SOURCE_STALE, REASON_SOURCE_MALFORMED,
    REASON_SOURCE_INCONSISTENT, REASON_EXTENDED_HOURS_REFUSED,
    REASON_MARKET_CLOSED, REASON_INTERNAL_ERROR,
)
from bot.market_clock.sources import (
    MarketStatusSource, SourceResult,
    STATUS_OK, STATUS_NO_CREDENTIALS, STATUS_UNAVAILABLE, STATUS_MALFORMED,
)

DEFAULT_MAX_AGE_SEC = 30.0


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # never assume a naive datetime is UTC — treat as unusable
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Injectable cache ────────────────────────────────────────────────────────
class MarketClockCache:
    """Minimal injectable cache. Stores the LAST decision per calendar id with
    the fetch instant. A cached OPEN may be reused ONLY within max_age_sec;
    past that it is stale and the guard must re-evaluate (and, if the source
    then fails, fail closed — the cache must not hide a live failure).

    Default guard behaviour is NO cache (pass cache=None). This class exists so
    D2 can opt in and so the no-stale-allow property is testable now.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    def get(self, market_calendar_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(market_calendar_id)

    def put(self, market_calendar_id: str, *, decision: MarketClockDecision,
            fetched_at_utc: datetime) -> None:
        self._store[market_calendar_id] = {
            "decision": decision, "fetched_at_utc": fetched_at_utc}

    def clear(self) -> None:
        self._store.clear()


def _fresh_cached_open(cache: Optional[MarketClockCache],
                       market_calendar_id: str, now_utc: datetime,
                       max_age_sec: float) -> Optional[MarketClockDecision]:
    """Return a cached OPEN decision ONLY if it is still within freshness.
    Any staleness (or a cached non-open) returns None so the guard re-evaluates.
    This is the sole place a cached allow can be surfaced, and it is guarded by
    the same freshness bound as a live reading — never a stale allow."""
    if cache is None:
        return None
    entry = cache.get(market_calendar_id)
    if not entry:
        return None
    dec = entry.get("decision")
    fetched = entry.get("fetched_at_utc")
    if dec is None or fetched is None:
        return None
    # Only an OPEN is sensitive; a cached closed/refusal isn't an allow.
    if dec.may_submit is not True:
        return None
    age = (now_utc - fetched).total_seconds()
    if age < 0 or age >= max_age_sec:
        return None  # stale (or clock skew) -> do not serve a cached allow
    return dec


def _age_ok(fetched_at_utc: Optional[datetime], as_of_utc: Optional[datetime],
            now_utc: datetime, max_age_sec: float) -> bool:
    """Fresh iff our fetch time is present and within max_age_sec, AND if the
    source provided its own as_of, that too is within max_age_sec. Missing
    fetch time -> not fresh (fail closed)."""
    if fetched_at_utc is None:
        return False
    fa = (now_utc - fetched_at_utc).total_seconds()
    if fa < 0 or fa >= max_age_sec:
        return False
    if as_of_utc is not None:
        ao = (now_utc - as_of_utc).total_seconds()
        if ao < 0 or ao >= max_age_sec:
            return False
    return True


def _interpret_one(res: SourceResult, *, market_calendar_id: str,
                   symbol: Optional[str], now_utc: datetime,
                   max_age_sec: float) -> MarketClockDecision:
    """Turn a single SourceResult into a fail-closed decision."""
    common = dict(
        market_calendar_id=market_calendar_id, symbol=symbol,
        source=res.source_name, source_status=res.status,
        as_of_utc=_iso(res.as_of_utc), fetched_at_utc=_iso(res.fetched_at_utc),
        next_open_utc=_iso(res.next_open_utc),
        next_close_utc=_iso(res.next_close_utc),
        raw_source_summary=dict(res.raw_summary or {}))

    # health first
    if res.status == STATUS_NO_CREDENTIALS:
        return do_not_submit(reason=REASON_NO_CREDENTIALS, **common)
    if res.status == STATUS_UNAVAILABLE:
        return do_not_submit(reason=REASON_SOURCE_UNAVAILABLE, **common)
    if res.status == STATUS_MALFORMED:
        return do_not_submit(reason=REASON_SOURCE_MALFORMED, **common)
    if res.status != STATUS_OK:
        # unknown status -> fail closed
        return do_not_submit(reason=REASON_SOURCE_UNAVAILABLE, **common)

    # ok status but missing the essential flag -> malformed
    if res.is_open is None:
        return do_not_submit(reason=REASON_SOURCE_MALFORMED, **common)

    # freshness (measured on our fetch time; as_of too if provided)
    if not _age_ok(res.fetched_at_utc, res.as_of_utc, now_utc, max_age_sec):
        return do_not_submit(reason=REASON_SOURCE_STALE, stale=True, **common)

    # closed is a clean, affirmative do-not-submit
    if res.is_open is not True:
        return do_not_submit(reason=REASON_MARKET_CLOSED, **common)

    # is_open is True -> we must confirm REGULAR session via the calendar.
    # Missing calendar while clock says open MUST fail closed (tightening rule).
    if not res.calendar_present or res.session_open_utc is None \
            or res.session_close_utc is None:
        return do_not_submit(reason=REASON_SOURCE_UNAVAILABLE, **common)

    # regular-session window check — source/calendar-driven, no hardcoded hours.
    if not (res.session_open_utc <= now_utc < res.session_close_utc):
        # open flag true but we're outside the regular window (pre/after hours,
        # or at/after an early close) -> refuse extended hours / closed.
        if now_utc < res.session_open_utc:
            return do_not_submit(reason=REASON_EXTENDED_HOURS_REFUSED, **common)
        # now >= session_close -> closed (covers early close after close)
        return do_not_submit(reason=REASON_MARKET_CLOSED, **common)

    # fresh + supported + regular session + affirmative open
    return market_open(
        market_calendar_id=market_calendar_id, symbol=symbol,
        source=res.source_name, source_status=res.status,
        as_of_utc=_iso(res.as_of_utc), fetched_at_utc=_iso(res.fetched_at_utc),
        next_open_utc=_iso(res.next_open_utc),
        next_close_utc=_iso(res.next_close_utc),
        raw_source_summary=dict(res.raw_summary or {}))


def evaluate_market_clock(
    *,
    market_calendar_id: str,
    symbol: Optional[str] = None,
    now_utc: Optional[datetime] = None,
    sources: Optional[Sequence[MarketStatusSource]] = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    cache: Optional[MarketClockCache] = None,
) -> MarketClockDecision:
    """Fail-closed market-clock guard. See module docstring.

    `sources` is injected. In Commit 1 there is no default live source; callers
    (and tests) inject mock sources. Commit 2 adds the Alpaca source and a
    convenience default. With no sources provided, the guard fails closed
    (unavailable) rather than guessing.
    """
    try:
        now = now_utc or _utcnow()

        # unsupported market id -> fail closed before any source work.
        if market_calendar_id not in SUPPORTED_CALENDAR_IDS:
            return do_not_submit(
                reason=REASON_CALENDAR_UNSUPPORTED,
                market_calendar_id=market_calendar_id, symbol=symbol,
                source="none", source_status="unsupported", unsupported=True,
                raw_source_summary={"supported": sorted(SUPPORTED_CALENDAR_IDS)})

        # a fresh cached OPEN may short-circuit (within freshness only).
        cached = _fresh_cached_open(cache, market_calendar_id, now, max_age_sec)
        if cached is not None:
            return cached

        if not sources:
            # nothing to ask -> fail closed (do not guess "open").
            return do_not_submit(
                reason=REASON_SOURCE_UNAVAILABLE,
                market_calendar_id=market_calendar_id, symbol=symbol,
                source="none", source_status=STATUS_UNAVAILABLE,
                raw_source_summary={"note": "no market-status source provided"})

        # query each source, interpret each fail-closed.
        decisions = []
        for src in sources:
            try:
                res = src.get_status(market_calendar_id=market_calendar_id,
                                     now_utc=now)
            except Exception as exc:   # a source must not, but never trust it
                decisions.append(do_not_submit(
                    reason=REASON_SOURCE_UNAVAILABLE,
                    market_calendar_id=market_calendar_id, symbol=symbol,
                    source=getattr(src, "name", "unknown"),
                    source_status=STATUS_UNAVAILABLE,
                    raw_source_summary={"exc": type(exc).__name__}))
                continue
            decisions.append(_interpret_one(
                res, market_calendar_id=market_calendar_id, symbol=symbol,
                now_utc=now, max_age_sec=max_age_sec))

        primary = decisions[0]

        # single source: its (already fail-closed) decision stands.
        if len(decisions) == 1:
            _maybe_cache(cache, market_calendar_id, primary, now)
            return primary

        # multiple sources: they must AGREE on open/closed. Any disagreement,
        # or any non-open secondary, blocks. Only unanimous fresh-open passes.
        all_open = all(d.may_submit is True for d in decisions)
        any_open = any(d.may_submit is True for d in decisions)
        if all_open:
            _maybe_cache(cache, market_calendar_id, primary, now)
            return primary
        if any_open:
            # some say open, some don't -> inconsistent (fail closed).
            return do_not_submit(
                reason=REASON_SOURCE_INCONSISTENT,
                market_calendar_id=market_calendar_id, symbol=symbol,
                source=primary.source, source_status=primary.source_status,
                inconsistent=True,
                raw_source_summary={"reasons": [d.reason for d in decisions]})
        # none open -> return the primary's fail-closed reason.
        return primary

    except Exception as exc:  # absolute backstop -> fail closed
        return do_not_submit(
            reason=REASON_INTERNAL_ERROR,
            market_calendar_id=market_calendar_id, symbol=symbol,
            source="none", source_status="internal_error",
            raw_source_summary={"exc": type(exc).__name__})


def _maybe_cache(cache: Optional[MarketClockCache], market_calendar_id: str,
                 decision: MarketClockDecision, now_utc: datetime) -> None:
    """Store only an OPEN decision (the only thing the fresh-cache short-circuit
    reads). Closed/refusals aren't cached as allows."""
    if cache is None:
        return
    if decision.may_submit is True:
        cache.put(market_calendar_id, decision=decision, fetched_at_utc=now_utc)

"""M21.1 A01 — exchange sessions and bar-close semantics (pure).

No I/O and no wall clock: every function takes the times it needs as
arguments. All datetimes must be tz-aware; everything returned is UTC.

A ``SessionCalendar`` classifies every date inside an explicit coverage range.
Dates outside that range are UNKNOWN: callers must treat them as unverifiable
(never as sessions, never as holidays). Nothing here ships a holiday list; a
verified calendar must be injected by the caller.

Bar timestamps follow the provider contract (``bot/providers/base.py``):
the index is the bar START in UTC. Bar closes are derived here:

* ``daily``    — the bar belongs to one session date; it closes at that
                 session's close (early closes honoured).
* ``intraday`` — a bar of N minutes closes at ``min(start + N, session close)``
                 for the regular session it overlaps.
* ``bucket``   — the scanner's 4H bars are ``DataFrame.resample('4h')`` UTC
                 buckets of 1H bars (``bot/data.resample_to_4h``). A bucket
                 closes when its last constituent base bar closes. If the base
                 bar grid is known (``base_anchor``) that close is exact;
                 otherwise a conservative upper bound is used for completeness
                 and a conservative lower bound for staleness.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import FrozenSet, Optional, Tuple

try:  # zoneinfo needs the system tz database; absence must fail closed
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - python < 3.9
    ZoneInfo = None  # type: ignore

UTC = timezone.utc

DAILY = "daily"
INTRADAY = "intraday"
BUCKET = "bucket"
ANCHOR_SESSION_OPEN = "session_open"
ANCHOR_CLOCK = "clock"

# How far the calendar search may walk to find a neighbouring session.
_MAX_SEARCH_DAYS = 15


class CalendarError(ValueError):
    """The calendar cannot answer (date outside coverage, no tz database)."""


class BarOutOfSession(ValueError):
    """A bar does not belong to any regular session (weekend, holiday,
    outside regular hours, or not aligned to its bucket grid)."""


def _aware(t: datetime, what: str) -> datetime:
    if not isinstance(t, datetime):
        raise TypeError("%s must be a datetime, got %r" % (what, type(t).__name__))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("%s must be timezone-aware (no UTC assumed)" % what)
    return t.astimezone(UTC)


@dataclass(frozen=True)
class SessionCalendar:
    """Regular-hours exchange calendar for a closed date range."""

    name: str
    tz: str
    regular_open: time
    regular_close: time
    coverage_start: date
    coverage_end: date
    holidays: FrozenSet[date] = frozenset()
    early_closes: Tuple[Tuple[date, time], ...] = ()
    source: str = ""

    def __post_init__(self):
        if ZoneInfo is None:
            raise CalendarError("zoneinfo unavailable")
        try:
            ZoneInfo(self.tz)
        except Exception as exc:  # unknown zone / no tz database
            raise CalendarError("unknown timezone %r: %s" % (self.tz, exc))
        if not (self.regular_open < self.regular_close):
            raise ValueError("regular_open must be before regular_close")
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start after coverage_end")
        for d in self.holidays:
            if not (self.coverage_start <= d <= self.coverage_end):
                raise ValueError("holiday %s outside coverage" % d)
        seen = set()
        for d, t in self.early_closes:
            if d in seen:
                raise ValueError("duplicate early close %s" % d)
            seen.add(d)
            if not (self.coverage_start <= d <= self.coverage_end):
                raise ValueError("early close %s outside coverage" % d)
            if d in self.holidays:
                raise ValueError("early close %s is also a holiday" % d)
            if not (self.regular_open < t < self.regular_close):
                raise ValueError("early close %s at %s not inside regular hours" % (d, t))

    # ── dates ──
    @property
    def zone(self):
        return ZoneInfo(self.tz)

    def covers(self, d: date) -> bool:
        return self.coverage_start <= d <= self.coverage_end

    def _require(self, d: date) -> None:
        if not self.covers(d):
            raise CalendarError("%s outside calendar coverage %s..%s (%s)"
                                % (d, self.coverage_start, self.coverage_end, self.name))

    def is_session(self, d: date) -> bool:
        self._require(d)
        return d.weekday() < 5 and d not in self.holidays

    def local_date(self, t: datetime) -> date:
        return _aware(t, "t").astimezone(self.zone).date()

    def session_bounds(self, d: date) -> Optional[Tuple[datetime, datetime]]:
        """(open_utc, close_utc) for a session date, None if the exchange is
        closed that day. Raises CalendarError outside coverage."""
        if not self.is_session(d):
            return None
        close_t = dict(self.early_closes).get(d, self.regular_close)
        z = self.zone
        o = datetime.combine(d, self.regular_open, tzinfo=z).astimezone(UTC)
        c = datetime.combine(d, close_t, tzinfo=z).astimezone(UTC)
        return o, c

    # ── instants ──
    def session_overlapping(self, start: datetime, end: datetime
                            ) -> Optional[Tuple[datetime, datetime]]:
        """The regular session whose [open, close) overlaps [start, end)."""
        start, end = _aware(start, "start"), _aware(end, "end")
        days = {self.local_date(start), self.local_date(end - timedelta(microseconds=1))}
        for d in sorted(days):
            b = self.session_bounds(d)
            if b is not None and start < b[1] and end > b[0]:
                return b
        return None

    def previous_close(self, t: datetime) -> datetime:
        """Latest session close <= t."""
        t = _aware(t, "t")
        d = self.local_date(t)
        for _ in range(_MAX_SEARCH_DAYS):
            b = self.session_bounds(d)
            if b is not None and b[1] <= t:
                return b[1]
            d -= timedelta(days=1)
        raise CalendarError("no session close within %d days before %s"
                            % (_MAX_SEARCH_DAYS, t.isoformat()))

    def next_open(self, t: datetime) -> datetime:
        """Earliest session open >= t."""
        t = _aware(t, "t")
        d = self.local_date(t)
        for _ in range(_MAX_SEARCH_DAYS):
            b = self.session_bounds(d)
            if b is not None and b[0] >= t:
                return b[0]
            d += timedelta(days=1)
        raise CalendarError("no session open within %d days after %s"
                            % (_MAX_SEARCH_DAYS, t.isoformat()))

    def market_now(self, t: datetime) -> datetime:
        """``t`` while a regular session is open, otherwise the most recent
        session close: the newest instant at which market data can exist."""
        t = _aware(t, "t")
        b = self.session_bounds(self.local_date(t))
        if b is not None and b[0] <= t < b[1]:
            return t
        return self.previous_close(t)


@dataclass(frozen=True)
class TimeframeSpec:
    """How a scanner timeframe label maps to bar-close semantics."""

    label: str
    kind: str                       # DAILY | INTRADAY | BUCKET
    minutes: int = 0                # intraday bar length / bucket length
    base_minutes: int = 0           # BUCKET only: constituent bar length
    base_anchor: Optional[str] = None  # BUCKET only: grid of the base bars

    def __post_init__(self):
        if self.kind not in (DAILY, INTRADAY, BUCKET):
            raise ValueError("unknown timeframe kind %r" % self.kind)
        if self.kind in (INTRADAY, BUCKET) and self.minutes <= 0:
            raise ValueError("%s needs minutes > 0" % self.label)
        if self.kind == BUCKET and self.base_minutes <= 0:
            raise ValueError("%s needs base_minutes > 0" % self.label)
        if self.base_anchor not in (None, ANCHOR_SESSION_OPEN, ANCHOR_CLOCK):
            raise ValueError("unknown base_anchor %r" % self.base_anchor)


# Mirrors bot/strategy.py defaults: 1D '1d'; 4H = '1h' resampled to UTC 4h
# buckets; 1H '1h'; 15m '15m'. The 1H grid differs by provider, so the 4H
# base anchor is left unknown (conservative bounds) unless the caller knows it.
DEFAULT_SPECS = {
    "1D": TimeframeSpec("1D", DAILY),
    "4H": TimeframeSpec("4H", BUCKET, minutes=240, base_minutes=60),
    "1H": TimeframeSpec("1H", INTRADAY, minutes=60),
    "15m": TimeframeSpec("15m", INTRADAY, minutes=15),
}


@dataclass(frozen=True)
class BarTiming:
    """Derived timing of one bar."""

    start: datetime
    close: datetime            # completeness: bar is complete once close <= t
    next_start: Optional[datetime]  # staleness: newer data exists from here
    session_open: datetime
    session_close: datetime
    exact: bool                # False when ``close`` is a conservative bound


def _next_after(cal: SessionCalendar, close: datetime, session_close: datetime,
                need: bool) -> Optional[datetime]:
    if close < session_close:
        return close
    return cal.next_open(close) if need else None


def bar_timing(cal: SessionCalendar, spec: TimeframeSpec, start: datetime,
               market_now: datetime) -> BarTiming:
    """Bar close and the start of the following bar. Raises BarOutOfSession or
    CalendarError. ``next_start`` is only resolved when market_now has passed
    the close (avoids needing calendar coverage beyond the evaluation)."""
    start = _aware(start, "start")
    if spec.kind == DAILY:
        # A daily bar's index may be 00:00 UTC, local midnight, or the session
        # open depending on provider; +12h lands on the session date for all.
        d = cal.local_date(start + timedelta(hours=12))
        b = cal.session_bounds(d)
        if b is None:
            raise BarOutOfSession("daily bar dated %s: exchange closed" % d)
        if start >= b[1]:
            raise BarOutOfSession("daily bar starts after its session close")
        nxt = _next_after(cal, b[1], b[1], market_now > b[1])
        return BarTiming(start, b[1], nxt, b[0], b[1], True)

    length = timedelta(minutes=spec.minutes)
    if spec.kind == INTRADAY:
        b = cal.session_overlapping(start, start + length)
        if b is None:
            raise BarOutOfSession("intraday bar outside regular session")
        close = min(start + length, b[1])
        nxt = _next_after(cal, close, b[1], market_now > close)
        return BarTiming(start, close, nxt, b[0], b[1], True)

    # BUCKET
    if int(start.timestamp()) % (spec.minutes * 60) != 0 or start.microsecond:
        raise BarOutOfSession("bucket start not aligned to %d-minute UTC grid"
                              % spec.minutes)
    end = start + length
    b = cal.session_overlapping(start, end)
    if b is None:
        raise BarOutOfSession("bucket outside regular session")
    base = timedelta(minutes=spec.base_minutes)
    if spec.base_anchor is None:
        close_upper = min(end + base, b[1])
        stale_from = min(end, b[1])
        nxt = _next_after(cal, stale_from, b[1], market_now > stale_from)
        return BarTiming(start, close_upper, nxt, b[0], b[1], False)
    if spec.base_anchor == ANCHOR_SESSION_OPEN:
        origin = b[0]
    else:  # clock grid: floor the open to the base length
        epoch = int(b[0].timestamp())
        origin = datetime.fromtimestamp(epoch - epoch % (spec.base_minutes * 60), UTC)
    # last base start s on the grid with start <= s < end, s < session close
    # and s + base > session open (a bar that holds session data)
    k_hi = (min(end, b[1]) - origin - timedelta(microseconds=1)) // base
    s_last = origin + k_hi * base
    if s_last < start or s_last + base <= b[0]:
        raise BarOutOfSession("bucket holds no base bar of the session")
    close = min(s_last + base, b[1])
    nxt = _next_after(cal, close, b[1], market_now > close)
    return BarTiming(start, close, nxt, b[0], b[1], True)


def following_bar_close(cal: SessionCalendar, spec: TimeframeSpec,
                        timing: BarTiming) -> Optional[Tuple[datetime, bool]]:
    """Close of the first NEWER bar after ``timing`` (completed-bar basis).

    Returns ``(close, exact)``, or None when no newer bar can exist yet
    (``timing.next_start`` unresolved). Daily: the following session's close.
    Intraday: the next bar on the recorded bar's own grid phase (clock- and
    session-anchored provider grids differ only at the session open). Bucket:
    the next UTC-grid bucket; with an unknown base anchor its close is a LOWER
    bound (``exact`` False), so staleness is never flagged later than it
    truly applies. Raises CalendarError / BarOutOfSession like ``bar_timing``.
    """
    s = timing.next_start
    if s is None:
        return None
    if spec.kind == DAILY:
        b = cal.session_bounds(cal.local_date(s))
        if b is None:
            raise CalendarError("no session for the bar following %s" % timing.start)
        return b[1], True
    grid = spec.minutes * 60
    e = int(s.timestamp())
    if spec.kind == INTRADAY:
        phase = int(timing.start.timestamp()) % grid
        fstart = datetime.fromtimestamp(e - (e - phase) % grid, UTC)
        return bar_timing(cal, spec, fstart, s).close, True
    fstart = datetime.fromtimestamp(e - e % grid, UTC)
    ft = bar_timing(cal, spec, fstart, s)
    if ft.exact:
        return ft.close, True
    return min(fstart + timedelta(minutes=spec.minutes), ft.session_close), False

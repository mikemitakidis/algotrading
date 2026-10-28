"""M21 WP5 — truthful evidence builders for the A01 contract (pure, offline).

Turns raw, already-fetched source data into the evidence types the contract
consumes. Nothing here fetches, guesses or defaults: anything that cannot be
established from the supplied data is reported as unavailable, and the
contract then fails closed.

* ``session_calendar_from_rows``: an exchange ``SessionCalendar`` from a
  provider's trading-day rows (Alpaca ``/v2/calendar``: one row per session
  with local open/close times). Non-session weekdays inside the requested
  range become holidays; short sessions become early closes. Rows that do
  not fit a regular 09:30 open and a close at or before 16:00, duplicate or
  weekend rows, and rows outside the requested range are REFUSED (the whole
  calendar, not the row): a calendar we cannot fully explain is no calendar.
* ``calendar_fit_for_decision``: point-in-time use of a calendar snapshot.
  Session facts on or before the decision date are realised history; the
  only risk is a closure announced after the snapshot was taken. A LIVE
  decision therefore needs a snapshot taken before the decision and no older
  than ``max_age``; a REPLAY may use a later snapshot (its past sessions are
  facts) and says so in the provenance.
* ``corporate_action_record_from_events``: a ``CorporateActionRecord`` that is
  ``verified`` only when the source fetch was complete for the whole window
  and every price-scale action in it has a usable ex-date (an undated action
  makes the record unverified, never silently ignored).
* ``measure_liquidity``: 20-session average dollar volume from completed daily
  bars observed at decision time, with coverage and value validation.

The absence of a detected split never establishes VERIFIED_CLEAN; only a
complete, verified corporate-action record does (see corporate_actions.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from bot.scoring_truth.corporate_actions import CorporateActionRecord
from bot.scoring_truth.observations import parse_utc
from bot.scoring_truth.sessions import CalendarError, SessionCalendar

UTC = timezone.utc
US_EQ_TZ = "America/New_York"
US_EQ_OPEN = time(9, 30)
US_EQ_CLOSE = time(16, 0)


class EvidenceError(ValueError):
    """Source data cannot be turned into trustworthy evidence."""


# ── calendar ────────────────────────────────────────────────────────────────

def _parse_date(v: Any) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            pass
    raise EvidenceError("bad session date %r" % (v,))


def _parse_hhmm(v: Any) -> time:
    if isinstance(v, time):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            if ":" in s:
                parts = s.split(":")
                if len(parts) in (2, 3):
                    return time(int(parts[0]), int(parts[1]))
            elif len(s) == 4 and s.isdigit():
                return time(int(s[:2]), int(s[2:]))
        except ValueError:
            pass
    raise EvidenceError("bad session time %r" % (v,))


def session_calendar_from_rows(rows: Iterable[Mapping[str, Any]], *,
                               coverage_start: Any, coverage_end: Any,
                               source: str, fetched_at_utc: Any,
                               name: str = "us-eq") -> SessionCalendar:
    """Build a regular-hours US equity calendar from trading-day rows
    ({"date", "open", "close"}). Raises EvidenceError on anything that does
    not fit; never fills a gap."""
    cs, ce = _parse_date(coverage_start), _parse_date(coverage_end)
    if cs > ce:
        raise EvidenceError("coverage_start after coverage_end")
    fetched = parse_utc(fetched_at_utc)
    if not isinstance(source, str) or not source.strip():
        raise EvidenceError("calendar source required")
    sessions = {}
    for r in rows:
        if not isinstance(r, Mapping):
            raise EvidenceError("calendar row is not a mapping")
        d = _parse_date(r.get("date"))
        if d in sessions:
            raise EvidenceError("duplicate session row %s" % d)
        if not (cs <= d <= ce):
            raise EvidenceError("session row %s outside requested range" % d)
        if d.weekday() >= 5:
            raise EvidenceError("weekend session row %s" % d)
        o, c = _parse_hhmm(r.get("open")), _parse_hhmm(r.get("close"))
        if o != US_EQ_OPEN:
            raise EvidenceError("session %s opens at %s, not %s (unsupported)"
                                % (d, o, US_EQ_OPEN))
        if not (o < c <= US_EQ_CLOSE):
            raise EvidenceError("session %s closes at %s (outside regular hours)" % (d, c))
        sessions[d] = c
    if not sessions:
        raise EvidenceError("no sessions in calendar rows")
    holidays, d = set(), cs
    while d <= ce:
        if d.weekday() < 5 and d not in sessions:
            holidays.add(d)
        d += timedelta(days=1)
    early = tuple(sorted((d, c) for d, c in sessions.items() if c < US_EQ_CLOSE))
    return SessionCalendar(
        name="%s@%s" % (name, fetched.date().isoformat()), tz=US_EQ_TZ,
        regular_open=US_EQ_OPEN, regular_close=US_EQ_CLOSE,
        coverage_start=cs, coverage_end=ce, holidays=frozenset(holidays),
        early_closes=early,
        source="%s fetched %s" % (source.strip(), fetched.isoformat()))


@dataclass(frozen=True)
class CalendarFit:
    usable: bool
    reason: str
    retrospective: bool = False


def calendar_fit_for_decision(calendar: Optional[SessionCalendar], *,
                              fetched_at_utc: Any, evaluated_at: Any, live: bool,
                              max_age: timedelta = timedelta(days=7)) -> CalendarFit:
    """May this calendar snapshot serve a decision at ``evaluated_at``?"""
    if calendar is None:
        return CalendarFit(False, "no_calendar")
    try:
        fetched, ev = parse_utc(fetched_at_utc), parse_utc(evaluated_at)
    except ValueError as exc:
        return CalendarFit(False, "calendar_times_invalid: %s" % exc)
    try:
        d = calendar.local_date(ev)
        if not calendar.covers(d) or not calendar.covers(d - timedelta(days=10)):
            return CalendarFit(False, "calendar_does_not_cover_decision_window")
    except CalendarError as exc:
        return CalendarFit(False, "calendar_error: %s" % exc)
    if fetched > ev:
        if live:
            return CalendarFit(False, "calendar_snapshot_from_after_the_decision")
        return CalendarFit(True, "replay_with_later_snapshot", retrospective=True)
    if live and ev - fetched > max_age:
        return CalendarFit(False, "calendar_snapshot_older_than_%s" % max_age)
    return CalendarFit(True, "point_in_time_snapshot")


# ── corporate actions ───────────────────────────────────────────────────────

# Action types that change the raw price scale (a raw series jumps on the
# ex-date). Cash dividends are excluded: they are not price-scale events for
# split detection (the M19 adjusted-price gate is about scale breaks).
PRICE_SCALE_ACTION_TYPES = ("forward_split", "reverse_split", "unit_split",
                            "stock_dividend", "spin_off")


@dataclass(frozen=True)
class CorporateActionEvent:
    symbol: str
    action_type: str
    ex_date: Optional[date]          # None: the source gave no usable ex-date
    detail: str = ""


def corporate_action_record_from_events(symbol: str,
                                        events: Sequence[CorporateActionEvent], *,
                                        window_start: Any, window_end: Any,
                                        fetch_complete: bool, source: str,
                                        calendar: Optional[SessionCalendar] = None
                                        ) -> CorporateActionRecord:
    """A record for ONE symbol over [window_start, window_end].

    ``verified`` is True only when ``fetch_complete`` is exactly True (every
    page fetched, no error, the window requested in full). Event times are the
    regular-session open on the ex-date (the first instant a raw price can
    jump); without a calendar the ex-date midnight in New York is used, which
    is earlier and therefore conservative. A price-scale action WITHOUT a
    usable ex-date cannot be placed in time, so the record is NOT verified
    (the contract then reports UNVERIFIED and fails closed)."""
    ws, we = parse_utc(window_start), parse_utc(window_end)
    sym = str(symbol).strip().upper()
    if not sym:
        raise EvidenceError("symbol required")
    times = []
    undated = False
    for e in events:
        if str(e.symbol).strip().upper() != sym:
            raise EvidenceError("event for %s in the record for %s" % (e.symbol, sym))
        if e.action_type not in PRICE_SCALE_ACTION_TYPES:
            continue
        if e.ex_date is None:
            undated = True
            continue
        if calendar is not None:
            try:
                b = calendar.session_bounds(e.ex_date)
            except CalendarError:
                b = None
            if b is not None:
                times.append(b[0].isoformat())
                continue
        times.append(datetime.combine(e.ex_date, time(0, 0), tzinfo=ZoneInfo(US_EQ_TZ))
                     .astimezone(UTC).isoformat())
    return CorporateActionRecord(sym, ws.isoformat(), we.isoformat(), tuple(times),
                                 source=str(source) + ("; undated action" if undated else ""),
                                 verified=fetch_complete is True and not undated)


# ── liquidity ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LiquidityTruth:
    avg_dollar_volume_20d: Optional[float]   # None == UNKNOWN
    sessions_used: int
    zero_volume_sessions: int
    reason: str

    @property
    def measured(self) -> bool:
        return self.avg_dollar_volume_20d is not None


def measure_liquidity(daily_bars: Sequence[Tuple[Any, Any, Any]], *,
                      evaluated_at: Any, calendar: Optional[SessionCalendar],
                      window: int = 20) -> LiquidityTruth:
    """Average dollar volume over the ``window`` most recent COMPLETED sessions.

    ``daily_bars`` holds (bar_start, close, volume) from a raw daily series.
    Only bars whose session closed at or before ``evaluated_at`` are used, so
    later observations can never change the result. The measure is UNKNOWN
    (never estimated) when: no calendar; a bar is not on a session; a value is
    non-finite, a close is not positive or a volume is negative; the most
    recent completed session is missing; or fewer than ``window`` consecutive
    completed sessions are present."""
    if calendar is None:
        return LiquidityTruth(None, 0, 0, "no_session_calendar")
    try:
        ev = parse_utc(evaluated_at)
    except ValueError as exc:
        return LiquidityTruth(None, 0, 0, "evaluation_time_invalid: %s" % exc)
    by_session = {}
    try:
        for start, close, volume in daily_bars:
            st = parse_utc(start)
            d = calendar.local_date(st + timedelta(hours=12))
            b = calendar.session_bounds(d)
            if b is None:
                return LiquidityTruth(None, 0, 0, "daily_bar_on_closed_day_%s" % d)
            if b[1] > ev:
                continue                       # not complete at decision time
            for v, nm in ((close, "close"), (volume, "volume")):
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not math.isfinite(float(v)):
                    return LiquidityTruth(None, 0, 0, "%s_not_finite_on_%s" % (nm, d))
            if close <= 0 or volume < 0:
                return LiquidityTruth(None, 0, 0, "invalid_close_or_volume_on_%s" % d)
            if d in by_session:
                return LiquidityTruth(None, 0, 0, "duplicate_daily_bar_%s" % d)
            by_session[d] = float(close) * float(volume)
    except (ValueError, TypeError) as exc:
        return LiquidityTruth(None, 0, 0, "bad_daily_bar: %s" % exc)
    except CalendarError as exc:
        return LiquidityTruth(None, 0, 0, "calendar_cannot_verify: %s" % exc)
    # walk back over the expected completed sessions
    try:
        last = calendar.previous_close(ev)
        d = calendar.local_date(last)
        expected = []
        guard = 0
        while len(expected) < window:
            guard += 1
            if guard > window * 3 + 30:
                return LiquidityTruth(None, 0, 0, "calendar_too_short_for_window")
            if calendar.session_bounds(d) is not None:
                expected.append(d)
            d -= timedelta(days=1)
    except CalendarError as exc:
        return LiquidityTruth(None, 0, 0, "calendar_cannot_cover_window: %s" % exc)
    missing = [x for x in expected if x not in by_session]
    if missing:
        return LiquidityTruth(None, len(expected) - len(missing), 0,
                              "missing_completed_sessions: %s"
                              % ",".join(m.isoformat() for m in missing[:5]))
    vals = [by_session[x] for x in expected]
    zeros = sum(1 for v in vals if v == 0.0)
    return LiquidityTruth(sum(vals) / len(vals), len(vals), zeros,
                          "measured_from_%d_completed_sessions" % len(vals))

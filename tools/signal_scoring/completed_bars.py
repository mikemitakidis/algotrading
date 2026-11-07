"""M21 WP6 — completed-bar completion policy for the scanner (C01 §11).

Implements the ``classify(tf_label, bar_start)`` policy that
``bot.bar_completion.select_completed`` (used by ``bot.scanner.scan_cycle``
in ``bar_basis="completed"`` mode) injects. Bar closes come from A01's
session-aware ``bar_timing`` against a VERIFIED exchange calendar at ONE
decision time:

  * 15m / 1H : ``min(start + length, session close)``; early closes honoured;
  * 4H       : UTC buckets of 1H bars; exact when the 1H base grid is known,
               otherwise the conservative upper bound (bucket end + 1 h,
               capped at the session close) — a bucket is never treated as
               complete before it provably is;
  * 1D       : the session close; during a session the completed daily bar
               is the previous session's.

Bars outside the regular session (extended hours, weekends, holidays,
misaligned buckets) are "outside" and dropped; a bar the calendar cannot
place (coverage, naive timestamp) is "unknown" and the timeframe becomes
unavailable (fail closed).

``infer_hour_grid`` infers the provider's 1H grid (clock-hour vs session-open)
from OBSERVED bar starts; anything ambiguous returns None (conservative).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Tuple

from bot.bar_completion import COMPLETE, FORMING, OUTSIDE, UNKNOWN
from bot.scoring_truth.observations import parse_utc
from bot.scoring_truth.sessions import (
    ANCHOR_CLOCK, ANCHOR_SESSION_OPEN, BUCKET, DEFAULT_SPECS, BarOutOfSession,
    CalendarError, SessionCalendar, TimeframeSpec, bar_timing,
)


class CompletedBarPolicy:
    """Completion policy for one scan decision time."""

    def __init__(self, calendar: SessionCalendar, decision_time_utc: Any, *,
                 hour_grid: Optional[str] = None,
                 specs: Optional[Mapping[str, TimeframeSpec]] = None):
        if not isinstance(calendar, SessionCalendar):
            raise TypeError("a verified SessionCalendar is required")
        self.calendar = calendar
        self.decision_time_utc = parse_utc(decision_time_utc)
        base = dict(specs or DEFAULT_SPECS)
        if hour_grid is not None:
            if hour_grid not in (ANCHOR_CLOCK, ANCHOR_SESSION_OPEN):
                raise ValueError("unknown hour grid %r" % (hour_grid,))
            b = base.get("4H")
            if b is not None and b.kind == BUCKET:
                base["4H"] = TimeframeSpec(b.label, BUCKET, b.minutes, b.base_minutes, hour_grid)
        self.specs = base
        self.hour_grid = hour_grid
        self._mnow = calendar.market_now(self.decision_time_utc)

    def classify(self, tf_label: str, start: Any) -> Tuple[str, Optional[datetime]]:
        spec = self.specs.get(tf_label)
        if spec is None:
            return UNKNOWN, None
        try:
            st = parse_utc(start)
        except ValueError:
            return UNKNOWN, None
        if st > self.decision_time_utc:
            return FORMING, None                      # a bar from the future
        try:
            tm = bar_timing(self.calendar, spec, st, self._mnow)
        except BarOutOfSession:
            return OUTSIDE, None
        except CalendarError:
            return UNKNOWN, None
        return (COMPLETE if tm.close <= self.decision_time_utc else FORMING), tm.close

    def first_interval_ready(self, bar_start_15m: Any) -> Tuple[bool, str]:
        """C01 §11 first-interval rule: during a session, an ENTRY needs a
        completed 15m bar from the CURRENT session. Outside a session the rule
        does not apply (the trusted market clock blocks entries anyway)."""
        cal, ev = self.calendar, self.decision_time_utc
        try:
            b = cal.session_bounds(cal.local_date(ev))
        except CalendarError as exc:
            return False, "calendar_cannot_verify: %s" % exc
        if b is None or not (b[0] <= ev < b[1]):
            return True, "outside_session"
        if bar_start_15m is None:
            return False, "no_15m_bar"
        try:
            st = parse_utc(bar_start_15m)
        except ValueError:
            return False, "15m_bar_start_invalid"
        if st < b[0]:
            return False, "first_interval_no_completed_bar_this_session"
        return True, "current_session_15m_bar"


def infer_hour_grid(starts: Iterable[Any], calendar: SessionCalendar) -> Optional[str]:
    """Infer the provider's 1H grid from observed REGULAR-SESSION bar starts:
    all on the clock hour -> ANCHOR_CLOCK; all aligned to the session open
    (half past in US equities) -> ANCHOR_SESSION_OPEN; mixed, empty or
    unreadable -> None (the conservative unknown grid)."""
    clock = session = 0
    for s in starts:
        try:
            st = parse_utc(s)
            b = calendar.session_overlapping(st, st.replace(minute=59))
        except Exception:
            return None
        if b is None:
            continue                                   # extended hours: ignore
        off = int((st - b[0]).total_seconds()) % 3600
        if st.minute == 0 and st.second == 0:
            clock += 1
        elif off == 0:
            session += 1
        else:
            return None
    if clock and not session:
        return ANCHOR_CLOCK
    if session and not clock:
        return ANCHOR_SESSION_OPEN
    return None

"""research.dataqa.calendar — exchange calendars for real-data readiness (M21 WP11).

Real bars must be judged against a real exchange calendar (holidays, early
closes, daylight saving), never the synthetic 14:30–21:00 UTC grid of
``research.common.bars``. This module builds a ``bot.scoring_truth.sessions.
SessionCalendar`` (pure; allowed by ``research/boundary.py``) from calendar
rows in the Alpaca ``/v2/calendar`` shape, the same rows the WP5 fetcher
stores:

    [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}, ...]

Validation follows WP5's ``session_calendar_from_rows`` rules: duplicate,
weekend, malformed, non-09:30-open or after-16:00-close rows are refused.
Weekdays inside the covered range that have no row are holidays. Once WP5
and WP11 are both merged, the two parsers should share one implementation.

``synthetic_calendar`` gives the synthetic research grid as a calendar, so
the same tooling can run on synthetic samples. Reports on it are labelled
SYNTHETIC and are never real-data evidence.
"""
from __future__ import annotations

from datetime import date, time, timedelta
from typing import Iterable, Mapping

from bot.scoring_truth.sessions import SessionCalendar

NY = "America/New_York"
REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)


def _hm(s: str) -> time:
    hh, mm = str(s).strip().split(":")[:2]
    return time(int(hh), int(mm))


def calendar_from_rows(rows: Iterable[Mapping[str, str]], *, name: str = "XNYS",
                       source: str = "alpaca /v2/calendar rows") -> SessionCalendar:
    days = {}
    for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
            o, c = _hm(r["open"]), _hm(r["close"])
        except Exception as exc:
            raise ValueError("malformed calendar row %r: %s" % (dict(r), type(exc).__name__))
        if d in days:
            raise ValueError("duplicate calendar row %s" % d)
        if d.weekday() >= 5:
            raise ValueError("calendar row on a weekend: %s" % d)
        if o != REGULAR_OPEN:
            raise ValueError("calendar row %s opens at %s, not 09:30" % (d, o))
        if not (o < c <= REGULAR_CLOSE):
            raise ValueError("calendar row %s closes at %s (must be after 09:30 and no later than 16:00)" % (d, c))
        days[d] = c
    if not days:
        raise ValueError("no calendar rows")
    start, end = min(days), max(days)
    holidays, d = set(), start
    while d <= end:
        if d.weekday() < 5 and d not in days:
            holidays.add(d)
        d += timedelta(days=1)
    early = tuple(sorted((d, c) for d, c in days.items() if c < REGULAR_CLOSE))
    return SessionCalendar(name=name, tz=NY, regular_open=REGULAR_OPEN, regular_close=REGULAR_CLOSE,
                           coverage_start=start, coverage_end=end, holidays=frozenset(holidays),
                           early_closes=early, source=source)


def synthetic_calendar(start: date, end: date) -> SessionCalendar:
    """The synthetic research grid (14:30–21:00 UTC weekdays, no holidays)."""
    return SessionCalendar(name="SYNTHETIC", tz="UTC", regular_open=time(14, 30), regular_close=time(21, 0),
                           coverage_start=start, coverage_end=end, source="synthetic research convention")


__all__ = ["NY", "calendar_from_rows", "synthetic_calendar"]

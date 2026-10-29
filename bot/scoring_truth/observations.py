"""M21.1 A01 — per-timeframe observation states (pure).

Classifies the bar a scanner decision was computed from, for one symbol and
timeframe, at an explicit evaluation time:

  MISSING     no bar was recorded for this timeframe
  INVALID     the record is unusable: unparseable or naive timestamp, a bar
              that starts after the evaluation time, or a bar outside every
              regular session (weekend, holiday, outside regular hours,
              misaligned bucket)
  UNVERIFIED  no session calendar, or the calendar does not cover the bar or
              the evaluation date: bar close / freshness cannot be derived
  STALE       newer market data has existed for longer than the allowed lag
              without being observed
  INCOMPLETE  the bar is still forming at the evaluation time (in progress)
  VALID       a complete bar, not stale

``lag_minutes`` is the staleness measure. Its basis follows how the scanner
evaluates bars (``basis``):

  in_progress (default; today's scanner, which scores the forming bar):
      minutes between the moment newer market data started to exist (the
      start of the following bar) and the newest instant market data can
      exist at evaluation time (``market_now``: the evaluation time during a
      session, otherwise the latest session close).
  completed (a scanner that scores only finished bars):
      minutes since a NEWER COMPLETED bar became available (the close of the
      following bar) at evaluation time. A prior daily close is therefore
      current for the whole next session and stale only once that session's
      bar has closed; a forming bar is INVALID under this basis.

It is 0 when no newer data (or no newer completed bar) can exist yet.
``age_minutes`` is plain data age: minutes since the bar closed (complete
bars) or since it opened (incomplete bars).
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

from bot.scoring_truth.sessions import (
    BarOutOfSession, CalendarError, DEFAULT_SPECS, SessionCalendar,
    TimeframeSpec, bar_timing, following_bar_close,
)

UTC = timezone.utc

BASIS_IN_PROGRESS = "in_progress"
BASIS_COMPLETED = "completed"
BAR_BASES = (BASIS_IN_PROGRESS, BASIS_COMPLETED)


class ObservationState(str, enum.Enum):
    VALID = "VALID"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"
    INVALID = "INVALID"
    MISSING = "MISSING"


@dataclass(frozen=True)
class TimeframeObservation:
    timeframe: str
    state: ObservationState
    reason: str
    bar_start_utc: Optional[str] = None
    bar_close_utc: Optional[str] = None
    close_exact: Optional[bool] = None
    session_close_utc: Optional[str] = None
    age_minutes: Optional[float] = None
    lag_minutes: Optional[float] = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["state"] = self.state.value
        return d


def parse_utc(raw: Any) -> datetime:
    """Parse an ISO string / datetime / pandas Timestamp into an aware UTC
    datetime. Raises ValueError on naive or unparseable input (no UTC is
    ever assumed)."""
    if raw is None or isinstance(raw, bool):
        raise ValueError("no timestamp")
    if hasattr(raw, "to_pydatetime"):          # pandas.Timestamp, duck-typed
        raw = raw.to_pydatetime()
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("unparseable timestamp %r" % raw)
    else:
        raise ValueError("unsupported timestamp %r" % (raw,))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive timestamp %r" % (raw,))
    return dt.astimezone(UTC)


def _minutes(a: datetime, b: datetime) -> float:
    return round((a - b).total_seconds() / 60.0, 3)


def classify_observation(timeframe: str, bar_start: Any, *,
                         evaluated_at: datetime,
                         calendar: Optional[SessionCalendar],
                         spec: Optional[TimeframeSpec] = None,
                         max_lag_minutes: float = 30.0,
                         basis: str = BASIS_IN_PROGRESS) -> TimeframeObservation:
    """Classify one recorded bar start. ``evaluated_at`` must be aware."""
    ev = parse_utc(evaluated_at)
    if not (isinstance(max_lag_minutes, (int, float)) and math.isfinite(max_lag_minutes)
            and max_lag_minutes >= 0):
        raise ValueError("max_lag_minutes must be a finite number >= 0")
    if basis not in BAR_BASES:
        raise ValueError("unknown bar basis %r" % (basis,))
    if bar_start is None:
        return TimeframeObservation(timeframe, ObservationState.MISSING,
                                    "no_bar_recorded")
    try:
        start = parse_utc(bar_start)
    except ValueError as exc:
        return TimeframeObservation(timeframe, ObservationState.INVALID,
                                    "bad_timestamp: %s" % exc)
    s_iso = start.isoformat()
    if start > ev:
        return TimeframeObservation(timeframe, ObservationState.INVALID,
                                    "bar_starts_after_evaluation", s_iso)
    spec = spec or DEFAULT_SPECS.get(timeframe)
    if spec is None:
        return TimeframeObservation(timeframe, ObservationState.INVALID,
                                    "unknown_timeframe", s_iso)
    if calendar is None:
        return TimeframeObservation(timeframe, ObservationState.UNVERIFIED,
                                    "no_session_calendar", s_iso,
                                    age_minutes=_minutes(ev, start))
    try:
        mnow = calendar.market_now(ev)
        tm = bar_timing(calendar, spec, start, mnow)
    except BarOutOfSession as exc:
        return TimeframeObservation(timeframe, ObservationState.INVALID,
                                    "bar_outside_regular_session: %s" % exc, s_iso)
    except CalendarError as exc:
        return TimeframeObservation(timeframe, ObservationState.UNVERIFIED,
                                    "calendar_cannot_verify: %s" % exc, s_iso,
                                    age_minutes=_minutes(ev, start))
    complete = tm.close <= ev
    age = _minutes(ev, tm.close if complete else start)
    if basis == BASIS_COMPLETED:
        if not complete:
            return TimeframeObservation(
                timeframe, ObservationState.INVALID,
                "bar_not_complete_under_completed_basis%s"
                % ("" if tm.exact else " (conservative close bound; declare base_anchor)"),
                s_iso, tm.close.isoformat(), tm.exact, tm.session_close.isoformat(),
                age_minutes=age)
        try:
            fol = following_bar_close(calendar, spec, tm)
        except (BarOutOfSession, CalendarError) as exc:
            return TimeframeObservation(timeframe, ObservationState.UNVERIFIED,
                                        "calendar_cannot_verify_following_bar: %s" % exc,
                                        s_iso, age_minutes=age)
        lag = 0.0 if fol is None or fol[0] >= ev else _minutes(ev, fol[0])
    elif tm.next_start is None or tm.next_start >= mnow:
        lag = 0.0
    else:
        lag = _minutes(mnow, tm.next_start)
    common = dict(bar_start_utc=s_iso, bar_close_utc=tm.close.isoformat(),
                  close_exact=tm.exact, session_close_utc=tm.session_close.isoformat(),
                  age_minutes=age, lag_minutes=lag)
    if lag > max_lag_minutes:
        return TimeframeObservation(timeframe, ObservationState.STALE,
                                    "newer_data_unobserved_for_%.1f_min" % lag, **common)
    if not complete:
        return TimeframeObservation(timeframe, ObservationState.INCOMPLETE,
                                    "bar_in_progress", **common)
    return TimeframeObservation(timeframe, ObservationState.VALID,
                                "complete_and_current", **common)


def classify_symbol(bar_asof: Optional[Mapping[str, Any]], *,
                    timeframes: Tuple[str, ...],
                    evaluated_at: datetime,
                    calendar: Optional[SessionCalendar],
                    specs: Optional[Mapping[str, TimeframeSpec]] = None,
                    max_lag_minutes: float = 30.0,
                    basis: str = BASIS_IN_PROGRESS
                    ) -> Optional[Tuple[TimeframeObservation, ...]]:
    """Classify every required timeframe for one symbol. Returns None when no
    as-of record was supplied at all (UNKNOWN, not 'all missing')."""
    if bar_asof is None:
        return None
    if not isinstance(bar_asof, Mapping):
        return tuple(TimeframeObservation(tf, ObservationState.INVALID,
                                          "bar_asof_not_a_mapping")
                     for tf in timeframes)
    specs = specs or DEFAULT_SPECS
    return tuple(classify_observation(tf, bar_asof.get(tf), evaluated_at=evaluated_at,
                                      calendar=calendar, spec=specs.get(tf),
                                      max_lag_minutes=max_lag_minutes, basis=basis)
                 for tf in timeframes)

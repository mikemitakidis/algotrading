"""research.dataqa.stamps — bar timestamp convention: detect it, convert to close-stamped (M21 WP11).

WHY. M16 stores provider bars stamped at their START (``providers_yfinance``
writes ``ts_utc = df.index``: local midnight for daily bars, the bar start for
intraday). M17 and every research lane treat ``ts_utc`` as the bar CLOSE. Fed
directly, a backtest at 15:10 UTC would "see" today's daily bar, whose close
is not known until the session ends: lookahead (WP6 finding, demonstrated by
``test_m21_1_wp6_completed_candles.N3``). Real bars must pass through
``to_close_stamped`` before any research use.

Detection uses only the exchange calendar (no provider assumptions):

* daily: a stamp equal to the session close is CLOSE evidence; a stamp at or
  before the session open (midnight, open) is START evidence;
* intraday / buckets of ``m`` minutes: a stamp at the session open, or before
  it with the bar overlapping the open, is START evidence; a stamp at the
  session close, or after it with the bar overlapping the close, is CLOSE
  evidence. Stamps inside the session are neutral. Stamps outside the
  session (extended hours, closed days) are counted, not used.

The convention is ``start`` or ``close`` only when the evidence is one-sided;
``mixed`` or ``unknown`` data cannot be converted (fail closed).

Conversion (regular-session policy): every output row has ``ts_utc`` = the
bar's CLOSE and ``bar_open_ts`` = when its regular-session trading began.

* daily: close = the session close (early closes and DST from the calendar);
  open = the session open;
* intraday: close = min(start + m, session close); open = max(start, session open);
* 4H buckets (resampled from 1H bars on an unknown grid): close is the
  CONSERVATIVE upper bound ``min(end + 60 min, session close)`` from
  ``bar_timing`` (later, never earlier, so never lookahead) and is counted as
  inexact.

Rows outside the regular session, outside calendar coverage, or inconsistent
with the stated convention are dropped and counted. Two rows that map to the
same close keep the first and are counted as duplicates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Dict, Optional, Tuple

import pandas as pd

from bot.scoring_truth.sessions import DEFAULT_SPECS, CalendarError, SessionCalendar, bar_timing

START, CLOSE, MIXED, UNKNOWN = "start", "close", "mixed", "unknown"
TF_MINUTES = {"15m": 15, "1H": 60, "4H": 240}
TIMEFRAMES = ("1D", "4H", "1H", "15m")
_UNCOVERED = "uncovered"


class _Sessions:
    def __init__(self, cal: SessionCalendar):
        self.cal = cal
        self._b: Dict = {}

    def bounds(self, d):
        if d not in self._b:
            try:
                b = self.cal.session_bounds(d)
                self._b[d] = None if b is None else (pd.Timestamp(b[0]), pd.Timestamp(b[1]))
            except CalendarError:
                self._b[d] = _UNCOVERED
        return self._b[d]

    def local_date(self, ts: pd.Timestamp):
        return ts.tz_convert(self.cal.tz).date()


def _check_tf(tf: str) -> None:
    if tf not in TIMEFRAMES:
        raise ValueError("timeframe must be one of %s" % (TIMEFRAMES,))


@dataclass(frozen=True)
class ConventionReport:
    timeframe: str
    n: int
    start_evidence: int
    close_evidence: int
    neutral: int
    outside_session: int
    uncovered: int
    convention: str

    def as_dict(self) -> Dict:
        return asdict(self)


def _classify(ts: pd.Timestamp, tf: str, S: _Sessions) -> str:
    if tf == "1D":
        same = S.bounds(S.local_date(ts))                  # a close stamp lies on its own session date
        if same not in (None, _UNCOVERED) and ts == same[1]:
            return CLOSE
        b = S.bounds(S.local_date(ts + pd.Timedelta(hours=12)))   # a start stamp may be local midnight
        if b == _UNCOVERED:
            return _UNCOVERED
        if b is None:
            return "outside"
        o, c = b
        return START if ts <= o else "neutral"
    m = pd.Timedelta(minutes=TF_MINUTES[tf])
    b = S.bounds(S.local_date(ts))
    if b == _UNCOVERED:
        return _UNCOVERED
    if b is None:
        return "outside"
    o, c = b
    if ts == o:
        return "start_boundary"
    if ts == c:
        return "close_boundary"
    if ts < o and ts + m > o:
        return START
    if ts > c and ts - m < c:
        return CLOSE
    return "neutral" if o < ts < c else "outside"


def detect_convention(ts_utc, timeframe: str, cal: SessionCalendar) -> ConventionReport:
    """With extended-hours rows present, a stamp exactly at the open or the
    close is produced by BOTH conventions (the pre-market bar closing at the
    open, the after-hours bar starting at the close), so it is neutral; the
    convention must then come from the provider declaration unless bars that
    straddle the open or close settle it."""
    _check_tf(timeframe)
    ts = pd.to_datetime(pd.Series(ts_utc), utc=True)
    S = _Sessions(cal)
    k = {START: 0, CLOSE: 0, "start_boundary": 0, "close_boundary": 0, "neutral": 0, "outside": 0,
         _UNCOVERED: 0}
    for t in ts:
        k[_classify(t, timeframe, S)] += 1
    if k["outside"]:
        k["neutral"] += k.pop("start_boundary") + k.pop("close_boundary")
    else:
        k[START] += k.pop("start_boundary")
        k[CLOSE] += k.pop("close_boundary")
    s, c = k[START], k[CLOSE]
    conv = START if s and not c else CLOSE if c and not s else MIXED if (s and c) else UNKNOWN
    return ConventionReport(timeframe, int(len(ts)), s, c, k["neutral"], k["outside"], k[_UNCOVERED], conv)


@dataclass(frozen=True)
class ConversionReport:
    timeframe: str
    convention: str
    n_in: int
    n_out: int
    dropped_outside_session: int
    dropped_uncovered: int
    dropped_inconsistent: int
    duplicate_close: int
    inexact_close: int

    def as_dict(self) -> Dict:
        return asdict(self)


def _convert_one(ts: pd.Timestamp, tf: str, conv: str, S: _Sessions) -> Tuple[str, Optional[Tuple]]:
    """-> (status, (close, open, exact)) with status ok | outside | uncovered | inconsistent."""
    if tf == "1D":
        d = S.local_date(ts + pd.Timedelta(hours=12)) if conv == START else S.local_date(ts)
        b = S.bounds(d)
        if b == _UNCOVERED:
            return _UNCOVERED, None
        if b is None:
            return "outside", None
        o, c = b
        if (conv == START and ts >= c) or (conv == CLOSE and ts != c):
            return "inconsistent", None
        return "ok", (c, o, True)
    m = pd.Timedelta(minutes=TF_MINUTES[tf])
    start = ts if conv == START else ts - m
    b = S.bounds(S.local_date(start if conv == START else ts - pd.Timedelta(microseconds=1)))
    if b == _UNCOVERED:
        return _UNCOVERED, None
    if b is None:
        return "outside", None
    o, c = b
    if start + m <= o or start >= c:
        return "outside", None
    if tf == "4H" and conv == START:
        try:
            bt = bar_timing(S.cal, DEFAULT_SPECS["4H"], start.to_pydatetime(), start.to_pydatetime())
        except Exception:
            return "outside", None
        return "ok", (pd.Timestamp(bt.close), max(start, o), bool(bt.exact))
    if conv == CLOSE and ts > c:
        return "inconsistent", None
    close = min(start + m, c) if conv == START else ts
    return "ok", (close, max(start, o), True)


def to_close_stamped(df: pd.DataFrame, timeframe: str, cal: SessionCalendar,
                     convention: str) -> Tuple[pd.DataFrame, ConversionReport]:
    _check_tf(timeframe)
    if convention not in (START, CLOSE):
        raise ValueError("only 'start' or 'close' stamped bars can be converted, got %r" % convention)
    S = _Sessions(cal)
    ts = pd.to_datetime(df["ts_utc"], utc=True)
    keep, closes, opens = [], [], []
    k = {"outside": 0, _UNCOVERED: 0, "inconsistent": 0}
    inexact = 0
    for i, t in enumerate(ts):
        st, val = _convert_one(t, timeframe, convention, S)
        if st != "ok":
            k[st] += 1
            continue
        keep.append(i)
        closes.append(val[0])
        opens.append(val[1])
        inexact += 0 if val[2] else 1
    out = df.iloc[keep].reset_index(drop=True)
    out["ts_utc"] = pd.DatetimeIndex(pd.to_datetime(closes, utc=True), dtype="datetime64[ns, UTC]")
    out["bar_open_ts"] = pd.DatetimeIndex(pd.to_datetime(opens, utc=True), dtype="datetime64[ns, UTC]")
    out = out.sort_values("ts_utc", kind="mergesort")
    dup = out["ts_utc"].duplicated(keep="first")
    out = out.loc[~dup].reset_index(drop=True)
    if convention == CLOSE and timeframe != "1D" and len(out) > 1:
        # a close-stamped bar starts no earlier than the previous close of the same session
        # (irregular grids, e.g. a session-anchored 4H bucket closing at the session close)
        prev = out["ts_utc"].shift(1)
        same = prev.notna() & (prev >= out["bar_open_ts"] - pd.Timedelta(minutes=TF_MINUTES[timeframe]))
        later = same & (prev > out["bar_open_ts"]) & (prev < out["ts_utc"])
        out.loc[later, "bar_open_ts"] = prev[later]
    rep = ConversionReport(timeframe, convention, int(len(df)), int(len(out)), k["outside"], k[_UNCOVERED],
                           k["inconsistent"], int(dup.sum()), inexact)
    return out, rep


__all__ = ["START", "CLOSE", "MIXED", "UNKNOWN", "TF_MINUTES", "TIMEFRAMES", "ConventionReport",
           "detect_convention", "ConversionReport", "to_close_stamped"]

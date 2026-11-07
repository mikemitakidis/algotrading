"""M21 WP6 — completed-bar selection for the scanner (C01 §11, decision D-4).

Generic and policy-driven: this module knows nothing about exchange
calendars. The caller injects a ``classify(tf_label, bar_start_utc)`` policy
for ONE decision time that answers, for a recorded bar start:

    ("complete", close_utc)   the bar has closed at or before the decision time
    ("forming", close_utc)    the bar has not closed yet (in progress)
    ("outside", None)         not a regular-session bar (extended hours,
                              weekend, holiday, misaligned bucket)
    ("unknown", None)         the policy cannot tell (e.g. calendar coverage)

``select_completed`` walks back from the provider's last row, dropping
forming and outside rows, and returns the frame ending at the most recent
COMPLETE regular-session bar. An "unknown" answer stops the walk and the
timeframe is unavailable (fail closed): a bar whose completion cannot be
proven is never used.

``regular_session_only`` removes every row the policy classifies "outside"
(extended hours, closed days) BEFORE indicators or 4H buckets are built, so
completed-basis features use regular-session bars only — the convention of
the M16 historical store and the M17 backtester.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

BASIS_IN_PROGRESS = "in_progress"
BASIS_COMPLETED = "completed"
BASES = (BASIS_IN_PROGRESS, BASIS_COMPLETED)
COMPLETE, FORMING, OUTSIDE, UNKNOWN = "complete", "forming", "outside", "unknown"
MAX_TRAILING_ROWS = 96          # 15m bars across a full extended-hours tail


@dataclass(frozen=True)
class Selection:
    tf_label: str
    selected_start: Optional[str]
    selected_close: Optional[str]
    dropped_rows: int
    reason: str


def select_completed(df: Any, tf_label: str,
                     classify: Callable[[str, Any], Tuple[str, Any]],
                     max_trailing: int = MAX_TRAILING_ROWS):
    """Return (frame_or_None, Selection). Never raises: any error makes the
    timeframe unavailable."""
    try:
        n = len(df)
        idx = df.index
    except Exception:
        return None, Selection(tf_label, None, None, 0, "frame_unreadable")
    dropped = 0
    for back in range(min(n, max_trailing)):
        i = n - 1 - back
        start = idx[i]
        try:
            verdict, close = classify(tf_label, start)
        except Exception as exc:
            return None, Selection(tf_label, None, None, dropped,
                                   "policy_error:%s" % type(exc).__name__)
        if verdict == COMPLETE:
            try:
                s_iso = start.isoformat()
                c_iso = close.isoformat() if close is not None else None
            except Exception:
                s_iso, c_iso = str(start), str(close)
            return df.iloc[:i + 1], Selection(tf_label, s_iso, c_iso, dropped,
                                              "completed_bar_selected")
        if verdict in (FORMING, OUTSIDE):
            dropped += 1
            continue
        return None, Selection(tf_label, None, None, dropped,
                               "completion_unknown_at_%s" % start)
    return None, Selection(tf_label, None, None, dropped, "no_completed_bar_in_tail")


def regular_session_only(df: Any, tf_label: str,
                         classify: Callable[[str, Any], Tuple[str, Any]]):
    """Drop rows classified "outside" (keeps complete, forming and unknown
    rows; ``select_completed`` decides on those). Returns None if the frame
    cannot be read or the policy errors (fail closed)."""
    try:
        keep = [classify(tf_label, ts)[0] != OUTSIDE for ts in df.index]
        return df[keep]
    except Exception:
        return None

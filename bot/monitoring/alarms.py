"""M21 WP10 — persistent alarm state machine with explicit recovery records.

States per check: ACTIVE (raised) -> RECOVERED (condition cleared) for WARN
alarms; CRITICAL alarms are LATCHED: when the condition clears they become
CLEARED_PENDING_ACK and stay visible until an operator acknowledges them, so
a recovery never silently erases an unresolved risk. An acknowledgement is
refused while the condition is still present.

* Persistent (SQLite, caller-supplied path): a restart reloads every open
  alarm; an alarm that was already raised is NOT re-notified on restart.
* Append-only event log: RAISED, RECOVERED, CLEARED_PENDING_ACK, ACKED,
  REOPENED, NOTIFY_FAILED, ACK_REFUSED.
* UNKNOWN results raise a "cannot verify" alarm (never healthy).
* Notification sinks are injected; a failing sink is recorded and never
  changes the alarm state. No real messaging sink exists in this module.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence

from bot.monitoring.checks import ALARM, CRITICAL, OK, UNKNOWN, CheckResult

log = logging.getLogger(__name__)

ACTIVE, RECOVERED = "ACTIVE", "RECOVERED"
CLEARED_PENDING_ACK, ACKED = "CLEARED_PENDING_ACK", "ACKED"
OPEN_STATES = (ACTIVE, CLEARED_PENDING_ACK)

_DDL = (
    """CREATE TABLE IF NOT EXISTS monitor_alarms (
        alarm_id TEXT PRIMARY KEY,
        check_name TEXT NOT NULL,
        severity TEXT NOT NULL,
        state TEXT NOT NULL,
        raised_at_utc TEXT NOT NULL,
        last_seen_at_utc TEXT NOT NULL,
        detail TEXT NOT NULL,
        closed_at_utc TEXT,
        close_detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS monitor_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        alarm_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        at_utc TEXT NOT NULL,
        detail TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS monitor_one_open_alarm_per_check ON "
    "monitor_alarms(check_name) WHERE state IN ('ACTIVE','CLEARED_PENDING_ACK')",
)


class NotificationSink(Protocol):
    def notify(self, event: Dict[str, Any]) -> None: ...


class MemorySink:
    """Test / dry-run sink: keeps every notification in memory."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def notify(self, event: Dict[str, Any]) -> None:
        self.events.append(dict(event))


class LogSink:
    """Writes notifications to the process log (no external messaging)."""

    def notify(self, event: Dict[str, Any]) -> None:
        log.warning("[MONITOR] %s %s %s: %s", event.get("kind"), event.get("severity"),
                    event.get("check"), event.get("detail"))


@dataclass(frozen=True)
class AlarmView:
    alarm_id: str
    check: str
    severity: str
    state: str
    raised_at_utc: str
    last_seen_at_utc: str
    detail: str


class Monitor:
    def __init__(self, db_path: str, *, sinks: Sequence[NotificationSink] = (),
                 clock: Callable[[], datetime]):
        self.db_path = db_path
        self.sinks = list(sinks)
        self.clock = clock
        c = self._conn()
        try:
            for stmt in _DDL:
                c.execute(stmt)
        finally:
            c.close()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    # ── notification ──
    def _notify(self, c, alarm_id, kind, check, severity, detail, now):
        ev = {"kind": kind, "check": check, "severity": severity, "detail": detail,
              "alarm_id": alarm_id, "at_utc": now}
        for s in self.sinks:
            try:
                s.notify(ev)
            except Exception as exc:
                c.execute("INSERT INTO monitor_events (alarm_id, kind, at_utc, detail) "
                          "VALUES (?,?,?,?)", (alarm_id, "NOTIFY_FAILED", now,
                                               "%s: %s" % (type(s).__name__,
                                                           type(exc).__name__)))

    def _event(self, c, alarm_id, kind, now, detail):
        c.execute("INSERT INTO monitor_events (alarm_id, kind, at_utc, detail) VALUES (?,?,?,?)",
                  (alarm_id, kind, now, detail))

    # ── evaluation ──
    def evaluate(self, results: Iterable[CheckResult]) -> List[Dict[str, Any]]:
        """Apply one round of check results; returns the transitions made."""
        now = self.clock().isoformat()
        out = []
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            for r in results:
                row = c.execute("SELECT alarm_id, state, severity FROM monitor_alarms "
                                "WHERE check_name=? AND state IN (?,?)",
                                (r.check, ACTIVE, CLEARED_PENDING_ACK)).fetchone()
                bad = r.status in (ALARM, UNKNOWN)
                detail = r.detail if r.status != UNKNOWN else "cannot verify: %s" % r.detail
                if bad:
                    if row is None:
                        aid = "%s@%s" % (r.check, now)
                        c.execute("INSERT INTO monitor_alarms (alarm_id, check_name, severity, "
                                  "state, raised_at_utc, last_seen_at_utc, detail) "
                                  "VALUES (?,?,?,?,?,?,?)",
                                  (aid, r.check, r.severity, ACTIVE, now, now, detail))
                        self._event(c, aid, "RAISED", now, detail)
                        self._notify(c, aid, "RAISED", r.check, r.severity, detail, now)
                        out.append({"alarm_id": aid, "transition": "RAISED"})
                    else:
                        aid, state, _sev = row
                        c.execute("UPDATE monitor_alarms SET last_seen_at_utc=?, detail=?, "
                                  "state=? WHERE alarm_id=?", (now, detail, ACTIVE, aid))
                        if state == CLEARED_PENDING_ACK:       # came back before an ack
                            self._event(c, aid, "REOPENED", now, detail)
                            self._notify(c, aid, "REOPENED", r.check, r.severity, detail, now)
                            out.append({"alarm_id": aid, "transition": "REOPENED"})
                elif r.status == OK and row is not None:
                    aid, state, sev = row
                    if sev == CRITICAL:
                        if state == ACTIVE:
                            c.execute("UPDATE monitor_alarms SET state=?, last_seen_at_utc=? "
                                      "WHERE alarm_id=?", (CLEARED_PENDING_ACK, now, aid))
                            self._event(c, aid, "CLEARED_PENDING_ACK", now, r.detail)
                            self._notify(c, aid, "CLEARED_PENDING_ACK", r.check, sev, r.detail, now)
                            out.append({"alarm_id": aid, "transition": "CLEARED_PENDING_ACK"})
                    else:
                        c.execute("UPDATE monitor_alarms SET state=?, closed_at_utc=?, "
                                  "close_detail=? WHERE alarm_id=?",
                                  (RECOVERED, now, "recovered: %s" % r.detail, aid))
                        self._event(c, aid, "RECOVERED", now, r.detail)
                        self._notify(c, aid, "RECOVERED", r.check, sev, r.detail, now)
                        out.append({"alarm_id": aid, "transition": "RECOVERED"})
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()
        return out

    def acknowledge(self, alarm_id: str, *, operator: str, reason: str) -> bool:
        """Close a latched CRITICAL alarm. Refused while its condition is
        still present (state ACTIVE) or without an operator and reason."""
        now = self.clock().isoformat()
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT state FROM monitor_alarms WHERE alarm_id=?",
                            (alarm_id,)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return False
            if row[0] != CLEARED_PENDING_ACK or not str(operator).strip() \
                    or not str(reason).strip():
                self._event(c, alarm_id, "ACK_REFUSED", now,
                            json.dumps({"state": row[0], "operator": operator}))
                c.execute("COMMIT")
                return False
            c.execute("UPDATE monitor_alarms SET state=?, closed_at_utc=?, close_detail=? "
                      "WHERE alarm_id=?", (ACKED, now, "acked by %s: %s" % (operator, reason),
                                           alarm_id))
            self._event(c, alarm_id, "ACKED", now, "%s: %s" % (operator, reason))
            c.execute("COMMIT")
            return True
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    # ── views ──
    def open_alarms(self) -> List[AlarmView]:
        c = self._conn()
        try:
            rows = c.execute("SELECT alarm_id, check_name, severity, state, raised_at_utc, "
                             "last_seen_at_utc, detail FROM monitor_alarms WHERE state IN (?,?) "
                             "ORDER BY raised_at_utc, alarm_id", OPEN_STATES).fetchall()
        finally:
            c.close()
        return [AlarmView(*r) for r in rows]

    def events(self, alarm_id: Optional[str] = None) -> List[Dict[str, Any]]:
        c = self._conn()
        try:
            q = "SELECT seq, alarm_id, kind, at_utc, detail FROM monitor_events"
            rows = (c.execute(q + " WHERE alarm_id=? ORDER BY seq", (alarm_id,)) if alarm_id
                    else c.execute(q + " ORDER BY seq")).fetchall()
        finally:
            c.close()
        return [dict(zip(("seq", "alarm_id", "kind", "at_utc", "detail"), r)) for r in rows]

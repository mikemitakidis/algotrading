"""M21.1 A02 — complete stage accounting and join integrity (read-only).

``account(conn)`` returns counts plus every violation it finds. It reads; it
never writes. Invariants:

  1. every candidate has exactly one ``scanned`` event;
  2. every event's required predecessor stages exist (pipeline order);
  3. ``closed`` exists iff ``terminal_kind`` is set, and the kind matches the
     ``closed`` event payload;
  4. the terminal kind is consistent with the recorded stages
     (e.g. ``outcome_resolved`` needs an ``outcome_resolved`` event;
     ``not_selected`` must not have a risk evaluation);
  5. an in-flight candidate is legitimate only while it awaits an outcome
     after an accepted broker result; any other in-flight candidate older
     than ``complete_before`` is an accounting gap;
  6. joins (when the source tables are in the same database): ``signal_id``
     exists in ``signals``; ``intent_id`` exists in ``execution_intents`` with
     the same signal_id, symbol and direction; ``outcome_id`` exists in
     ``signal_outcomes`` with the same signal_id; a ``risk_decision_id``
     exists in ``risk_decisions``.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from bot.candidate_ledger.stages import ACCEPTED_STATUSES, REQUIRES


@dataclass
class LedgerReport:
    candidates: int = 0
    terminal: Counter = field(default_factory=Counter)
    in_flight: Counter = field(default_factory=Counter)
    stages: Counter = field(default_factory=Counter)
    violations: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {"candidates": self.candidates, "terminal": dict(self.terminal),
                "in_flight": dict(self.in_flight), "stages": dict(self.stages),
                "violations": list(self.violations), "ok": self.ok}


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def account(conn: sqlite3.Connection, *, complete_before: Optional[str] = None) -> LedgerReport:
    rep = LedgerReport()
    tables = _tables(conn)
    events: Dict[str, Dict[str, list]] = {}
    for cid, stage, payload in conn.execute(
            "SELECT candidate_id, stage, payload_json FROM candidate_ledger_events"):
        events.setdefault(cid, {}).setdefault(stage, []).append(json.loads(payload))
        rep.stages[stage] += 1
    rows = conn.execute(
        "SELECT candidate_id, cycle_key, symbol, direction, scanner_stage, signal_id, "
        "intent_id, risk_decision_id, outcome_id, terminal_kind FROM candidate_ledger"
    ).fetchall()
    known = set()
    for (cid, cycle_key, symbol, direction, scanner_stage, signal_id, intent_id,
         risk_decision_id, outcome_id, terminal) in rows:
        known.add(cid)
        rep.candidates += 1
        ev = events.get(cid, {})
        v = rep.violations.append
        if len(ev.get("scanned", [])) != 1:
            v("%s: expected exactly one scanned event" % cid)
        for stage in ev:
            for req in REQUIRES[stage]:
                if req not in ev:
                    v("%s: %s without %s" % (cid, stage, req))
        closed = ev.get("closed", [])
        if bool(closed) != (terminal is not None):
            v("%s: closed event / terminal_kind mismatch" % cid)
        elif closed and closed[0].get("terminal_kind") != terminal:
            v("%s: closed payload kind differs from terminal_kind" % cid)
        broker = (ev.get("broker_result") or [{}])[-1].get("status")
        if terminal is not None:
            rep.terminal[terminal] += 1
            if terminal == "not_selected" and "risk_evaluated" in ev:
                v("%s: not_selected but risk was evaluated" % cid)
            if terminal == "outcome_resolved" and "outcome_resolved" not in ev:
                v("%s: outcome_resolved without an outcome event" % cid)
            if terminal == "risk_rejected" and \
                    (ev.get("risk_evaluated") or [{}])[-1].get("verdict") != "reject":
                v("%s: risk_rejected without a reject verdict" % cid)
            if terminal in ("broker_unready", "broker_not_accepted", "cancelled") \
                    and broker is None:
                v("%s: %s without a broker result" % (cid, terminal))
        else:
            awaiting = broker in ACCEPTED_STATUSES and "outcome_resolved" not in ev
            rep.in_flight["awaiting_outcome" if awaiting else "incomplete"] += 1
            if not awaiting and complete_before is not None and cycle_key < complete_before:
                v("%s: in flight without an accepted order (incomplete stage accounting)"
                  % cid)
        # joins
        if signal_id is not None and "signals" in tables:
            if conn.execute("SELECT 1 FROM signals WHERE id=?", (signal_id,)).fetchone() is None:
                v("%s: signal_id %s not in signals" % (cid, signal_id))
        if intent_id is not None and "execution_intents" in tables:
            r = conn.execute("SELECT signal_id, symbol, direction FROM execution_intents "
                             "WHERE id=?", (intent_id,)).fetchone()
            if r is None or (r[0], r[1], r[2]) != (signal_id, symbol, direction):
                v("%s: intent_id %s does not join (signal/symbol/direction)" % (cid, intent_id))
        if outcome_id is not None and "signal_outcomes" in tables:
            r = conn.execute("SELECT signal_id FROM signal_outcomes WHERE id=?",
                             (outcome_id,)).fetchone()
            if r is None or r[0] != signal_id:
                v("%s: outcome_id %s does not join on signal_id" % (cid, outcome_id))
        if risk_decision_id is not None and "risk_decisions" in tables:
            if conn.execute("SELECT 1 FROM risk_decisions WHERE decision_id=?",
                            (risk_decision_id,)).fetchone() is None:
                v("%s: risk_decision_id not in risk_decisions" % cid)
    for cid in events:
        if cid not in known:
            rep.violations.append("%s: events for an unknown candidate" % cid)
    return rep

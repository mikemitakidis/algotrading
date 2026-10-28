"""M21 WP7 — candidate-ledger runtime wiring for the paper learning loop.

``LoopLedger`` carries ONE candidate id from the scanner to the terminal
outcome and records every decision stage of the WS2 paper loop:

    scanned -> scored -> risk_evaluated -> intent_logged -> broker_result
            -> outcome_resolved -> closed

Rules
-----
* OFF unless built from an ENABLED ``CandidateLedgerWriter`` (which itself
  needs ``CANDIDATE_LEDGER_ENABLED=1`` and a migrated schema). When off, every
  call is a no-op and the loop behaves exactly as before.
* Exactly-once logical processing: candidate and event ids are deterministic,
  writes are idempotent (A02), so retries, repeated ticks and restarts never
  duplicate an event.
* Business decisions are independent of ledger failures — EXCEPT the one
  mandatory audit point: when the ledger is enabled, the ``intent_logged``
  event must be persisted BEFORE an order may be planned or submitted
  (``intent`` returns False otherwise and the loop blocks the entry).
* Nothing mandatory is silently lost after submission: the loop journal is
  the durable system of record for orders and outcomes, and ``reconcile``
  (run every tick) backfills any missing post-submit event from it,
  idempotently. ``journal_consistency`` reports missing, duplicated or
  contradictory transitions.
* Simulated outcomes stay distinguishable from genuine paper fills: every
  intent, broker result and outcome payload carries the broker's
  ``data_source``.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from bot.candidate_ledger.ids import make_candidate_id
from bot.candidate_ledger.writer import CandidateLedgerWriter, risk_payload

ACCEPTED_JOURNAL_STATES = ("SUBMITTED", "OPEN", "CLOSED")
NOT_PLACED_JOURNAL_STATES = ("REJECTED", "NOT_SUBMITTED")


def _iso(t: Any) -> str:
    return t.isoformat() if hasattr(t, "isoformat") else str(t)


def outcome_label(row: Mapping[str, Any]) -> str:
    """WIN / LOSS / TIMEOUT for a closed paper trade: TIMEOUT for a time-stop
    exit, otherwise the sign of the net (or, while fees are unknown, gross)
    P&L; break-even counts as LOSS (not a win)."""
    if str(row.get("exit_reason") or "").startswith("time"):
        return "TIMEOUT"
    pnl = row.get("realized_pnl")
    if pnl is None:
        pnl = row.get("gross_pnl")
    return "WIN" if pnl is not None and float(pnl) > 0 else "LOSS"


class LoopLedger:
    def __init__(self, writer: CandidateLedgerWriter):
        if not isinstance(writer, CandidateLedgerWriter):
            raise TypeError("a CandidateLedgerWriter is required")
        self.w = writer
        self.stats: Counter = Counter()

    @property
    def enabled(self) -> bool:
        return self.w.enabled is True

    # ── identity ──
    @staticmethod
    def candidate_id(cand: Any) -> Optional[str]:
        cid = getattr(cand, "candidate_id", None)
        if isinstance(cid, str) and cid.startswith("C-"):
            return cid
        try:
            return make_candidate_id(cand.symbol, cand.signal_ts_utc, cand.direction,
                                     cand.strategy_id)
        except Exception:
            return None

    def _ensure_open(self, cand: Any, now: datetime, cycle_key: str) -> Optional[str]:
        """Candidates from the WP5 provider were opened (scanned + scored) at
        scan time. A candidate from any other provider is opened here so its
        later stages are still accounted for."""
        cid = self.candidate_id(cand)
        if cid is None:
            self.stats["no_candidate_id"] += 1
            return None
        if not getattr(cand, "candidate_id", None):
            self.w.open_candidate(
                symbol=cand.symbol, direction=cand.direction, strategy_id=cand.strategy_id,
                decision_ts_utc=cand.signal_ts_utc, cycle_key=cycle_key, cycle_id=None,
                scanner_stage="loop_candidate", at_utc=_iso(now),
                payload={"opened_by": "paper_loop", "score": cand.score})
        return cid

    # ── selection / risk / intent / broker ──
    def selection(self, items: Sequence[Any], picked: Any, notes: Sequence[str],
                  now: datetime, cycle_key: str) -> None:
        if not self.enabled:
            return
        for c in items:
            if not hasattr(c, "symbol") or c is picked:
                continue
            cid = self._ensure_open(c, now, cycle_key)
            if cid is None:
                continue
            why = [n for n in notes if n.endswith(":%s" % c.symbol)] or ["lower_rank_or_below_min"]
            self.w.close(cid, "not_selected", at_utc=_iso(now), reason_codes=why,
                         payload={"stage": "paper_loop_selection"})

    def risk(self, cand: Any, *, passed: bool, reason: Optional[str], now: datetime,
             cycle_key: str, decision_id: Optional[str] = None,
             checks: Optional[Mapping[str, Any]] = None) -> None:
        if not self.enabled:
            return
        cid = self._ensure_open(cand, now, cycle_key)
        if cid is None:
            return
        body = risk_payload(passed is True, reason, checks)
        body["risk_decision_id"] = decision_id
        self.w.record(cid, "risk_evaluated", at_utc=_iso(now), payload=body,
                      reason_codes=[reason] if reason and passed is not True else [])
        if decision_id:
            self.w.link(cid, risk_decision_id=decision_id)
        if passed is not True:
            self.w.close(cid, "risk_rejected", at_utc=_iso(now),
                         reason_codes=[reason] if reason else [])

    def intent(self, cand: Any, *, client_ref: str, order_ref: str, data_source: str,
               now: datetime, cycle_key: str) -> bool:
        """MANDATORY audit point. True when the ledger is off, or the intent is
        durably recorded; False (the loop must not plan or submit) otherwise."""
        if not self.enabled:
            return True
        cid = self._ensure_open(cand, now, cycle_key)
        if cid is None:
            return False
        ok = self.w.record(cid, "intent_logged", at_utc=_iso(now),
                           payload={"client_ref": client_ref, "order_ref": order_ref,
                                    "data_source": data_source})
        self.stats["intent_ok" if ok else "intent_failed"] += 1
        return ok is True

    def not_persisted(self, cand: Any, reason: str, now: datetime, cycle_key: str) -> None:
        if not self.enabled:
            return
        cid = self._ensure_open(cand, now, cycle_key)
        if cid is not None:
            self.w.close(cid, "not_persisted", at_utc=_iso(now), reason_codes=[reason])

    def broker_result(self, cand: Any, *, accepted: bool, status: str, now: datetime,
                      cycle_key: str, data_source: str,
                      detail: Optional[Mapping[str, Any]] = None) -> None:
        if not self.enabled:
            return
        cid = self._ensure_open(cand, now, cycle_key)
        if cid is None:
            return
        body = dict(detail or {})
        body.update(status="accepted" if accepted else status, data_source=data_source)
        self.w.record(cid, "broker_result", at_utc=_iso(now), payload=body)
        if not accepted:
            self.w.close(cid, "broker_not_accepted", at_utc=_iso(now), reason_codes=[status])

    # ── reconciliation from the durable journal ──
    def reconcile(self, journal_db: str, now: datetime) -> Dict[str, int]:
        """Backfill post-submit events from the loop journal (idempotent).
        Never raises; returns counts. Candidates that were never opened in the
        ledger are counted as ``missing_candidate`` (e.g. the ledger was
        enabled after the order was planned)."""
        out: Counter = Counter()
        if not self.enabled:
            return dict(out)
        try:
            path = str(journal_db).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
            jc = sqlite3.connect("file:%s?mode=ro" % path, uri=True)   # read-only
            jc.row_factory = sqlite3.Row
            try:
                orders = jc.execute("SELECT * FROM loop_orders WHERE candidate_id IS NOT NULL "
                                    "ORDER BY planned_at_utc, client_ref").fetchall()
                outcomes = {r["trade_id"]: dict(r) for r in
                            jc.execute("SELECT * FROM paper_trade_outcomes")}
            finally:
                jc.close()
        except sqlite3.Error as exc:
            out["journal_unreadable:%s" % type(exc).__name__] += 1
            return dict(out)
        known = self._known_candidates([o["candidate_id"] for o in orders])
        refs = set()
        for o in orders:
            cid, ref, state = o["candidate_id"], o["client_ref"], o["state"]
            refs.add(ref)
            if cid not in known:
                out["missing_candidate"] += 1
                continue
            have = self._stages(cid)
            at = o["submitted_at_utc"] or o["planned_at_utc"]
            src = o["data_source"]
            if "risk_evaluated" not in have:            # an order exists only after a pass
                self.w.record(cid, "risk_evaluated", at_utc=o["planned_at_utc"],
                              payload=dict(risk_payload(True, None, {}),
                                           risk_decision_id=o["risk_decision_id"],
                                           source="journal_reconcile"))
                out["backfilled:risk_evaluated"] += 1
            if o["risk_decision_id"]:
                self.w.link(cid, risk_decision_id=o["risk_decision_id"])
            if "intent_logged" not in have:
                self.w.record(cid, "intent_logged", at_utc=o["planned_at_utc"],
                              payload={"client_ref": ref, "order_ref": o["order_ref"],
                                       "data_source": src, "source": "journal_reconcile"})
                out["backfilled:intent_logged"] += 1
            if state in ACCEPTED_JOURNAL_STATES:
                if "broker_result" not in have:
                    self.w.record(cid, "broker_result", at_utc=at,
                                  payload={"status": "accepted", "data_source": src,
                                           "source": "journal_reconcile"})
                    out["backfilled:broker_result"] += 1
            elif state in NOT_PLACED_JOURNAL_STATES:
                if "broker_result" not in have:
                    self.w.record(cid, "broker_result", at_utc=at,
                                  payload={"status": state.lower(), "data_source": src,
                                           "source": "journal_reconcile"})
                    out["backfilled:broker_result"] += 1
                self.w.close(cid, "broker_not_accepted", at_utc=o["updated_at_utc"],
                             reason_codes=[state.lower()])
                continue
            else:
                out["in_flight"] += 1
                continue
            row = outcomes.get(ref)
            if state == "CLOSED" and row is not None:
                if "outcome_resolved" not in have:
                    self.w.record(cid, "outcome_resolved",
                                  at_utc=row["exit_filled_at_utc"] or at,
                                  payload={"outcome": outcome_label(row), "trade_id": ref,
                                           "realized_pnl": row.get("realized_pnl"),
                                           "gross_pnl": row.get("gross_pnl"),
                                           "fees_final": row.get("fees_final"),
                                           "r_multiple": row.get("r_multiple"),
                                           "exit_reason": row.get("exit_reason"),
                                           "data_source": row.get("data_source")})
                    out["backfilled:outcome_resolved"] += 1
                self.w.close(cid, "outcome_resolved", at_utc=row["exit_filled_at_utc"] or at)
        # an intent whose order was never planned (e.g. a crash between the
        # audit write and planning) cannot have reached the broker: planning
        # always precedes submission
        for cid, ref in self._open_intents():
            if ref not in refs:
                self.w.close(cid, "not_persisted", at_utc=_iso(now),
                             reason_codes=["intent_without_journal_order"])
                out["closed_unplanned_intent"] += 1
        self.stats.update({"reconcile:%s" % k: v for k, v in out.items()})
        return dict(out)

    def _stages(self, cid: str) -> set:
        try:
            conn = self.w._factory()
            try:
                return {r[0] for r in conn.execute(
                    "SELECT stage FROM candidate_ledger_events WHERE candidate_id=?", (cid,))}
            finally:
                conn.close()
        except Exception:
            return set()

    def _open_intents(self):
        try:
            conn = self.w._factory()
            try:
                rows = conn.execute(
                    "SELECT e.candidate_id, e.payload_json FROM candidate_ledger_events e "
                    "JOIN candidate_ledger l ON l.candidate_id = e.candidate_id "
                    "WHERE e.stage='intent_logged' AND l.terminal_kind IS NULL").fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return [(cid, json.loads(p).get("client_ref")) for cid, p in rows]

    def _known_candidates(self, ids: Iterable[str]) -> set:
        ids = sorted(set(i for i in ids if i))
        if not ids:
            return set()
        try:
            conn = self.w._factory()
            try:
                q = "SELECT candidate_id FROM candidate_ledger WHERE candidate_id IN (%s)" % (
                    ",".join("?" * len(ids)))
                return {r[0] for r in conn.execute(q, ids)}
            finally:
                conn.close()
        except Exception:
            return set()


# ── consistency between the journal and the ledger (read-only) ──────────────

@dataclass
class ConsistencyReport:
    checked: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def journal_consistency(ledger_conn: sqlite3.Connection,
                        journal_conn: sqlite3.Connection) -> ConsistencyReport:
    """Missing, duplicated or contradictory transitions between the loop
    journal (system of record) and the ledger."""
    rep = ConsistencyReport()
    jc = journal_conn
    rows = jc.execute("SELECT client_ref, candidate_id, state FROM loop_orders "
                      "WHERE candidate_id IS NOT NULL").fetchall()
    seen: Counter = Counter(r[1] for r in rows)
    for cid, n in seen.items():
        if n > 1:
            rep.problems.append("%s: %d journal orders share one candidate id" % (cid, n))
    for ref, cid, state in rows:
        rep.checked += 1
        led = ledger_conn.execute("SELECT terminal_kind FROM candidate_ledger "
                                  "WHERE candidate_id=?", (cid,)).fetchone()
        if led is None:
            rep.problems.append("%s: journal order %s has no ledger candidate" % (cid, ref))
            continue
        ev = {}
        for stage, payload in ledger_conn.execute(
                "SELECT stage, payload_json FROM candidate_ledger_events WHERE candidate_id=?",
                (cid,)):
            ev.setdefault(stage, []).append(json.loads(payload))
        intents = ev.get("intent_logged", [])
        if not intents:
            rep.problems.append("%s: no intent_logged for journal order %s" % (cid, ref))
        elif intents[-1].get("client_ref") != ref:
            rep.problems.append("%s: intent client_ref %r != journal %r"
                                % (cid, intents[-1].get("client_ref"), ref))
        term = led[0]
        if state == "CLOSED" and term not in (None, "outcome_resolved"):
            rep.problems.append("%s: journal CLOSED but ledger terminal %s" % (cid, term))
        if state in NOT_PLACED_JOURNAL_STATES and term not in (None, "broker_not_accepted"):
            rep.problems.append("%s: journal %s but ledger terminal %s" % (cid, state, term))
        if state in ("PENDING_SUBMIT", "SUBMITTED", "OPEN", "NEEDS_REVIEW") and term is not None:
            rep.problems.append("%s: journal %s (active) but ledger closed as %s"
                                % (cid, state, term))
    # ledger intents whose journal order does not exist (and were not marked
    # not_persisted) are orphans
    refs = {r[0] for r in rows}
    for cid, payload, term in ledger_conn.execute(
            "SELECT e.candidate_id, e.payload_json, l.terminal_kind FROM candidate_ledger_events e "
            "JOIN candidate_ledger l ON l.candidate_id = e.candidate_id "
            "WHERE e.stage='intent_logged'"):
        ref = json.loads(payload).get("client_ref")
        if ref not in refs and term != "not_persisted":
            rep.problems.append("%s: ledger intent %r has no journal order" % (cid, ref))
    return rep

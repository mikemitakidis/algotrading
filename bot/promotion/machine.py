"""M21 WP14 (canonical B13 + the auto-revert part of B23) — the promotion state machine.

    submit -> CLASSIFIED -> SHADOW -> ELIGIBLE -> APPROVED -> MONITORING -> CONFIRMED
                                   \\-> INELIGIBLE            \\-> REVERTED
    (any non-terminal state) -> WITHDRAWN

Rules enforced here (every refusal is an audited event, never silent):

* Shadow first. A proposal becomes ELIGIBLE only from a shadow comparison
  over at least its tier's minimum of live paper signals, with evidence the
  tier classifier accepted. INCONCLUSIVE shadow evidence keeps it in SHADOW.
* Change budget. One active proposal at a time (SHADOW .. MONITORING), and
  at most one APPLY in any rolling 7 days.
* Approval. Every tier needs a valid operator token bound to the proposal's
  exact content digest and tier. GREEN auto-apply is OFF in Phase 1.
* RED cooling-off. After a RED apply or any revert, no new proposal can be
  submitted or applied until the cooling-off ends. It is stored in the
  database, so a restart does not reset it.
* Apply goes through ``bot.templates.controls`` (propose -> approve ->
  activate, with the token's approver), so every change is versioned and
  can be rolled back. The paper lane only: routing stays Template 1 for
  live orders until a promotion of kind ``promote`` is applied AND a later
  reviewed code change widens ``LIVE_TEMPLATES``. This module never changes
  routing itself.
* Auto-revert. ``monitor`` feeds realised paper R to ``check_decay``. On decay
  it rolls the controls back, raises an alert through the injected sink and
  starts cooling-off. It never cancels or modifies orders.

Deterministic: the clock and every id are injected; the same inputs give the
same states and events.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from bot.promotion.approvals import ApprovalToken, ApprovalVerifier
from bot.promotion.evaluation import ELIGIBLE, INCONCLUSIVE, INELIGIBLE, ShadowVerdict, check_decay
from bot.promotion.tiers import RED, Proposal, classify
from bot.templates.controls import SOURCE_STORE, TemplateControls, read_active

CLASSIFIED, SHADOW, S_ELIGIBLE, S_INELIGIBLE = "CLASSIFIED", "SHADOW", "ELIGIBLE", "INELIGIBLE"
APPROVED, MONITORING, CONFIRMED, REVERTED, WITHDRAWN = "APPROVED", "MONITORING", "CONFIRMED", "REVERTED", "WITHDRAWN"
ACTIVE_STATES = (CLASSIFIED, SHADOW, S_ELIGIBLE, APPROVED, MONITORING)
TERMINAL = (S_INELIGIBLE, CONFIRMED, REVERTED, WITHDRAWN)

APPLY_WINDOW = timedelta(days=7)
COOLING_OFF = timedelta(days=14)
CONFIRM_TRADES = 30

_DDL = (
    """CREATE TABLE IF NOT EXISTS promotion_proposals (
        proposal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, author TEXT NOT NULL, kind TEXT NOT NULL,
        tier TEXT NOT NULL, state TEXT NOT NULL, digest TEXT NOT NULL, proposal_json TEXT NOT NULL,
        decision_json TEXT NOT NULL, controls_version INTEGER, baseline_mean_r REAL)""",
    """CREATE TABLE IF NOT EXISTS promotion_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL, proposal_id TEXT, kind TEXT NOT NULL,
        from_state TEXT, to_state TEXT, actor TEXT NOT NULL, detail TEXT NOT NULL)""",
)


class PromotionRefused(RuntimeError):
    pass


def proposal_digest(p: Proposal) -> str:
    return hashlib.sha256(json.dumps(asdict(p), sort_keys=True, separators=(",", ":"), default=str)
                          .encode()).hexdigest()


class PromotionBook:
    def __init__(self, path: str, *, clock: Callable[[], datetime]):
        if not path:
            raise ValueError("a promotion store path is required")
        self.path, self.clock = path, clock
        c = self._conn()
        try:
            for s in _DDL:
                c.execute(s)
        finally:
            c.close()

    # ── plumbing ──
    def _conn(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def _tx(self, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def _event(self, c, pid, kind, frm, to, actor, detail):
        c.execute("INSERT INTO promotion_events (at_utc, proposal_id, kind, from_state, to_state, actor, detail) "
                  "VALUES (?,?,?,?,?,?,?)", (self.clock().isoformat(), pid, kind, frm, to, actor or "",
                                             json.dumps(detail, sort_keys=True, default=str)))

    def _refuse(self, pid, action, actor, why):
        self._tx(lambda c: self._event(c, pid, "REFUSED", None, None, actor, {"action": action, "why": why}))
        raise PromotionRefused(why)

    def _row(self, pid) -> Dict[str, Any]:
        c = self._conn()
        try:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM promotion_proposals WHERE proposal_id=?", (pid,)).fetchone()
        finally:
            c.close()
        if r is None:
            raise PromotionRefused("unknown proposal %s" % pid)
        return dict(r)

    def _move(self, pid, frm, to, actor, detail, **cols):
        def run(c):
            sets = ", ".join(["state=?"] + ["%s=?" % k for k in cols])
            cur = c.execute("UPDATE promotion_proposals SET %s WHERE proposal_id=? AND state=?" % sets,
                            [to] + list(cols.values()) + [pid, frm])
            if cur.rowcount != 1:
                return False
            self._event(c, pid, "TRANSITION", frm, to, actor, detail)
            return True
        if not self._tx(run):
            self._refuse(pid, "transition_to_%s" % to, actor, "proposal %s is not in state %s" % (pid, frm))

    # ── budget and cooling-off (persisted, survive restarts) ──
    def cooling_off_until(self) -> Optional[datetime]:
        c = self._conn()
        try:
            rows = c.execute("SELECT detail FROM promotion_events WHERE kind='COOLING_OFF'").fetchall()
        finally:
            c.close()
        ends = [datetime.fromisoformat(json.loads(r[0])["until"]) for r in rows]
        return max(ends) if ends else None

    def _cooling(self, now) -> Optional[str]:
        u = self.cooling_off_until()
        return "cooling_off_until_%s" % u.isoformat() if u is not None and now < u else None

    def _active_other(self, pid) -> Optional[str]:
        c = self._conn()
        try:
            r = c.execute("SELECT proposal_id FROM promotion_proposals WHERE state IN (%s) AND proposal_id != ?"
                          % ",".join("?" * len(ACTIVE_STATES)), list(ACTIVE_STATES) + [pid]).fetchone()
        finally:
            c.close()
        return r[0] if r else None

    def _last_apply(self) -> Optional[datetime]:
        c = self._conn()
        try:
            r = c.execute("SELECT MAX(at_utc) FROM promotion_events WHERE kind='APPLIED'").fetchone()
        finally:
            c.close()
        return datetime.fromisoformat(r[0]) if r and r[0] else None

    # ── lifecycle ──
    def submit(self, p: Proposal) -> str:
        now = self.clock()
        why = self._cooling(now)
        if why:
            self._refuse(p.proposal_id, "submit", p.author, why)
        other = self._active_other(p.proposal_id)
        if other:
            self._refuse(p.proposal_id, "submit", p.author, "change_budget_one_active_change:%s" % other)
        c = self._conn()
        try:
            used = c.execute("SELECT 1 FROM promotion_proposals WHERE proposal_id=?", (p.proposal_id,)).fetchone()
        finally:
            c.close()
        if used:
            self._refuse(p.proposal_id, "submit", p.author, "proposal_id_already_used")
        d = classify(p)

        def run(c):
            c.execute("INSERT INTO promotion_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (p.proposal_id, now.isoformat(), p.author, p.kind, d.tier, CLASSIFIED, proposal_digest(p),
                       json.dumps(asdict(p), sort_keys=True, default=str), json.dumps(asdict(d), sort_keys=True),
                       None, None))
            self._event(c, p.proposal_id, "TRANSITION", None, CLASSIFIED, p.author, asdict(d))
        self._tx(run)
        return d.tier

    def start_shadow(self, pid: str, *, actor: str) -> None:
        r = self._row(pid)
        if not json.loads(r["decision_json"])["eligible_evidence"]:
            self._refuse(pid, "start_shadow", actor, "evidence_not_acceptable:%s"
                         % ",".join(json.loads(r["decision_json"])["evidence_reasons"]))
        self._move(pid, CLASSIFIED, SHADOW, actor, {})

    def record_shadow(self, pid: str, verdict: ShadowVerdict, *, actor: str) -> str:
        r = self._row(pid)
        need = json.loads(r["decision_json"])["shadow_min_signals"]
        if verdict.status == INCONCLUSIVE or verdict.n_signals < need:
            self._tx(lambda c: self._event(c, pid, "SHADOW_INCONCLUSIVE", SHADOW, SHADOW, actor,
                                           {"n": verdict.n_signals, "need": need}))
            return SHADOW
        p = json.loads(r["proposal_json"])
        exit_change = p.get("exits_proposed") is not None and p.get("exits_proposed") != p.get("exits_current")
        ok = verdict.status == ELIGIBLE and (verdict.loss_rate_not_higher or not exit_change)
        to = S_ELIGIBLE if ok else S_INELIGIBLE
        self._move(pid, SHADOW, to, actor, asdict(verdict), baseline_mean_r=verdict.mean_current_r)
        return to

    def approve(self, pid: str, token: ApprovalToken, verifier: ApprovalVerifier) -> None:
        r = self._row(pid)
        ok, why = verifier.verify(token, proposal_id=pid, proposal_digest=r["digest"], tier=r["tier"],
                                  now=self.clock())
        if not ok:
            self._refuse(pid, "approve", getattr(token, "approver", ""), "approval_rejected:%s" % why)
        self._move(pid, S_ELIGIBLE, APPROVED, token.approver, {"approver": token.approver, "model": verifier.model,
                                                               "independent": verifier.independent})

    def apply(self, pid: str, controls: TemplateControls, *, token: ApprovalToken, verifier: ApprovalVerifier) -> int:
        """Apply an APPROVED proposal to the paper-lane controls (budget and
        cooling-off checked again here; the token is re-verified)."""
        now = self.clock()
        r = self._row(pid)
        if r["state"] != APPROVED:
            self._refuse(pid, "apply", token.approver, "not_approved:%s" % r["state"])
        why = self._cooling(now)
        if why:
            self._refuse(pid, "apply", token.approver, why)
        last = self._last_apply()
        if last is not None and now - last < APPLY_WINDOW:
            self._refuse(pid, "apply", token.approver, "change_budget_one_apply_per_7_days:last_%s" % last.isoformat())
        ok, why = verifier.verify(token, proposal_id=pid, proposal_digest=r["digest"], tier=r["tier"], now=now)
        if not ok:
            self._refuse(pid, "apply", token.approver, "approval_rejected:%s" % why)
        p = json.loads(r["proposal_json"])
        active = read_active(controls.path)
        if active.source != SOURCE_STORE or active.version is None:
            # without an intact active version there is nothing to auto-revert to
            self._refuse(pid, "apply", token.approver, "no_intact_active_version_to_revert_to:%s" % active.source)
        if active.config != p["current"]:
            self._refuse(pid, "apply", token.approver, "proposal_based_on_stale_config:active_v%s" % active.version)
        v = controls.propose(p["proposed"], author=p["author"], reason="promotion %s (%s)" % (pid, r["tier"]))
        controls.approve(v, approver=token.approver, reason="promotion token %s" % token.signature[:12])
        controls.activate(v, actor=token.approver)

        def run(c):
            cur = c.execute("UPDATE promotion_proposals SET state=?, controls_version=? WHERE proposal_id=? AND "
                            "state=?", (MONITORING, v, pid, APPROVED))
            if cur.rowcount != 1:
                raise PromotionRefused("proposal %s changed state during apply" % pid)
            self._event(c, pid, "APPLIED", APPROVED, MONITORING, token.approver, {"controls_version": v})
            if r["tier"] == RED:
                self._event(c, pid, "COOLING_OFF", None, None, "policy",
                            {"until": (now + COOLING_OFF).isoformat(), "why": "red_change_applied"})
        self._tx(run)
        return v

    def monitor(self, pid: str, post_r: Sequence[float], controls: TemplateControls, *, sink,
                min_trades: int = 10, max_drawdown_r: float = 6.0) -> str:
        r = self._row(pid)
        if r["state"] != MONITORING:
            self._refuse(pid, "monitor", "guard", "not_monitoring:%s" % r["state"])
        d = check_decay(post_r, baseline_mean_r=float(r["baseline_mean_r"] or 0.0), min_trades=min_trades,
                        max_drawdown_r=max_drawdown_r)
        now = self.clock()
        if d.revert:
            back = controls.rollback(actor="promotion-guard", reason="auto-revert %s: %s" % (pid, ";".join(d.reasons)))
            alert = {"kind": "PROMOTION_AUTO_REVERT", "proposal_id": pid, "reverted_to_version": back,
                     "reasons": list(d.reasons), "cancel_orders": d.cancel_orders}

            def run(c):
                c.execute("UPDATE promotion_proposals SET state=? WHERE proposal_id=? AND state=?",
                          (REVERTED, pid, MONITORING))
                self._event(c, pid, "TRANSITION", MONITORING, REVERTED, "promotion-guard", alert)
                self._event(c, pid, "COOLING_OFF", None, None, "policy",
                            {"until": (now + COOLING_OFF).isoformat(), "why": "auto_revert"})
            self._tx(run)
            try:
                sink.notify(alert)
            except Exception as exc:                 # the revert stands even if the alert cannot be sent
                self._tx(lambda c: self._event(c, pid, "ALERT_FAILED", None, None, "promotion-guard",
                                               {"error": type(exc).__name__}))
            return REVERTED
        if d.n_trades >= CONFIRM_TRADES:
            self._move(pid, MONITORING, CONFIRMED, "promotion-guard", {"n": d.n_trades, "mean_r": d.mean_r})
            return CONFIRMED
        return MONITORING

    def withdraw(self, pid: str, *, actor: str, reason: str) -> None:
        r = self._row(pid)
        if r["state"] in TERMINAL or r["state"] == MONITORING:
            self._refuse(pid, "withdraw", actor, "cannot_withdraw_from_%s" % r["state"])
        self._move(pid, r["state"], WITHDRAWN, actor, {"reason": reason})

    # ── views ──
    def state(self, pid: str) -> str:
        return self._row(pid)["state"]

    def events(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        c = self._conn()
        try:
            c.row_factory = sqlite3.Row
            q = "SELECT * FROM promotion_events"
            rs = (c.execute(q + " WHERE proposal_id=? ORDER BY seq", (pid,)) if pid
                  else c.execute(q + " ORDER BY seq")).fetchall()
        finally:
            c.close()
        return [dict(x) for x in rs]


__all__ = ["CLASSIFIED", "SHADOW", "S_ELIGIBLE", "S_INELIGIBLE", "APPROVED", "MONITORING", "CONFIRMED", "REVERTED",
           "WITHDRAWN", "ACTIVE_STATES", "TERMINAL", "APPLY_WINDOW", "COOLING_OFF", "CONFIRM_TRADES",
           "PromotionRefused", "proposal_digest", "PromotionBook"]

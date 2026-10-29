"""M21 WP14 (canonical B13 + the auto-revert part of B23) — the promotion state machine.

    submit -> CLASSIFIED -> SHADOW -> ELIGIBLE -> APPROVED -> MONITORING -> CONFIRMED
                                   \\-> INELIGIBLE            \\-> REVERTED
    (any non-terminal state except MONITORING) -> WITHDRAWN

Rules enforced here (every refusal is an audited event, never silent):

* Shadow first. A proposal becomes ELIGIBLE only from a shadow comparison
  over at least its tier's minimum of live paper signals, with evidence the
  tier classifier accepted. INCONCLUSIVE shadow evidence keeps it in SHADOW
  (and is recorded only while the proposal really is in SHADOW).
* Change budget. One active proposal at a time (CLASSIFIED .. MONITORING),
  enforced inside the insert transaction AND by a partial unique index, and at
  most one APPLY in any rolling 7 days, checked inside the apply-intent
  transaction.
* Approval. Every tier needs a valid operator token bound to the proposal's
  exact content digest and tier. GREEN auto-apply is OFF in Phase 1.
* RED cooling-off. After a RED apply or any revert decision, no new proposal
  can be submitted or applied until the cooling-off ends. It is stored in the
  database (for a revert: in the same transaction as the revert intent), so a
  crash or restart does not reset it.
* Apply goes through ``bot.templates.controls`` (propose -> approve ->
  activate, with the token's approver), so every change is versioned and can
  be rolled back. The paper lane only: routing stays Template 1 for live
  orders until a promotion of kind ``promote`` is applied AND a later reviewed
  code change widens ``LIVE_TEMPLATES``. This module never changes routing.
* Auto-revert. ``monitor`` feeds realised paper R to ``check_decay``. On decay
  it rolls the controls back to the exact version that was active before the
  apply, raises an alert through the injected sink and starts cooling-off. It
  never cancels or modifies orders.

Cross-store consistency (the promotion DB and the controls DB are separate
SQLite files, so no single transaction spans both):

* Every cross-store change is a durable OPERATION in ``promotion_ops``,
  written BEFORE the controls store is touched: a deterministic op id (proposal
  digest + attempt), the version that was active (``from_version``), the
  intended config's sha256, and the exact ``reason`` string stamped on the
  controls version, which is how a retry or a restart finds the version this
  op created instead of creating a second one.
* At most one operation is open (PENDING or BLOCKED) at a time — a partial
  unique index. While one is open, every other state change is refused.
* An op carries a lease. ``reconcile(controls)`` (call it on start-up; ``apply``
  and ``monitor`` call it first) settles PENDING ops whose lease has expired,
  from what the controls store actually shows:
    - APPLY, intended version active  -> recorded as APPLIED (``recovered``);
    - APPLY, pre-apply version active and the intended version never
      activated -> ABORTED, the orphan version withdrawn, the proposal stays
      APPROVED. Reconciliation never activates anything for an apply;
    - REVERT, pre-apply version active -> REVERTED;
    - REVERT, applied version still active -> the rollback is performed (the
      revert decision and cooling-off are already durable);
    - anything else (a third version active, the controls store unreadable or
      failing its integrity check, a version activated and replaced outside
      this book) -> BLOCKED.
  A MONITORING proposal whose controls version is no longer the active one is
  BLOCKED as a DIVERGENCE.
* BLOCKED stops every promotion action until a named human operator calls
  ``resolve_block`` with ROLL_FORWARD or ROLL_BACK, and the resolution is
  accepted only when the controls store already shows that outcome.

Deterministic: the clock and every id are injected; the same inputs give the
same states and events. ``fault_hook`` is a test seam (None in production)
that lets tests stop the process at named points.
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
from bot.templates.controls import (ACTIVE, NON_HUMAN_APPROVERS, PROPOSED, SOURCE_STORE, SUPERSEDED,
                                    ControlError, TemplateControls, read_active, sha256, validate_config)
from bot.templates.controls import APPROVED as V_APPROVED

CLASSIFIED, SHADOW, S_ELIGIBLE, S_INELIGIBLE = "CLASSIFIED", "SHADOW", "ELIGIBLE", "INELIGIBLE"
APPROVED, MONITORING, CONFIRMED, REVERTED, WITHDRAWN = "APPROVED", "MONITORING", "CONFIRMED", "REVERTED", "WITHDRAWN"
ACTIVE_STATES = (CLASSIFIED, SHADOW, S_ELIGIBLE, APPROVED, MONITORING)
TERMINAL = (S_INELIGIBLE, CONFIRMED, REVERTED, WITHDRAWN)

OP_APPLY, OP_REVERT, OP_DIVERGENCE = "APPLY", "REVERT", "DIVERGENCE"
OP_PENDING, OP_COMPLETED, OP_ABORTED, OP_BLOCKED = "PENDING", "COMPLETED", "ABORTED", "BLOCKED"
OPEN_OPS = (OP_PENDING, OP_BLOCKED)
ROLL_FORWARD, ROLL_BACK = "ROLL_FORWARD", "ROLL_BACK"

APPLY_WINDOW = timedelta(days=7)
COOLING_OFF = timedelta(days=14)
CONFIRM_TRADES = 30
OP_LEASE = timedelta(minutes=10)
GUARD = "promotion-guard"

_q = lambda xs: ",".join("'%s'" % x for x in xs)          # noqa: E731  (constants only, never user data)
_DDL = (
    """CREATE TABLE IF NOT EXISTS promotion_proposals (
        proposal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, author TEXT NOT NULL, kind TEXT NOT NULL,
        tier TEXT NOT NULL, state TEXT NOT NULL, digest TEXT NOT NULL, proposal_json TEXT NOT NULL,
        decision_json TEXT NOT NULL, controls_version INTEGER, baseline_mean_r REAL)""",
    """CREATE TABLE IF NOT EXISTS promotion_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL, proposal_id TEXT, kind TEXT NOT NULL,
        from_state TEXT, to_state TEXT, actor TEXT NOT NULL, detail TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS promotion_ops (
        op_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, kind TEXT NOT NULL, attempt INTEGER NOT NULL,
        state TEXT NOT NULL, from_version INTEGER, target_version INTEGER, intended_sha256 TEXT,
        controls_reason TEXT, actor TEXT NOT NULL, created_at_utc TEXT NOT NULL, lease_until_utc TEXT NOT NULL,
        closed_at_utc TEXT, detail TEXT NOT NULL)""",
    # the one-active-change rule and the one-open-operation rule hold at the database level
    "CREATE UNIQUE INDEX IF NOT EXISTS promotion_one_active ON promotion_proposals((1)) WHERE state IN (%s)"
    % _q(ACTIVE_STATES),
    "CREATE UNIQUE INDEX IF NOT EXISTS promotion_one_open_op ON promotion_ops((1)) WHERE state IN (%s)"
    % _q(OPEN_OPS),
)


class PromotionRefused(RuntimeError):
    pass


class _Diverged(RuntimeError):
    """The controls store does not show what the operation expects."""


def proposal_digest(p: Proposal) -> str:
    return hashlib.sha256(json.dumps(asdict(p), sort_keys=True, separators=(",", ":"), default=str)
                          .encode()).hexdigest()


class PromotionBook:
    def __init__(self, path: str, *, clock: Callable[[], datetime],
                 fault_hook: Optional[Callable[[str], None]] = None):
        if not path:
            raise ValueError("a promotion store path is required")
        self.path, self.clock, self.fault_hook = path, clock, fault_hook
        c = self._conn()
        try:
            for s in _DDL:
                c.execute(s)
        finally:
            c.close()

    # ── plumbing ──
    def _conn(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def _hook(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

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

    def _read(self, sql, args=()):
        c = self._conn()
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args).fetchall()]
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
        rs = self._read("SELECT * FROM promotion_proposals WHERE proposal_id=?", (pid,))
        if not rs:
            raise PromotionRefused("unknown proposal %s" % pid)
        return rs[0]

    @staticmethod
    def _gate(c) -> Optional[str]:
        """Inside a write transaction: refuse while an operation is open."""
        r = c.execute("SELECT op_id, state FROM promotion_ops WHERE state IN (%s)" % _q(OPEN_OPS)).fetchone()
        if r is None:
            return None
        return ("blocked:%s" if r[1] == OP_BLOCKED else "operation_open_reconcile_required:%s") % r[0]

    @staticmethod
    def _state_c(c, pid) -> Optional[str]:
        r = c.execute("SELECT state FROM promotion_proposals WHERE proposal_id=?", (pid,)).fetchone()
        return r[0] if r else None

    def _move(self, pid, frm, to, actor, detail, **cols):
        def run(c):
            why = self._gate(c)
            if why:
                return why
            sets = ", ".join(["state=?"] + ["%s=?" % k for k in cols])
            cur = c.execute("UPDATE promotion_proposals SET %s WHERE proposal_id=? AND state=?" % sets,
                            [to] + list(cols.values()) + [pid, frm])
            if cur.rowcount != 1:
                return "proposal %s is not in state %s" % (pid, frm)
            self._event(c, pid, "TRANSITION", frm, to, actor, detail)
            return None
        why = self._tx(run)
        if why:
            self._refuse(pid, "transition_to_%s" % to, actor, why)

    # ── budget and cooling-off (persisted, survive restarts) ──
    @staticmethod
    def _cooling_until_c(c) -> Optional[datetime]:
        ends = [datetime.fromisoformat(json.loads(r[0])["until"])
                for r in c.execute("SELECT detail FROM promotion_events WHERE kind='COOLING_OFF'")]
        return max(ends) if ends else None

    def cooling_off_until(self) -> Optional[datetime]:
        c = self._conn()
        try:
            return self._cooling_until_c(c)
        finally:
            c.close()

    def _cooling_c(self, c, now) -> Optional[str]:
        u = self._cooling_until_c(c)
        return "cooling_off_until_%s" % u.isoformat() if u is not None and now < u else None

    @staticmethod
    def _last_apply_c(c) -> Optional[datetime]:
        r = c.execute("SELECT MAX(at_utc) FROM promotion_events WHERE kind='APPLIED'").fetchone()
        return datetime.fromisoformat(r[0]) if r and r[0] else None

    # ── lifecycle ──
    def submit(self, p: Proposal) -> str:
        now = self.clock()
        d = classify(p)

        def run(c):
            why = self._gate(c) or self._cooling_c(c, now)
            if why:
                return why
            other = c.execute("SELECT proposal_id FROM promotion_proposals WHERE state IN (%s) AND proposal_id != ?"
                              % _q(ACTIVE_STATES), (p.proposal_id,)).fetchone()
            if other:
                return "change_budget_one_active_change:%s" % other[0]
            if self._state_c(c, p.proposal_id) is not None:
                return "proposal_id_already_used"
            try:
                c.execute("INSERT INTO promotion_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (p.proposal_id, now.isoformat(), p.author, p.kind, d.tier, CLASSIFIED, proposal_digest(p),
                           json.dumps(asdict(p), sort_keys=True, default=str), json.dumps(asdict(d), sort_keys=True),
                           None, None))
            except sqlite3.IntegrityError as exc:             # the database-level guard
                return "integrity_refused:%s" % exc
            self._event(c, p.proposal_id, "TRANSITION", None, CLASSIFIED, p.author, asdict(d))
            return None
        why = self._tx(run)
        if why:
            self._refuse(p.proposal_id, "submit", p.author, why)
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
            def run(c):
                why = self._gate(c)
                if why:
                    return why
                st = self._state_c(c, pid)
                if st != SHADOW:
                    return "not_in_shadow:%s" % st
                self._event(c, pid, "SHADOW_INCONCLUSIVE", SHADOW, SHADOW, actor,
                            {"n": verdict.n_signals, "need": need})
                return None
            why = self._tx(run)
            if why:
                self._refuse(pid, "record_shadow", actor, why)
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

    # ── apply (cross-store) ──
    def apply(self, pid: str, controls: TemplateControls, *, token: ApprovalToken, verifier: ApprovalVerifier) -> int:
        """Apply an APPROVED proposal to the paper-lane controls. The token is
        re-verified; budget, cooling-off and state are checked inside the
        intent transaction; the intent is durable before the controls store is
        touched; the outcome is settled from the controls store on failure."""
        self.reconcile(controls)
        r = self._row(pid)
        actor = getattr(token, "approver", "")
        if r["state"] != APPROVED:
            self._refuse(pid, "apply", actor, "not_approved:%s" % r["state"])
        now = self.clock()
        ok, why = verifier.verify(token, proposal_id=pid, proposal_digest=r["digest"], tier=r["tier"], now=now)
        if not ok:
            self._refuse(pid, "apply", actor, "approval_rejected:%s" % why)
        p = json.loads(r["proposal_json"])
        active = read_active(controls.path)
        if active.source != SOURCE_STORE or active.version is None:
            # without an intact active version there is nothing to auto-revert to
            self._refuse(pid, "apply", actor, "no_intact_active_version_to_revert_to:%s" % active.source)
        if active.config != p["current"]:
            self._refuse(pid, "apply", actor, "proposal_based_on_stale_config:active_v%s" % active.version)
        try:
            intended = validate_config(p["proposed"])
        except ControlError as exc:
            self._refuse(pid, "apply", actor, "proposed_config_invalid:%s" % exc)

        def intent(c):
            why = self._gate(c) or self._cooling_c(c, now)
            if why:
                return why, None
            st = self._state_c(c, pid)
            if st != APPROVED:
                return "not_approved:%s" % st, None
            last = self._last_apply_c(c)
            if last is not None and now - last < APPLY_WINDOW:
                return "change_budget_one_apply_per_7_days:last_%s" % last.isoformat(), None
            attempt = 1 + c.execute("SELECT COUNT(*) FROM promotion_ops WHERE proposal_id=? AND kind=?",
                                    (pid, OP_APPLY)).fetchone()[0]
            op_id = "APPLY-%s-%d" % (hashlib.sha256(("%s|%s" % (pid, r["digest"])).encode()).hexdigest()[:16],
                                     attempt)
            reason = "promotion %s op %s (%s)" % (pid, op_id, r["tier"])
            detail = {"author": p["author"], "proposed": intended, "tier": r["tier"],
                      "token_signature_prefix": token.signature[:12]}
            self._insert_op(c, op_id, pid, OP_APPLY, attempt, OP_PENDING, active.version, None, sha256(intended),
                            reason, actor, now, detail)
            self._event(c, pid, "APPLY_INTENT", APPROVED, APPROVED, actor,
                        {"op_id": op_id, "from_version": active.version, "intended_sha256": sha256(intended)})
            return None, op_id
        why, op_id = self._tx(intent)
        if why:
            self._refuse(pid, "apply", actor, why)
        try:
            v = self._drive_apply(self._op(op_id), controls)
            self._complete_apply(self._op(op_id), v, recovered=False, cause=None)
        except Exception as exc:
            try:
                outcome = self._settle(op_id, controls, cause=type(exc).__name__, owner=True)
            except Exception as exc2:                        # the promotion store itself is failing
                outcome = "UNSETTLED:%s" % type(exc2).__name__
            if outcome == OP_COMPLETED:
                return self._row(pid)["controls_version"]
            raise PromotionRefused("apply_not_completed:%s:%s" % (type(exc).__name__, outcome)) from exc
        return v

    def _insert_op(self, c, op_id, pid, kind, attempt, state, frm, target, sha, reason, actor, now, detail):
        c.execute("INSERT INTO promotion_ops VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (op_id, pid, kind, attempt, state, frm, target, sha, reason, actor, now.isoformat(),
                   (now + OP_LEASE).isoformat(), None, json.dumps(detail, sort_keys=True, default=str)))

    @staticmethod
    def _find_version(controls: TemplateControls, reason: str) -> Optional[Dict[str, Any]]:
        try:
            rows = [v for v in controls.versions() if v["reason"] == reason]
        except sqlite3.DatabaseError as exc:                  # the controls store cannot be read
            raise _Diverged("controls_store_error:%s" % type(exc).__name__)
        if len(rows) > 1:
            raise _Diverged("more_than_one_controls_version_for_op")
        return rows[0] if rows else None

    def _drive_apply(self, op, controls: TemplateControls) -> int:
        # Only the owner drives an op, once; after a failure or crash an op is
        # settled from the controls store, never driven again.
        d = json.loads(op["detail"])
        self._hook("apply:intent_recorded")
        v = controls.propose(d["proposed"], author=d["author"], reason=op["controls_reason"])
        self._hook("apply:version_proposed")
        controls.approve(v, approver=op["actor"], reason="promotion token %s" % d["token_signature_prefix"])
        self._hook("apply:version_approved")
        active = read_active(controls.path)
        if active.source != SOURCE_STORE or active.version != op["from_version"]:
            raise _Diverged("active_v%s_%s_is_not_pre_apply_v%s" % (active.version, active.source,
                                                                    op["from_version"]))
        self._hook("apply:checked_before_activate")
        # compare-and-swap inside the controls store's own transaction: a change that lands after the
        # check above is refused there, never overwritten
        controls.activate(v, actor=op["actor"], expected_current=op["from_version"])
        self._hook("apply:activated")
        self._verify_applied(controls, op, v)
        return v

    @staticmethod
    def _activation_previous(controls: TemplateControls, v: int):
        """The version that was active when ``v`` was (last) activated, from the controls audit."""
        try:
            rows = [a for a in controls.audit() if a["action"] == "ACTIVATED" and a["version"] == v]
        except sqlite3.DatabaseError as exc:
            raise _Diverged("controls_store_error:%s" % type(exc).__name__)
        return json.loads(rows[-1]["detail"]).get("previous") if rows else None

    def _verify_applied(self, controls: TemplateControls, op, v: int) -> None:
        """The intended config is active, exactly as recorded, and it replaced the pre-apply version."""
        active = read_active(controls.path)
        if active.source != SOURCE_STORE or active.version != v:
            raise _Diverged("after_activation_active_v%s_%s_is_not_v%s" % (active.version, active.source, v))
        if sha256(active.config) != op["intended_sha256"]:
            raise _Diverged("active_config_differs_from_intended_sha256")
        prev = self._activation_previous(controls, v)
        if prev != op["from_version"]:
            raise _Diverged("v%s_was_activated_over_v%s_not_pre_apply_v%s" % (v, prev, op["from_version"]))

    def _complete_apply(self, op, v, *, recovered, cause, resolved_by=None) -> None:
        now = self.clock()
        d = json.loads(op["detail"])

        def run(c):
            self._hook("apply:complete_tx")
            cur = c.execute("UPDATE promotion_ops SET state=?, target_version=?, closed_at_utc=? WHERE op_id=? AND "
                            "state IN (%s)" % _q(OPEN_OPS), (OP_COMPLETED, v, now.isoformat(), op["op_id"]))
            if cur.rowcount != 1:
                raise _Diverged("op_%s_already_closed" % op["op_id"])
            cur = c.execute("UPDATE promotion_proposals SET state=?, controls_version=? WHERE proposal_id=? AND "
                            "state=?", (MONITORING, v, op["proposal_id"], APPROVED))
            if cur.rowcount != 1:
                raise _Diverged("proposal_%s_not_approved_at_apply_completion" % op["proposal_id"])
            self._event(c, op["proposal_id"], "APPLIED", APPROVED, MONITORING, resolved_by or op["actor"],
                        {"controls_version": v, "op_id": op["op_id"], "recovered": recovered, "cause": cause,
                         "resolved_by": resolved_by})
            if d["tier"] == RED:
                self._event(c, op["proposal_id"], "COOLING_OFF", None, None, "policy",
                            {"until": (now + COOLING_OFF).isoformat(), "why": "red_change_applied"})
        self._tx(run)

    # ── monitor and auto-revert (cross-store) ──
    def monitor(self, pid: str, post_r: Sequence[float], controls: TemplateControls, *, sink,
                min_trades: int = 10, max_drawdown_r: float = 6.0) -> str:
        self.reconcile(controls, sink=sink)
        r = self._row(pid)
        if r["state"] != MONITORING:
            self._refuse(pid, "monitor", GUARD, "not_monitoring:%s" % r["state"])
        d = check_decay(post_r, baseline_mean_r=float(r["baseline_mean_r"] or 0.0), min_trades=min_trades,
                        max_drawdown_r=max_drawdown_r)
        if not d.revert:
            if d.n_trades >= CONFIRM_TRADES:
                self._move(pid, MONITORING, CONFIRMED, GUARD, {"n": d.n_trades, "mean_r": d.mean_r})
                return CONFIRMED
            return MONITORING
        now = self.clock()
        applied = self._read("SELECT * FROM promotion_ops WHERE proposal_id=? AND kind=? AND state=? "
                             "ORDER BY attempt DESC", (pid, OP_APPLY, OP_COMPLETED))
        target = applied[0]["from_version"] if applied else None
        alert = {"kind": "PROMOTION_AUTO_REVERT", "proposal_id": pid, "reverted_to_version": target,
                 "reasons": list(d.reasons), "cancel_orders": d.cancel_orders}

        def intent(c):
            why = self._gate(c)
            if why:
                return why, None
            if self._state_c(c, pid) != MONITORING:
                return "not_monitoring:%s" % self._state_c(c, pid), None
            attempt = 1 + c.execute("SELECT COUNT(*) FROM promotion_ops WHERE proposal_id=? AND kind=?",
                                    (pid, OP_REVERT)).fetchone()[0]
            op_id = "REVERT-%s-%d" % (hashlib.sha256(("%s|%s" % (pid, r["controls_version"])).encode())
                                      .hexdigest()[:16], attempt)
            self._insert_op(c, op_id, pid, OP_REVERT, attempt, OP_PENDING, r["controls_version"], target, None,
                            "auto-revert %s op %s: %s" % (pid, op_id, ";".join(d.reasons)), GUARD, now,
                            {"alert": alert})
            self._event(c, pid, "REVERT_INTENT", MONITORING, MONITORING, GUARD, dict(alert, op_id=op_id))
            # cooling-off is durable together with the decision, before any controls change
            self._event(c, pid, "COOLING_OFF", None, None, "policy",
                        {"until": (now + COOLING_OFF).isoformat(), "why": "auto_revert"})
            return None, op_id
        why, op_id = self._tx(intent)
        if why:
            self._refuse(pid, "monitor", GUARD, why)
        self._hook("revert:intent_recorded")
        try:
            outcome = self._settle(op_id, controls, cause=None, owner=True)
        except Exception as exc:
            outcome = "UNSETTLED:%s" % type(exc).__name__
        if outcome == OP_COMPLETED:
            self._notify(pid, sink, alert)
            return REVERTED
        self._notify(pid, sink, dict(alert, kind="PROMOTION_AUTO_REVERT_INCOMPLETE", op_state=outcome))
        raise PromotionRefused("auto_revert_not_completed:%s" % outcome)

    def _complete_revert(self, op, *, recovered, resolved_by=None, close_kind=None) -> None:
        now = self.clock()
        d = json.loads(op["detail"])

        def run(c):
            self._hook("revert:complete_tx")
            cur = c.execute("UPDATE promotion_ops SET state=?, closed_at_utc=? WHERE op_id=? AND state IN (%s)"
                            % _q(OPEN_OPS), (OP_COMPLETED, now.isoformat(), op["op_id"]))
            if cur.rowcount != 1:
                raise _Diverged("op_%s_already_closed" % op["op_id"])
            cur = c.execute("UPDATE promotion_proposals SET state=? WHERE proposal_id=? AND state=?",
                            (REVERTED, op["proposal_id"], MONITORING))
            if cur.rowcount != 1:
                raise _Diverged("proposal_%s_not_monitoring_at_revert_completion" % op["proposal_id"])
            self._event(c, op["proposal_id"], "TRANSITION", MONITORING, REVERTED, resolved_by or GUARD,
                        dict(d.get("alert", {}), op_id=op["op_id"], recovered=recovered, resolved_by=resolved_by))
            if close_kind == OP_DIVERGENCE:                   # a divergence found by reconcile: start cooling-off
                self._event(c, op["proposal_id"], "COOLING_OFF", None, None, "policy",
                            {"until": (now + COOLING_OFF).isoformat(), "why": "divergence_rolled_back"})
        self._tx(run)

    # ── settlement, reconciliation, blocks ──
    def _op(self, op_id) -> Dict[str, Any]:
        rs = self._read("SELECT * FROM promotion_ops WHERE op_id=?", (op_id,))
        if not rs:
            raise PromotionRefused("unknown operation %s" % op_id)
        return rs[0]

    def _block(self, op, why) -> str:
        def run(c):
            d = dict(json.loads(op["detail"]), blocked_because=why)
            cur = c.execute("UPDATE promotion_ops SET state=?, detail=? WHERE op_id=? AND state=?",
                            (OP_BLOCKED, json.dumps(d, sort_keys=True, default=str), op["op_id"], OP_PENDING))
            if cur.rowcount == 1:
                self._event(c, op["proposal_id"], "BLOCKED", None, None, GUARD, {"op_id": op["op_id"], "why": why})
        self._tx(run)
        return OP_BLOCKED

    def _settle(self, op_id, controls: TemplateControls, *, cause, owner=False) -> str:
        """Bring one open operation to a recorded outcome from what the
        controls store shows. Never activates an apply; a revert may be driven
        to completion. Returns the op state afterwards."""
        op = self._op(op_id)
        if op["state"] != OP_PENDING:
            return op["state"]                                # a BLOCKED op waits for an operator
        if not owner and datetime.fromisoformat(op["lease_until_utc"]) > self.clock():
            return OP_PENDING                                 # another process may still be driving it
        try:
            active = read_active(controls.path)
            if active.source != SOURCE_STORE:
                raise _Diverged("controls_store_not_intact:%s:%s" % (active.source,
                                                                    ",".join(active.fallback_reasons)))
            if op["kind"] == OP_APPLY:
                return self._settle_apply(op, controls, active, cause)
            if op["kind"] == OP_REVERT:
                return self._settle_revert(op, controls, active, cause, recovered=not owner)
            raise _Diverged("unexpected_pending_%s" % op["kind"])
        except _Diverged as exc:
            return self._block(op, str(exc))
        # a promotion-store error propagates: the op stays PENDING and every other action stays shut

    def _settle_apply(self, op, controls, active, cause) -> str:
        found = self._find_version(controls, op["controls_reason"])
        if found is not None and active.version == found["version"]:
            self._verify_applied(controls, op, found["version"])        # digest and what it replaced
            self._complete_apply(op, found["version"], recovered=True, cause=cause)
            return OP_COMPLETED
        if active.version != op["from_version"]:
            raise _Diverged("active_v%s_is_neither_pre_apply_v%s_nor_intended_v%s"
                            % (active.version, op["from_version"], (found or {}).get("version")))
        if found is not None and found["status"] in (ACTIVE, SUPERSEDED):
            raise _Diverged("intended_v%s_was_activated_and_replaced_outside_the_book" % found["version"])
        withdrawn = None
        if found is not None and found["status"] in (PROPOSED, V_APPROVED):
            try:
                controls.withdraw(found["version"], actor=GUARD, reason="aborted promotion op %s" % op["op_id"])
                withdrawn = found["version"]
            except Exception as exc:                          # harmless: an unactivated version is inert
                withdrawn = "withdraw_failed:%s" % type(exc).__name__
        now = self.clock()

        def run(c):
            cur = c.execute("UPDATE promotion_ops SET state=?, closed_at_utc=? WHERE op_id=? AND state=?",
                            (OP_ABORTED, now.isoformat(), op["op_id"], OP_PENDING))
            if cur.rowcount == 1:
                self._event(c, op["proposal_id"], "APPLY_ABORTED", APPROVED, APPROVED, GUARD,
                            {"op_id": op["op_id"], "cause": cause, "orphan_version": (found or {}).get("version"),
                             "withdrawn": withdrawn, "active_version": active.version})
        self._tx(run)
        return OP_ABORTED

    def _settle_revert(self, op, controls, active, cause, *, recovered) -> str:
        target = op["target_version"]
        if target is None:
            raise _Diverged("no_recorded_pre_apply_version_to_revert_to")
        if active.version == op["from_version"]:
            self._hook("revert:checked_before_rollback")
            try:
                controls.rollback(actor=GUARD, reason=op["controls_reason"], expected_current=op["from_version"])
            except ControlError as exc:
                raise _Diverged("rollback_refused:%s" % exc)
            except sqlite3.DatabaseError as exc:              # transient: stays PENDING, every action stays shut
                self._tx(lambda c: self._event(c, op["proposal_id"], "REVERT_RETRY_PENDING", None, None, GUARD,
                                               {"op_id": op["op_id"], "error": type(exc).__name__}))
                return OP_PENDING
            self._hook("revert:rolled_back")
            active = read_active(controls.path)
        if active.source == SOURCE_STORE and active.version == target:
            self._complete_revert(op, recovered=recovered)
            return OP_COMPLETED
        raise _Diverged("after_revert_active_v%s_%s_is_not_pre_apply_v%s" % (active.version, active.source, target))

    def reconcile(self, controls: TemplateControls, *, sink=None) -> Dict[str, Any]:
        """Settle open operations whose lease has expired and check that every
        MONITORING proposal's controls version is the active one. Idempotent:
        with nothing to do it writes nothing. Call on start-up."""
        out: Dict[str, Any] = {"settled": {}, "blocked": [], "diverged": []}
        for op in self._read("SELECT * FROM promotion_ops WHERE state=?", (OP_PENDING,)):
            out["settled"][op["op_id"]] = self._settle(op["op_id"], controls, cause="reconcile")
        if not self._read("SELECT op_id FROM promotion_ops WHERE state IN (%s)" % _q(OPEN_OPS)):
            for r in self._read("SELECT * FROM promotion_proposals WHERE state=?", (MONITORING,)):
                active = read_active(controls.path)
                if active.source == SOURCE_STORE and active.version == r["controls_version"]:
                    continue
                self._open_divergence(r, active)
                out["diverged"].append(r["proposal_id"])
        out["blocked"] = [o["op_id"] for o in self.blocked()]
        if sink is not None:
            self.retry_alerts(sink)
            for op_id in out["blocked"]:
                if op_id not in self._alerted_blocks():
                    self._notify(self._op(op_id)["proposal_id"], sink,
                                 {"kind": "PROMOTION_BLOCKED", "op_id": op_id, "cancel_orders": False})
        return out

    def _open_divergence(self, r, active) -> None:
        now = self.clock()
        applied = self._read("SELECT from_version FROM promotion_ops WHERE proposal_id=? AND kind=? AND state=? "
                             "ORDER BY attempt DESC", (r["proposal_id"], OP_APPLY, OP_COMPLETED))
        pre = applied[0]["from_version"] if applied else None
        why = "monitoring_v%s_but_active_v%s_%s:%s" % (r["controls_version"], active.version, active.source,
                                                       ",".join(active.fallback_reasons))

        def run(c):
            now_row = c.execute("SELECT state, controls_version FROM promotion_proposals WHERE proposal_id=?",
                                (r["proposal_id"],)).fetchone()
            if self._gate(c) or tuple(now_row or ()) != (MONITORING, r["controls_version"]):
                return                                        # another process moved on meanwhile
            attempt = 1 + c.execute("SELECT COUNT(*) FROM promotion_ops WHERE proposal_id=? AND kind=?",
                                    (r["proposal_id"], OP_DIVERGENCE)).fetchone()[0]
            op_id = "DIVERGENCE-%s-%d" % (hashlib.sha256(r["proposal_id"].encode()).hexdigest()[:16], attempt)
            alert = {"kind": "PROMOTION_AUTO_REVERT", "proposal_id": r["proposal_id"], "reverted_to_version": pre,
                     "reasons": ["controls_diverged"], "cancel_orders": False}
            self._insert_op(c, op_id, r["proposal_id"], OP_DIVERGENCE, attempt, OP_BLOCKED, pre,
                            r["controls_version"], None, None, GUARD, now, {"blocked_because": why, "alert": alert})
            self._event(c, r["proposal_id"], "BLOCKED", None, None, GUARD, {"op_id": op_id, "why": why})
        self._tx(run)

    def resolve_block(self, op_id: str, controls: TemplateControls, *, actor: str, resolution: str,
                      reason: str) -> None:
        """A named human operator closes a BLOCKED operation. Accepted only
        when the controls store already shows the chosen outcome:

        * ROLL_FORWARD — the promotion's version is active (APPLY: record it as
          applied; DIVERGENCE: monitoring continues). Not allowed for a REVERT.
        * ROLL_BACK — the pre-apply version is active (APPLY: aborted, the
          proposal stays APPROVED; REVERT/DIVERGENCE: the proposal is REVERTED).
        """
        op = self._read("SELECT * FROM promotion_ops WHERE op_id=?", (op_id,))
        pid = op[0]["proposal_id"] if op else None
        if not str(actor or "").strip() or str(actor).strip().lower() in NON_HUMAN_APPROVERS \
                or not str(reason or "").strip():
            self._refuse(pid, "resolve_block", actor, "a named human operator and a reason are required")
        if not op or op[0]["state"] != OP_BLOCKED:
            self._refuse(pid, "resolve_block", actor, "operation %s is not BLOCKED" % op_id)
        op = op[0]
        if resolution not in (ROLL_FORWARD, ROLL_BACK):
            self._refuse(pid, "resolve_block", actor, "unknown resolution %r" % resolution)
        active = read_active(controls.path)
        if active.source != SOURCE_STORE:
            self._refuse(pid, "resolve_block", actor, "controls_store_not_intact:%s" % active.source)
        kind, by = op["kind"], str(actor).strip()
        try:
            if resolution == ROLL_FORWARD:
                if kind == OP_REVERT:
                    raise _Diverged("a_decay_revert_cannot_be_rolled_forward")
                if kind == OP_APPLY:
                    found = self._find_version(controls, op["controls_reason"])
                    if found is None or active.version != found["version"] \
                            or sha256(active.config) != op["intended_sha256"]:
                        raise _Diverged("intended_version_is_not_active")
                    self._complete_apply(op, found["version"], recovered=True, cause="operator", resolved_by=by)
                else:
                    if active.version != op["target_version"]:
                        raise _Diverged("promotion_version_v%s_is_not_active" % op["target_version"])
                    self._close(op, OP_COMPLETED, by, {"resolution": resolution, "reason": reason})
            else:
                pre = op["target_version"] if kind == OP_REVERT else op["from_version"]
                never_live = False
                if kind == OP_APPLY:
                    found = self._find_version(controls, op["controls_reason"])
                    never_live = found is None or found["status"] not in (ACTIVE, SUPERSEDED)
                if not never_live and (pre is None or active.version != pre):
                    raise _Diverged("pre_apply_version_v%s_is_not_active" % pre)
                if kind == OP_APPLY:
                    # the promotion's version never went live: nothing of it to undo; withdraw the orphan
                    if found is not None and found["status"] in (PROPOSED, V_APPROVED):
                        controls.withdraw(found["version"], actor=by, reason="aborted promotion op %s" % op_id)
                    self._close(op, OP_ABORTED, by, {"resolution": resolution, "reason": reason,
                                                     "intended_never_active": never_live})
                else:
                    self._complete_revert(op, recovered=True, resolved_by=by, close_kind=kind)
        except _Diverged as exc:
            self._refuse(pid, "resolve_block", actor, "stores_do_not_match_%s:%s" % (resolution, exc))
        self._tx(lambda c: self._event(c, pid, "BLOCK_RESOLVED", None, None, by,
                                       {"op_id": op_id, "resolution": resolution, "reason": reason,
                                        "active_version": active.version}))

    def _close(self, op, state, actor, detail) -> None:
        now = self.clock()

        def run(c):
            c.execute("UPDATE promotion_ops SET state=?, closed_at_utc=? WHERE op_id=? AND state=?",
                      (state, now.isoformat(), op["op_id"], OP_BLOCKED))
            self._event(c, op["proposal_id"], "OP_CLOSED", None, None, actor, dict(detail, op_id=op["op_id"],
                                                                                   state=state))
        self._tx(run)

    # ── alerts ──
    def _notify(self, pid, sink, alert) -> bool:
        try:
            sink.notify(alert)
        except Exception as exc:                 # the revert stands even if the alert cannot be sent
            self._tx(lambda c: self._event(c, pid, "ALERT_FAILED", None, None, GUARD,
                                           {"error": type(exc).__name__, "alert": alert}))
            return False
        self._tx(lambda c: self._event(c, pid, "ALERT_SENT", None, None, GUARD, {"alert": alert}))
        return True

    def _alerted_blocks(self) -> List[str]:
        return [json.loads(e["detail"])["alert"].get("op_id") for e in self.events() if e["kind"] == "ALERT_SENT"
                and json.loads(e["detail"])["alert"].get("kind") == "PROMOTION_BLOCKED"]

    def unsent_alerts(self) -> List[str]:
        """REVERTED proposals whose revert alert has not been delivered."""
        out = []
        for r in self._read("SELECT proposal_id FROM promotion_proposals WHERE state=?", (REVERTED,)):
            sent = [e for e in self.events(r["proposal_id"]) if e["kind"] == "ALERT_SENT"
                    and json.loads(e["detail"])["alert"].get("kind") == "PROMOTION_AUTO_REVERT"]
            if not sent:
                out.append(r["proposal_id"])
        return out

    def retry_alerts(self, sink) -> List[str]:
        sent = []
        for pid in self.unsent_alerts():
            ops = self._read("SELECT detail FROM promotion_ops WHERE proposal_id=? AND kind IN (?,?) AND state=? "
                             "ORDER BY created_at_utc DESC", (pid, OP_REVERT, OP_DIVERGENCE, OP_COMPLETED))
            alert = json.loads(ops[0]["detail"]).get("alert") if ops else None
            if alert and self._notify(pid, sink, alert):
                sent.append(pid)
        return sent

    def withdraw(self, pid: str, *, actor: str, reason: str) -> None:
        r = self._row(pid)
        if r["state"] in TERMINAL or r["state"] == MONITORING:
            self._refuse(pid, "withdraw", actor, "cannot_withdraw_from_%s" % r["state"])
        self._move(pid, r["state"], WITHDRAWN, actor, {"reason": reason})

    # ── views ──
    def state(self, pid: str) -> str:
        return self._row(pid)["state"]

    def ops(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        return (self._read("SELECT * FROM promotion_ops WHERE proposal_id=? ORDER BY created_at_utc", (pid,)) if pid
                else self._read("SELECT * FROM promotion_ops ORDER BY created_at_utc"))

    def blocked(self) -> List[Dict[str, Any]]:
        return self._read("SELECT * FROM promotion_ops WHERE state=?", (OP_BLOCKED,))

    def events(self, pid: Optional[str] = None) -> List[Dict[str, Any]]:
        return (self._read("SELECT * FROM promotion_events WHERE proposal_id=? ORDER BY seq", (pid,)) if pid
                else self._read("SELECT * FROM promotion_events ORDER BY seq"))


__all__ = ["CLASSIFIED", "SHADOW", "S_ELIGIBLE", "S_INELIGIBLE", "APPROVED", "MONITORING", "CONFIRMED", "REVERTED",
           "WITHDRAWN", "ACTIVE_STATES", "TERMINAL", "APPLY_WINDOW", "COOLING_OFF", "CONFIRM_TRADES", "OP_LEASE",
           "OP_APPLY", "OP_REVERT", "OP_DIVERGENCE", "OP_PENDING", "OP_COMPLETED", "OP_ABORTED", "OP_BLOCKED",
           "ROLL_FORWARD", "ROLL_BACK", "PromotionRefused", "proposal_digest", "PromotionBook"]

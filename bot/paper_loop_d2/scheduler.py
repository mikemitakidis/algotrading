"""M21.1extra-D2.3 (scheduler) + D2.4 (outcome capture) — the unattended
IBKR-paper learning loop.

Each ``tick()`` handles one scheduler slot (``interval_minutes`` wide):

  1. Take the single-holder CycleLock (overlap prevention) — held -> skip.
  2. Journal the slot (cycle_key = slot start, UTC). A slot runs at most once:
     a re-trigger of the same slot does maintenance only, never a new entry.
     RUNNING cycles of a dead process are marked INTERRUPTED.
  3. Maintenance (every tick, idempotent):
       * crash recovery of PENDING_SUBMIT entries (see ``_recover``);
       * outcome capture (see ``_capture``);
       * broker equity snapshot (net liquidation) for drawdown tracking.
  4. Entry (new slots only), fail-closed in this order: maintenance complete,
     no NEEDS_REVIEW, no unresolved PENDING_SUBMIT, kill switch, D0, D1, max
     open trades, daily entry cap, equity, broker state readable and fully
     attributable, a valid candidate on a symbol with no exposure, a FRESH
     LIVE QUOTE within the drift limit and inside the bracket, notional cap,
     Risk Authority allow, write-ahead journal row (with the broker-visible
     order_ref), then ONE bracket submit carrying that order_ref.

OWNERSHIP (see broker_port): every broker order / execution is attributed to
a trade only through its order_ref or a recorded leg permId — never by symbol,
side and time. Identity conflicts send the trade to NEEDS_REVIEW, which blocks
all further entries until an operator resolves it.

CRASH UNCERTAINTY: a PENDING_SUBMIT entry is adopted only on positive broker
evidence carrying its order_ref; it becomes NOT_SUBMITTED only after
``recovery_absence_confirmations`` consecutive COMPLETE broker lookups (past a
grace period) show nothing; an incomplete view never proves absence, and an
entry that stays unresolved goes to NEEDS_REVIEW. It is never resubmitted.

TIME STOP: the loop closes only its own quantity, after proving the contract
position equals that quantity and no foreign order is working on the contract;
otherwise it does nothing and requires operator review.

Quantity is fixed at 1 share (no variable sizing before F1/F2). Everything
external is injected (clock, broker, gates, candidate provider, lock path,
journal), so the whole loop is testable with the simulated broker.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from bot.paper_loop_d2.broker_port import (
    DEAD_STATUSES, LEG_ENTRY, LEG_FLATTEN, LEG_SL, LEG_TP, WORKING_STATUSES, BracketRequest,
    BrokerUnavailable, OwnedTrade, attribute_fills, classify_owned_orders, flatten_ref,
    foreign_orders, leg_perm_ids, make_order_ref, symbols_with_exposure, unattributed_orders,
)
from bot.paper_loop_d2.candidates import select_candidate
from bot.paper_loop_d2.journal import ACTIVE_STATES, JournalConflict, LoopJournal
from bot.paper_loop_d2.lock import CycleLock, LockHeld, _pid_alive

Gate = Callable[[], Tuple[bool, str]]


@dataclass(frozen=True)
class LoopConfig:
    interval_minutes: int = 15
    max_open_trades: int = 1
    max_entries_per_day: int = 3
    min_score: float = 60.0
    qty: int = 1
    max_notional_usd: float = 5000.0
    max_notional_pct_of_equity: float = 0.05
    min_equity_usd: float = 1000.0
    max_hold_minutes: int = 3 * 24 * 60
    entry_fill_timeout_minutes: int = 30
    allow_short: bool = False
    max_signal_age_minutes: float = 30.0
    max_entry_drift_pct: float = 0.02
    max_quote_age_seconds: float = 60.0
    allow_delayed_quotes: bool = False
    recovery_absence_grace_minutes: float = 5.0
    recovery_absence_confirmations: int = 2
    pending_review_after_minutes: float = 60.0
    close_resolution_minutes: float = 30.0          # unresolved time-stop close -> NEEDS_REVIEW
    fee_finalization_hours: float = 72.0            # how long to wait for broker commissions

    def validate(self) -> "LoopConfig":
        if self.qty != 1:
            raise ValueError("qty is fixed at 1 share until real sizing (F1/F2) exists")
        if not (1 <= self.interval_minutes <= 24 * 60):
            raise ValueError("interval_minutes out of range")
        if self.max_open_trades < 1 or self.max_entries_per_day < 1:
            raise ValueError("caps must be >= 1")
        if not (0 < self.max_notional_pct_of_equity <= 1) or self.max_notional_usd <= 0:
            raise ValueError("notional caps invalid")
        if not (0 < self.max_entry_drift_pct < 1) or self.max_quote_age_seconds <= 0:
            raise ValueError("drift / quote-age limits invalid")
        if self.recovery_absence_confirmations < 2:
            raise ValueError("absence must be confirmed by at least two complete lookups")
        return self


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _fees(fills) -> Optional[float]:
    """Sum of broker-reported commissions; None if ANY is unknown (never 0)."""
    vals = [f.commission for f in fills]
    if any(v is None for v in vals):
        return None
    return float(sum(vals))


def _vwap(fills) -> Tuple[float, str, Optional[float]]:
    qty = sum(f.qty for f in fills)
    return (sum(f.qty * f.price for f in fills) / qty, max(f.time_utc for f in fills), _fees(fills))


class PaperLearningLoop:
    def __init__(self, *, journal: LoopJournal, broker: Any, d0_gate: Gate, d1_gate: Gate,
                 kill_switch_active: Callable[[], bool], risk_gate: Callable[..., Tuple[bool, str, Optional[str]]],
                 candidate_provider: Callable[[datetime], Sequence[Any]], lock_path: str,
                 config: Optional[LoopConfig] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 pid: Optional[int] = None, pid_alive: Callable[[int], bool] = _pid_alive,
                 ledger: Any = None):
        self.journal = journal
        # M21 WP7: optional ledger sink, injected by the caller (the loop never
        # imports it). None, or a sink whose writer is disabled, changes nothing.
        self.ledger = ledger
        self.broker = broker
        self.d0_gate = d0_gate
        self.d1_gate = d1_gate
        self.kill_switch_active = kill_switch_active
        self.risk_gate = risk_gate
        self.candidate_provider = candidate_provider
        self.lock_path = lock_path
        self.config = (config or LoopConfig()).validate()
        self.clock = clock
        self.pid = pid if pid is not None else os.getpid()
        self.pid_alive = pid_alive

    @property
    def _account(self) -> Optional[str]:
        return getattr(self.broker, "account", None)

    # ── slots ──
    def slot_start(self, now: datetime) -> datetime:
        now = now.astimezone(timezone.utc)
        minutes = (now.hour * 60 + now.minute) // self.config.interval_minutes * self.config.interval_minutes
        return now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)

    def cycle_key(self, now: datetime) -> str:
        return "slot:%s" % self.slot_start(now).strftime("%Y-%m-%dT%H:%MZ")

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return now.astimezone(timezone.utc)

    # ── one tick ──
    def tick(self) -> Dict[str, Any]:
        now = self._now()
        key = self.cycle_key(now)
        res: Dict[str, Any] = {"cycle_key": key, "at_utc": _iso(now), "skipped": None,
                               "interrupted_cycles": [], "recovered": [], "opened": [],
                               "closed": [], "review": [], "fees_finalized": [],
                               "entry": None, "entry_block": None,
                               "errors": [], "data_source": getattr(self.broker, "data_source", None)}
        try:
            lock = CycleLock(self.lock_path, pid=self.pid, pid_alive=self.pid_alive,
                             clock=lambda: now)
            lock.acquire()
        except LockHeld:
            res["skipped"] = "lock_held"
            return res
        try:
            res["interrupted_cycles"] = self.journal.mark_interrupted_cycles(self.pid_alive, now)
            created = self.journal.begin_cycle(key, self.pid, now)
            if not created:
                res["skipped"] = "slot_already_processed"
            self._maintenance(now, key, res)
            if created:
                try:
                    self._entry(now, key, res)
                except Exception as exc:                    # backstop: fail closed
                    res["errors"].append("entry_internal:%s" % type(exc).__name__)
                    res["entry_block"] = res["entry_block"] or "internal_error"
                finally:
                    outcome = "entry_submitted" if res["entry"] else "no_entry"
                    self.journal.finish_cycle(key, outcome, res["entry_block"],
                                              {k: res[k] for k in ("recovered", "opened", "closed",
                                                                   "review", "errors", "entry")},
                                              self._now())
            self._ledger_reconcile(now, res)
        finally:
            lock.release()
        res.pop("_equity_snapshot", None)
        res.pop("_maintenance_incomplete", None)
        return res

    def run(self, *, max_ticks: Optional[int] = None, sleep: Callable[[float], None] = time.sleep,
            should_stop: Callable[[], bool] = lambda: False) -> List[Dict[str, Any]]:
        """Unattended loop: one tick per slot. ``max_ticks`` bounds it for tests
        and one-shot use (e.g. a systemd timer calling with max_ticks=1)."""
        results = []
        while not should_stop() and (max_ticks is None or len(results) < max_ticks):
            results.append(self.tick())
            if max_ticks is not None and len(results) >= max_ticks:
                break
            nxt = self.slot_start(self._now()) + timedelta(minutes=self.config.interval_minutes)
            sleep(max(1.0, (nxt - self._now()).total_seconds()))
        return results

    # ── M21 WP7: candidate ledger (never breaks the loop) ──
    def _ledger_call(self, method: str, res: Dict[str, Any], *args: Any, **kw: Any) -> None:
        if self.ledger is None:
            return
        try:
            getattr(self.ledger, method)(*args, **kw)
        except Exception as exc:
            res["errors"].append("ledger_%s:%s" % (method, type(exc).__name__))

    def _ledger_reconcile(self, now: datetime, res: Dict[str, Any]) -> None:
        if self.ledger is None:
            return
        try:
            res["ledger"] = self.ledger.reconcile(self.journal.db_path, now)
        except Exception as exc:
            res["errors"].append("ledger_reconcile:%s" % type(exc).__name__)

    # ── maintenance: recovery + outcome capture + equity ──
    def _maintenance(self, now: datetime, key: str, res: Dict[str, Any]) -> None:
        """Recovery, outcome capture and equity. A broker outage is retried next
        tick. Any other failure is recorded and blocks this tick's entry
        (state may be inconsistent), but never kills the unattended loop."""
        for name, step in (("recovery", lambda: self._recover(now, res)),
                           ("capture", lambda: self._capture(now, res)),
                           ("fees", lambda: self._finalize_fees(now, res))):
            try:
                step()
            except BrokerUnavailable as exc:
                res["errors"].append("%s_broker_unavailable:%s" % (name, exc))
                res["_maintenance_incomplete"] = True
            except Exception as exc:
                res["errors"].append("%s_internal:%s" % (name, type(exc).__name__))
                res["_maintenance_incomplete"] = True
        try:
            snap = self.broker.account_snapshot()
            self.journal.record_equity(cycle_key=key, snap=snap)
            res["equity"] = snap.net_liquidation
            res["_equity_snapshot"] = snap
        except BrokerUnavailable as exc:
            res["errors"].append("equity_unavailable:%s" % exc)
        except Exception as exc:
            res["errors"].append("equity_internal:%s" % type(exc).__name__)

    def _review(self, o: Dict[str, Any], expected: str, now: datetime, why: str,
                res: Dict[str, Any]) -> None:
        self.journal.transition(o["client_ref"], expected, "NEEDS_REVIEW", now, note=why[:500])
        res["review"].append({"client_ref": o["client_ref"], "reason": why[:200]})

    @staticmethod
    def _leg_fields(legs: Dict[str, Any]) -> Dict[str, Any]:
        cols = {LEG_ENTRY: ("broker_order_id", "broker_perm_id"), LEG_TP: ("tp_order_id", "tp_perm_id"),
                LEG_SL: ("sl_order_id", "sl_perm_id"),
                LEG_FLATTEN: ("flatten_order_id", "flatten_perm_id")}
        out: Dict[str, Any] = {}
        for leg, v in legs.items():
            if leg in cols and v is not None:
                oid, perm = cols[leg]
                if getattr(v, "order_id", None):
                    out[oid] = str(v.order_id)
                if getattr(v, "perm_id", None):
                    out[perm] = str(v.perm_id)
        return out

    def _recover(self, now: datetime, res: Dict[str, Any]) -> None:
        """PENDING_SUBMIT = the broker may or may not have the entry. Resolve
        ONLY by the entry's own order_ref; never resubmit."""
        cfg = self.config
        for o in self.journal.orders(["PENDING_SUBMIT"]):
            ref, cref = o["order_ref"], o["client_ref"]
            age = now - _parse(o["planned_at_utc"])
            if not ref:
                self._review(o, "PENDING_SUBMIT", now, "crash recovery: no order_ref recorded", res)
                continue
            lk = self.broker.lookup_order_ref(ref, o["planned_at_utc"])
            legs, owned, conflicts = classify_owned_orders(o, lk.orders, account=self._account)
            att = attribute_fills(o, lk.fills, account=self._account,
                                  claims=self.journal.exec_claims())
            conflicts = conflicts + att.conflicts
            if conflicts:
                self._review(o, "PENDING_SUBMIT", now,
                             "crash recovery: identity conflict: " + "; ".join(conflicts), res)
                continue
            if owned or att.entry or att.exits:
                fields = self._leg_fields(legs)
                con = next((x.con_id for x in owned if x.con_id), None) or next(
                    (f.con_id for f in att.entry + att.exits if f.con_id), None)
                for leg, perm in att.learned_perms.items():
                    fields.setdefault({LEG_ENTRY: "broker_perm_id",
                                       LEG_FLATTEN: "flatten_perm_id"}[leg], perm)
                self.journal.transition(cref, "PENDING_SUBMIT", "SUBMITTED", now,
                                        submit_status="recovered_after_crash",
                                        submitted_at_utc=o["planned_at_utc"], con_id=con,
                                        note="adopted: broker shows orders/executions with this "
                                             "order_ref", **fields)
                res["recovered"].append({"client_ref": cref, "resolution": "adopted"})
                continue
            if lk.complete and age >= timedelta(minutes=cfg.recovery_absence_grace_minutes):
                n = int(o["absence_checks"] or 0) + 1
                if n >= cfg.recovery_absence_confirmations:
                    self.journal.transition(
                        cref, "PENDING_SUBMIT", "NOT_SUBMITTED", now, absence_checks=n,
                        last_lookup_at_utc=_iso(now), last_lookup_note="complete:absent",
                        note="crash recovery: %d consecutive complete broker lookups show no "
                             "order or execution with this order_ref" % n)
                    res["recovered"].append({"client_ref": cref, "resolution": "not_submitted"})
                else:
                    self.journal.update_fields(cref, "PENDING_SUBMIT", now, absence_checks=n,
                                               last_lookup_at_utc=_iso(now),
                                               last_lookup_note="complete:absent")
                    res["recovered"].append({"client_ref": cref, "resolution": "absence_check_%d" % n})
                continue
            # incomplete view (or still inside the grace period): absence proves nothing.
            note = "incomplete:" + ",".join(lk.reasons) if not lk.complete else "within_grace"
            if age >= timedelta(minutes=cfg.pending_review_after_minutes):
                self._review(o, "PENDING_SUBMIT", now,
                             "crash recovery: fate of the entry cannot be proven (%s)" % note, res)
                continue
            fields = {"last_lookup_at_utc": _iso(now), "last_lookup_note": note[:200]}
            if not lk.complete:
                fields["absence_checks"] = 0          # confirmations must be consecutive
            self.journal.update_fields(cref, "PENDING_SUBMIT", now, **fields)
            res["recovered"].append({"client_ref": cref, "resolution": "unresolved:" + note[:80]})

    def _capture(self, now: datetime, res: Dict[str, Any]) -> None:
        live = self.journal.orders(["SUBMITTED", "OPEN"])
        if not live:
            return
        fills = self.broker.fills(min(o["planned_at_utc"] for o in live))
        claims = self.journal.exec_claims()
        for o in live:
            ref = o["client_ref"]
            att = attribute_fills(o, fills, account=self._account, claims=claims)
            if att.conflicts:
                self._review(o, o["state"], now, "identity conflict: " + "; ".join(att.conflicts), res)
                continue
            qty = float(o["qty"])
            if o["state"] == "SUBMITTED":
                if att.entry_qty < qty - 1e-9:
                    self._entry_not_filled(o, att, now, res)
                    continue
                vwap, t_last, fees = _vwap(att.entry)
                fields = {"entry_fill_price": vwap, "entry_filled_at_utc": t_last, "entry_fees": fees}
                for leg, perm in att.learned_perms.items():
                    col = {LEG_ENTRY: "broker_perm_id", LEG_FLATTEN: "flatten_perm_id"}[leg]
                    if not o.get(col):
                        fields[col] = perm
                if not o.get("con_id"):
                    fields["con_id"] = next((f.con_id for f in att.entry if f.con_id), None)
                self.journal.transition(ref, "SUBMITTED", "OPEN", now,
                                        claims=[(f.exec_id, LEG_ENTRY) for f in att.entry], **fields)
                res["opened"].append(ref)
                o = self.journal.get_order(ref)
            self._capture_open(o, att, now, res)

    def _entry_not_filled(self, o: Dict[str, Any], att, now: datetime, res: Dict[str, Any]) -> None:
        """SUBMITTED without a complete entry fill. Only a complete broker
        lookup showing the entry order dead with no execution proves REJECTED."""
        started = _parse(o["submitted_at_utc"] or o["planned_at_utc"])
        if now - started <= timedelta(minutes=self.config.entry_fill_timeout_minutes):
            return
        if att.entry:
            return self._review(o, "SUBMITTED", now, "entry partially filled beyond timeout", res)
        lk = self.broker.lookup_order_ref(o["order_ref"], o["planned_at_utc"])
        legs, owned, conflicts = classify_owned_orders(o, lk.orders, account=self._account)
        if conflicts:
            return self._review(o, "SUBMITTED", now, "identity conflict: " + "; ".join(conflicts), res)
        ent = legs.get(LEG_ENTRY)
        if lk.complete and ent is not None and ent.status in DEAD_STATUSES and not lk.fills and \
                not any(x.status in WORKING_STATUSES for x in owned):
            self.journal.transition(o["client_ref"], "SUBMITTED", "REJECTED", now,
                                    note="entry order %s at broker with no execution" % ent.status)
            return
        why = ("entry still working beyond timeout" if ent is not None and ent.status in WORKING_STATUSES
               else "entry fate cannot be proven (%s)" % ("complete lookup: no trace" if lk.complete
                                                           else "incomplete broker view"))
        self._review(o, "SUBMITTED", now, why, res)

    def _capture_open(self, o: Dict[str, Any], att, now: datetime, res: Dict[str, Any]) -> None:
        ref, qty = o["client_ref"], float(o["qty"])
        exits = [f for f in att.exits if _parse(f.time_utc) >= _parse(o["entry_filled_at_utc"])]
        exit_qty = sum(f.qty for f in exits)
        if exit_qty >= qty - 1e-9:
            return self._close(o, exits, att.legs, now, res)
        if o.get("close_state"):
            # a close may exist at the broker: reconcile it, NEVER send another
            return self._reconcile_close(o, exit_qty, now, res)
        entry_t = _parse(o["entry_filled_at_utc"])
        if now - entry_t >= timedelta(minutes=self.config.max_hold_minutes):
            return self._time_stop(o, qty - exit_qty, now, res)
        if exit_qty == 0:
            pos = self._contract_position(o)
            if pos is not None and pos == 0:
                self._review(o, "OPEN", now, "contract position is flat without an exit execution", res)

    def _contract_position(self, o: Dict[str, Any]) -> Optional[float]:
        """Signed broker position on this trade's contract, or None if the
        contract is unknown (then nothing is concluded from positions)."""
        con = o.get("con_id")
        if not con:
            return None
        return sum(p.qty for p in self.broker.positions()
                   if str(p.con_id) == str(con) and (not p.account or not self._account
                                                     or p.account == self._account))

    def _time_stop(self, o: Dict[str, Any], open_qty: float, now: datetime,
                   res: Dict[str, Any], recheck: bool = True) -> None:
        """Close ONLY this trade's own quantity, after proving ownership."""
        ref = o["client_ref"]
        signed = open_qty if o["direction"] == "long" else -open_qty
        if not o.get("con_id"):
            return self._review(o, "OPEN", now, "time stop: contract id unknown; cannot prove "
                                                "the position is the loop's", res)
        pos = self._contract_position(o)
        if pos is None or abs(pos - signed) > 1e-9:
            if recheck:
                # a bracket leg may have filled since the fills were read: re-read our own
                # executions before concluding the position is not ours.
                att = attribute_fills(o, self.broker.fills(o["entry_filled_at_utc"]),
                                      account=self._account, claims=self.journal.exec_claims())
                if not att.conflicts and att.exit_qty > float(o["qty"]) - open_qty + 1e-9:
                    if att.exit_qty >= float(o["qty"]) - 1e-9:
                        return self._capture_open(o, att, now, res)
                    return self._time_stop(o, float(o["qty"]) - att.exit_qty, now, res,
                                           recheck=False)
            return self._review(o, "OPEN", now, "time stop refused: broker position %s on the "
                                                "contract is not the loop's own %s" % (pos, signed), res)
        perms = set(leg_perm_ids(o))
        on_contract = [x for x in self.broker.open_orders()
                       if str(x.con_id or "") == str(o["con_id"]) or
                       (not x.con_id and x.symbol == o["symbol"])]
        foreign = foreign_orders(on_contract, {o["order_ref"], flatten_ref(o["order_ref"])}, perms)
        if foreign:
            return self._review(o, "OPEN", now, "time stop refused: %d foreign working order(s) on "
                                                "the contract" % len(foreign), res)
        trade = OwnedTrade(client_ref=ref, order_ref=o["order_ref"], symbol=o["symbol"],
                           con_id=o["con_id"], account=o.get("account") or self._account,
                           direction=o["direction"], open_qty=open_qty,
                           leg_perm_ids=tuple(sorted(perms)),
                           exit_leg_perm_ids=tuple(p for p in (o.get("tp_perm_id"), o.get("sl_perm_id"))
                                                   if p))
        # DURABLE CLOSE INTENT before the broker is called: from here on a close
        # may exist at the broker, so this trade is never sent another close
        # unless the broker PROVES none was placed.
        self.journal.update_fields(ref, "OPEN", now, close_state="INTENT",
                                   time_stop_requested_at_utc=_iso(now),
                                   close_attempts=int(o.get("close_attempts") or 0) + 1)
        try:
            flat = self.broker.flatten_owned(trade)
        except Exception as exc:
            res["errors"].append("time_stop_close_outcome_unknown:%s:%s"
                                 % (o["symbol"], type(exc).__name__))
            return                                   # intent stays; reconciled by order_ref
        ids = {}
        if flat.close_order_id:
            ids["flatten_order_id"] = flat.close_order_id
        if flat.close_perm_id:
            ids["flatten_perm_id"] = flat.close_perm_id
        if flat.close_placed is False or (flat.refused and flat.close_placed is not True):
            # the broker proves no close order was placed (a refusal takes no
            # broker action by contract): clear the intent
            self.journal.update_fields(ref, "OPEN", now, close_state=None,
                                       time_stop_requested_at_utc=None, **ids)
            if flat.refused:
                return self._review(o, "OPEN", now, "time stop refused by broker: %s"
                                    % flat.detail.get("reason", "ownership not proven"), res)
            res["errors"].append("time_stop_not_placed:%s:%s"
                                 % (o["symbol"], flat.detail.get("reason", "")))
            return
        state = "CONFIRMED" if flat.confirmed else ("SUBMITTED" if flat.close_placed else "INTENT")
        self.journal.update_fields(ref, "OPEN", now, close_state=state, **ids)
        if not flat.confirmed:
            res["errors"].append("time_stop_close_unconfirmed:%s:%s"
                                 % (o["symbol"], flat.detail.get("reason", "")))
            return                                   # reconciled on later ticks, never resent
        o = self.journal.get_order(ref)
        att = attribute_fills(o, self.broker.fills(o["entry_filled_at_utc"]),
                              account=self._account, claims=self.journal.exec_claims())
        if att.conflicts:
            return self._review(o, "OPEN", now, "identity conflict after time stop: "
                                + "; ".join(att.conflicts), res)
        self._capture_open(o, att, now, res)

    def _reconcile_close(self, o: Dict[str, Any], exit_qty: float, now: datetime,
                         res: Dict[str, Any]) -> None:
        """A time-stop close intent exists. Look for the ``order_ref:FLAT``
        order and executions across open orders, completed orders and
        executions. Close on the executions; learn the close's ids; never send
        another close; anything unresolved goes to NEEDS_REVIEW."""
        ref = o["client_ref"]
        lk = self.broker.lookup_order_ref(o["order_ref"], o["planned_at_utc"])
        legs, _owned, conflicts = classify_owned_orders(o, lk.orders, account=self._account)
        att = attribute_fills(o, lk.fills, account=self._account,
                              claims=self.journal.exec_claims())
        conflicts = conflicts + att.conflicts
        if conflicts:
            return self._review(o, "OPEN", now, "identity conflict while reconciling the time-stop "
                                                "close: " + "; ".join(conflicts), res)
        close = legs.get(LEG_FLATTEN)
        ids = {}
        if close is not None:
            if close.order_id and not o.get("flatten_order_id"):
                ids["flatten_order_id"] = close.order_id
            if close.perm_id and not o.get("flatten_perm_id"):
                ids["flatten_perm_id"] = close.perm_id
            if o.get("close_state") == "INTENT":
                ids["close_state"] = "SUBMITTED"
        if ids:
            self.journal.update_fields(ref, "OPEN", now, **ids)
            o = self.journal.get_order(ref)
        exits = [f for f in att.exits if _parse(f.time_utc) >= _parse(o["entry_filled_at_utc"])]
        if sum(f.qty for f in exits) >= float(o["qty"]) - 1e-9:
            return self._close(o, exits, att.legs, now, res)
        age = now - _parse(o["time_stop_requested_at_utc"])
        if close is not None and close.status in DEAD_STATUSES and \
                not any(att.legs.get(f.exec_id) == LEG_FLATTEN for f in exits):
            return self._review(o, "OPEN", now, "time-stop close order %s without an execution; "
                                                "not resent" % close.status, res)
        if age <= timedelta(minutes=self.config.close_resolution_minutes):
            res["errors"].append("time_stop_close_pending:%s:%s"
                                 % (o["symbol"], close.status if close is not None else
                                    ("no_close_order_seen" + ("" if lk.complete else ":incomplete"))))
            return
        what = ("close order %s" % close.status if close is not None else
                "no close order or execution found%s" % ("" if lk.complete else " (incomplete view)"))
        self._review(o, "OPEN", now, "time-stop close unresolved after %d min: %s; not resent"
                     % (self.config.close_resolution_minutes, what), res)

    def _close(self, o: Dict[str, Any], exits, legs: Dict[str, str], now: datetime,
               res: Dict[str, Any]) -> None:
        ref = o["client_ref"]
        vwap, t_last, fees = _vwap(exits)
        reason = _exit_reason(o, vwap, [legs[f.exec_id] for f in exits])
        out = self.journal.close_with_outcome(
            ref, exit_price=vwap, exit_filled_at_utc=t_last, exit_reason=reason, exit_fees=fees,
            now=now, claims=[(f.exec_id, legs[f.exec_id]) for f in exits])
        res["closed"].append({"client_ref": ref, "exit_reason": reason,
                              "realized_pnl": out["realized_pnl"],
                              "fees_final": out["fees_final"]})

    def _finalize_fees(self, now: datetime, res: Dict[str, Any]) -> None:
        """Closed trades whose broker commissions were not all reported yet:
        once EVERY claimed execution shows a reported commission, write the
        proven net P&L. Until then realized_pnl stays NULL (D3 ignores it)."""
        since = _iso(now - timedelta(hours=self.config.fee_finalization_hours))
        pending = self.journal.unfinalized_outcomes(since)
        if not pending:
            return
        fills = {f.exec_id: f for f in self.broker.fills(min(p["entry_filled_at_utc"] for p in pending))}
        for p in pending:
            claims = self.journal.claims_for(p["trade_id"])
            got = [fills.get(x) for x in claims]
            if not claims or any(f is None or f.commission is None for f in got):
                continue                                  # still unknown: stays unfinalized
            entry = [fills[x] for x, leg in claims.items() if leg == LEG_ENTRY]
            exits = [fills[x] for x, leg in claims.items() if leg != LEG_ENTRY]
            if not entry or not exits:
                continue
            self.journal.finalize_fees(p["trade_id"], entry_fees=_fees(entry),
                                       exit_fees=_fees(exits), now=now)
            res["fees_finalized"].append(p["trade_id"])

    # ── entry ──
    def _quote_block(self, cand, now: datetime) -> Tuple[Optional[str], Optional[float]]:
        """Fresh live quote, enforced. Returns (block_reason, price)."""
        cfg = self.config
        quote_fn = getattr(self.broker, "quote", None)
        if not callable(quote_fn):
            return "quote_unsupported", None
        try:
            q = quote_fn(cand.symbol)
        except BrokerUnavailable:
            return "live_price_unavailable", None
        px = getattr(q, "price", None)
        if isinstance(px, bool) or not isinstance(px, (int, float)) or not math.isfinite(px) or px <= 0:
            return "live_price_unavailable", None
        if getattr(q, "symbol", cand.symbol) != cand.symbol:
            return "quote_symbol_mismatch", None
        try:
            as_of = _parse(q.as_of_utc)
        except (TypeError, ValueError):
            return "quote_time_unusable", None
        if as_of.tzinfo is None:
            return "quote_time_unusable", None
        age = (now - as_of).total_seconds()
        if age > cfg.max_quote_age_seconds or age < -5.0:
            return "quote_stale:%.0fs" % age, None
        if q.delayed and not cfg.allow_delayed_quotes:
            return "quote_delayed", None
        if cand.direction == "long" and not (cand.stop_loss < px < cand.take_profit):
            return "price_outside_bracket:%s" % cand.symbol, None
        if cand.direction == "short" and not (cand.take_profit < px < cand.stop_loss):
            return "price_outside_bracket:%s" % cand.symbol, None
        drift = abs(px - cand.entry_ref_price) / cand.entry_ref_price
        if drift > cfg.max_entry_drift_pct:
            return "entry_drift:%s:%.4f" % (cand.symbol, drift), None
        return None, float(px)

    def _entry(self, now: datetime, key: str, res: Dict[str, Any]) -> None:
        cfg = self.config

        def block(reason: str) -> None:
            res["entry_block"] = reason

        if res.get("_maintenance_incomplete"):
            return block("maintenance_incomplete")
        if self.journal.orders(["NEEDS_REVIEW"]):
            return block("journal_needs_review")
        if self.journal.orders(["PENDING_SUBMIT"]):
            return block("unresolved_pending_submit")
        try:
            if self.kill_switch_active() is not False:
                return block("kill_switch_active")
        except Exception as exc:
            return block("kill_switch_error:%s" % type(exc).__name__)
        for name, gate in (("d0", self.d0_gate), ("d1", self.d1_gate)):
            try:
                ok, why = gate()
            except Exception as exc:
                return block("%s_error:%s" % (name, type(exc).__name__))
            if ok is not True:
                return block("%s_blocked:%s" % (name, why))
        active = self.journal.orders(list(ACTIVE_STATES))
        if len(active) >= cfg.max_open_trades:
            return block("max_open_trades")
        if self.journal.count_cycles_with_entries_on(now.strftime("%Y-%m-%d")) >= cfg.max_entries_per_day:
            return block("daily_entry_cap")
        snap = res.get("_equity_snapshot")
        if snap is None:
            return block("equity_unavailable")
        if snap.net_liquidation < cfg.min_equity_usd:
            return block("equity_below_minimum")
        try:
            positions = self.broker.positions()
            orders = self.broker.open_orders()
        except BrokerUnavailable:
            return block("broker_state_unavailable")
        if unattributed_orders(orders):
            return block("unattributed_open_orders")
        busy = symbols_with_exposure(positions, orders) | {o["symbol"] for o in active}
        try:
            items = list(self.candidate_provider(now))
        except Exception as exc:
            return block("candidate_provider_error:%s" % type(exc).__name__)
        cand, notes = select_candidate(items, min_score=cfg.min_score, allow_short=cfg.allow_short,
                                       excluded_symbols=busy, now=now,
                                       max_signal_age_minutes=cfg.max_signal_age_minutes)
        res["candidate_notes"] = notes
        self._ledger_call("selection", res, items, cand, notes, now, key)
        if cand is None:
            return block("no_candidate")

        def reject(reason: str, decision_id: Optional[str] = None,
                   notional_usd: Optional[float] = None) -> None:
            self._ledger_call("risk", res, cand, passed=False, reason=reason, now=now,
                              cycle_key=key, decision_id=decision_id, notional_usd=notional_usd)
            block(reason)

        why, px = self._quote_block(cand, now)
        if why:
            return reject(why)
        res["quote"] = px
        notional = max(px, cand.entry_ref_price) * cfg.qty
        cap = min(cfg.max_notional_usd, cfg.max_notional_pct_of_equity * snap.net_liquidation)
        if notional > cap:
            return reject("notional_cap:%.2f>%.2f" % (notional, cap))
        try:
            allowed, why, decision_id = self.risk_gate(
                symbol=cand.symbol, side=cand.direction, notional_usd=notional,
                stop_loss=cand.stop_loss, take_profit=cand.take_profit)
        except Exception as exc:
            return reject("risk_gate_error:%s" % type(exc).__name__)
        if allowed is not True:
            return reject("risk:%s" % why, decision_id, notional)
        self._ledger_call("risk", res, cand, passed=True, reason=None, now=now, cycle_key=key,
                          decision_id=decision_id, notional_usd=notional)
        ref = "%s|%s" % (key, cand.symbol)
        order_ref = make_order_ref(self.journal.journal_id, ref)
        data_source = self.broker.data_source
        if self.ledger is not None:
            # MANDATORY audit point: with the ledger enabled, the intent must be
            # durably recorded before anything is planned or submitted.
            try:
                audited = self.ledger.intent(cand, client_ref=ref, order_ref=order_ref,
                                             data_source=data_source, now=now, cycle_key=key)
            except Exception as exc:
                res["errors"].append("ledger_intent:%s" % type(exc).__name__)
                audited = False
            if audited is not True:
                return block("ledger_audit_unavailable")
        candidate_id = (self.ledger.candidate_id(cand) if self.ledger is not None
                        else getattr(cand, "candidate_id", None))
        try:
            self.journal.plan_order(
                client_ref=ref, cycle_key=key, strategy_id=cand.strategy_id, symbol=cand.symbol,
                direction=cand.direction, qty=cfg.qty, entry_ref_price=cand.entry_ref_price,
                stop_loss=cand.stop_loss, take_profit=cand.take_profit, score=cand.score,
                data_source=data_source, risk_decision_id=decision_id,
                equity_at_entry=snap.net_liquidation, now=now, order_ref=order_ref,
                account=self._account, candidate_id=candidate_id)
        except sqlite3.IntegrityError:
            self._ledger_call("not_persisted", res, cand, "duplicate_or_symbol_busy", now, key)
            return block("duplicate_or_symbol_busy")
        req = BracketRequest(client_ref=ref, symbol=cand.symbol, direction=cand.direction,
                             qty=cfg.qty, entry_ref_price=cand.entry_ref_price,
                             stop_loss=cand.stop_loss, take_profit=cand.take_profit,
                             strategy_id=cand.strategy_id, order_ref=order_ref)
        try:
            ack = self.broker.submit_bracket(req)
        except Exception as exc:
            # Unknown whether the broker received it: leave PENDING_SUBMIT for
            # crash recovery to resolve by order_ref on later ticks.
            res["errors"].append("submit_exception:%s" % type(exc).__name__)
            res["entry"] = {"client_ref": ref, "state": "PENDING_SUBMIT", "order_ref": order_ref}
            return block("submit_outcome_unknown")
        if ack.accepted:
            if ack.order_ref and ack.order_ref != order_ref:
                self.journal.transition(ref, "PENDING_SUBMIT", "NEEDS_REVIEW", self._now(),
                                        note="broker acknowledged a different order_ref %r"
                                             % ack.order_ref)
                return block("ack_order_ref_mismatch")
            fields = self._leg_fields(ack.legs)
            if not fields.get("broker_order_id") and ack.broker_order_id:
                fields["broker_order_id"] = ack.broker_order_id
            if not fields.get("broker_perm_id") and ack.perm_id:
                fields["broker_perm_id"] = ack.perm_id
            self.journal.transition(ref, "PENDING_SUBMIT", "SUBMITTED", self._now(),
                                    submit_status=ack.status, submitted_at_utc=_iso(self._now()),
                                    con_id=ack.con_id, account=ack.account or self._account,
                                    **fields)
            res["entry"] = {"client_ref": ref, "state": "SUBMITTED", "order_ref": order_ref,
                            "candidate": asdict(cand)}
            self._ledger_call("broker_result", res, cand, accepted=True, status=str(ack.status),
                              now=now, cycle_key=key, data_source=data_source,
                              detail={"client_ref": ref, "order_ref": order_ref})
        else:
            self.journal.transition(ref, "PENDING_SUBMIT", "REJECTED", self._now(),
                                    submit_status=ack.status, note=ack.reason)
            self._ledger_call("broker_result", res, cand, accepted=False,
                              status=str(ack.status or "rejected"), now=now, cycle_key=key,
                              data_source=data_source,
                              detail={"client_ref": ref, "reason": ack.reason})
            return block("broker_rejected:%s" % ack.status)


def _exit_reason(order: Dict[str, Any], vwap: float, legs: List[str]) -> str:
    kinds = set(legs)
    if LEG_FLATTEN in kinds:
        return "time_stop"
    if len(kinds) == 1 and next(iter(kinds)) in (LEG_TP, LEG_SL):
        return next(iter(kinds))
    if order.get("time_stop_requested_at_utc"):
        return "time_stop"
    tp, sl = order["take_profit"], order["stop_loss"]
    if order["direction"] == "long":
        if vwap >= tp:
            return "take_profit"
        if vwap <= sl:
            return "stop_loss"
    else:
        if vwap <= tp:
            return "take_profit"
        if vwap >= sl:
            return "stop_loss"
    return "unclassified_exit"

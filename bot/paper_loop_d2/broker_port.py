"""M21.1extra-D2.3/D2.4 — broker port + trade-ownership rules for the
unattended paper learning loop.

The loop talks to a broker only through the ``LoopBroker`` surface (below).
Two implementations exist:

  * ``bot.paper_loop_d2.sim_broker.SimulatedPaperBroker`` — deterministic,
    in-process; every test and the CLI's default mode. ``data_source =
    'simulated'`` so D3 never counts its outcomes as genuine.
  * ``bot.paper_loop_d2.ibkr_adapter.IbkrLoopBroker`` — the IBKR paper
    gateway, contract-aware and account-scoped, with broker-visible order
    references (see that module).

OWNERSHIP (the core rule of this module). A broker order or execution belongs
to a learning-loop trade only through a DURABLE, BROKER-VISIBLE identity:

  * the trade's ``order_ref`` — a unique reference written to the journal
    BEFORE submission and set as IBKR ``orderRef`` on every leg (the time-stop
    close uses ``order_ref + FLATTEN_SUFFIX``); or
  * a broker ``permId`` the journal recorded for one of the trade's legs.

Symbol, side and time are NEVER sufficient. A record that matches an identity
but disagrees on anything else (account, contract id, symbol, the other
identity, side for its leg, or an execution already claimed by another trade)
is a CONFLICT: the trade goes to NEEDS_REVIEW and nothing is inferred from it.
Records without our identity are foreign and are never attributed, cancelled
or closed by the loop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

DATA_SOURCE_IBKR_PAPER = "ibkr_paper"
DATA_SOURCE_SIMULATED = "simulated"
ORDER_REF_PREFIX = "M21L"
FLATTEN_SUFFIX = ":FLAT"

LEG_ENTRY = "entry"
LEG_TP = "take_profit"
LEG_SL = "stop_loss"
LEG_FLATTEN = "flatten"
LEG_EXIT = "exit"                      # exit leg whose exact role is not known yet
EXIT_LEGS = (LEG_TP, LEG_SL, LEG_FLATTEN, LEG_EXIT)

WORKING_STATUSES = frozenset({"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted",
                              "PendingCancel"})
DEAD_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})


class BrokerUnavailable(RuntimeError):
    """The broker could not answer (gateway down, timeout, malformed data)."""


class PaperModeRefused(RuntimeError):
    """Refused to build a broker that is not provably paper."""


# ── value types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccountSnapshot:
    account: str
    net_liquidation: float
    cash: Optional[float]
    currency: str
    as_of_utc: str
    data_source: str


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: float            # signed: >0 long, <0 short
    avg_cost: Optional[float] = None
    con_id: Optional[str] = None
    account: Optional[str] = None


@dataclass(frozen=True)
class OrderView:
    order_id: Optional[str]
    symbol: Optional[str]          # None when the broker cannot attribute it to a contract
    side: Optional[str]
    qty: Optional[float]
    status: Optional[str] = None
    order_ref: Optional[str] = None
    perm_id: Optional[str] = None
    parent_id: Optional[str] = None
    con_id: Optional[str] = None
    account: Optional[str] = None
    order_type: Optional[str] = None   # 'MKT' | 'LMT' | 'STP' ...


@dataclass(frozen=True)
class FillView:
    exec_id: str
    symbol: str
    side: str             # 'BUY' | 'SELL'
    qty: float
    price: float
    time_utc: str
    order_id: Optional[str] = None
    perm_id: Optional[str] = None
    commission: Optional[float] = None  # None = not (yet) reported by the broker; 0.0 = proven zero
    order_kind: Optional[str] = None   # informational only (simulator); never used for ownership
    order_ref: Optional[str] = None
    account: Optional[str] = None
    con_id: Optional[str] = None


@dataclass(frozen=True)
class BracketRequest:
    client_ref: str
    symbol: str
    direction: str        # 'long' | 'short'
    qty: int
    entry_ref_price: float
    stop_loss: float
    take_profit: float
    strategy_id: str
    order_ref: str = ""   # broker-visible identity set on every leg


@dataclass(frozen=True)
class LegId:
    order_id: Optional[str] = None
    perm_id: Optional[str] = None


@dataclass(frozen=True)
class SubmitAck:
    accepted: bool
    status: str
    broker_order_id: Optional[str] = None
    perm_id: Optional[str] = None
    reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    order_ref: Optional[str] = None
    legs: Dict[str, LegId] = field(default_factory=dict)   # LEG_ENTRY / LEG_TP / LEG_SL
    con_id: Optional[str] = None
    account: Optional[str] = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Optional[float]
    as_of_utc: Optional[str]
    delayed: bool
    source: str
    bid: Optional[float] = None
    ask: Optional[float] = None


@dataclass(frozen=True)
class OrderLookup:
    """Everything the broker shows for one order reference. ``complete`` is
    True only when every source the broker has (open orders from all clients,
    completed orders, executions) was read successfully AND covers the time
    window in question. Absence of evidence is only meaningful when complete."""
    order_ref: str
    complete: bool
    orders: List[OrderView] = field(default_factory=list)
    fills: List[FillView] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class OwnedTrade:
    """What the broker needs to close ONLY this trade's own position."""
    client_ref: str
    order_ref: str
    symbol: str
    con_id: Optional[str]
    account: Optional[str]
    direction: str
    open_qty: float                    # unsigned quantity the loop itself holds
    leg_perm_ids: Tuple[str, ...] = ()
    exit_leg_perm_ids: Tuple[str, ...] = ()   # take-profit / stop-loss permIds

    def is_own_exit_leg(self, order_ref: Optional[str], perm_id: Optional[str],
                        side: Optional[str]) -> bool:
        if _s(perm_id) and _s(perm_id) in self.exit_leg_perm_ids:
            return True
        return _s(order_ref) == self.order_ref and side == exit_side(self.direction)

    @property
    def signed_qty(self) -> float:
        return self.open_qty if self.direction == "long" else -self.open_qty


@dataclass(frozen=True)
class FlattenResult:
    """Outcome of one close attempt.

    ``close_placed`` is the broker's statement about the closing order:
      * False — PROVEN not placed (every exit before ``placeOrder``); the
        caller may clear its close intent;
      * True  — placed (ids below when known), filled or not;
      * None  — unknown (``placeOrder`` raised or an unexpected error):
        the caller must treat the close as possibly live and never resend it.
    ``confirmed`` — placed, filled and the contract position proven flat."""
    confirmed: bool
    detail: Dict[str, Any] = field(default_factory=dict)
    refused: bool = False              # ownership could not be proven: no broker action taken
    close_order_ref: Optional[str] = None
    close_perm_id: Optional[str] = None
    close_placed: Optional[bool] = None
    close_order_id: Optional[str] = None


def entry_side(direction: str) -> str:
    return "BUY" if direction == "long" else "SELL"


def exit_side(direction: str) -> str:
    return "SELL" if direction == "long" else "BUY"


def make_order_ref(journal_id: str, client_ref: str) -> str:
    """Deterministic, journal-unique, broker-visible reference."""
    return "%s:%s:%s" % (ORDER_REF_PREFIX, journal_id, client_ref.replace("slot:", ""))


def flatten_ref(order_ref: str) -> str:
    return order_ref + FLATTEN_SUFFIX


def _finite_pos(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _finite_or_none(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s not in ("0", "None") else None


# ── ownership ───────────────────────────────────────────────────────────────

LEG_PERM_COLUMNS = ((LEG_ENTRY, "broker_perm_id"), (LEG_TP, "tp_perm_id"),
                    (LEG_SL, "sl_perm_id"), (LEG_FLATTEN, "flatten_perm_id"))


def leg_perm_ids(order: Mapping[str, Any]) -> Dict[str, str]:
    """permId -> leg for the legs the journal has recorded."""
    out: Dict[str, str] = {}
    for leg, col in LEG_PERM_COLUMNS:
        p = _s(order.get(col))
        if p:
            out[p] = leg
    return out


@dataclass
class Attribution:
    entry: List[FillView] = field(default_factory=list)
    exits: List[FillView] = field(default_factory=list)
    legs: Dict[str, str] = field(default_factory=dict)      # exec_id -> leg
    conflicts: List[str] = field(default_factory=list)
    learned_perms: Dict[str, str] = field(default_factory=dict)   # leg -> perm id

    @property
    def entry_qty(self) -> float:
        return sum(f.qty for f in self.entry)

    @property
    def exit_qty(self) -> float:
        return sum(f.qty for f in self.exits)


def _identity(rec: Any, ref: str, perms: Mapping[str, str]) -> Tuple[bool, bool, bool]:
    oref = _s(getattr(rec, "order_ref", None))
    perm = _s(getattr(rec, "perm_id", None))
    by_ref = oref in (ref, flatten_ref(ref)) if oref else False
    by_perm = perm in perms if perm else False
    return by_ref, by_perm, bool(oref)


def _common_conflicts(rec: Any, order: Mapping[str, Any], account: Optional[str],
                      by_ref: bool, by_perm: bool, has_ref: bool, what: str) -> List[str]:
    out = []
    rid = getattr(rec, "exec_id", None) or getattr(rec, "order_id", None)
    if by_perm and has_ref and not by_ref:
        out.append("%s %s: permId is ours but orderRef %r is not" % (what, rid, rec.order_ref))
    acct = _s(getattr(rec, "account", None))
    if acct and account and acct != account:
        out.append("%s %s: account %s is not %s" % (what, rid, acct, account))
    sym = getattr(rec, "symbol", None)
    if sym and sym != order["symbol"]:
        out.append("%s %s: symbol %s is not %s" % (what, rid, sym, order["symbol"]))
    con = _s(getattr(rec, "con_id", None))
    if con and _s(order.get("con_id")) and con != _s(order.get("con_id")):
        out.append("%s %s: contract %s is not %s" % (what, rid, con, order.get("con_id")))
    return out


def attribute_fills(order: Mapping[str, Any], fills: Iterable[FillView], *,
                    account: Optional[str], claims: Mapping[str, str]) -> Attribution:
    """Split broker executions into this trade's entry / exit fills by
    verified identity only. Foreign executions are ignored; inconsistent ones
    become conflicts."""
    att = Attribution()
    ref = order.get("order_ref")
    if not ref:
        att.conflicts.append("journal row has no order_ref")
        return att
    perms = leg_perm_ids(order)
    ent, ext = entry_side(order["direction"]), exit_side(order["direction"])
    for f in sorted(fills, key=lambda x: (x.time_utc, x.exec_id)):
        by_ref, by_perm, has_ref = _identity(f, ref, perms)
        if not (by_ref or by_perm):
            continue                                        # foreign: never ours
        bad = _common_conflicts(f, order, account, by_ref, by_perm, has_ref, "execution")
        owner = claims.get(f.exec_id)
        if owner is not None and owner != order["client_ref"]:
            bad.append("execution %s already attributed to %s" % (f.exec_id, owner))
        perm = _s(f.perm_id)
        if by_perm:
            leg = perms[perm]
        elif _s(f.order_ref) == flatten_ref(ref):
            leg = LEG_FLATTEN
        else:
            leg = LEG_ENTRY if f.side == ent else LEG_EXIT
        if by_ref and not by_perm and perm:
            # our orderRef, but a permId the journal has not recorded: only
            # acceptable while that leg's permId is still unknown.
            known = {p for p, lg in perms.items()
                     if lg == leg or (leg == LEG_EXIT and lg in (LEG_TP, LEG_SL))}
            if (leg != LEG_EXIT and known) or (leg == LEG_EXIT and len(known) == 2):
                bad.append("execution %s: orderRef is ours but permId %s is not the recorded "
                           "leg permId" % (f.exec_id, perm))
        want = ent if leg == LEG_ENTRY else ext
        if f.side != want:
            bad.append("execution %s: side %s does not fit leg %s" % (f.exec_id, f.side, leg))
        if bad:
            att.conflicts.extend(bad)
            continue
        att.legs[f.exec_id] = leg
        (att.entry if leg == LEG_ENTRY else att.exits).append(f)
        if perm and leg == LEG_ENTRY and LEG_ENTRY not in perms.values():
            att.learned_perms.setdefault(LEG_ENTRY, perm)
        if perm and leg == LEG_FLATTEN and LEG_FLATTEN not in perms.values():
            att.learned_perms.setdefault(LEG_FLATTEN, perm)
    try:
        qty = float(order["qty"])
    except (TypeError, ValueError, KeyError):
        qty = float("nan")
    if att.entry_qty > qty + 1e-9:
        att.conflicts.append("entry over-filled: %s > %s" % (att.entry_qty, qty))
    if att.exit_qty > att.entry_qty + 1e-9:
        att.conflicts.append("exit quantity %s exceeds entry %s" % (att.exit_qty, att.entry_qty))
    return att


def classify_owned_orders(order: Mapping[str, Any], orders: Iterable[OrderView], *,
                          account: Optional[str]) -> Tuple[Dict[str, OrderView], List[OrderView],
                                                           List[str]]:
    """Orders carrying this trade's identity -> (legs by role, all owned, conflicts).
    Roles: the leg on the entry side is the entry; on the exit side a LMT is
    the take-profit, a STP the stop-loss, the flatten reference the close."""
    ref = order.get("order_ref")
    perms = leg_perm_ids(order)
    ent = entry_side(order["direction"])
    legs: Dict[str, OrderView] = {}
    owned: List[OrderView] = []
    conflicts: List[str] = []
    if not ref:
        return legs, owned, ["journal row has no order_ref"]
    for o in orders:
        by_ref, by_perm, has_ref = _identity(o, ref, perms)
        if not (by_ref or by_perm):
            continue
        bad = _common_conflicts(o, order, account, by_ref, by_perm, has_ref, "order")
        if bad:
            conflicts.extend(bad)
            continue
        owned.append(o)
        perm = _s(o.perm_id)
        if by_perm:
            role = perms[perm]
        elif _s(o.order_ref) == flatten_ref(ref):
            role = LEG_FLATTEN
        elif o.side == ent:
            role = LEG_ENTRY
        elif (o.order_type or "").upper().startswith("LMT"):
            role = LEG_TP
        elif (o.order_type or "").upper().startswith("STP"):
            role = LEG_SL
        else:
            conflicts.append("order %s: cannot tell which leg it is" % o.order_id)
            continue
        if role in legs and _s(legs[role].perm_id) != perm:
            conflicts.append("two broker orders claim leg %s" % role)
            continue
        legs[role] = o
    return legs, owned, conflicts


def foreign_orders(orders: Iterable[OrderView], owned_refs: Set[str],
                   owned_perms: Set[str]) -> List[OrderView]:
    return [o for o in orders
            if not (_s(o.order_ref) in owned_refs or _s(o.perm_id) in owned_perms)]


# Structural type: account_snapshot, positions, open_orders, fills,
# lookup_order_ref, submit_bracket, quote, flatten_owned (+ data_source,
# account). There is deliberately no symbol-wide flatten in this surface.
LoopBroker = Any


def symbols_with_exposure(positions: Sequence[PositionView], orders: Sequence[OrderView]) -> set:
    return {p.symbol for p in positions if p.qty != 0} | {o.symbol for o in orders if o.symbol}


def unattributed_orders(orders: Sequence[OrderView]) -> List[OrderView]:
    return [o for o in orders if not o.symbol]

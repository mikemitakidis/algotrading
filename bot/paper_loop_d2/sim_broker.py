"""M21.1extra-D2.3/D2.4 — deterministic simulated paper broker (no network).

Implements the ``LoopBroker`` surface for tests and for the scheduler CLI's
default mode, with the same identity model as the IBKR adapter: every order
and execution carries ``order_ref``, ``perm_id``, ``con_id`` and ``account``.
A bracket entry fills at market; take-profit (limit) and stop-loss (stop) form
one OCA group. Prices come from an injected ``price_fn(symbol, now)``.

It can also model what the real account can contain and the real gateway can
do: MANUAL trades and orders on the same symbols (no ``order_ref``; never the
loop's), incomplete broker reads, a lost submit acknowledgement, cancelled
entries and missing / stale / delayed quotes.

``state_path`` persists all state as JSON after every mutation, so tests can
"restart" the loop while the broker keeps its orders and positions.
Outcomes carry ``data_source='simulated'``: D3 never counts them as genuine.
"""
from __future__ import annotations

import json
import os
import tempfile
import zlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from bot.paper_loop_d2.broker_port import (
    DATA_SOURCE_SIMULATED, LEG_ENTRY, LEG_SL, LEG_TP, AccountSnapshot, BracketRequest,
    BrokerUnavailable, FillView, FlattenResult, LegId, OrderLookup, OrderView, OwnedTrade,
    PositionView, Quote, SubmitAck, entry_side, exit_side, flatten_ref,
)

PriceFn = Callable[[str, datetime], float]
WORKING = "Submitted"


def sim_con_id(symbol: str) -> str:
    return str(100000 + zlib.crc32(symbol.encode()) % 900000)


class SimulatedPaperBroker:
    data_source = DATA_SOURCE_SIMULATED

    def __init__(self, *, price_fn: PriceFn, clock: Callable[[], datetime],
                 account: str = "SIM-PAPER", starting_cash: float = 100000.0,
                 commission_per_share: float = 0.005, min_commission: float = 1.0,
                 state_path: Optional[str] = None):
        self._price_fn = price_fn
        self._clock = clock
        self.account = account
        self._cps = commission_per_share
        self._min_comm = min_commission
        self._state_path = state_path
        self.fail_always: Dict[str, Exception] = {}     # method -> exception
        self.fail_once: Dict[str, Exception] = {}
        self.reject_next_submit: Optional[str] = None
        self.cancel_entry_next_submit = False            # broker accepts, then cancels the entry
        self.lose_ack_next_submit = False                # orders placed, then the call fails
        self.incomplete_lookup = False                   # lookup_order_ref cannot see everything
        self.quote_override: Optional[Callable[[str, datetime], Quote]] = None
        self.calls: List[str] = []
        self.cancelled: List[str] = []                   # perm ids cancelled by flatten_owned
        self._s: Dict[str, Any] = {"cash": float(starting_cash), "positions": {},
                                   "orders": [], "fills": [], "next_id": 1}
        if state_path and os.path.exists(state_path):
            with open(state_path) as fh:
                self._s = json.load(fh)

    # ── helpers ──
    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_once:
            raise self.fail_once.pop(name)
        if name in self.fail_always:
            raise self.fail_always[name]

    def _id(self) -> str:
        i = self._s["next_id"]
        self._s["next_id"] = i + 1
        return str(i)

    def _save(self) -> None:
        if not self._state_path:
            return
        d = os.path.dirname(self._state_path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".simbroker_")
        with os.fdopen(fd, "w") as fh:
            json.dump(self._s, fh)
        os.replace(tmp, self._state_path)

    def _commission(self, qty: float) -> float:
        return max(self._min_comm, abs(qty) * self._cps)

    def _order(self, *, symbol, side, qty, order_type, kind, order_ref=None, parent_perm=None,
               limit=None, stop=None, group=None, status=WORKING) -> Dict[str, Any]:
        oid = self._id()
        o = {"order_id": oid, "perm_id": "P%s" % oid, "symbol": symbol, "con_id": sim_con_id(symbol),
             "side": side, "qty": float(qty), "order_type": order_type, "kind": kind,
             "order_ref": order_ref, "parent_perm": parent_perm, "limit": limit, "stop": stop,
             "group": group, "status": status, "account": self.account}
        self._s["orders"].append(o)
        return o

    def _fill(self, order: Dict[str, Any], price: float) -> None:
        now = self._now().isoformat()
        qty = order["qty"]
        comm = self._commission(qty)
        self._s["fills"].append({
            "exec_id": "sim-%s" % self._id(), "symbol": order["symbol"], "side": order["side"],
            "qty": float(qty), "price": float(price), "time_utc": now,
            "order_id": order["order_id"], "perm_id": order["perm_id"], "commission": comm,
            "order_kind": order["kind"], "order_ref": order["order_ref"],
            "account": self.account, "con_id": order["con_id"]})
        order["status"] = "Filled"
        sym = order["symbol"]
        signed = qty if order["side"] == "BUY" else -qty
        pos = self._s["positions"].get(sym, {"qty": 0.0, "avg_cost": None})
        new_qty = pos["qty"] + signed
        if new_qty == 0:
            self._s["positions"].pop(sym, None)
        else:
            avg = price if pos["qty"] == 0 else pos["avg_cost"]
            self._s["positions"][sym] = {"qty": new_qty, "avg_cost": avg}
        self._s["cash"] -= signed * price + comm

    def _process(self) -> None:
        """Evaluate working bracket legs (OCA) against the current price."""
        now = self._now()
        changed = False
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for o in self._s["orders"]:
            if o["group"] and o["kind"] in ("take_profit", "stop_loss") and o["status"] == WORKING:
                groups.setdefault(o["group"], []).append(o)
        for legs in groups.values():
            px = float(self._price_fn(legs[0]["symbol"], now))
            sl = next((o for o in legs if o["kind"] == "stop_loss"), None)
            tp = next((o for o in legs if o["kind"] == "take_profit"), None)
            long_exit = legs[0]["side"] == "SELL"
            hit = None
            if sl and (px <= sl["stop"] if long_exit else px >= sl["stop"]):
                hit, price = sl, px                       # stop -> fills at market (gap-aware)
            elif tp and (px >= tp["limit"] if long_exit else px <= tp["limit"]):
                hit, price = tp, tp["limit"]              # limit -> fills at the limit
            if hit is not None:
                self._fill(hit, price)
                for o in legs:
                    if o is not hit and o["status"] == WORKING:
                        o["status"] = "Cancelled"         # OCA
                changed = True
        if changed:
            self._save()

    def _view(self, o: Dict[str, Any]) -> OrderView:
        return OrderView(order_id=o["order_id"], symbol=o["symbol"], side=o["side"], qty=o["qty"],
                         status=o["status"], order_ref=o["order_ref"], perm_id=o["perm_id"],
                         parent_id=o["parent_perm"], con_id=o["con_id"], account=o["account"],
                         order_type=o["order_type"])

    # ── LoopBroker surface ──
    def account_snapshot(self) -> AccountSnapshot:
        self._maybe_fail("account_snapshot")
        self._process()
        now = self._now()
        mtm = sum(p["qty"] * float(self._price_fn(sym, now))
                  for sym, p in self._s["positions"].items())
        return AccountSnapshot(account=self.account, net_liquidation=self._s["cash"] + mtm,
                               cash=self._s["cash"], currency="USD", as_of_utc=now.isoformat(),
                               data_source=DATA_SOURCE_SIMULATED)

    def positions(self) -> List[PositionView]:
        self._maybe_fail("positions")
        self._process()
        return [PositionView(symbol=s, qty=p["qty"], avg_cost=p["avg_cost"], con_id=sim_con_id(s),
                             account=self.account)
                for s, p in sorted(self._s["positions"].items())]

    def open_orders(self) -> List[OrderView]:
        self._maybe_fail("open_orders")
        self._process()
        return [self._view(o) for o in self._s["orders"] if o["status"] == WORKING]

    def fills(self, since_utc: str) -> List[FillView]:
        self._maybe_fail("fills")
        self._process()
        since = datetime.fromisoformat(since_utc)
        return [FillView(**f) for f in self._s["fills"]
                if datetime.fromisoformat(f["time_utc"]) >= since]

    def lookup_order_ref(self, order_ref: str, since_utc: str) -> OrderLookup:
        self._maybe_fail("lookup_order_ref")
        self._process()
        if self.incomplete_lookup:
            return OrderLookup(order_ref=order_ref, complete=False,
                               reasons=["simulated: completed-orders source unavailable"])
        refs = (order_ref, flatten_ref(order_ref))
        since = datetime.fromisoformat(since_utc)
        return OrderLookup(
            order_ref=order_ref, complete=True,
            orders=[self._view(o) for o in self._s["orders"] if o["order_ref"] in refs],
            fills=[FillView(**f) for f in self._s["fills"] if f["order_ref"] in refs
                   and datetime.fromisoformat(f["time_utc"]) >= since])

    def submit_bracket(self, req: BracketRequest) -> SubmitAck:
        self._maybe_fail("submit_bracket")
        if not req.order_ref:
            return SubmitAck(accepted=False, status="rejected", reason="order_ref required")
        if self.reject_next_submit:
            reason, self.reject_next_submit = self.reject_next_submit, None
            return SubmitAck(accepted=False, status="rejected", reason=reason)
        self._process()
        px = float(self._price_fn(req.symbol, self._now()))
        group = "G%s" % self._id()
        ent = self._order(symbol=req.symbol, side=entry_side(req.direction), qty=req.qty,
                          order_type="MKT", kind="entry", order_ref=req.order_ref, group=group)
        xs = exit_side(req.direction)
        tp = self._order(symbol=req.symbol, side=xs, qty=req.qty, order_type="LMT",
                         kind="take_profit", order_ref=req.order_ref, parent_perm=ent["perm_id"],
                         limit=req.take_profit, group=group)
        sl = self._order(symbol=req.symbol, side=xs, qty=req.qty, order_type="STP",
                         kind="stop_loss", order_ref=req.order_ref, parent_perm=ent["perm_id"],
                         stop=req.stop_loss, group=group)
        if self.cancel_entry_next_submit:
            self.cancel_entry_next_submit = False
            for o in (ent, tp, sl):
                o["status"] = "Cancelled"
        else:
            self._fill(ent, px)
        self._save()
        if self.lose_ack_next_submit:
            self.lose_ack_next_submit = False
            raise TimeoutError("simulated: acknowledgement lost after the broker accepted")
        legs = {LEG_ENTRY: LegId(ent["order_id"], ent["perm_id"]),
                LEG_TP: LegId(tp["order_id"], tp["perm_id"]),
                LEG_SL: LegId(sl["order_id"], sl["perm_id"])}
        return SubmitAck(accepted=True, status="accepted", broker_order_id=ent["order_id"],
                         perm_id=ent["perm_id"], order_ref=req.order_ref, legs=legs,
                         con_id=ent["con_id"], account=self.account,
                         raw={"client_ref": req.client_ref})

    def quote(self, symbol: str) -> Quote:
        self._maybe_fail("quote")
        now = self._now()
        if self.quote_override is not None:
            return self.quote_override(symbol, now)
        return Quote(symbol=symbol, price=float(self._price_fn(symbol, now)),
                     as_of_utc=now.isoformat(), delayed=False, source="simulated")

    def flatten_owned(self, trade: OwnedTrade) -> FlattenResult:
        """Close ONLY the loop's own quantity, after proving it is the loop's:
        the contract position must equal the trade's own open quantity and no
        foreign working order may exist on the contract. Cancels only this
        trade's legs. Otherwise refuses without touching anything."""
        self._maybe_fail("flatten_owned")
        self._process()
        refs = {trade.order_ref, flatten_ref(trade.order_ref)}
        perms = set(trade.leg_perm_ids)
        mine = lambda o: o["order_ref"] in refs or o["perm_id"] in perms   # noqa: E731
        if any(trade.is_own_exit_leg(o["order_ref"], o["perm_id"], o["side"])
               and o["status"] == "Filled" for o in self._s["orders"]):
            return FlattenResult(confirmed=False, detail={"reason": "exit_leg_filled"})
        con = sim_con_id(trade.symbol)
        if trade.con_id and trade.con_id != con:
            return FlattenResult(confirmed=False, refused=True,
                                 detail={"reason": "contract_mismatch"})
        pos = self._s["positions"].get(trade.symbol, {"qty": 0.0})["qty"]
        if abs(pos - trade.signed_qty) > 1e-9:
            return FlattenResult(confirmed=False, refused=True,
                                 detail={"reason": "position_not_owned", "broker_qty": pos,
                                         "loop_qty": trade.signed_qty})
        working = [o for o in self._s["orders"] if o["symbol"] == trade.symbol
                   and o["status"] == WORKING]
        foreign = [o["order_id"] for o in working if not mine(o)]
        if foreign:
            return FlattenResult(confirmed=False, refused=True,
                                 detail={"reason": "foreign_orders_on_contract",
                                         "foreign_order_ids": foreign})
        for o in working:                                   # only ours remain
            o["status"] = "Cancelled"
            self.cancelled.append(o["perm_id"])
        close = self._order(symbol=trade.symbol, side=exit_side(trade.direction),
                            qty=trade.open_qty, order_type="MKT", kind="flatten",
                            order_ref=flatten_ref(trade.order_ref))
        self._fill(close, float(self._price_fn(trade.symbol, self._now())))
        self._save()
        flat = trade.symbol not in self._s["positions"]
        return FlattenResult(confirmed=flat, detail={"symbol": trade.symbol},
                             close_order_ref=close["order_ref"], close_perm_id=close["perm_id"])

    # ── foreign (manual) activity on the same account ──
    def manual_fill(self, symbol: str, side: str, qty: float, price: Optional[float] = None) -> str:
        o = self._order(symbol=symbol, side=side, qty=qty, order_type="MKT", kind="manual")
        self._fill(o, float(price if price is not None else self._price_fn(symbol, self._now())))
        self._save()
        return o["perm_id"]

    def manual_order(self, symbol: str, side: str, qty: float, *, order_type: str = "LMT",
                     price: float = 1.0) -> str:
        o = self._order(symbol=symbol, side=side, qty=qty, order_type=order_type, kind="manual",
                        limit=price if order_type == "LMT" else None,
                        stop=price if order_type == "STP" else None)
        self._save()
        return o["perm_id"]

    # test conveniences
    def raw_state(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._s))

    def order_by_perm(self, perm_id: str) -> Optional[Dict[str, Any]]:
        return next((o for o in self._s["orders"] if o["perm_id"] == perm_id), None)


def broker_unavailable(msg: str = "simulated outage") -> BrokerUnavailable:
    return BrokerUnavailable(msg)

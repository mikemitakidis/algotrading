"""M21.1extra-D2.3/D2.4 — IBKR paper adapter for the unattended learning loop.

Why a loop-specific adapter (instead of ``IBKRBroker.submit`` / ``reconcile``
/ ``flatten_paper_position``): the loop must prove which broker orders,
executions and positions are ITS OWN. The existing path cannot:

  * ``IBKRBroker.submit`` sets no broker-visible reference and returns only
    the parent ``IB-PERM-…`` id (no take-profit / stop-loss leg identities);
  * ``IBKRBroker.reconcile`` returns this client's open orders without
    contract, account, orderRef or permId, and positions by bare symbol;
  * ``IBKRBroker.flatten_paper_position`` closes the WHOLE symbol position and
    cancels EVERY order on the symbol, including manual ones.

This adapter keeps the existing safety checks and order construction —
the same live-mode refusal, paper-port and paper-account assertions, kill
switch, runtime broker-allocation policy, gateway probe, connected-account
verification, contract qualification and ``IBKRBroker._make_bracket`` (market
DAY parent, GTC take-profit limit, GTC stop) — and adds:

  * ``orderRef = order_ref`` on all three bracket legs (the time-stop close
    uses ``order_ref + ':FLAT'``), so every order and execution the loop
    creates is identifiable at the broker, across client ids and restarts;
  * leg identities (orderId + permId of entry, take-profit, stop-loss), the
    qualified contract id and the account in the acknowledgement;
  * account-scoped, contract-aware reads: positions by ``conId``, open orders
    from ALL clients (``reqAllOpenOrders`` + ``openTrades``), completed
    orders, and executions filtered to the paper account with ``orderRef``;
  * ``lookup_order_ref`` that reports ``complete=False`` whenever any source
    failed or the execution history may not cover the window;
  * a live quote (``reqTickers``) with its market-data type and time;
  * ``flatten_owned``: closes only the trade's own quantity after proving the
    contract position equals it and no foreign order works on the contract;
    cancels only the trade's own legs. Otherwise it refuses and does nothing.

The submit acknowledgement is decided by the parent order's broker status:
dead (Inactive / Cancelled) -> rejected; working or filled -> accepted;
anything else after placement -> the call raises, the entry stays
PENDING_SUBMIT and crash recovery resolves it by orderRef (never resubmits).

Everything broker-facing is injected (``ib_factory`` returns a CONNECTED
ib_insync-like client), so tests use a fake IB. VPS VERIFICATION REQUIRED:
not yet run against the real paper gateway (that places paper orders and
needs explicit approval).
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from bot.paper_loop_d2.broker_port import (
    DATA_SOURCE_IBKR_PAPER, DEAD_STATUSES, LEG_ENTRY, LEG_SL, LEG_TP, WORKING_STATUSES,
    AccountSnapshot, BracketRequest, BrokerUnavailable, FillView, FlattenResult, LegId,
    OrderLookup, OrderView, OwnedTrade, PaperModeRefused, PositionView, Quote, SubmitAck,
    _finite_or_none, _finite_pos, exit_side, flatten_ref,
)

EXPECTED_PAPER_ACCOUNT = "DUP623346"


def _default_kill_switch() -> bool:
    from bot.kill_switch import is_kill_switch_active
    return is_kill_switch_active()


def _default_runtime_policy() -> Tuple[bool, str]:
    from bot.runtime_policy import get_signal_only_reason
    return get_signal_only_reason("ibkr_paper")


def _default_gateway(host: str, port: int) -> bool:
    from bot.brokers.ibkr_broker import _gateway_available
    return _gateway_available(host, port)


def _assert_paper(get_params: Callable[[], tuple], is_live: Callable[[], bool],
                  paper_port: int, live_port: int, expected_account: Optional[str]) -> str:
    if is_live():
        raise PaperModeRefused("paper learning loop: live mode configured, refusing")
    _host, port, account, _cid = get_params()
    if int(port) == int(live_port) or int(port) != int(paper_port):
        raise PaperModeRefused("paper learning loop: port %s is not the paper port %s"
                               % (port, paper_port))
    if expected_account is not None and str(account).strip() != expected_account:
        raise PaperModeRefused("paper learning loop: account %r is not the expected paper "
                               "account" % (account,))
    return str(account).strip()


def _str_id(v: Any) -> Optional[str]:
    if v in (None, "", 0, "0"):
        return None
    return str(v)


UNSET_DOUBLE = 1.7976931348623157e308      # ib_insync / IB API "value not set"


def _reported_commission(fill: Any, ex: Any) -> Optional[float]:
    """The commission IBKR actually reported for this execution, or None.

    IBKR delivers commission reports asynchronously; until the report for this
    execId arrives, ib_insync carries a DEFAULT CommissionReport (execId '',
    commission 0.0). A default, missing, unset, non-finite or mismatched
    report is UNKNOWN, never zero. Only a report whose execId matches the
    execution proves the fee (including a genuine 0.0)."""
    rep = getattr(fill, "commissionReport", None)
    if rep is None or not str(getattr(rep, "execId", "") or "").strip():
        return None
    if str(rep.execId) != str(ex.execId):
        return None
    c = _finite_or_none(getattr(rep, "commission", None))
    if c is None or abs(c) >= UNSET_DOUBLE / 2 or c < 0:
        return None
    return c


def _aware(t: Any) -> datetime:
    if isinstance(t, datetime):
        dt = t
    else:
        dt = datetime.fromisoformat(str(t))
    if dt.tzinfo is None:
        raise BrokerUnavailable("broker time without timezone")
    return dt.astimezone(timezone.utc)


class IbkrLoopBroker:
    data_source = DATA_SOURCE_IBKR_PAPER

    def __init__(self, *, ib_factory: Callable[[], Any],
                 get_params: Optional[Callable[[], tuple]] = None,
                 is_live: Optional[Callable[[], bool]] = None,
                 kill_switch_active: Callable[[], Any] = _default_kill_switch,
                 runtime_policy: Callable[[], Tuple[bool, str]] = _default_runtime_policy,
                 gateway_available: Callable[[str, int], bool] = _default_gateway,
                 expected_account: Optional[str] = EXPECTED_PAPER_ACCOUNT,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ack_wait_seconds: float = 3.0, cancel_wait_seconds: float = 6.0,
                 close_fill_wait_seconds: float = 10.0, poll_seconds: float = 0.5,
                 exec_history_hours: float = 12.0, market_data_type: int = 1):
        from bot.brokers import ibkr_broker as _ib       # read-only reuse of constants/helpers
        self._get_params = get_params or _ib._get_connection_params
        self._is_live = is_live or _ib._is_live_mode
        self._paper_port, self._live_port = _ib.PAPER_PORT, _ib.LIVE_PORT
        self.account = _assert_paper(self._get_params, self._is_live, self._paper_port,
                                     self._live_port, expected_account)
        self._ib_factory = ib_factory
        self._kill = kill_switch_active
        self._policy = runtime_policy
        self._gateway = gateway_available
        self._clock = clock
        self._ack_wait = ack_wait_seconds
        self._cancel_wait = cancel_wait_seconds
        self._close_wait = close_fill_wait_seconds
        self._poll = poll_seconds
        self._exec_hours = exec_history_hours
        self._mdt = market_data_type

    # ── safety ──
    def _preflight(self, *, entry: bool) -> Optional[str]:
        """The existing IBKR submit/flatten safety checks, re-evaluated per call."""
        try:
            if self._is_live():
                return "live_mode_refused"
            _host, port, account, _cid = self._get_params()
            if int(port) != int(self._paper_port) or int(port) == int(self._live_port):
                return "paper_port_refused"
            if str(account).strip() != self.account:
                return "account_changed"
        except Exception as exc:
            return "preflight_error:%s" % type(exc).__name__
        try:
            if self._kill() is not False:
                return "kill_switch_active"
        except Exception as exc:
            return "kill_switch_error:%s" % type(exc).__name__
        if entry:
            try:
                skip, reason = self._policy()
            except Exception as exc:
                return "runtime_policy_error:%s" % type(exc).__name__
            if skip:
                return "signal_only_skipped:%s" % reason
        return None

    @contextmanager
    def _session(self) -> Iterator[Any]:
        try:
            ib = self._ib_factory()
        except Exception as exc:
            raise BrokerUnavailable("connect failed: %s" % type(exc).__name__)
        try:
            try:
                managed = list(ib.managedAccounts() or [])
            except Exception as exc:
                raise BrokerUnavailable("managedAccounts failed: %s" % type(exc).__name__)
            if self.account not in managed:
                raise BrokerUnavailable("paper account not managed by this connection")
            yield ib
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def _read(self, fn: Callable[[Any], Any], what: str) -> Any:
        try:
            with self._session() as ib:
                return fn(ib)
        except BrokerUnavailable:
            raise
        except Exception as exc:
            raise BrokerUnavailable("%s failed: %s" % (what, type(exc).__name__))

    # ── views ──
    def _order_view(self, tr: Any) -> Optional[OrderView]:
        o, st, c = tr.order, getattr(tr, "orderStatus", None), getattr(tr, "contract", None)
        acct = (getattr(o, "account", "") or "").strip() or None
        if acct and acct != self.account:
            return None                                  # another account's order: not ours to see
        sym = getattr(c, "symbol", None) if c is not None else None
        return OrderView(
            order_id=_str_id(getattr(o, "orderId", None)), symbol=sym or None,
            side=getattr(o, "action", None), qty=_finite_or_none(getattr(o, "totalQuantity", None)),
            status=getattr(st, "status", None) if st is not None else None,
            order_ref=(getattr(o, "orderRef", "") or "").strip() or None,
            perm_id=_str_id(getattr(o, "permId", None) or getattr(st, "permId", None)),
            parent_id=_str_id(getattr(o, "parentId", None)),
            con_id=_str_id(getattr(c, "conId", None)) if c is not None else None,
            account=acct, order_type=getattr(o, "orderType", None))

    def _open_trades(self, ib: Any) -> List[Any]:
        ib.reqAllOpenOrders()                            # orders of ALL clients, incl. manual
        return list(ib.openTrades())

    def _fill_view(self, f: Any) -> Optional[FillView]:
        ex = f.execution
        if getattr(ex, "acctNumber", None) != self.account:
            return None
        t = _aware(getattr(f, "time", None) or ex.time)
        comm = _reported_commission(f, ex)
        side = {"BOT": "BUY", "SLD": "SELL"}.get(ex.side, ex.side)
        return FillView(exec_id=str(ex.execId), symbol=str(f.contract.symbol), side=side,
                        qty=float(ex.shares), price=float(ex.price), time_utc=t.isoformat(),
                        order_id=_str_id(ex.orderId), perm_id=_str_id(ex.permId), commission=comm,
                        order_ref=(getattr(ex, "orderRef", "") or "").strip() or None,
                        account=ex.acctNumber, con_id=_str_id(getattr(f.contract, "conId", None)))

    def _executions(self, ib: Any) -> List[FillView]:
        from ib_insync import ExecutionFilter
        out = []
        for f in ib.reqExecutions(ExecutionFilter(acctCode=self.account)) or []:
            v = self._fill_view(f)
            if v is not None:
                out.append(v)
        return out

    def _contract_qty(self, ib: Any, con_id: str) -> float:
        return sum(float(p.position) for p in ib.positions(self.account) or []
                   if getattr(p, "account", self.account) == self.account
                   and _str_id(getattr(p.contract, "conId", None)) == str(con_id))

    def _qualify(self, ib: Any, symbol: str) -> Any:
        from ib_insync import Stock
        q = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if len(q or []) != 1 or not _str_id(getattr(q[0], "conId", None)) \
                or getattr(q[0], "symbol", symbol) != symbol:
            raise BrokerUnavailable("contract for %s not uniquely qualified" % symbol)
        return q[0]

    # ── LoopBroker surface: reads ──
    def account_snapshot(self) -> AccountSnapshot:
        def fn(ib):
            by_tag: Dict[str, Any] = {}
            for v in ib.accountValues(self.account) or []:
                if getattr(v, "account", self.account) != self.account:
                    continue
                if getattr(v, "currency", "") in ("", "BASE"):
                    continue
                by_tag.setdefault(v.tag, v)
            nl = by_tag.get("NetLiquidation")
            if nl is None or not _finite_pos(nl.value):
                raise BrokerUnavailable("NetLiquidation unavailable for paper account")
            cash = by_tag.get("TotalCashValue")
            return AccountSnapshot(
                account=self.account, net_liquidation=float(nl.value),
                cash=_finite_or_none(cash.value) if cash is not None else None,
                currency=str(nl.currency), as_of_utc=self._clock().astimezone(timezone.utc).isoformat(),
                data_source=DATA_SOURCE_IBKR_PAPER)
        return self._read(fn, "account read")

    def positions(self) -> List[PositionView]:
        def fn(ib):
            out = []
            for p in ib.positions(self.account) or []:
                if getattr(p, "account", self.account) != self.account or float(p.position) == 0:
                    continue
                out.append(PositionView(symbol=str(p.contract.symbol), qty=float(p.position),
                                        avg_cost=_finite_or_none(getattr(p, "avgCost", None)),
                                        con_id=_str_id(getattr(p.contract, "conId", None)),
                                        account=self.account))
            return out
        return self._read(fn, "positions read")

    def open_orders(self) -> List[OrderView]:
        def fn(ib):
            out = []
            for tr in self._open_trades(ib):
                v = self._order_view(tr)
                if v is not None and (v.status is None or v.status not in DEAD_STATUSES | {"Filled"}):
                    out.append(v)
            return out
        return self._read(fn, "open orders read")

    def fills(self, since_utc: str) -> List[FillView]:
        since = datetime.fromisoformat(since_utc)

        def fn(ib):
            return [f for f in self._executions(ib) if datetime.fromisoformat(f.time_utc) >= since]
        return self._read(fn, "executions read")

    def lookup_order_ref(self, order_ref: str, since_utc: str) -> OrderLookup:
        refs = (order_ref, flatten_ref(order_ref))
        since = datetime.fromisoformat(since_utc)
        reasons: List[str] = []
        if self._clock() - since > timedelta(hours=self._exec_hours):
            reasons.append("outside_execution_history_window")
        orders: List[OrderView] = []
        fills: List[FillView] = []
        with self._session() as ib:
            for name, read in (("open_orders", lambda: self._open_trades(ib)),
                               ("completed_orders", lambda: list(ib.reqCompletedOrders(False)))):
                try:
                    for tr in read():
                        v = self._order_view(tr)
                        if v is not None and v.order_ref in refs and \
                                not any(x.perm_id == v.perm_id and x.order_id == v.order_id
                                        and x.status == v.status for x in orders):
                            orders.append(v)
                except Exception as exc:
                    reasons.append("%s_unavailable:%s" % (name, type(exc).__name__))
            try:
                fills = [f for f in self._executions(ib) if f.order_ref in refs]
            except Exception as exc:
                reasons.append("executions_unavailable:%s" % type(exc).__name__)
        return OrderLookup(order_ref=order_ref, complete=not reasons, orders=orders, fills=fills,
                           reasons=reasons)

    def quote(self, symbol: str) -> Quote:
        def fn(ib):
            contract = self._qualify(ib, symbol)
            ib.reqMarketDataType(self._mdt)
            tickers = ib.reqTickers(contract) or []
            if len(tickers) != 1:
                raise BrokerUnavailable("no ticker for %s" % symbol)
            t = tickers[0]
            last, bid, ask = (_finite_or_none(getattr(t, k, None)) for k in ("last", "bid", "ask"))
            last = last if last and last > 0 else None
            bid = bid if bid and bid > 0 else None
            ask = ask if ask and ask > 0 else None
            price = last if last is not None else ((bid + ask) / 2.0 if bid and ask and bid <= ask
                                                   else None)
            if price is None:
                raise BrokerUnavailable("no usable price for %s" % symbol)
            if getattr(t, "time", None) is None:
                raise BrokerUnavailable("quote without a timestamp")
            mdt = getattr(t, "marketDataType", None)
            return Quote(symbol=symbol, price=float(price), as_of_utc=_aware(t.time).isoformat(),
                         delayed=(mdt != 1), source="ibkr:mdt=%s" % mdt, bid=bid, ask=ask)
        return self._read(fn, "quote")

    # ── LoopBroker surface: orders ──
    def submit_bracket(self, req: BracketRequest) -> SubmitAck:
        if not req.order_ref:
            return SubmitAck(accepted=False, status="rejected", reason="order_ref required")
        why = self._preflight(entry=True)
        if why:
            return SubmitAck(accepted=False, status=why.split(":")[0], reason=why)
        host, port, _acct, _cid = self._get_params()
        if not self._gateway(host, int(port)):
            return SubmitAck(accepted=False, status="connection_failed",
                             reason="IB Gateway not reachable at %s:%s" % (host, port))
        placed = False
        try:
            ib = self._ib_factory()
        except Exception as exc:
            return SubmitAck(accepted=False, status="connection_failed", reason=type(exc).__name__)
        try:
            if self.account not in list(ib.managedAccounts() or []):
                return SubmitAck(accepted=False, status="account_mismatch",
                                 reason="paper account not managed by this connection")
            contract = self._qualify(ib, req.symbol)
            from bot.brokers.base import OrderIntent
            from bot.brokers.ibkr_broker import IBKRBroker
            intent = OrderIntent(signal_id=0, symbol=req.symbol, direction=req.direction,
                                 route="IBKR", entry_price=float(req.entry_ref_price),
                                 stop_loss=float(req.stop_loss), target_price=float(req.take_profit),
                                 valid_count=1, strategy_version=0, position_size=float(req.qty))
            parent, tp, sl = IBKRBroker()._make_bracket(intent, self.account)
            for o in (parent, tp, sl):
                o.orderRef = req.order_ref
                o.account = self.account
            parent.orderId = ib.client.getReqId()
            tp.parentId = parent.orderId
            sl.parentId = parent.orderId
            placed = True                                # from here the outcome may be unknown
            trades = [ib.placeOrder(contract, o) for o in (parent, tp, sl)]
            ib.sleep(self._ack_wait)
            status = trades[0].orderStatus.status
            errors = ["%s:%s" % (getattr(e, "errorCode", ""), getattr(e, "message", ""))
                      for e in (trades[0].log or []) if getattr(e, "errorCode", 0)]
            if status in DEAD_STATUSES:
                return SubmitAck(accepted=False, status="broker_rejected",
                                 reason="parent %s: %s" % (status, "; ".join(errors)[:200]),
                                 order_ref=req.order_ref)
            if status not in WORKING_STATUSES | {"Filled"} or status in ("PendingSubmit", "ApiPending"):
                raise BrokerUnavailable("bracket placed but parent status %r is not conclusive"
                                        % status)

            def leg(tr):
                return LegId(order_id=_str_id(tr.order.orderId),
                             perm_id=_str_id(tr.orderStatus.permId or tr.order.permId))
            legs = {LEG_ENTRY: leg(trades[0]), LEG_TP: leg(trades[1]), LEG_SL: leg(trades[2])}
            return SubmitAck(accepted=True, status="accepted",
                             broker_order_id=legs[LEG_ENTRY].order_id, perm_id=legs[LEG_ENTRY].perm_id,
                             order_ref=req.order_ref, legs=legs, con_id=_str_id(contract.conId),
                             account=self.account,
                             raw={"parent_status": status, "warnings": errors[:5]})
        except Exception as exc:
            if placed:
                raise                                    # unknown outcome: recovery by orderRef
            return SubmitAck(accepted=False, status="error", reason="%s before placement"
                             % type(exc).__name__)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def flatten_owned(self, trade: OwnedTrade) -> FlattenResult:
        """Close only ``trade.open_qty`` after proving it is the loop's own
        position. Never cancels or closes anything that is not the trade's."""
        why = self._preflight(entry=False)
        if why:
            return FlattenResult(confirmed=False, close_placed=False, detail={"reason": why})
        if not trade.con_id:
            return FlattenResult(confirmed=False, refused=True, close_placed=False,
                                 detail={"reason": "contract_unknown"})
        if trade.account and trade.account != self.account:
            return FlattenResult(confirmed=False, refused=True, close_placed=False,
                                 detail={"reason": "account_mismatch"})
        refs = {trade.order_ref, flatten_ref(trade.order_ref)}
        perms = set(trade.leg_perm_ids)
        st: Dict[str, Any] = {"placing": False, "placed": False, "order_id": None, "perm_id": None}
        try:
            with self._session() as ib:
                return self._flatten_on(ib, trade, refs, perms, st)
        except Exception as exc:
            # Whether a close reached the broker decides what the caller may do next.
            placed = True if st["placed"] else (None if st["placing"] else False)
            return FlattenResult(confirmed=False, close_placed=placed,
                                 close_order_ref=flatten_ref(trade.order_ref) if placed is not False
                                 else None,
                                 close_order_id=st["order_id"], close_perm_id=st["perm_id"],
                                 detail={"reason": "%s:%s" % ("close_outcome_unknown" if placed is not False
                                                              else "broker_unavailable",
                                                              type(exc).__name__)})

    def _exit_leg_filled(self, ib: Any, trade: OwnedTrade) -> Optional[bool]:
        """True if one of the trade's own exit legs is already Filled at the
        broker (then closing would double the exit). None if unknowable."""
        try:
            done = list(ib.reqCompletedOrders(False)) + list(ib.openTrades())
        except Exception:
            return None
        return any(tr.orderStatus.status == "Filled" and trade.is_own_exit_leg(
            getattr(tr.order, "orderRef", None), tr.order.permId or tr.orderStatus.permId,
            getattr(tr.order, "action", None)) for tr in done)

    def _flatten_on(self, ib: Any, trade: OwnedTrade, refs: set, perms: set,
                    st: Dict[str, Any]) -> FlattenResult:
        """Every return before ``placeOrder`` reports close_placed=False."""
        from ib_insync import Contract, MarketOrder
        signed = trade.signed_qty

        def not_placed(reason: str, refused: bool = False, **extra: Any) -> FlattenResult:
            return FlattenResult(confirmed=False, refused=refused, close_placed=False,
                                 detail=dict(reason=reason, **extra))
        filled = self._exit_leg_filled(ib, trade)
        if filled is None:
            return not_placed("leg_state_unknown")
        if filled:
            return not_placed("exit_leg_filled")
        pos = self._contract_qty(ib, trade.con_id)
        if abs(pos - signed) > 1e-9:
            return not_placed("position_not_owned", refused=True, broker_qty=pos, loop_qty=signed)
        on_contract = [tr for tr in self._open_trades(ib)
                       if _str_id(getattr(tr.contract, "conId", None)) == str(trade.con_id)
                       and tr.orderStatus.status not in DEAD_STATUSES | {"Filled"}]

        def mine(tr):
            return ((getattr(tr.order, "orderRef", "") or "").strip() in refs
                    or _str_id(tr.order.permId or tr.orderStatus.permId) in perms)
        foreign = [tr for tr in on_contract if not mine(tr)]
        if foreign:
            return not_placed("foreign_orders_on_contract", refused=True,
                              foreign_order_ids=[_str_id(t.order.orderId) for t in foreign])
        if any((getattr(tr.order, "orderRef", "") or "").strip() == flatten_ref(trade.order_ref)
               for tr in on_contract):
            return not_placed("close_already_working")     # never a second close
        own = [tr for tr in on_contract if mine(tr)]
        for tr in own:
            ib.cancelOrder(tr.order)
        waited = 0.0
        while any(tr.orderStatus.status not in DEAD_STATUSES | {"Filled"} for tr in own) \
                and waited < self._cancel_wait:
            ib.sleep(self._poll)
            waited += self._poll
        if any(tr.orderStatus.status == "Filled" for tr in own):
            return not_placed("exit_leg_filled")
        # Second, post-cancel check: an exit leg may have filled during the
        # cancellation. Unknown (read failed) is NOT safe: fail closed.
        filled = self._exit_leg_filled(ib, trade)
        if filled is None:
            return not_placed("leg_state_unknown_after_cancel")
        if filled:
            return not_placed("exit_leg_filled")
        if any(tr.orderStatus.status not in DEAD_STATUSES for tr in own):
            return not_placed("cancel_unconfirmed")
        pos = self._contract_qty(ib, trade.con_id)
        if abs(pos - signed) > 1e-9:
            return not_placed("position_changed", refused=True, broker_qty=pos)
        q = ib.qualifyContracts(Contract(conId=int(trade.con_id)))
        if len(q or []) != 1 or getattr(q[0], "symbol", None) != trade.symbol:
            return not_placed("contract_mismatch", refused=True)
        close = MarketOrder(exit_side(trade.direction), abs(signed))
        close.account = self.account
        close.tif = "DAY"
        close.orderRef = flatten_ref(trade.order_ref)
        st["placing"] = True
        tr = ib.placeOrder(q[0], close)
        st["placed"] = True
        st["order_id"] = _str_id(getattr(tr.order, "orderId", None))
        st["perm_id"] = _str_id(tr.orderStatus.permId or tr.order.permId)
        waited = 0.0
        while tr.orderStatus.status != "Filled" and waited < self._close_wait:
            ib.sleep(self._poll)
            waited += self._poll
        st["perm_id"] = _str_id(tr.orderStatus.permId or tr.order.permId) or st["perm_id"]
        left = self._contract_qty(ib, trade.con_id)
        return FlattenResult(confirmed=(tr.orderStatus.status == "Filled" and abs(left) < 1e-9),
                             close_placed=True,
                             detail={"close_status": tr.orderStatus.status, "remaining_qty": left,
                                     "cancelled_perm_ids": [_str_id(t.order.permId) for t in own]},
                             close_order_ref=close.orderRef, close_perm_id=st["perm_id"],
                             close_order_id=st["order_id"])

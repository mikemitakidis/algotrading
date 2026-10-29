"""research.economics.statement — IBKR paper statement reconciliation and fee calibration (M21 E5).

Our databases record CLAIMS. Proof that a paper order executed, at what quantity and price, and what it
cost, is the IBKR statement the operator exports. This module reconciles the two, offline and read-only:

* ``parse_statement``   an IBKR Flex Query (Trades) CSV or an Activity Statement CSV, header-driven and
                        fail-closed: a missing required column, an unparseable number or time, a side that
                        contradicts the quantity sign, a duplicated execution id, or no time zone refuses the
                        whole file. Nothing is guessed.
* ``ws2_claims``        the fills the WS2/D2 loop journal claims (a COPY, opened ``mode=ro``): entry and exit
                        legs of IBKR-paper orders with their orderRef, perm ids and claimed execution ids.
                        Simulated orders are counted and excluded: they can never appear on a statement.
* ``legacy_claims``     fills the old system claims in ``execution_intents`` (a COPY, ``mode=ro``). It recorded
                        no orderRef or execution id, so it can only ever reach PROBABLE_MATCH.
* ``reconcile``         one class per claimed leg and per statement execution:

    MATCHED                         an id (execution id, orderRef, perm id) ties the leg to statement
                                    executions and symbol, side, quantity, price, time and fees agree;
    MISMATCH                        an id ties them but something disagrees (each reason listed);
    PROBABLE_MATCH                  only symbol, side, quantity and a +-time window agree: never proof;
    UNMATCHED_CLAIM                 the journal claims a fill the statement does not show;
    UNRECORDED_FILL_OF_KNOWN_ORDER  the statement shows a fill of one of our orders the journal never recorded;
    UNATTRIBUTED_EXECUTION          a statement execution no claim accounts for.

  Executions on an account that is not a paper account by IBKR's convention (ids starting ``DU``) raise a
  LIVE_ACCOUNT_ACTIVITY alert. Account ids are masked in every output.
* ``fee_records``       MATCHED legs whose statement commission is known feed
                        ``research.economics.analysis.fee_calibration`` (statement commission vs the modelled
                        IBKR fee). A missing commission is reported and excluded, never treated as zero. The
                        calibration only SUGGESTS a multiplier; nothing is applied.

A trade is statement-matched only when its entry and every exit leg are MATCHED; ``matched_trade_ids`` is
what the E4 score-versus-outcome report accepts as genuine IBKR paper executions.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

from research.economics.analysis import fee_calibration
from research.economics.fees import BUY as FEE_BUY, IBKR_PRO_FIXED, IBKR_PRO_TIERED, SELL as FEE_SELL, order_fees

SCHEMA = "m21-e5-reconciliation/1"
MATCHED, MISMATCH, PROBABLE = "MATCHED", "MISMATCH", "PROBABLE_MATCH"
UNMATCHED_CLAIM, UNRECORDED, UNATTRIBUTED = "UNMATCHED_CLAIM", "UNRECORDED_FILL_OF_KNOWN_ORDER", \
    "UNATTRIBUTED_EXECUTION"
LIVE_ALERT = "LIVE_ACCOUNT_ACTIVITY"
BOT_ORDER_REF_PREFIX = "M21L"          # bot.paper_loop_d2.broker_port.ORDER_REF_PREFIX
FLATTEN_SUFFIX = ":FLAT"               # bot.paper_loop_d2.broker_port.FLATTEN_SUFFIX
EXIT_LEGS = ("take_profit", "stop_loss", "flatten", "exit")
PLANS = {"fixed": IBKR_PRO_FIXED, "tiered": IBKR_PRO_TIERED}
FEE_ASSUMPTION = ("statement commission is compared with the modelled ALL-IN fee (broker + regulatory); "
                  "confirm on the first statement that its commission column is all-in")

_ALIASES = {
    "account": ("ClientAccountID", "AccountId", "Account"),
    "asset_class": ("AssetClass", "Asset Category"),
    "symbol": ("Symbol",),
    "side": ("Buy/Sell",),
    "qty": ("Quantity",),
    "price": ("TradePrice", "T. Price"),
    "datetime": ("DateTime", "Date/Time"),
    "commission": ("IBCommission", "Comm/Fee"),
    "commission_ccy": ("IBCommissionCurrency",),
    "currency": ("CurrencyPrimary", "Currency"),
    "order_ref": ("OrderReference",),
    "exec_id": ("IBExecID",),
    "order_id": ("IBOrderID",),
    "level": ("LevelOfDetail", "DataDiscriminator"),
    "code": ("Code", "Notes/Codes"),
}
_REQUIRED = ("symbol", "qty", "price", "datetime")
_LEVELS = ("EXECUTION", "TRADE", "ORDER")          # most granular first; others are summaries
_TIME_FORMATS = ("%Y%m%d;%H%M%S", "%Y-%m-%d;%H:%M:%S", "%Y-%m-%d, %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                 "%Y%m%d %H%M%S", "%Y-%m-%dT%H:%M:%S")


class StatementRefused(ValueError):
    """The statement cannot be read without guessing; nothing from it is used."""


@dataclass(frozen=True)
class Execution:
    file: str
    row: int
    account: Optional[str]
    asset_class: Optional[str]
    symbol: str
    side: str                       # BUY | SELL
    qty: float                      # > 0
    price: float
    time_utc: datetime
    commission: Optional[float]     # >= 0, None = not reported
    commission_ccy: Optional[str]
    currency: Optional[str]
    order_ref: Optional[str]
    exec_id: Optional[str]
    order_id: Optional[str]


@dataclass(frozen=True)
class Claim:
    claim_id: str
    system: str                     # ws2_journal | legacy_intents
    trade_id: Optional[str]
    leg: str                        # entry | exit
    symbol: str
    side: str
    qty: float
    price: float
    time_utc: Optional[datetime]
    fees: Optional[float]
    ref_price: Optional[float] = None
    order_refs: Tuple[str, ...] = ()
    perm_ids: Tuple[str, ...] = ()
    exec_ids: Tuple[str, ...] = ()
    closed_trade: bool = False
    notes: Tuple[str, ...] = ()


@dataclass
class StatementFile:
    name: str
    sha256: str
    format: str
    level_used: Optional[str]
    rows_used: int
    rows_ignored_by_level: int
    correction_rows: int
    executions: List[Execution] = field(default_factory=list)


# ── statement parsing ────────────────────────────────────────────────────────
def _num(s: str, *, what: str, where: str, allow_blank: bool = False) -> Optional[float]:
    t = (s or "").strip().replace(",", "")
    if t in ("", "--"):
        if allow_blank:
            return None
        raise StatementRefused("%s: %s is blank" % (where, what))
    try:
        v = float(t)
    except ValueError:
        raise StatementRefused("%s: %s %r is not a number" % (where, what, s))
    if not math.isfinite(v):
        raise StatementRefused("%s: %s is not finite" % (where, what))
    return v


def _time(s: str, tz: ZoneInfo, where: str) -> datetime:
    t = (s or "").strip()
    try:
        d = datetime.fromisoformat(t)
        if d.tzinfo is not None:
            return d.astimezone(ZoneInfo("UTC"))
    except ValueError:
        pass
    for f in _TIME_FORMATS:
        try:
            return datetime.strptime(t, f).replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
        except ValueError:
            continue
    raise StatementRefused("%s: time %r is not in a supported unambiguous format" % (where, s))


def _tables(text: str) -> Tuple[str, List[Tuple[int, List[str], List[str]]]]:
    """(format, [(row number, header, cells)]) for the trade rows of a Flex or Activity CSV."""
    rows = list(csv.reader(io.StringIO(text)))
    out, header, fmt = [], None, "flex"
    for n, r in enumerate(rows, start=1):
        if not r or not any(c.strip() for c in r):
            continue
        head = r[0].strip()
        if len(r) > 2 and r[1].strip() in ("Header", "Data"):            # Activity Statement: Section,Kind,...
            fmt = "activity"
            if head != "Trades":
                continue
            if r[1].strip() == "Header":
                header = [c.strip() for c in r[2:]]
            elif header is not None:
                out.append((n, header, r[2:]))
            continue
        if head in ("BOF", "EOF", "BOS", "EOS", "BOA", "EOA"):           # Flex header/trailer records
            continue
        if head in ("HEADER", "DATA") and len(r) > 2:                    # Flex record-type prefix
            cells = r[2:]
            if head == "HEADER":
                header = [c.strip() for c in cells]
            elif header is not None:
                out.append((n, header, cells))
            continue
        if "Symbol" in [c.strip() for c in r] and any(c.strip() in _ALIASES["qty"] for c in r):
            header = [c.strip() for c in r]
            continue
        if header is None:
            raise StatementRefused("row %d: data before any header row" % n)
        out.append((n, header, r))
    return fmt, out


def parse_statement(path: str, *, tz: str) -> StatementFile:
    if not tz:
        raise StatementRefused("the statement time zone is required (no default is assumed)")
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise StatementRefused("unknown time zone %r" % tz)
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StatementRefused("%s is not UTF-8 text" % Path(path).name)
    fmt, rows = _tables(text)
    name = Path(path).name
    if not rows:
        raise StatementRefused("%s has no trade rows" % name)
    parsed: List[Tuple[Optional[str], Dict[str, str], int]] = []
    for n, header, cells in rows:
        col = {}
        for key, names in _ALIASES.items():
            for nm in names:
                if nm in header:
                    col[key] = header.index(nm)
                    break
        missing = [k for k in _REQUIRED if k not in col]
        if missing:
            raise StatementRefused("%s row %d: required column(s) missing: %s" % (name, n, ", ".join(missing)))
        get = {k: (cells[i].strip() if i < len(cells) else "") for k, i in col.items()}
        lvl = get.get("level")
        parsed.append(((lvl or "").upper() or None, get, n))
    present = {lvl for lvl, _, _ in parsed if lvl}
    level = next((x for x in _LEVELS if x in present), None)
    if present and level is None:
        raise StatementRefused("%s: no execution, trade or order level rows (levels: %s)" % (
            name, ", ".join(sorted(present))))
    sf = StatementFile(name, hashlib.sha256(raw).hexdigest(), fmt, level, 0, 0, 0)
    seen_exec = set()
    for lvl, g, n in parsed:
        if level is not None and lvl != level:
            sf.rows_ignored_by_level += 1
            continue
        where = "%s row %d" % (name, n)
        side_raw = (g.get("side") or "").upper()
        code = (g.get("code") or "").upper()
        if "(CA." in side_raw or "CA" in [c.strip() for c in code.replace(",", ";").split(";")]:
            sf.correction_rows += 1                       # a cancellation / correction: manual review
            continue
        qty = _num(g["qty"], what="quantity", where=where)
        if qty == 0:
            raise StatementRefused("%s: zero quantity" % where)
        sign_side = "BUY" if qty > 0 else "SELL"
        if side_raw:
            side = "BUY" if side_raw.startswith("BUY") else "SELL" if side_raw.startswith("SELL") else None
            if side is None:
                raise StatementRefused("%s: side %r not understood" % (where, g.get("side")))
            if side != sign_side:
                raise StatementRefused("%s: side %s contradicts the quantity sign" % (where, side))
        else:
            side = sign_side
        price = _num(g["price"], what="price", where=where)
        if not price > 0:
            raise StatementRefused("%s: price must be positive" % where)
        comm = _num(g.get("commission", ""), what="commission", where=where, allow_blank=True)
        exec_id = g.get("exec_id") or None
        if exec_id is not None:
            if exec_id in seen_exec:
                raise StatementRefused("%s: execution id repeated (duplicated export?)" % where)
            seen_exec.add(exec_id)
        sym = g["symbol"].strip()
        if not sym:
            raise StatementRefused("%s: blank symbol" % where)
        sf.executions.append(Execution(
            file=name, row=n, account=g.get("account") or None, asset_class=g.get("asset_class") or None,
            symbol=sym, side=side, qty=abs(qty), price=price, time_utc=_time(g["datetime"], zone, where),
            commission=None if comm is None else abs(comm), commission_ccy=g.get("commission_ccy") or None,
            currency=g.get("currency") or None, order_ref=g.get("order_ref") or None, exec_id=exec_id,
            order_id=g.get("order_id") or None))
        sf.rows_used += 1
    return sf


def mask_account(a: Optional[str]) -> Optional[str]:
    if not a:
        return None
    return a[:2] + "***" + a[-2:] if len(a) > 4 else "***"


def is_paper_account(a: Optional[str]) -> Optional[bool]:
    """IBKR convention: paper trading account ids start with DU. None = no account column."""
    return None if not a else a.upper().startswith("DU")


# ── claims ──────────────────────────────────────────────────────────────────
def _ro(path: str) -> sqlite3.Connection:
    p = str(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    c.row_factory = sqlite3.Row
    return c


def _aware(s: Any) -> Tuple[Optional[datetime], Optional[str]]:
    if s in (None, ""):
        return None, "no_claim_time"
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None, "claim_time_unreadable"
    if d.tzinfo is None:
        return None, "claim_time_naive"
    return d.astimezone(ZoneInfo("UTC")), None


def _entry_side(direction: Any) -> Optional[str]:
    d = str(direction or "").strip().lower()
    return "BUY" if d in ("long", "buy") else "SELL" if d in ("short", "sell") else None


def ws2_claims(journal_db: str) -> Tuple[List[Claim], Dict[str, Any]]:
    """Claimed fills of IBKR-paper orders in a COPY of the WS2/D2 loop journal."""
    c = _ro(journal_db)
    try:
        orders = [dict(r) for r in c.execute("SELECT * FROM loop_orders ORDER BY planned_at_utc, client_ref")]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        execs: Dict[Tuple[str, str], List[str]] = {}
        if "loop_exec_claims" in tables:
            for r in c.execute("SELECT exec_id, client_ref, leg FROM loop_exec_claims ORDER BY exec_id"):
                kind = "entry" if r["leg"] == "entry" else "exit" if r["leg"] in EXIT_LEGS else r["leg"]
                execs.setdefault((r["client_ref"], kind), []).append(r["exec_id"])
    finally:
        c.close()
    stats = {"orders_ibkr_paper": 0, "orders_simulated_excluded": 0, "orders_other_source_excluded": 0,
             "legs_claimed": 0, "known_order_refs": []}
    claims: List[Claim] = []
    for o in orders:
        src = o.get("data_source")
        if src == "simulated":
            stats["orders_simulated_excluded"] += 1
            continue
        if src != "ibkr_paper":
            stats["orders_other_source_excluded"] += 1
            continue
        stats["orders_ibkr_paper"] += 1
        ref, oref = o["client_ref"], o.get("order_ref")
        if oref:
            stats["known_order_refs"].append(oref)
        side = _entry_side(o.get("direction"))
        if side is None:
            continue
        exit_side = "SELL" if side == "BUY" else "BUY"
        closed = o.get("state") == "CLOSED"
        legs = [("entry", side, o.get("entry_fill_price"), o.get("entry_filled_at_utc"), o.get("entry_fees"),
                 (oref,) if oref else (), tuple(x for x in (o.get("broker_perm_id"),) if x),
                 o.get("entry_ref_price")),
                ("exit", exit_side, o.get("exit_fill_price"), o.get("exit_filled_at_utc"), o.get("exit_fees"),
                 (oref, oref + FLATTEN_SUFFIX) if oref else (),
                 tuple(x for x in (o.get("tp_perm_id"), o.get("sl_perm_id"), o.get("flatten_perm_id")) if x),
                 None)]
        for leg, sd, px, at, fees, orefs, perms, refpx in legs:
            if px is None:
                continue
            t, why = _aware(at)
            claims.append(Claim(
                claim_id="%s:%s" % (ref, leg), system="ws2_journal", trade_id=ref, leg=leg, symbol=o["symbol"],
                side=sd, qty=float(o["qty"]), price=float(px), time_utc=t,
                fees=None if fees is None else float(fees), ref_price=None if refpx is None else float(refpx),
                order_refs=orefs, perm_ids=tuple(str(p) for p in perms),
                exec_ids=tuple(execs.get((ref, leg), ())), closed_trade=closed, notes=(why,) if why else ()))
            stats["legs_claimed"] += 1
    return claims, stats


def legacy_claims(signals_db: str) -> Tuple[List[Claim], Dict[str, Any]]:
    """Fills the old system claims (``execution_intents``, broker ibkr*, status filled) in a COPY of
    signals.db. No orderRef or execution id was recorded, so these reach PROBABLE_MATCH at most."""
    c = _ro(signals_db)
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT id, symbol, direction, broker, status, fill_price, fill_qty, filled_at "
            "FROM execution_intents WHERE lower(broker) LIKE 'ibkr%' ORDER BY id")]
    finally:
        c.close()
    claims, stats = [], {"intents_ibkr": len(rows), "filled_claims": 0, "not_filled": 0, "unusable": 0}
    for r in rows:
        if r["status"] != "filled" or r["fill_price"] is None:
            stats["not_filled"] += 1
            continue
        side = _entry_side(r["direction"])
        qty = r["fill_qty"] if r["fill_qty"] is not None else None
        if side is None or qty is None or not float(qty) > 0:
            stats["unusable"] += 1
            continue
        t, why = _aware(r["filled_at"])
        claims.append(Claim(claim_id="intent:%s" % r["id"], system="legacy_intents", trade_id=None, leg="entry",
                            symbol=r["symbol"], side=side, qty=float(qty), price=float(r["fill_price"]),
                            time_utc=t, fees=None, notes=(why,) if why else ()))
        stats["filled_claims"] += 1
    return claims, stats


# ── reconciliation ───────────────────────────────────────────────────────────
@dataclass
class LegResult:
    claim: Claim
    klass: str
    key: Optional[str]
    reasons: List[str]
    executions: List[Execution]

    @property
    def qty(self) -> float:
        return float(sum(e.qty for e in self.executions))

    @property
    def vwap(self) -> Optional[float]:
        q = self.qty
        return float(sum(e.qty * e.price for e in self.executions) / q) if q > 0 else None

    @property
    def commission(self) -> Optional[float]:
        if not self.executions or any(e.commission is None for e in self.executions):
            return None
        return float(sum(e.commission for e in self.executions))


def _compare(c: Claim, group: Sequence[Execution], *, time_tol: timedelta, price_tol: float, fee_tol: float
             ) -> List[str]:
    why = []
    if any(e.symbol != c.symbol for e in group):
        why.append("symbol_differs")
    if any(e.side != c.side for e in group):
        why.append("side_differs")
    q = sum(e.qty for e in group)
    if abs(q - c.qty) > 1e-6:
        why.append("quantity_differs:claimed=%g,statement=%g" % (c.qty, q))
    if q > 0:
        vwap = sum(e.qty * e.price for e in group) / q
        if abs(vwap - c.price) > price_tol:
            why.append("price_differs:claimed=%.4f,statement=%.4f" % (c.price, vwap))
    if c.time_utc is not None and group:
        first = min(e.time_utc for e in group)
        if abs(first - c.time_utc) > time_tol:
            why.append("time_differs_by_%ds" % abs((first - c.time_utc).total_seconds()))
    comm = None if any(e.commission is None for e in group) else sum(e.commission for e in group)
    if c.fees is not None and comm is not None and abs(c.fees - comm) > fee_tol:
        why.append("fees_differ:claimed=%.4f,statement=%.4f" % (c.fees, comm))
    if any(is_paper_account(e.account) is False for e in group):
        why.append("non_paper_account")
    return why


def reconcile(executions: Sequence[Execution], claims: Sequence[Claim], *, known_order_refs: Iterable[str] = (),
              time_tol_s: float = 120.0, price_tol: float = 0.01, fee_tol: float = 0.01
              ) -> Tuple[List[LegResult], List[Tuple[Execution, str]]]:
    tol = timedelta(seconds=time_tol_s)
    unused = list(range(len(executions)))
    by_exec = {e.exec_id: i for i, e in enumerate(executions) if e.exec_id}
    results: List[LegResult] = []
    order = sorted(range(len(claims)), key=lambda k: (claims[k].system != "ws2_journal",
                                                      claims[k].time_utc or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
                                                      claims[k].claim_id))
    kw = dict(time_tol=tol, price_tol=price_tol, fee_tol=fee_tol)
    for k in order:
        c = claims[k]
        group: List[int] = []
        key, extra = None, []
        if c.exec_ids:
            key = "exec_id"
            group = [by_exec[x] for x in c.exec_ids if x in by_exec and by_exec[x] in unused]
            absent = [x for x in c.exec_ids if x not in by_exec]
            if absent:
                extra.append("claimed_exec_ids_absent_from_statement:%d" % len(absent))
        if not group and c.order_refs:
            key = "order_ref"
            group = [i for i in unused if executions[i].order_ref in c.order_refs and executions[i].side == c.side]
        if not group and c.perm_ids:
            key = "perm_id_as_IBOrderID"
            group = [i for i in unused if executions[i].order_id in c.perm_ids and executions[i].side == c.side]
        if group:
            why = extra + _compare(c, [executions[i] for i in group], **kw)
            results.append(LegResult(c, MISMATCH if why else MATCHED, key, why, [executions[i] for i in group]))
            unused = [i for i in unused if i not in group]
            continue
        if c.time_utc is not None:
            near = [i for i in unused if executions[i].symbol == c.symbol and executions[i].side == c.side
                    and abs(executions[i].time_utc - c.time_utc) <= tol]
            if near and abs(sum(executions[i].qty for i in near) - c.qty) <= 1e-6:
                why = [w for w in _compare(c, [executions[i] for i in near], **kw)]
                results.append(LegResult(c, PROBABLE if not why else MISMATCH, "time_window", why or
                                         ["no_id_link:symbol_side_quantity_time_only"],
                                         [executions[i] for i in near]))
                unused = [i for i in unused if i not in near]
                continue
        results.append(LegResult(c, UNMATCHED_CLAIM, key, extra + ["no_statement_execution_found"] +
                                 list(c.notes), []))
    refs = set(known_order_refs)
    leftovers = []
    for i in unused:
        e = executions[i]
        ref = e.order_ref or ""
        base = ref[:-len(FLATTEN_SUFFIX)] if ref.endswith(FLATTEN_SUFFIX) else ref
        ours = base in refs or ref.startswith(BOT_ORDER_REF_PREFIX)
        leftovers.append((e, UNRECORDED if ours else UNATTRIBUTED))
    return results, leftovers


def trade_status(results: Sequence[LegResult]) -> List[Dict[str, Any]]:
    by: Dict[str, List[LegResult]] = {}
    for r in results:
        if r.claim.system == "ws2_journal" and r.claim.trade_id:
            by.setdefault(r.claim.trade_id, []).append(r)
    out = []
    for tid in sorted(by):
        legs = by[tid]
        closed = any(r.claim.closed_trade for r in legs)
        has_exit = any(r.claim.leg == "exit" for r in legs)
        ok = closed and has_exit and any(r.claim.leg == "entry" for r in legs) and all(
            r.klass == MATCHED for r in legs)
        why = ["%s:%s" % (r.claim.leg, r.klass) for r in legs if r.klass != MATCHED]
        if not closed:
            why.append("trade_not_closed")
        elif not has_exit:
            why.append("no_exit_fill_claimed")
        out.append({"trade_id": tid, "statement_matched": bool(ok), "reasons": why})
    return out


def _ny_date(t: datetime) -> date:
    return t.astimezone(ZoneInfo("America/New_York")).date()


def fee_records(results: Sequence[LegResult], *, plan=IBKR_PRO_FIXED) -> Tuple[List[Dict[str, float]], Dict[str, int]]:
    """Calibration inputs from MATCHED legs only. Missing or non-USD commissions are counted, never zero."""
    recs, counts = [], {"matched_legs": 0, "with_statement_commission": 0, "commission_missing": 0,
                        "commission_not_usd": 0, "zero_commission_reported": 0}
    for r in results:
        if r.klass != MATCHED:
            continue
        counts["matched_legs"] += 1
        comm = r.commission
        if comm is None:
            counts["commission_missing"] += 1
            continue
        ccys = {e.commission_ccy or e.currency for e in r.executions}
        if ccys != {"USD"}:
            counts["commission_not_usd"] += 1
            continue
        counts["with_statement_commission"] += 1
        if comm == 0:
            counts["zero_commission_reported"] += 1
        modelled = sum(order_fees(e.qty, e.price, FEE_BUY if e.side == "BUY" else FEE_SELL, _ny_date(e.time_utc),
                                  plan=plan).total for e in r.executions)
        slip = float("nan")
        if r.claim.leg == "entry" and r.claim.ref_price and r.vwap:
            sgn = 1.0 if r.claim.side == "BUY" else -1.0
            slip = sgn * (r.vwap - r.claim.ref_price) / r.claim.ref_price * 1e4
        recs.append({"modelled_fees": modelled, "realised_fees": comm, "modelled_slippage_bps": float("nan"),
                     "realised_slippage_bps": slip})
    return recs, counts


def _q(x: Sequence[float]) -> Dict[str, Any]:
    a = np.asarray([v for v in x if math.isfinite(v)], dtype=float)
    if len(a) == 0:
        return {"n": 0, "median": None, "p75": None, "p90": None}
    return {"n": int(len(a)), "median": float(np.median(a)), "p75": float(np.quantile(a, 0.75)),
            "p90": float(np.quantile(a, 0.9))}


def _jsonable(x: Any) -> Any:
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def _exec_view(e: Execution) -> Dict[str, Any]:
    return {"file": e.file, "row": e.row, "account": mask_account(e.account), "asset_class": e.asset_class,
            "symbol": e.symbol, "side": e.side, "qty": e.qty, "price": e.price, "time_utc": e.time_utc.isoformat(),
            "commission": e.commission, "order_ref": e.order_ref, "exec_id": e.exec_id}


def build_report(files: Sequence[StatementFile], claims: Sequence[Claim], claim_stats: Mapping[str, Any], *,
                 tz: str, plan: str = "fixed", min_n: int = 30, **tol) -> Dict[str, Any]:
    execs = [e for f in files for e in f.executions]
    ids = [e.exec_id for e in execs if e.exec_id]
    if len(ids) != len(set(ids)):
        raise StatementRefused("an execution id appears in more than one statement file (overlapping exports?)")
    known = [r for s in claim_stats.values() if isinstance(s, dict) for r in s.get("known_order_refs", ())]
    results, leftovers = reconcile(execs, claims, known_order_refs=known, **tol)
    trades = trade_status(results)
    recs, fee_counts = fee_records(results, plan=PLANS[plan])
    cal = fee_calibration(recs, min_n=min_n)
    counts: Dict[str, int] = {k: 0 for k in (MATCHED, MISMATCH, PROBABLE, UNMATCHED_CLAIM, UNRECORDED, UNATTRIBUTED)}
    for r in results:
        counts[r.klass] += 1
    for _, k in leftovers:
        counts[k] += 1
    accounts: Dict[str, Dict[str, Any]] = {}
    for e in execs:
        a = accounts.setdefault(mask_account(e.account) or "(no account column)",
                                {"executions": 0, "paper_by_ibkr_convention": is_paper_account(e.account)})
        a["executions"] += 1
    alerts = []
    live = [e for e in execs if is_paper_account(e.account) is False]
    if live:
        alerts.append({"alert": LIVE_ALERT, "executions": len(live),
                       "detail": "executions on an account that is not a DU paper account: escalate, do not proceed"})
    if any(f.correction_rows for f in files):
        alerts.append({"alert": "CORRECTION_ROWS_PRESENT", "rows": sum(f.correction_rows for f in files),
                       "detail": "cancellation/correction rows were excluded from matching; review them manually"})
    if any(e.asset_class not in (None, "STK", "Stocks") for e in execs):
        alerts.append({"alert": "NON_STOCK_EXECUTIONS",
                       "executions": sum(1 for e in execs if e.asset_class not in (None, "STK", "Stocks"))})
    if not all(any(e.exec_id for e in f.executions) for f in files if f.executions):
        alerts.append({"alert": "NO_EXECUTION_IDS_IN_SOME_FILES",
                       "detail": "files without execution ids or order references can only give PROBABLE_MATCH"})
    return _jsonable({
        "schema": SCHEMA,
        "statement": {"files": [{k: v for k, v in asdict(f).items() if k != "executions"} for f in files],
                      "statement_tz": tz, "accounts": accounts},
        "claims": {k: {kk: vv for kk, vv in v.items() if kk != "known_order_refs"} for k, v in claim_stats.items()},
        "counts": counts, "alerts": alerts,
        "legs": [{"claim_id": r.claim.claim_id, "system": r.claim.system, "leg": r.claim.leg,
                  "symbol": r.claim.symbol, "side": r.claim.side, "class": r.klass, "key": r.key,
                  "reasons": r.reasons, "claimed": {"qty": r.claim.qty, "price": r.claim.price,
                                                    "fees": r.claim.fees,
                                                    "time_utc": r.claim.time_utc.isoformat() if r.claim.time_utc
                                                    else None},
                  "statement": {"qty": r.qty, "vwap": r.vwap, "commission": r.commission,
                                "executions": len(r.executions)}} for r in results],
        "statement_only": [dict(_exec_view(e), **{"class": k}) for e, k in leftovers],
        "trades": trades,
        "matched_trade_ids": [t["trade_id"] for t in trades if t["statement_matched"]],
        "fees": {"plan": PLANS[plan].name, "assumption": FEE_ASSUMPTION, "counts": fee_counts,
                 "calibration": asdict(cal),
                 "note": "suggestions are never applied; missing commissions are excluded, never zero"},
        "entry_slippage_vs_reference_bps": _q([r["realised_slippage_bps"] for r in recs]),
    })


def render_markdown(rep: Mapping[str, Any]) -> str:
    c = rep["counts"]
    out = ["# IBKR paper statement reconciliation", "",
           "Statement time zone: %s. Files: %s." % (rep["statement"]["statement_tz"], ", ".join(
               "%s (%s, level %s, %d rows)" % (f["name"], f["format"], f["level_used"], f["rows_used"])
               for f in rep["statement"]["files"])), ""]
    for a in rep["alerts"]:
        out.append("**ALERT %s** — %s" % (a["alert"], a.get("detail", a)))
    out += ["", "| class | count |", "|---|---|"] + ["| %s | %d |" % (k, v) for k, v in c.items()]
    out += ["", "Statement-matched closed trades: %d of %d." % (len(rep["matched_trade_ids"]), len(rep["trades"]))]
    fc, cal = rep["fees"]["counts"], rep["fees"]["calibration"]
    out += ["", "## Fees", "",
            "Matched legs %d; with statement commission %d; commission missing %d (excluded, never zero); "
            "non-USD %d." % (fc["matched_legs"], fc["with_statement_commission"], fc["commission_missing"],
                             fc["commission_not_usd"]),
            "Fee calibration (%s): verdict **%s** on %d legs; suggested multiplier %s (not applied)." % (
                rep["fees"]["plan"], cal["fees"]["verdict"], cal["fees"]["n"], cal["suggested_fee_multiplier"]),
            "", "> %s" % rep["fees"]["assumption"], ""]
    bad = [l for l in rep["legs"] if l["class"] != MATCHED]
    if bad:
        out += ["## Legs needing attention", "", "| claim | class | reasons |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (l["claim_id"], l["class"], "; ".join(l["reasons"])) for l in bad]
    if rep["statement_only"]:
        out += ["", "## Statement executions without a claim", "", "| file:row | symbol | side | qty | class |",
                "|---|---|---|---|---|"]
        out += ["| %s:%d | %s | %s | %g | %s |" % (e["file"], e["row"], e["symbol"], e["side"], e["qty"], e["class"])
                for e in rep["statement_only"]]
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m research.economics.statement",
                                 description="Reconcile IBKR paper statements with journal claims (read-only).")
    ap.add_argument("--statement", action="append", required=True, help="IBKR Flex Trades or Activity CSV")
    ap.add_argument("--statement-tz", required=True, help="time zone of the statement times, e.g. America/New_York")
    ap.add_argument("--journal-db", help="COPY of the WS2/D2 loop journal")
    ap.add_argument("--legacy-signals-db", help="COPY of the old system's signals.db")
    ap.add_argument("--plan", choices=sorted(PLANS), default="fixed")
    ap.add_argument("--time-tolerance-s", type=float, default=120.0)
    ap.add_argument("--out-json")
    ap.add_argument("--out-md")
    a = ap.parse_args(argv)
    if not a.journal_db and not a.legacy_signals_db:
        print("at least one claims source (--journal-db or --legacy-signals-db) is required", file=sys.stderr)
        return 2
    try:
        files = [parse_statement(p, tz=a.statement_tz) for p in a.statement]
        claims, stats = [], {}
        if a.journal_db:
            cl, stats["ws2_journal"] = ws2_claims(a.journal_db)
            claims += cl
        if a.legacy_signals_db:
            cl, stats["legacy_intents"] = legacy_claims(a.legacy_signals_db)
            claims += cl
        rep = build_report(files, claims, stats, tz=a.statement_tz, plan=a.plan, time_tol_s=a.time_tolerance_s)
    except (StatementRefused, OSError, sqlite3.Error) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    if a.out_json:
        Path(a.out_json).write_text(json.dumps(rep, indent=1, sort_keys=True))
    md = render_markdown(rep)
    if a.out_md:
        Path(a.out_md).write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["SCHEMA", "MATCHED", "MISMATCH", "PROBABLE", "UNMATCHED_CLAIM", "UNRECORDED", "UNATTRIBUTED",
           "LIVE_ALERT", "StatementRefused", "Execution", "Claim", "StatementFile", "parse_statement",
           "mask_account", "is_paper_account", "ws2_claims", "legacy_claims", "reconcile", "trade_status",
           "fee_records", "build_report", "render_markdown", "main"]

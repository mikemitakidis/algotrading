"""bot/risk_authority/ibkr_paper_pnl_reader.py: M21 PR-b, IBKR PAPER daily P&L source.

READ-ONLY, PAPER-ONLY executions reader for bot.risk_authority.ingest_ibkr.IBKRPnLAdapter
(scope ``ibkr_paper``). It replaces the NotImplementedError stub that kept ibkr_paper
P&L UNKNOWN (Legacy D-3 / R2-3).

Contract with the adapter
-------------------------
``reader(today) -> list[dict]`` where each dict is one execution of the paper account
on the UTC trading day ``today`` with ``time_utc`` (ISO-8601, UTC) and ``realized_pnl``
(USD float). The adapter sums ``realized_pnl`` and treats an EMPTY list as a KNOWN
ZERO. This reader therefore returns a list ONLY when the day is proven complete and
consistent, and raises ``PaperPnLUnavailable`` (the adapter turns any exception into
UNKNOWN, which M14 blocks) in every other case. Unknown is never replaced with zero.

What "complete and consistent" means here (each is a test):
  1. Isolation, before any API call: the M15.4 gateway health gate says ready, mode
     paper, port 4002. The port is 4002 and never the live port. The connection is
     ``readonly=True`` with a dedicated client id.
  2. Account: ``managedAccounts()`` contains the expected paper account and ONLY paper
     accounts (ids starting with "D"). Any other account, such as a live one, refuses.
  3. Execution list: ``reqExecutions`` filtered to the paper account returns after
     ``execDetailsEnd``. Every fill must belong to the paper account and carry a
     timezone-aware timestamp that is not in the future. (ib_insync converts the
     gateway's time to UTC; a gateway time string without a zone is read in the
     host's zone, so the operations plan verifies the gateway and host zones match
     before P3. A wrong zone moves fills across days and then fails item 9.)
  4. Commission reports: every fill must have its commission report (execId match)
     within ``commission_wait_sec``. A missing report makes the day UNKNOWN.
  5. Currency: the contract and the commission must be USD. No FX is invented.
  6. Values: commission must be finite and >= 0. ``realizedPNL`` must be finite, or
     exactly IB's UNSET_DOUBLE, which IB uses for an opening execution (no realised
     P&L). NaN or infinity is UNKNOWN.
  7. Corrections: an execution corrected by IB (same execId base, a different revision
     suffix) makes the day UNKNOWN. Exact duplicates of one execId are de-duplicated.
  8. Accounting (conservative): realised P&L for the day = the sum of ``realizedPNL``
     on closing fills, minus the commission of opening fills (IB books those in
     ``realizedPNL`` only when the position closes). Daily loss is never understated.
  9. Independent cross-check, IB's own account P&L (``reqPnL``):
       * ``realizedPnL`` must arrive and be finite within ``pnl_wait_sec``;
       * it is in the account's BASE currency. The base currency is the account value
         currency whose ``ExchangeRate`` is 1. A non-USD base converts with IB's own
         ``ExchangeRate`` for USD (base units per USD). A missing or unusable rate is
         UNKNOWN;
       * the two figures must agree within max(``abs_tol_usd``, 1.5 x today's
         commissions, ``rel_tol`` x the larger magnitude). The commission term covers
         the documented accounting difference in point 8;
       * the reqPnL day resets on the account's own schedule; with regular-hours-only
         trading, the UTC day and the reset day contain the same session, and any
         coverage gap shows up as a mismatch, which is UNKNOWN.
 10. Verified zero-trade day: no executions AND ``reqPnL.realizedPnL`` == 0 (within
     0.01 base units) AND, when a journal source is supplied, no loop execution today.
     Only then is ``[]`` returned. An empty list is otherwise never returned.
 11. Journal coverage (optional ``journal_exec_ids``): every execution the paper loop
     journalled for today must be in the list the gateway returned. A missing one
     means the broker list is incomplete, which is UNKNOWN.

No order method, no Order/Trade import, no live port literal (AST-tested).
Credentials and identifiers are injected by the caller; this module reads no
environment variable.
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

log = logging.getLogger(__name__)

# Reserved client id (11/12 broker adapter, 15 M15.5 exposure reader, 18 paper loop,
# 99 gateway watchdog).
M21_PNL_CLIENT_ID = 16
IBKR_PAPER_HOST = "127.0.0.1"
IBKR_PAPER_PORT = 4002
UNSET_DOUBLE = 1.7976931348623157e308

DEFAULT_CONNECT_TIMEOUT_SEC = 5.0
DEFAULT_COMMISSION_WAIT_SEC = 10.0
DEFAULT_PNL_WAIT_SEC = 10.0
DEFAULT_ABS_TOL_USD = 2.0
DEFAULT_REL_TOL = 0.02
ZERO_TOL_BASE = 0.01
_POLL_SEC = 0.25

_REVISION_RE = re.compile(r"^(?P<base>.+)\.(?P<rev>\d+)$")


class PaperPnLUnavailable(RuntimeError):
    """The paper P&L for today cannot be proven complete and consistent. The adapter
    converts any exception into UNKNOWN; M14 then blocks."""


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _default_health_checker():
    from bot.gateway_health import assemble_health
    return assemble_health()


def _default_ib_factory():
    from ib_insync import IB
    return IB()


def _default_filter_factory(account: str):
    from ib_insync import ExecutionFilter
    return ExecutionFilter(acctCode=account)


def _check_gateway(health_checker: Callable[[], dict]) -> None:
    h = health_checker()
    if not isinstance(h, dict):
        raise PaperPnLUnavailable("gateway_health_not_a_dict")
    if not h.get("ready_for_ibkr_trading"):
        raise PaperPnLUnavailable("gateway_not_ready:%s" % h.get("status"))
    if h.get("login_error_detected"):
        raise PaperPnLUnavailable("gateway_login_error_detected")
    if h.get("mode") != "paper":
        raise PaperPnLUnavailable("gateway_mode_not_paper:%r" % (h.get("mode"),))
    if h.get("expected_port") != IBKR_PAPER_PORT:
        raise PaperPnLUnavailable("gateway_unexpected_port:%r" % (h.get("expected_port"),))


def _exec_base(exec_id: str) -> str:
    m = _REVISION_RE.match(exec_id)
    return m.group("base") if m else exec_id


def _to_utc(ts: Any) -> datetime:
    if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise PaperPnLUnavailable("execution_time_not_timezone_aware")
    return ts.astimezone(timezone.utc)


def _usd_per_base(account_values: Iterable[Any], account: str) -> Dict[str, Any]:
    """From IB account values: the base currency (the currency whose ExchangeRate is 1)
    and the rate for USD (base units per 1 USD). Raises if either is unusable."""
    rates: Dict[str, float] = {}
    for v in account_values or []:
        if getattr(v, "account", account) not in (account, "", None):
            continue
        if getattr(v, "tag", None) != "ExchangeRate":
            continue
        cur = getattr(v, "currency", None)
        try:
            val = float(getattr(v, "value", "nan"))
        except (TypeError, ValueError):
            continue
        if isinstance(cur, str) and cur and cur != "BASE" and math.isfinite(val) and val > 0:
            rates[cur] = val
    bases = [c for c, r in rates.items() if abs(r - 1.0) < 1e-9]
    if len(bases) != 1:
        raise PaperPnLUnavailable("base_currency_undetermined:%d" % len(bases))
    base = bases[0]
    if base == "USD":
        return {"base": "USD", "base_per_usd": 1.0}
    if "USD" not in rates:
        raise PaperPnLUnavailable("usd_exchange_rate_missing:base=%s" % base)
    return {"base": base, "base_per_usd": rates["USD"]}


class PaperExecutionsReader:
    """Callable ``reader(today)`` for IBKRPnLAdapter(broker_scope='ibkr_paper')."""

    def __init__(self, *, expected_account: str, host: str = IBKR_PAPER_HOST,
                 port: int = IBKR_PAPER_PORT, client_id: int = M21_PNL_CLIENT_ID,
                 connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
                 commission_wait_sec: float = DEFAULT_COMMISSION_WAIT_SEC,
                 pnl_wait_sec: float = DEFAULT_PNL_WAIT_SEC,
                 abs_tol_usd: float = DEFAULT_ABS_TOL_USD, rel_tol: float = DEFAULT_REL_TOL,
                 journal_exec_ids: Optional[Callable[[str], Set[str]]] = None,
                 health_checker: Optional[Callable[[], dict]] = None,
                 ib_factory: Optional[Callable[[], Any]] = None,
                 filter_factory: Optional[Callable[[str], Any]] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 sleep: Optional[Callable[[Any, float], None]] = None):
        if not isinstance(expected_account, str) or not expected_account.startswith("D"):
            raise ValueError("expected_account must be an IBKR paper account id (starts with 'D')")
        if port != IBKR_PAPER_PORT:
            raise ValueError("paper P&L reader is paper-port only (%d)" % IBKR_PAPER_PORT)
        self.expected_account = expected_account
        self.host, self.port, self.client_id = host, port, int(client_id)
        self.connect_timeout_sec = connect_timeout_sec
        self.commission_wait_sec = commission_wait_sec
        self.pnl_wait_sec = pnl_wait_sec
        self.abs_tol_usd, self.rel_tol = abs_tol_usd, rel_tol
        self._journal = journal_exec_ids
        self._health = health_checker or _default_health_checker
        self._ib_factory = ib_factory or _default_ib_factory
        self._filter_factory = filter_factory or _default_filter_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or (lambda ib, s: ib.sleep(s))
        self.last_evidence: Dict[str, Any] = {}

    # ── public ───────────────────────────────────────────────────────────────
    def __call__(self, today: str) -> List[dict]:
        _check_gateway(self._health)                          # (1) before any API call
        ib = self._ib_factory()
        pnl_requested = False
        try:
            try:
                ib.connect(self.host, self.port, clientId=self.client_id,
                           timeout=self.connect_timeout_sec, readonly=True,
                           account=self.expected_account)
            except Exception as e:
                raise PaperPnLUnavailable("connect_failed:%s" % type(e).__name__)
            self._check_accounts(ib)                           # (2)
            fills = self._executions(ib)                       # (3)
            self._await_commissions(ib, fills)                 # (4)
            rows, commissions_usd, all_ids = self._validate(fills, today)  # (3, 5-8)
            pnl_requested = True
            xcheck = self._cross_check(ib, rows, commissions_usd)  # (9, 10)
            self._check_journal(today, all_ids)                # (11)
            self.last_evidence = {"executions": len(rows), "commissions_usd": round(commissions_usd, 4),
                                  "realized_usd": round(sum(r["realized_pnl"] for r in rows), 4), **xcheck}
            return rows
        finally:
            if pnl_requested:
                try:
                    ib.cancelPnL(self.expected_account)
                except Exception:
                    pass
            try:
                ib.disconnect()
            except Exception:
                pass

    # ── steps ────────────────────────────────────────────────────────────────
    def _check_accounts(self, ib) -> None:
        accts = list(ib.managedAccounts() or [])
        if self.expected_account not in accts:
            raise PaperPnLUnavailable("expected_paper_account_not_managed")
        if any(not (isinstance(a, str) and a.startswith("D")) for a in accts):
            raise PaperPnLUnavailable("non_paper_account_visible")

    def _executions(self, ib) -> List[Any]:
        try:
            fills = ib.reqExecutions(self._filter_factory(self.expected_account))
        except Exception as e:
            raise PaperPnLUnavailable("reqExecutions_failed:%s" % type(e).__name__)
        if fills is None:
            raise PaperPnLUnavailable("reqExecutions_returned_none")
        return list(fills)

    def _await_commissions(self, ib, fills: List[Any]) -> None:
        deadline = time.monotonic() + max(0.0, self.commission_wait_sec)
        while True:
            missing = [f for f in fills if getattr(getattr(f, "commissionReport", None), "execId", "")
                       != getattr(getattr(f, "execution", None), "execId", None)]
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise PaperPnLUnavailable("commission_report_missing:%d" % len(missing))
            self._sleep(ib, _POLL_SEC)

    def _validate(self, fills: List[Any], today: str):
        now = self._clock()
        by_id: Dict[str, Any] = {}
        for f in fills:
            ex = getattr(f, "execution", None)
            eid = getattr(ex, "execId", None)
            if not isinstance(eid, str) or not eid:
                raise PaperPnLUnavailable("execution_without_id")
            if eid in by_id:
                continue                                            # exact duplicate: one execution
            by_id[eid] = f
        bases: Dict[str, Set[str]] = {}
        for eid in by_id:
            bases.setdefault(_exec_base(eid), set()).add(eid)
        if any(len(v) > 1 for v in bases.values()):
            raise PaperPnLUnavailable("execution_corrected")       # (7) corrections block

        rows: List[dict] = []
        commissions = 0.0
        for eid, f in sorted(by_id.items()):
            ex, cr, ct = f.execution, f.commissionReport, getattr(f, "contract", None)
            if getattr(ex, "acctNumber", None) != self.expected_account:
                raise PaperPnLUnavailable("execution_for_another_account")
            t = _to_utc(getattr(ex, "time", None))
            if t > now + timedelta(seconds=60):
                raise PaperPnLUnavailable("execution_time_in_future")
            if getattr(ct, "currency", None) != "USD":
                raise PaperPnLUnavailable("execution_currency_not_usd")
            if getattr(cr, "currency", None) != "USD":
                raise PaperPnLUnavailable("commission_currency_not_usd")
            com = getattr(cr, "commission", None)
            if not _finite(com) or float(com) < 0:
                raise PaperPnLUnavailable("commission_invalid")
            rp = getattr(cr, "realizedPNL", None)
            if isinstance(rp, (int, float)) and not isinstance(rp, bool) and float(rp) == UNSET_DOUBLE:
                realized = -float(com)                              # opening fill: its fee only
            elif _finite(rp):
                realized = float(rp)                                # closing fill: IB's realised P&L
            else:
                raise PaperPnLUnavailable("realized_pnl_invalid")
            if t.strftime("%Y-%m-%d") != today:
                continue                                            # another UTC day
            commissions += float(com)
            rows.append({"exec_id": eid, "time_utc": t.isoformat(), "realized_pnl": realized,
                         "symbol": getattr(ct, "symbol", None), "commission_usd": float(com)})
        return rows, commissions, set(by_id)

    def _cross_check(self, ib, rows: List[dict], commissions_usd: float) -> Dict[str, Any]:
        try:
            pnl = ib.reqPnL(self.expected_account)
        except Exception as e:
            raise PaperPnLUnavailable("reqPnL_failed:%s" % type(e).__name__)
        deadline = time.monotonic() + max(0.0, self.pnl_wait_sec)
        while not _finite(getattr(pnl, "realizedPnL", None)):
            if time.monotonic() >= deadline:
                raise PaperPnLUnavailable("reqPnL_realized_unavailable")
            self._sleep(ib, _POLL_SEC)
        ib_realized_base = float(pnl.realizedPnL)
        fx = _usd_per_base(ib.accountValues(self.expected_account), self.expected_account)
        if not rows:
            if abs(ib_realized_base) > ZERO_TOL_BASE:
                raise PaperPnLUnavailable("zero_trade_day_not_verified")
            return {"cross_check": "verified_zero_trade_day", "base_currency": fx["base"]}
        ours = sum(r["realized_pnl"] for r in rows)
        ib_usd = ib_realized_base / fx["base_per_usd"]
        tol = max(self.abs_tol_usd, 1.5 * commissions_usd, self.rel_tol * max(abs(ours), abs(ib_usd)))
        if abs(ours - ib_usd) > tol:
            raise PaperPnLUnavailable("pnl_cross_check_mismatch")
        return {"cross_check": "agree", "base_currency": fx["base"], "ib_realized_usd": round(ib_usd, 4),
                "tolerance_usd": round(tol, 4)}

    def _check_journal(self, today: str, broker_ids: Set[str]) -> None:
        """Every execution the paper loop journalled for ``today`` must be in the
        broker's execution list (any revision of it). The broker list is the one
        the gateway returned, before the UTC-day filter."""
        if self._journal is None:
            return
        try:
            expected = set(self._journal(today) or set())
        except Exception as e:
            raise PaperPnLUnavailable("journal_read_failed:%s" % type(e).__name__)
        have = set(broker_ids) | {_exec_base(i) for i in broker_ids}
        expected = {e if e in have else _exec_base(e) for e in expected}
        if expected - have:
            raise PaperPnLUnavailable("broker_executions_incomplete:%d" % len(expected - have))


_JOURNAL_SQL = (
    "SELECT c.exec_id FROM loop_exec_claims c JOIN loop_orders o ON o.client_ref = c.client_ref "
    "WHERE (c.leg = 'entry' AND substr(o.entry_filled_at_utc, 1, 10) = :day) "
    "   OR (c.leg != 'entry' AND substr(o.exit_filled_at_utc, 1, 10) = :day) "
    "   OR (substr(c.claimed_at_utc, 1, 10) = :day AND "
    "       ((c.leg = 'entry' AND o.entry_filled_at_utc IS NULL) OR "
    "        (c.leg != 'entry' AND o.exit_filled_at_utc IS NULL)))")


def make_journal_exec_ids(db_path: str) -> Callable[[str], Set[str]]:
    """Read-only source of the paper loop's own executions for a UTC day: every
    claimed execution whose leg fill time (or, while the fill time is not yet
    recorded, its claim time) falls on that day. A journal that does not exist
    yet means the loop has recorded no execution (empty set); a journal that
    exists but cannot be read raises, which makes the day UNKNOWN."""
    import os
    import sqlite3

    def _ids(today: str) -> Set[str]:
        if not os.path.exists(db_path):
            return set()
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
        try:
            return {r[0] for r in conn.execute(_JOURNAL_SQL, {"day": today}).fetchall()}
        finally:
            conn.close()
    return _ids


def make_paper_pnl_reader(**kwargs) -> PaperExecutionsReader:
    """Factory used by tools/ingest_risk_state.py for scope ibkr_paper only."""
    return PaperExecutionsReader(**kwargs)


__all__ = ["PaperExecutionsReader", "PaperPnLUnavailable", "make_paper_pnl_reader",
           "make_journal_exec_ids", "M21_PNL_CLIENT_ID", "IBKR_PAPER_PORT"]

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

What "complete and consistent" means here. Every obligation below must hold, or the
day is UNKNOWN (each is a test; PR #37 review, Issue #3 comment 6733991637):

  P1 Isolation, before any API call: the M15.4 gateway health gate says ready, mode
     paper, port 4002. The port is 4002 and never the live port. The connection is
     ``readonly=True`` with a dedicated client id (16).
  P2 Account: ``managedAccounts()`` contains the expected paper account and ONLY paper
     accounts (ids starting with "D"). Every execution must be on that account.
  P3 Period: the reader proves only the CURRENT UTC day (``today`` must be the UTC date
     of now). IBKR's ``reqExecutions`` returns the executions since midnight of the
     gateway's day (IBKR TWS API docs, "Execution details"). That list covers the whole
     UTC day only when the gateway's day began at or before UTC midnight, so the
     operator-verified gateway time zone (``gateway_tz``, IANA name, from P0) is
     required: if the gateway day starts after UTC midnight (a zone west of UTC, or an
     eastern zone late in the UTC day) the early part of the UTC day is not in the
     list, and the day is UNKNOWN. ``ib.TimezoneTWS`` is set to the same zone, so a
     gateway time without a zone is read in the gateway's zone, not the host's. The
     zone claim is also checked against the data: an execution earlier than the
     gateway day's start (as the claimed zone defines it) is UNKNOWN. The opposite
     error (a gateway actually west of the claim, whose list silently starts later)
     cannot be seen in the data; P0 verification, P8 and P9 are the guards there.
  P4 List completion: ``reqExecutions`` (filtered to the paper account) returned after
     ``execDetailsEnd``; every execution has its commission report (execId match)
     within ``commission_wait_sec``; times are timezone-aware and not in the future.
  P5 Currency and values: USD contract and USD commission only (no FX is invented);
     commission finite and >= 0; ``realizedPNL`` finite, or IB's UNSET_DOUBLE, which
     IB uses for an opening execution.
  P6 Corrections: several revisions of one execution (same execId base) make the day
     UNKNOWN; exact duplicates of one execId are de-duplicated.
  P7 Restart persistence: every execution this reader has seen earlier in the same UTC
     day (state file ``seen_path``) must still be listed under the same execId. An
     execution that disappears (a gateway restart that lost the day's list) or is
     replaced by a revision makes the day UNKNOWN. The state file is written only
     after a fully proven read; an unreadable state file is UNKNOWN.
  P8 Loop coverage: every execution the paper loop journalled for the day (read-only
     journal, by leg fill time) must be in the gateway's list.
  P9 Independent agreement with IB's account P&L (``reqPnL``):
       * ``realizedPnL`` must arrive and be finite within ``pnl_wait_sec``;
       * it is compared with the sum of ``realizedPNL`` on today's CLOSING executions.
         IB books an opening execution's commission into the position's average cost,
         so it reaches realised P&L inside the closing execution's ``realizedPNL``;
         opening executions carry no realised P&L (UNSET). With aligned periods (P3),
         the two figures are therefore the same quantity, and the tolerance covers
         rounding only: ``ABS_TOL_USD`` ($1.00, i.e. $0.005 x 200 closing executions,
         and more than ``MAX_CLOSING_FILLS`` closing executions is UNKNOWN);
       * reqPnL is in the account's BASE currency (the currency whose ``ExchangeRate``
         is 1). For a non-USD base it is converted with IB's own USD ``ExchangeRate``,
         and the tolerance adds ``FX_REL_TOL`` (0.5 %) of the larger magnitude, capped
         at ``FX_TOL_CAP_USD`` ($10). The tolerance therefore never exceeds $1 (USD
         base) or $11 (any base), whatever the size of the P&L or of the commissions;
         a larger difference is UNKNOWN. A missing or unusable rate is UNKNOWN;
       * reqPnL resets on IB's own schedule. If that reset is not UTC midnight and an
         execution falls between the two, the figures disagree and the day is
         UNKNOWN; with regular-hours-only trading no execution falls there.
  P10 Verified zero-trade day: no executions today AND ``reqPnL.realizedPnL`` == 0
     (within 0.01 base units) AND no loop execution journalled today. Only then is
     ``[]`` returned.

The value returned for risk is conservative: realised P&L = the closing executions'
``realizedPNL`` minus the commissions of today's opening executions, so daily loss is
never understated (it double-counts an opening commission that IB will later book).

What remains for the real-gateway acceptance tests (operations plan P0-P3), because a
fake cannot establish it: the gateway's actual time zone and day boundary, that
reqExecutions returns the full day after a gateway restart, the reqPnL reset time,
the base currency and ExchangeRate rows of the paper account, and the account ids.

No order method, no Order/Trade import, no live port literal (AST-tested).
Credentials, identifiers, the gateway time zone and file paths are injected by the
caller; this module reads no environment variable. It writes only its own state file.
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
# P9 tolerance (see the module docstring). Not constructor parameters: they cannot be
# loosened by configuration.
ABS_TOL_USD = 1.00
FX_REL_TOL = 0.005
FX_TOL_CAP_USD = 10.00
MAX_CLOSING_FILLS = 200
ZERO_TOL_BASE = 0.01
FUTURE_SKEW = timedelta(seconds=60)
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


def _gateway_day_start_utc(gateway_tz: str, now_utc: datetime) -> datetime:
    """UTC instant at which the gateway's current local day began."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(gateway_tz)
    local = now_utc.astimezone(tz)
    return datetime(local.year, local.month, local.day, tzinfo=tz).astimezone(timezone.utc)


def tolerance_usd(ib_usd: float, closing_usd: float, base_currency: str) -> float:
    """P9 tolerance. At most ABS_TOL_USD for a USD base, ABS_TOL_USD + FX_TOL_CAP_USD
    otherwise, independent of P&L size and commissions."""
    tol = ABS_TOL_USD
    if base_currency != "USD":
        tol += min(FX_REL_TOL * max(abs(ib_usd), abs(closing_usd)), FX_TOL_CAP_USD)
    return tol


class PaperExecutionsReader:
    """Callable ``reader(today)`` for IBKRPnLAdapter(broker_scope='ibkr_paper')."""

    def __init__(self, *, expected_account: str, gateway_tz: Optional[str],
                 seen_path: Optional[str],
                 journal_exec_ids: Optional[Callable[[str], Set[str]]],
                 host: str = IBKR_PAPER_HOST,
                 port: int = IBKR_PAPER_PORT, client_id: int = M21_PNL_CLIENT_ID,
                 connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
                 commission_wait_sec: float = DEFAULT_COMMISSION_WAIT_SEC,
                 pnl_wait_sec: float = DEFAULT_PNL_WAIT_SEC,
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
        self.gateway_tz = gateway_tz
        self.seen_path = seen_path
        self.host, self.port, self.client_id = host, port, int(client_id)
        self.connect_timeout_sec = connect_timeout_sec
        self.commission_wait_sec = commission_wait_sec
        self.pnl_wait_sec = pnl_wait_sec
        self._journal = journal_exec_ids
        self._health = health_checker or _default_health_checker
        self._ib_factory = ib_factory or _default_ib_factory
        self._filter_factory = filter_factory or _default_filter_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or (lambda ib, s: ib.sleep(s))
        self.last_evidence: Dict[str, Any] = {}

    # ── public ───────────────────────────────────────────────────────────────
    def __call__(self, today: str) -> List[dict]:
        now = self._clock()
        self._check_configured()                              # P3/P7/P8 inputs present
        self._check_period(today, now)                        # P3, before any connection
        seen = self._load_seen(today)                         # P7 state readable
        _check_gateway(self._health)                          # P1 before any API call
        ib = self._ib_factory()
        pnl_requested = False
        try:
            try:
                ib.TimezoneTWS = self.gateway_tz              # P3: zone-less times in gateway zone
            except Exception:
                raise PaperPnLUnavailable("gateway_timezone_not_settable")
            try:
                ib.connect(self.host, self.port, clientId=self.client_id,
                           timeout=self.connect_timeout_sec, readonly=True,
                           account=self.expected_account)
            except Exception as e:
                raise PaperPnLUnavailable("connect_failed:%s" % type(e).__name__)
            self._check_accounts(ib)                           # P2
            fills = self._executions(ib)                       # P4
            self._await_commissions(ib, fills)                 # P4
            rows, today_ids, all_ids = self._validate(fills, today, now)  # P2, P4-P6
            self._check_seen(seen, all_ids)                    # P7
            self._check_journal(today, all_ids)                # P8
            pnl_requested = True
            xcheck = self._cross_check(ib, rows)               # P9, P10
            self._save_seen(today, seen | today_ids)           # only after a proven read
            self.last_evidence = {
                "executions": len(rows),
                "closing_executions": sum(1 for r in rows if r["closing"]),
                "commissions_usd": round(sum(r["commission_usd"] for r in rows), 4),
                "realized_usd": round(sum(r["realized_pnl"] for r in rows), 4),
                "gateway_tz": self.gateway_tz, **xcheck}
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
    def _check_configured(self) -> None:
        if not isinstance(self.gateway_tz, str) or not self.gateway_tz:
            raise PaperPnLUnavailable("gateway_timezone_unverified")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(self.gateway_tz)
        except Exception:
            raise PaperPnLUnavailable("gateway_timezone_invalid")
        if not self.seen_path:
            raise PaperPnLUnavailable("seen_state_unconfigured")
        if self._journal is None:
            raise PaperPnLUnavailable("journal_source_unconfigured")

    def _check_period(self, today: str, now: datetime) -> None:
        now = _to_utc(now)
        if now.strftime("%Y-%m-%d") != today:
            raise PaperPnLUnavailable("day_not_current")
        utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        if _gateway_day_start_utc(self.gateway_tz, now) > utc_midnight:
            raise PaperPnLUnavailable("gateway_day_does_not_cover_utc_day")

    def _load_seen(self, today: str) -> Set[str]:
        import json
        import os
        if not os.path.exists(self.seen_path):
            return set()
        try:
            with open(self.seen_path) as f:
                st = json.load(f)
            if not isinstance(st, dict) or not isinstance(st.get("exec_ids"), list) \
                    or not all(isinstance(x, str) for x in st["exec_ids"]):
                raise ValueError("shape")
        except Exception:
            raise PaperPnLUnavailable("seen_state_unreadable")
        return set(st["exec_ids"]) if st.get("day") == today else set()

    def _save_seen(self, today: str, ids: Set[str]) -> None:
        import json
        import os
        d = os.path.dirname(os.path.abspath(self.seen_path))
        os.makedirs(d, exist_ok=True)
        tmp = "%s.tmp.%d" % (self.seen_path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"day": today, "exec_ids": sorted(ids)}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.seen_path)

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

    def _validate(self, fills: List[Any], today: str, now: datetime):
        now = _to_utc(now)
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
            raise PaperPnLUnavailable("execution_corrected")       # P6

        rows: List[dict] = []
        today_ids: Set[str] = set()
        day_start = _gateway_day_start_utc(self.gateway_tz, now)
        for eid, f in sorted(by_id.items()):
            ex, cr, ct = f.execution, f.commissionReport, getattr(f, "contract", None)
            if getattr(ex, "acctNumber", None) != self.expected_account:
                raise PaperPnLUnavailable("execution_for_another_account")
            t = _to_utc(getattr(ex, "time", None))
            if t > now + FUTURE_SKEW:
                raise PaperPnLUnavailable("execution_time_in_future")
            if t < day_start:
                # The list holds an execution from before the gateway's day as the
                # configured zone defines it: the zone claim is wrong (P3).
                raise PaperPnLUnavailable("execution_before_gateway_day")
            if getattr(ct, "currency", None) != "USD":
                raise PaperPnLUnavailable("execution_currency_not_usd")
            if getattr(cr, "currency", None) != "USD":
                raise PaperPnLUnavailable("commission_currency_not_usd")
            com = getattr(cr, "commission", None)
            if not _finite(com) or float(com) < 0:
                raise PaperPnLUnavailable("commission_invalid")
            rp = getattr(cr, "realizedPNL", None)
            if isinstance(rp, (int, float)) and not isinstance(rp, bool) and float(rp) == UNSET_DOUBLE:
                closing, ib_realized = False, 0.0                   # opening fill: no realised P&L
            elif _finite(rp):
                closing, ib_realized = True, float(rp)              # closing fill: IB's realised P&L
            else:
                raise PaperPnLUnavailable("realized_pnl_invalid")
            if t.strftime("%Y-%m-%d") != today:
                continue                                            # another UTC day (P3)
            today_ids.add(eid)
            rows.append({"exec_id": eid, "time_utc": t.isoformat(), "closing": closing,
                         "ib_realized_usd": ib_realized,
                         # conservative value for risk: opening fills count their fee
                         "realized_pnl": ib_realized if closing else -float(com),
                         "symbol": getattr(ct, "symbol", None), "commission_usd": float(com)})
        return rows, today_ids, set(by_id)

    def _check_seen(self, seen: Set[str], listed: Set[str]) -> None:
        gone = seen - listed
        if gone:
            raise PaperPnLUnavailable("execution_disappeared_or_corrected:%d" % len(gone))

    def _cross_check(self, ib, rows: List[dict]) -> Dict[str, Any]:
        closing = [r for r in rows if r["closing"]]
        if len(closing) > MAX_CLOSING_FILLS:
            raise PaperPnLUnavailable("too_many_closing_executions:%d" % len(closing))
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
        closing_usd = sum(r["ib_realized_usd"] for r in closing)
        ib_usd = ib_realized_base / fx["base_per_usd"]
        tol = tolerance_usd(ib_usd, closing_usd, fx["base"])
        if abs(closing_usd - ib_usd) > tol:
            raise PaperPnLUnavailable("pnl_cross_check_mismatch")
        return {"cross_check": "agree", "base_currency": fx["base"],
                "ib_realized_usd": round(ib_usd, 4), "closing_realized_usd": round(closing_usd, 4),
                "tolerance_usd": round(tol, 4)}

    def _check_journal(self, today: str, broker_ids: Set[str]) -> None:
        """P8: every execution the paper loop journalled for ``today`` must be in the
        broker's execution list (any revision of it). The broker list is the one
        the gateway returned, before the UTC-day filter."""
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
           "make_journal_exec_ids", "tolerance_usd", "M21_PNL_CLIENT_ID", "IBKR_PAPER_PORT",
           "ABS_TOL_USD", "FX_REL_TOL", "FX_TOL_CAP_USD", "MAX_CLOSING_FILLS"]

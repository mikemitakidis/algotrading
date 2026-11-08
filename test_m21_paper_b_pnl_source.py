"""test_m21_paper_b_pnl_source.py: M21 PR-b, IBKR PAPER daily P&L source.

Every test uses an in-process FakeIB. Nothing here opens a socket, contacts a
gateway or reads a real account. The account id used is synthetic.

Covers the operator's PR-b acceptance list:
  * complete current-day coverage and the UTC/account-day boundary;
  * execution-list completion and every commission report;
  * a genuinely verified zero-trade day;
  * compatible reqPnL values (reset basis, base currency, accounting difference);
  * missing, corrected, inconsistent or stale data always gives UNKNOWN, never zero;
  * paper never polls LIVE; ibkr_live stays UNKNOWN; no order method exists.
"""
from __future__ import annotations

import ast
import inspect
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_REPO = Path(__file__).resolve().parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# C5a: this module imports tools.ingest_risk_state (loads .env); isolate first.
import _test_isolation  # noqa: E402
_test_isolation.install()

from bot.risk_authority import ibkr_paper_pnl_reader as R  # noqa: E402
from bot.risk_authority.ibkr_paper_pnl_reader import (  # noqa: E402
    PaperExecutionsReader,
    PaperPnLUnavailable,
    UNSET_DOUBLE,
)
from bot.risk_authority.ingest_ibkr import IBKRPnLAdapter  # noqa: E402

ACCT = "DU0000001"                       # synthetic paper id
TODAY = "2026-11-09"
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
SRC = (_REPO / "bot" / "risk_authority" / "ibkr_paper_pnl_reader.py").read_text()

HEALTHY = {"ready_for_ibkr_trading": True, "mode": "paper", "expected_port": 4002,
           "login_error_detected": False, "status": "service_active_api_port_open"}


def _t(h, m=0, day=9):
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


def fill(eid, *, t=None, realized=0.0, commission=1.0, acct=ACCT, cur="USD", ccur="USD",
         report=True, report_eid=None, symbol="AAPL"):
    t = _t(15) if t is None else t
    cr = (SimpleNamespace(execId=report_eid or eid, commission=commission, currency=ccur,
                          realizedPNL=realized)
          if report else SimpleNamespace(execId="", commission=0.0, currency="", realizedPNL=0.0))
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol, currency=cur),
                           execution=SimpleNamespace(execId=eid, time=t, acctNumber=acct),
                           commissionReport=cr)


def av(tag, value, currency, account=ACCT):
    return SimpleNamespace(account=account, tag=tag, value=str(value), currency=currency)


USD_BASE = [av("ExchangeRate", "1.00", "USD"), av("ExchangeRate", "1.00", "BASE")]


class FakeIB:
    """Records every call. ``pnl_after`` = number of sleeps before reqPnL turns finite;
    ``late_reports`` = {execId: report} delivered on the first sleep."""

    def __init__(self, *, accounts=(ACCT,), fills=(), pnl=0.0, pnl_after=0, values=None,
                 connect_exc=None, exec_exc=None, exec_none=False, pnl_exc=None,
                 late_reports=None):
        self.accounts = list(accounts)
        self.fills = list(fills)
        self.pnl_value = pnl
        self.pnl_after = pnl_after
        self.values = USD_BASE if values is None else values
        self.connect_exc, self.exec_exc, self.exec_none, self.pnl_exc = (
            connect_exc, exec_exc, exec_none, pnl_exc)
        self.late_reports = dict(late_reports or {})
        self.calls = []
        self.connect_kwargs = None
        self.sleeps = 0
        self._pnl = None

    def connect(self, host, port, clientId=0, timeout=4, readonly=False, account=""):
        self.calls.append("connect")
        self.connect_kwargs = dict(host=host, port=port, clientId=clientId, readonly=readonly,
                                   account=account)
        if self.connect_exc:
            raise self.connect_exc

    def managedAccounts(self):
        self.calls.append("managedAccounts")
        return list(self.accounts)

    def reqExecutions(self, flt=None):
        self.calls.append("reqExecutions")
        self.exec_filter = flt
        if self.exec_exc:
            raise self.exec_exc
        return None if self.exec_none else list(self.fills)

    def reqPnL(self, account, modelCode=""):
        self.calls.append("reqPnL")
        if self.pnl_exc:
            raise self.pnl_exc
        self._pnl = SimpleNamespace(account=account, realizedPnL=(
            self.pnl_value if self.pnl_after == 0 else float("nan")))
        return self._pnl

    def accountValues(self, account=""):
        self.calls.append("accountValues")
        return [v for v in self.values if not account or v.account == account]

    def cancelPnL(self, account, modelCode=""):
        self.calls.append("cancelPnL")

    def disconnect(self):
        self.calls.append("disconnect")

    def sleep(self, s):
        self.sleeps += 1
        time.sleep(0.001)
        for f in self.fills:
            rep = self.late_reports.pop(f.execution.execId, None)
            if rep is not None:
                f.commissionReport = rep
        if self._pnl is not None and self.sleeps >= self.pnl_after:
            self._pnl.realizedPnL = self.pnl_value


def make(ib, **kw):
    kw.setdefault("health_checker", lambda: dict(HEALTHY))
    kw.setdefault("commission_wait_sec", 0.05)
    kw.setdefault("pnl_wait_sec", 0.05)
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("filter_factory", lambda a: SimpleNamespace(acctCode=a))
    return PaperExecutionsReader(expected_account=ACCT, ib_factory=lambda: ib, **kw)


class _Base(unittest.TestCase):
    def assertUnknown(self, reader, fragment):
        with self.assertRaises(PaperPnLUnavailable) as cm:
            reader(TODAY)
        self.assertIn(fragment, str(cm.exception))
        return cm.exception


# ─── 1. isolation: paper only, read-only, no order surface ──────────────────
class Isolation(_Base):
    def test_no_order_methods_or_order_types_in_source(self):
        tree = ast.parse(SRC)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for bad in ("placeOrder", "cancelOrder", "reqGlobalCancel", "bracketOrder",
                    "MarketOrder", "LimitOrder", "StopOrder", "Order", "Trade",
                    "reqAccountUpdatesMulti", "exerciseOptions"):
            self.assertNotIn(bad, names, bad)

    def test_no_live_port_literal_and_no_env_reads(self):
        consts = {n.value for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Constant)}
        for port in (4001, 7496, 7497):
            self.assertNotIn(port, consts)
        tree = ast.parse(SRC)
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse({"environ", "getenv", "putenv"} & attrs)

    def test_constructor_refuses_live_port_and_non_paper_account(self):
        with self.assertRaises(ValueError):
            PaperExecutionsReader(expected_account=ACCT, port=4001)
        with self.assertRaises(ValueError):
            PaperExecutionsReader(expected_account="U1234567")
        with self.assertRaises(ValueError):
            PaperExecutionsReader(expected_account="")

    def test_connect_is_readonly_paper_port_dedicated_client(self):
        ib = FakeIB()
        make(ib)(TODAY)
        self.assertEqual(ib.connect_kwargs, dict(host="127.0.0.1", port=4002, clientId=16,
                                                 readonly=True, account=ACCT))
        self.assertNotIn(R.M21_PNL_CLIENT_ID, (11, 12, 15, 18, 99))

    def test_gateway_not_ready_refuses_before_any_connection(self):
        for bad in ({"ready_for_ibkr_trading": False},
                    {"mode": "live"},
                    {"expected_port": 4001},
                    {"login_error_detected": True}):
            h = dict(HEALTHY, **bad)
            created = []
            r = PaperExecutionsReader(expected_account=ACCT, health_checker=lambda h=h: h,
                                      ib_factory=lambda: created.append(1))
            with self.assertRaises(PaperPnLUnavailable):
                r(TODAY)
            self.assertEqual(created, [], bad)

    def test_health_checker_failure_refuses(self):
        def boom():
            raise OSError("no systemctl")
        r = PaperExecutionsReader(expected_account=ACCT, health_checker=boom,
                                  ib_factory=lambda: self.fail("must not connect"))
        with self.assertRaises(Exception):
            r(TODAY)

    def test_connect_failure_is_unknown_and_disconnects(self):
        ib = FakeIB(connect_exc=ConnectionRefusedError())
        self.assertUnknown(make(ib), "connect_failed")
        self.assertEqual(ib.calls[-1], "disconnect")


# ─── 2. account: only the paper account, nothing else visible ───────────────
class Accounts(_Base):
    def test_expected_account_missing(self):
        self.assertUnknown(make(FakeIB(accounts=("DU0000002",))), "expected_paper_account_not_managed")

    def test_live_account_visible_refuses(self):
        ib = FakeIB(accounts=(ACCT, "U7654321"))
        self.assertUnknown(make(ib), "non_paper_account_visible")
        self.assertNotIn("reqExecutions", ib.calls)
        self.assertNotIn("reqPnL", ib.calls)

    def test_filter_is_the_paper_account(self):
        ib = FakeIB()
        make(ib)(TODAY)
        self.assertEqual(ib.exec_filter.acctCode, ACCT)

    def test_fill_for_another_account_refuses(self):
        ib = FakeIB(fills=[fill("a.01", acct="DU0000002")])
        self.assertUnknown(make(ib), "execution_for_another_account")


# ─── 3/4. execution list and commission reports ────────────────────────────
class ExecutionsAndCommissions(_Base):
    def test_reqexecutions_failure_or_none(self):
        self.assertUnknown(make(FakeIB(exec_exc=TimeoutError())), "reqExecutions_failed")
        self.assertUnknown(make(FakeIB(exec_none=True)), "reqExecutions_returned_none")

    def test_missing_commission_report_is_unknown(self):
        ib = FakeIB(fills=[fill("a.01", realized=10), fill("b.01", report=False)], pnl=8.0)
        self.assertUnknown(make(ib), "commission_report_missing:1")
        self.assertGreater(ib.sleeps, 0)

    def test_commission_report_for_another_exec_is_missing(self):
        ib = FakeIB(fills=[fill("a.01", report_eid="zzz.01")])
        self.assertUnknown(make(ib), "commission_report_missing")

    def test_late_commission_report_is_awaited(self):
        late = SimpleNamespace(execId="b.01", commission=1.0, currency="USD", realizedPNL=-5.0)
        ib = FakeIB(fills=[fill("a.01", realized=UNSET_DOUBLE),
                           fill("b.01", report=False)],
                    pnl=-5.0, late_reports={"b.01": late})
        rows = make(ib, commission_wait_sec=2.0)(TODAY)
        self.assertEqual(sorted(r["exec_id"] for r in rows), ["a.01", "b.01"])

    def test_naive_or_missing_timestamp(self):
        ib = FakeIB(fills=[fill("a.01", t=datetime(2026, 11, 9, 15, 0))])
        self.assertUnknown(make(ib), "execution_time_not_timezone_aware")
        ib = FakeIB(fills=[fill("a.01", t="2026-11-09T15:00:00Z")])
        self.assertUnknown(make(ib), "execution_time_not_timezone_aware")

    def test_future_timestamp(self):
        ib = FakeIB(fills=[fill("a.01", t=NOW + timedelta(minutes=5))])
        self.assertUnknown(make(ib), "execution_time_in_future")

    def test_execution_without_id(self):
        f = fill("a.01")
        f.execution.execId = ""
        f.commissionReport.execId = ""
        self.assertUnknown(make(FakeIB(fills=[f])), "execution_without_id")


# ─── 5/6. currency and values ───────────────────────────────────────────────
class CurrencyAndValues(_Base):
    def test_non_usd_contract_or_commission(self):
        self.assertUnknown(make(FakeIB(fills=[fill("a.01", cur="EUR")])), "execution_currency_not_usd")
        self.assertUnknown(make(FakeIB(fills=[fill("a.01", ccur="EUR")])), "commission_currency_not_usd")

    def test_invalid_commission(self):
        for bad in (float("nan"), float("inf"), -0.5, None, True):
            ib = FakeIB(fills=[fill("a.01", commission=bad)])
            self.assertUnknown(make(ib), "commission_invalid")

    def test_invalid_realized(self):
        for bad in (float("nan"), float("inf"), float("-inf"), None, "12", True):
            ib = FakeIB(fills=[fill("a.01", realized=bad)])
            self.assertUnknown(make(ib), "realized_pnl_invalid")

    def test_opening_fill_counts_its_commission_as_a_loss(self):
        ib = FakeIB(fills=[fill("a.01", realized=UNSET_DOUBLE, commission=1.25)], pnl=0.0)
        rows = make(ib)(TODAY)
        self.assertEqual(rows[0]["realized_pnl"], -1.25)

    def test_closing_fill_uses_ib_realized(self):
        ib = FakeIB(fills=[fill("a.01", realized=UNSET_DOUBLE, commission=1.0, t=_t(14)),
                           fill("b.01", realized=-42.0, commission=1.0, t=_t(16))], pnl=-42.0)
        rows = make(ib)(TODAY)
        self.assertAlmostEqual(sum(r["realized_pnl"] for r in rows), -43.0)


# ─── 7. corrections and duplicates ─────────────────────────────────────────
class Corrections(_Base):
    def test_corrected_execution_is_unknown(self):
        ib = FakeIB(fills=[fill("0001f4e8.57427fc1.01.01", realized=-10.0),
                           fill("0001f4e8.57427fc1.01.02", realized=-12.0)], pnl=-12.0)
        self.assertUnknown(make(ib), "execution_corrected")

    def test_exact_duplicate_counted_once(self):
        f = fill("0001f4e8.57427fc1.01.01", realized=-10.0)
        ib = FakeIB(fills=[f, f], pnl=-10.0)
        rows = make(ib)(TODAY)
        self.assertEqual(len(rows), 1)


# ─── 9. reqPnL cross-check ─────────────────────────────────────────────────
class CrossCheck(_Base):
    def _two(self, pnl, values=None):
        return FakeIB(fills=[fill("a.01", realized=UNSET_DOUBLE, commission=1.0, t=_t(14)),
                             fill("b.01", realized=-30.0, commission=1.0, t=_t(16))],
                      pnl=pnl, values=values)

    def test_agrees_within_commission_difference(self):
        r = make(self._two(-30.0))
        rows = r(TODAY)
        self.assertEqual(len(rows), 2)
        self.assertEqual(r.last_evidence["cross_check"], "agree")

    def test_mismatch_is_unknown(self):
        self.assertUnknown(make(self._two(-90.0)), "pnl_cross_check_mismatch")
        self.assertUnknown(make(self._two(+40.0)), "pnl_cross_check_mismatch")

    def test_reqpnl_never_finite(self):
        ib = self._two(-30.0)
        ib.pnl_after = 10 ** 6
        self.assertUnknown(make(ib), "reqPnL_realized_unavailable")

    def test_reqpnl_late_but_arrives(self):
        ib = self._two(-30.0)
        ib.pnl_after = 3
        make(ib, pnl_wait_sec=2.0)(TODAY)
        self.assertGreaterEqual(ib.sleeps, 3)

    def test_reqpnl_raises(self):
        ib = self._two(-30.0)
        ib.pnl_exc = RuntimeError("x")
        self.assertUnknown(make(ib), "reqPnL_failed")

    def test_non_usd_base_converts_with_ib_rate(self):
        # base EUR; 1 USD = 0.9 EUR; IB reports -27 EUR = -30 USD
        vals = [av("ExchangeRate", "1.00", "EUR"), av("ExchangeRate", "0.9", "USD")]
        r = make(self._two(-27.0, values=vals))
        r(TODAY)
        self.assertEqual(r.last_evidence["base_currency"], "EUR")
        self.assertAlmostEqual(r.last_evidence["ib_realized_usd"], -30.0)

    def test_non_usd_base_mismatch_after_conversion(self):
        vals = [av("ExchangeRate", "1.00", "EUR"), av("ExchangeRate", "0.5", "USD")]
        self.assertUnknown(make(self._two(-27.0, values=vals)), "pnl_cross_check_mismatch")

    def test_base_currency_or_usd_rate_unknown(self):
        self.assertUnknown(make(self._two(-30.0, values=[])), "base_currency_undetermined")
        two_bases = [av("ExchangeRate", "1", "EUR"), av("ExchangeRate", "1", "USD")]
        self.assertUnknown(make(self._two(-30.0, values=two_bases)), "base_currency_undetermined")
        no_usd = [av("ExchangeRate", "1", "EUR")]
        self.assertUnknown(make(self._two(-30.0, values=no_usd)), "usd_exchange_rate_missing")
        junk = [av("ExchangeRate", "n/a", "USD")]
        self.assertUnknown(make(self._two(-30.0, values=junk)), "base_currency_undetermined")

    def test_other_accounts_rates_ignored(self):
        vals = [av("ExchangeRate", "1", "EUR", account="DU0000002"), *USD_BASE]
        make(self._two(-30.0, values=vals))(TODAY)


# ─── 10/11. verified zero day, day boundary, journal coverage ───────────────
class ZeroDayAndCoverage(_Base):
    def test_verified_zero_trade_day(self):
        r = make(FakeIB(fills=[], pnl=0.0), journal_exec_ids=lambda d: set())
        self.assertEqual(r(TODAY), [])
        self.assertEqual(r.last_evidence["cross_check"], "verified_zero_trade_day")

    def test_empty_list_with_nonzero_ib_realized_is_unknown(self):
        self.assertUnknown(make(FakeIB(fills=[], pnl=-50.0)), "zero_trade_day_not_verified")

    def test_empty_list_with_ib_realized_unavailable_is_unknown(self):
        self.assertUnknown(make(FakeIB(fills=[], pnl=0.0, pnl_after=10 ** 6)),
                           "reqPnL_realized_unavailable")

    def test_empty_list_with_journalled_fill_is_unknown(self):
        r = make(FakeIB(fills=[], pnl=0.0), journal_exec_ids=lambda d: {"x.01"})
        self.assertUnknown(r, "broker_executions_incomplete:1")

    def test_journal_fill_missing_from_broker_list(self):
        ib = FakeIB(fills=[fill("a.01", realized=-5.0)], pnl=-5.0)
        r = make(ib, journal_exec_ids=lambda d: {"a.01", "b.01"})
        self.assertUnknown(r, "broker_executions_incomplete:1")

    def test_journal_matches_broker_list(self):
        ib = FakeIB(fills=[fill("a.01", realized=-5.0)], pnl=-5.0)
        self.assertEqual(len(make(ib, journal_exec_ids=lambda d: {"a.01"})(TODAY)), 1)

    def test_journal_read_failure_is_unknown(self):
        def boom(d):
            raise sqlite3.DatabaseError("x")
        self.assertUnknown(make(FakeIB(fills=[], pnl=0.0), journal_exec_ids=boom),
                           "journal_read_failed")

    def test_previous_utc_day_fill_is_excluded(self):
        ib = FakeIB(fills=[fill("y.01", realized=-70.0, t=_t(22, day=8)),
                           fill("a.01", realized=-5.0)], pnl=-5.0)
        rows = make(ib)(TODAY)
        self.assertEqual([r["exec_id"] for r in rows], ["a.01"])

    def test_reset_basis_mismatch_is_unknown(self):
        # IB's day still holds yesterday's fill; the UTC day does not -> disagree -> UNKNOWN
        ib = FakeIB(fills=[fill("y.01", realized=-70.0, t=_t(22, day=8)),
                           fill("a.01", realized=-5.0)], pnl=-75.0)
        self.assertUnknown(make(ib), "pnl_cross_check_mismatch")

    def test_zero_day_before_ib_reset_is_unknown(self):
        ib = FakeIB(fills=[fill("y.01", realized=-70.0, t=_t(22, day=8))], pnl=-70.0)
        self.assertUnknown(make(ib), "zero_trade_day_not_verified")

    def test_previous_day_fill_still_validated(self):
        ib = FakeIB(fills=[fill("y.01", realized=float("nan"), t=_t(22, day=8))], pnl=0.0)
        self.assertUnknown(make(ib), "realized_pnl_invalid")


# ─── 12. clean-up ──────────────────────────────────────────────────────────
class CleanUp(_Base):
    def test_cancel_pnl_and_disconnect_on_success_and_failure(self):
        ib = FakeIB(fills=[], pnl=0.0)
        make(ib)(TODAY)
        self.assertEqual(ib.calls[-2:], ["cancelPnL", "disconnect"])
        ib = FakeIB(fills=[], pnl=-9.0)
        with self.assertRaises(PaperPnLUnavailable):
            make(ib)(TODAY)
        self.assertEqual(ib.calls[-2:], ["cancelPnL", "disconnect"])

    def test_no_cancel_when_pnl_never_requested(self):
        ib = FakeIB(accounts=("DU0000002",))
        with self.assertRaises(PaperPnLUnavailable):
            make(ib)(TODAY)
        self.assertNotIn("cancelPnL", ib.calls)
        self.assertEqual(ib.calls[-1], "disconnect")


# ─── 13. through the M14.C adapter: unknown is never zero ───────────────────
class ThroughAdapter(_Base):
    def test_success_gives_known_realised(self):
        ib = FakeIB(fills=[fill("a.01", realized=UNSET_DOUBLE, commission=1.0, t=_t(14)),
                           fill("b.01", realized=-30.0, commission=1.0, t=_t(16))], pnl=-30.0)
        rd = IBKRPnLAdapter(broker_scope="ibkr_paper", executions_reader=make(ib)).read(today=TODAY)
        self.assertTrue(rd.success)
        self.assertAlmostEqual(rd.realised_pnl_usd, -31.0)
        self.assertAlmostEqual(rd.realised_daily_loss, 31.0)

    def test_verified_zero_day_is_known_zero(self):
        rd = IBKRPnLAdapter(broker_scope="ibkr_paper",
                            executions_reader=make(FakeIB(fills=[], pnl=0.0))).read(today=TODAY)
        self.assertTrue(rd.success)
        self.assertEqual(rd.realised_pnl_usd, 0.0)

    def test_every_failure_is_unknown_never_zero(self):
        cases = [FakeIB(fills=[], pnl=-1.0), FakeIB(accounts=(ACCT, "U1")),
                 FakeIB(fills=[fill("a.01", report=False)]),
                 FakeIB(fills=[fill("a.01", cur="EUR")]),
                 FakeIB(fills=[fill("a.01", realized=-5.0)], pnl=-500.0),
                 FakeIB(connect_exc=OSError())]
        for ib in cases:
            rd = IBKRPnLAdapter(broker_scope="ibkr_paper", executions_reader=make(ib)).read(today=TODAY)
            self.assertFalse(rd.success)
            self.assertIsNone(rd.realised_pnl_usd)
            self.assertEqual(rd.quality.value, "unknown")

    def test_orchestrator_records_unknown_not_zero(self):
        from bot.flywheel import init_flywheel_tables
        from bot.risk_authority.ingest import ingest_once
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "s.db")
            with sqlite3.connect(db) as c:
                init_flywheel_tables(c)
            ad = IBKRPnLAdapter(broker_scope="ibkr_paper",
                                executions_reader=make(FakeIB(fills=[], pnl=-1.0)))
            conn = sqlite3.connect(db)
            try:
                out = ingest_once(conn, scope="ibkr_paper", today=TODAY, adapter=ad)
                row = conn.execute("SELECT daily_pnl_available, lifecycle_json FROM "
                                   "daily_state_per_broker WHERE date=? AND broker_scope=?",
                                   (TODAY, "ibkr_paper")).fetchone()
                from bot.risk_authority.snapshot import _load_scope_row
                view = _load_scope_row(conn, TODAY, "ibkr_paper")
            finally:
                conn.close()
        self.assertEqual(out["quality"], "unknown")
        self.assertEqual(row[0], 0)                      # M14.C: unknown is not available
        self.assertEqual(json.loads(row[1])["status"], "unknown")
        self.assertFalse(view.is_pnl_known())            # M14 sees unknown, not zero


# ─── 14. CLI wiring: paper reader for ibkr_paper only; live stays UNKNOWN ───
class Wiring(_Base):
    def setUp(self):
        import tools.ingest_risk_state as cli
        self.cli = cli

    def test_ibkr_paper_uses_paper_reader_without_connecting(self):
        from bot.paper_loop_d2.ibkr_adapter import EXPECTED_PAPER_ACCOUNT
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IBKR_ACCOUNT", None)
            ad = self.cli._build_ibkr_adapter("ibkr_paper")
        rd = ad._executions_reader
        self.assertIsInstance(rd, PaperExecutionsReader)
        self.assertTrue(rd.expected_account == EXPECTED_PAPER_ACCOUNT)
        self.assertEqual((rd.port, rd.client_id), (4002, 16))
        self.assertIsNotNone(rd._journal)

    def test_ibkr_live_stays_unknown_and_never_builds_paper_reader(self):
        with mock.patch.object(R, "make_paper_pnl_reader",
                               side_effect=AssertionError("paper reader built for live")):
            ad = self.cli._build_ibkr_adapter("ibkr_live")
        rd = ad.read(today=TODAY)
        self.assertEqual(rd.quality.value, "unknown")
        self.assertIn("NotImplementedError", rd.error)

    def test_env_account_mismatch_refuses(self):
        with mock.patch.dict(os.environ, {"IBKR_ACCOUNT": "DU9999999"}):
            ad = self.cli._build_ibkr_adapter("ibkr_paper")
        self.assertNotIsInstance(ad._executions_reader, PaperExecutionsReader)
        rd = ad.read(today=TODAY)
        self.assertEqual(rd.quality.value, "unknown")
        self.assertNotIn("DU9999999", rd.error or "")

    def test_env_account_live_refuses(self):
        with mock.patch.dict(os.environ, {"IBKR_ACCOUNT": "U1234567"}):
            ad = self.cli._build_ibkr_adapter("ibkr_paper")
        self.assertEqual(ad.read(today=TODAY).quality.value, "unknown")

    def test_construction_failure_refuses(self):
        with mock.patch.object(R, "make_paper_pnl_reader", side_effect=ImportError("x")):
            ad = self.cli._build_ibkr_adapter("ibkr_paper")
        self.assertEqual(ad.read(today=TODAY).quality.value, "unknown")


# ─── 15. journal source: read-only, day by fill time ───────────────────────
class JournalSource(_Base):
    def _db(self, d):
        from bot.paper_loop_d2.journal import LoopJournal
        p = os.path.join(d, "j.db")
        LoopJournal(p)                              # creates the schema
        c = sqlite3.connect(p)
        common = ("cyc", "AAPL", "LONG", 1.0, "s", "ibkr_paper", "2026-11-09T14:00:00+00:00",
                  "2026-11-09T14:00:00+00:00")
        sql = ("INSERT INTO loop_orders (client_ref, cycle_key, symbol, direction, qty, state, "
               "data_source, planned_at_utc, updated_at_utc, entry_filled_at_utc, "
               "exit_filled_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        c.execute(sql, ("r1",) + common + ("2026-11-09T14:31:00+00:00", "2026-11-09T19:00:00+00:00"))
        c.execute(sql, ("r2",) + common + ("2026-11-08T15:00:00+00:00", "2026-11-09T15:00:00+00:00"))
        c.execute(sql, ("r3",) + common + (None, None))
        c.execute(sql, ("r4",) + common + ("2026-11-08T15:00:00+00:00", None))
        claims = [("e1", "r1", "entry", "2026-11-09T14:31:05+00:00"),
                  ("x1", "r1", "take_profit", "2026-11-09T19:00:05+00:00"),
                  ("e2", "r2", "entry", "2026-11-08T15:00:05+00:00"),
                  ("x2", "r2", "stop_loss", "2026-11-09T15:00:05+00:00"),
                  ("e3", "r3", "entry", "2026-11-09T15:10:00+00:00"),
                  ("e4", "r4", "entry", "2026-11-09T00:10:00+00:00")]
        c.executemany("INSERT INTO loop_exec_claims VALUES (?,?,?,?)", claims)
        c.commit()
        c.close()
        return p

    def test_day_selection_by_fill_time(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._db(d)
            before = Path(p).read_bytes()
            ids = R.make_journal_exec_ids(p)(TODAY)
            after = Path(p).read_bytes()
        # e4: claimed today but its entry fill time is yesterday -> not today
        self.assertEqual(ids, {"e1", "x1", "x2", "e3"})
        self.assertEqual(before, after)

    def test_absent_journal_is_empty_and_corrupt_raises(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(R.make_journal_exec_ids(os.path.join(d, "none.db"))(TODAY), set())
            bad = os.path.join(d, "bad.db")
            Path(bad).write_bytes(b"not a database" * 100)
            with self.assertRaises(sqlite3.DatabaseError):
                R.make_journal_exec_ids(bad)(TODAY)
            self.assertFalse(os.path.exists(os.path.join(d, "none.db")))


# ─── contract with ib_insync (skipped where ib_insync is not installed) ─────
class IbInsyncContract(unittest.TestCase):
    def setUp(self):
        try:
            import ib_insync  # noqa: F401
        except Exception:
            self.skipTest("ib_insync not installed")

    def test_field_and_method_names(self):
        import ib_insync as I
        self.assertTrue({"execId", "commission", "currency", "realizedPNL"}
                        <= set(I.CommissionReport.__dataclass_fields__))
        self.assertTrue({"execId", "time", "acctNumber"} <= set(I.Execution.__dataclass_fields__))
        self.assertIn("acctCode", I.ExecutionFilter.__dataclass_fields__)
        self.assertIn("realizedPnL", I.PnL.__dataclass_fields__)
        self.assertTrue({"account", "tag", "value", "currency"} <= set(I.AccountValue._fields))
        sig = inspect.signature(I.IB.connect)
        self.assertTrue({"readonly", "account", "clientId", "timeout"} <= set(sig.parameters))
        for m in ("managedAccounts", "reqExecutions", "reqPnL", "cancelPnL", "accountValues",
                  "disconnect", "sleep"):
            self.assertTrue(callable(getattr(I.IB, m)), m)
        self.assertEqual(I.util.UNSET_DOUBLE, UNSET_DOUBLE)


if __name__ == "__main__":
    unittest.main()

"""test_m21_paper_e2e_ibkr_paper.py: M21 paper-trading cross-lane end-to-end (PR-a..PR-e).

One simulated IBKR PAPER gateway (a shared fake broker world, no socket) behind every
component that talks to IBKR, and the REAL code everywhere else:

  candidate -> PaperLearningLoop (real) -> D0 gate (real, temp policy DB)
            -> D1 gate (real, stubbed Alpaca HTTP) -> risk gate (real)
               -> refresher: M15.5 positions reader + IBKRExposureAdapter + ingest_exposure_once
                             PR-b PaperExecutionsReader + IBKRPnLAdapter + ingest_once
               -> M14 run_risk_preflight (real engine, real snapshot, PR-a domains, PR-c ages)
            -> IbkrLoopBroker (real adapter) -> fake gateway
            -> journal (real) -> monitoring checks (real, PR-d)

Negative cases requested in review: LIVE isolation, order duplication, missing/stale data,
failures during startup and recovery. The account id is synthetic.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import _test_isolation  # noqa: E402
_test_isolation.install()

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import test_m14_f_preflight as F  # noqa: E402
import test_m21_1extra_d2_3_learning_loop as D23  # noqa: E402
from bot.paper_loop_d2.ibkr_adapter import IbkrLoopBroker  # noqa: E402
from bot.paper_loop_d2.journal import LoopJournal  # noqa: E402
from bot.paper_loop_d2.scheduler import LoopConfig, PaperLearningLoop  # noqa: E402
from bot.risk_authority.ibkr_paper_pnl_reader import (  # noqa: E402
    UNSET_DOUBLE, PaperExecutionsReader, make_journal_exec_ids)

SYN = "DU0000001"                         # synthetic paper account
LIVE = "U0000001"                         # synthetic live account (must never be read)
HEALTHY = {"ready_for_ibkr_trading": True, "mode": "paper", "expected_port": 4002,
           "login_error_detected": False, "status": "service_active_api_port_open"}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


# ─── the simulated IBKR PAPER gateway ───────────────────────────────────────
class PaperWorld(D23.FakeWorld):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.accounts = [SYN]
        self.account_values = [
            D23.FakeIBValue(account=SYN, tag="NetLiquidation", value="1000000", currency="USD"),
            D23.FakeIBValue(account=SYN, tag="ExchangeRate", value="1.00", currency="USD")]
        self.ticker = SimpleNamespace(last=100.0, bid=99.9, ask=100.1, time=clock(),
                                      marketDataType=1)
        self.ib_realized = 0.0
        self.health = dict(HEALTHY)
        self.pnl_health = None             # if set, what the P&L reader's health gate sees
        self.snapshot_ready = True
        self.pnl_available = True
        self.connects = []                 # (client_id, readonly, port) of read-only sessions
        self.fill_market = True
        self.n = 0

    def add_execution(self, symbol, con_id, side, qty, price, *, ref="", perm=1, acct=None, t=None,
                      report="match", commission=1.0, realized=None):
        self.n += 1
        ex = SimpleNamespace(execId="0001f4e8.%08d.01.01" % self.n, acctNumber=acct or SYN,
                             time=t or self.clock(), side=side, shares=qty, price=price, orderId=1,
                             permId=perm, orderRef=ref)
        rep = {"match": SimpleNamespace(execId=ex.execId, commission=commission, currency="USD",
                                        realizedPNL=UNSET_DOUBLE if realized is None else realized),
               "default": SimpleNamespace(execId="", commission=0.0, currency="", realizedPNL=0.0),
               "none": None}[report]
        self.executions.append(SimpleNamespace(execution=ex, contract=self.contract(symbol, con_id),
                                               commissionReport=rep, time=ex.time))
        if report == "match" and realized is not None:
            self.ib_realized += realized

    def set_position(self, symbol, con_id, qty, account=SYN):
        super().set_position(symbol, con_id, qty, account=account)

    def add_trade(self, *a, account=SYN, **k):
        return super().add_trade(*a, account=account, **k)

    def portfolio(self):
        return [SimpleNamespace(contract=p.contract, position=p.position,
                                marketPrice=self.ticker.last,
                                marketValue=p.position * self.ticker.last,
                                averageCost=p.avgCost,
                                unrealizedPNL=(self.ticker.last - p.avgCost) * p.position)
                for p in self.positions if p.account == SYN]


class ReadOnlyIB(D23.FakeIB):
    """A read-only session (P&L reader). Any order method is a test failure."""

    def connect(self, host, port, clientId=0, timeout=4, readonly=False, account=""):
        self.w.connects.append((clientId, readonly, port))
        if port != 4002:
            raise AssertionError("read-only session on a non-paper port")

    def reqPnL(self, account, modelCode=""):
        return SimpleNamespace(realizedPnL=(self.w.ib_realized if self.w.pnl_available
                                            else float("nan")))

    def cancelPnL(self, account, modelCode=""):
        pass

    def placeOrder(self, *a, **k):
        raise AssertionError("read-only session placed an order")

    def cancelOrder(self, *a, **k):
        raise AssertionError("read-only session cancelled an order")


# ─── the chain ──────────────────────────────────────────────────────────────
class Chain:
    def __init__(self, tc, *, lane=True, policy_overrides=None):
        self.tc = tc
        self.dir = tempfile.mkdtemp(prefix="paper_e2e_")
        tc.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.now = utc_now()
        et = self.now.astimezone(ZoneInfo("America/New_York"))
        if et.hour == 23 and et.minute >= 50:
            tc.skipTest("too close to the end of the New York day for the session fixture")
        self.clock = D23.LoopClock(self.now)
        self.world = PaperWorld(self.clock)
        self.signals = os.path.join(self.dir, "signals.db")
        self.journal_db = os.path.join(self.dir, "paper_lifecycles.db")
        self.kill = False
        self.refreshes = 0
        with sqlite3.connect(self.signals) as c:
            from bot.flywheel import init_flywheel_tables
            init_flywheel_tables(c)
        pol = dict(policy_overrides or {})
        if lane:
            pol.update({"ibkr_paper.auto_trading_enabled": True,
                        "ibkr_paper.auto_trading_enabled_until_utc": F._future_utc_iso(8)})
        from bot.broker_allocation import save_policy
        with sqlite3.connect(self.signals) as c:
            save_policy(c, F._valid_policy(**pol))
        self.candidates = [D23.cand()]
        self.loop = self.make_loop()

    # sources
    def alpaca(self, is_open=True):
        et = self.now.astimezone(ZoneInfo("America/New_York"))
        close = et.replace(hour=23, minute=59, second=0).astimezone(timezone.utc)
        z = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731
        return D23.alpaca_source(is_open=is_open, ts=z(self.clock()), next_close=z(close),
                                 next_open=z(close + timedelta(hours=10)),
                                 cal=({"date": et.strftime("%Y-%m-%d"), "open": "00:00",
                                       "close": "23:59"},))

    def refresher(self):
        """In-process equivalent of the two ingestion CLIs the production refresher runs
        (rc 0 iff the reading is known), wired to the REAL readers, adapters and writers."""
        from bot.risk_authority.ibkr_paper_reader import make_ibkr_paper_positions_reader
        from bot.risk_authority.ingest import ingest_once
        from bot.risk_authority.ingest_exposure import ingest_exposure_once
        from bot.risk_authority.ingest_ibkr import IBKRPnLAdapter
        from bot.risk_authority.ingest_ibkr_exposure import IBKRExposureAdapter
        w = self.world

        def session(host, port, client_id, connect_timeout, api_timeout):
            w.connects.append((client_id, True, port))
            return {"portfolio_items": w.portfolio(),
                    "position_records": [p for p in w.positions if p.account == SYN],
                    "snapshot_ready": w.snapshot_ready, "account_values_count": 2,
                    "snapshot_waited_sec": 0.0}

        def refresh():
            self.refreshes += 1
            pos = make_ibkr_paper_positions_reader(health_checker=lambda: dict(w.health),
                                                   ib_session_factory=session)
            pnl = PaperExecutionsReader(
                expected_account=SYN, health_checker=lambda: dict(w.pnl_health or w.health),
                ib_factory=lambda: ReadOnlyIB(w), filter_factory=lambda a: SimpleNamespace(acctCode=a),
                clock=self.clock, commission_wait_sec=0.05, pnl_wait_sec=0.05,
                journal_exec_ids=make_journal_exec_ids(self.journal_db), sleep=lambda ib, s: None)
            conn = sqlite3.connect(self.signals)
            try:
                e = ingest_exposure_once(conn, scope="ibkr_paper",
                                         adapter=IBKRExposureAdapter("ibkr_paper", positions_reader=pos))
                if e.get("quality") == "exposure_unknown":
                    return False, "exposure_refresh_rc_1"
                p = ingest_once(conn, scope="ibkr_paper",
                                adapter=IBKRPnLAdapter(broker_scope="ibkr_paper", executions_reader=pnl))
                if p.get("quality") == "unknown":
                    return False, "pnl_refresh_rc_1"
            finally:
                conn.close()
            return True, "refreshed"
        return refresh

    def make_loop(self, **kw):
        from bot.paper_loop_d2 import make_d0_gate, make_d1_gate
        from bot.paper_loop_d2.risk_gate import make_market_state_provider, make_risk_authority_gate
        from bot.runtime_policy import get_signal_only_reason
        broker = IbkrLoopBroker(
            ib_factory=lambda: D23.FakeIB(self.world),
            get_params=lambda: ("127.0.0.1", 4002, SYN, 1), is_live=lambda: False,
            kill_switch_active=lambda: self.kill,
            runtime_policy=lambda: get_signal_only_reason("ibkr_paper", db_path=self.signals,
                                                          ttl_sec=0),
            gateway_available=lambda h, p: True, expected_account=SYN, clock=self.clock,
            ack_wait_seconds=0, cancel_wait_seconds=1, close_fill_wait_seconds=1, poll_seconds=0.5)
        ms = make_market_state_provider(sources=[self.alpaca()], clock=self.clock)
        gate = make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(self.signals),
                                        market_state=ms, clock=self.clock,
                                        refresher=kw.pop("refresher", self.refresher()))
        args = dict(journal=LoopJournal(self.journal_db), broker=broker,
                    d0_gate=make_d0_gate(db_path=self.signals, ttl_sec=0),
                    d1_gate=make_d1_gate(sources=[self.alpaca()], now_utc=self.clock()),
                    kill_switch_active=lambda: self.kill, risk_gate=gate,
                    candidate_provider=lambda now: [
                        __import__("dataclasses").replace(c, signal_ts_utc=now.isoformat())
                        for c in self.candidates],
                    lock_path=os.path.join(self.dir, "loop.lock"),
                    config=LoopConfig(interval_minutes=15, max_hold_minutes=600),
                    clock=self.clock, pid=os.getpid())
        args.update(kw)
        return PaperLearningLoop(**args)

    # observations
    def tick(self):
        return self.loop.tick()

    def parents(self):
        return [o for o in self.world.placed if not getattr(o, "parentId", 0)]

    def decisions(self, scope="ibkr_paper"):
        with sqlite3.connect(self.signals) as c:
            return c.execute("SELECT decision_id, result, reason_codes FROM risk_decisions "
                             "WHERE broker_scope=?", (scope,)).fetchall()

    def orders(self):
        return LoopJournal(self.journal_db).orders()

    def cycles(self):
        from bot.monitoring.probe import read_journal
        return read_journal(self.journal_db)["cycles"]

    def row(self, scope):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with sqlite3.connect(self.signals) as c:
            return c.execute("SELECT * FROM daily_state_per_broker WHERE date=? AND broker_scope=?",
                             (today, scope)).fetchone()


class _E2E(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"ALPACA_KEY": "test-key", "ALPACA_SECRET": "test-secret"})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("IBKR_LIVE_CONFIRMED", None)

    def assertNoOrder(self, ch):
        self.assertEqual(ch.world.placed, [])
        self.assertEqual([o for o in ch.orders() if o["state"] != "REJECTED"], [])


# ─── happy path ─────────────────────────────────────────────────────────────
class HappyPath(_E2E):
    def test_entry_through_real_m14_with_fresh_paper_data(self):
        ch = Chain(self)
        r = ch.tick()
        self.assertEqual(r["entry"]["state"], "SUBMITTED", r)
        self.assertEqual(len(ch.parents()), 1)
        self.assertTrue(all(o.orderRef.startswith("M21L:") for o in ch.world.placed))
        o = ch.orders()[0]
        allow = [d for d in ch.decisions() if d[1] == "allow"]
        self.assertEqual(len(allow), 1)
        self.assertEqual(o["risk_decision_id"], allow[0][0])
        self.assertEqual(ch.refreshes, 1)
        # every read-only session: paper port, read-only, the reserved ids (15 positions, 16 P&L)
        self.assertTrue(ch.world.connects)
        self.assertEqual({(cid, ro, port) for cid, ro, port in ch.world.connects},
                         {(15, True, 4002), (16, True, 4002)})
        # nothing was read or written for a real-money scope
        self.assertIsNone(ch.row("ibkr_live"))
        self.assertIsNone(ch.row("etoro_real"))
        self.assertEqual(ch.decisions("ibkr_live"), [])

    def test_after_the_fill_the_next_refresh_sees_position_and_commission(self):
        ch = Chain(self)
        ch.tick()                                        # entry filled at market (simulated)
        ch.clock.advance(15)
        ch.world.ticker.time = ch.clock()
        r = ch.tick()
        self.assertEqual(r["entry_block"], "max_open_trades")      # loop cap before any refresh
        self.assertEqual(ch.refresher()(), (True, "refreshed"))
        with sqlite3.connect(ch.signals) as c:
            lc = json.loads(c.execute("SELECT lifecycle_json FROM daily_state_per_broker "
                                      "WHERE broker_scope='ibkr_paper'").fetchone()[0])
        self.assertIn(lc["exposure_status"], ("exposure_fresh", "exposure_partial"))
        self.assertEqual(lc["exposure_latest_reading"]["open_positions_count"], 1)
        self.assertEqual(lc["latest_reading"]["realised_pnl_usd"], -1.0)   # opening commission
        self.assertIn(lc["status"], ("fresh", "partial"))          # M14: both are known


# ─── LIVE isolation ─────────────────────────────────────────────────────────
class LiveIsolation(_E2E):
    def test_live_account_visible_blocks_without_reading_it(self):
        ch = Chain(self)
        ch.world.accounts = [SYN, LIVE]
        r = ch.tick()
        # the PR-b reader refuses a session that can see a non-paper account
        self.assertEqual(r["entry_block"], "risk:risk_refresh_block:pnl_refresh_rc_1", r)
        self.assertNoOrder(ch)
        self.assertEqual(ch.decisions(), [])            # M14 never asked
        self.assertIsNone(ch.row("ibkr_live"))
        self.assertEqual(ch.decisions("ibkr_live"), [])

    def test_family_flag_alone_never_trades_paper(self):
        ch = Chain(self, lane=False, policy_overrides={"ibkr.auto_trading_enabled": True})
        r = ch.tick()
        self.assertTrue(r["entry_block"].startswith("d0_blocked"), r["entry_block"])
        self.assertNoOrder(ch)
        self.assertEqual(ch.refreshes, 0)

    def test_m14_checks_the_paper_lane_itself(self):
        # Defence in depth: even if the loop's D0 gate said yes, M14 refuses a
        # paper entry without the paper lane (the family flag alone is not enough).
        ch = Chain(self, lane=False, policy_overrides={"ibkr.auto_trading_enabled": True})
        ch.loop = ch.make_loop(d0_gate=lambda: (True, "authorized"))
        r = ch.tick()
        self.assertEqual(r["entry_block"], "risk:risk_authority_block:broker_auto_disabled", r)
        self.assertNoOrder(ch)

    def test_paper_authorisation_never_authorises_live(self):
        from bot.risk_authority import Authority, run_risk_preflight
        from bot.risk_authority.engine import TradeRequest
        ch = Chain(self, policy_overrides={"ibkr.auto_trading_enabled": True,
                                           "ibkr_live.auto_trading_enabled": True,
                                           "ibkr_live.auto_trading_enabled_until_utc":
                                               F._future_utc_iso()})
        ch.tick()                                       # paper entry allowed
        with sqlite3.connect(ch.signals) as c:          # no IBKR_LIVE_CONFIRMED: live refused
            res = run_risk_preflight(c, broker_scope="ibkr_live",
                                     request=TradeRequest(symbol="AAA", amount_usd=100.0, side="long",
                                                          sl=95.0, tp=110.0),
                                     current_authority=Authority.AUTO_ALLOWED, actor="e2e",
                                     audit_source="auto", market_open=True)
        self.assertFalse(res.allowed)
        self.assertIn("broker_auto_disabled", res.reason_codes)

    def test_live_holding_and_live_loss_never_gate_paper(self):
        ch = Chain(self)
        with sqlite3.connect(ch.signals) as c:          # a large, unknown/stale LIVE state
            F._seed_fresh_state(c, scope="ibkr_live", pnl_known=False, exposure_known=False,
                                fetched_at=(ch.now - timedelta(days=1)).isoformat())
            before = c.execute("SELECT * FROM daily_state_per_broker WHERE broker_scope='ibkr_live'"
                               ).fetchall()
        r = ch.tick()
        self.assertEqual(r["entry"]["state"], "SUBMITTED", r)
        with sqlite3.connect(ch.signals) as c:
            after = c.execute("SELECT * FROM daily_state_per_broker WHERE broker_scope='ibkr_live'"
                              ).fetchall()
        self.assertEqual(before, after)                 # the live row is never written

    def test_live_fill_in_the_paper_feed_blocks(self):
        ch = Chain(self)
        ch.world.add_execution("ZZZ", 999, "BOT", 1, 10.0, acct=LIVE)
        r = ch.tick()
        self.assertEqual(r["entry_block"], "risk:risk_refresh_block:pnl_refresh_rc_1")
        self.assertNoOrder(ch)


# ─── order duplication ──────────────────────────────────────────────────────
class Duplication(_E2E):
    def test_same_slot_twice_one_order(self):
        ch = Chain(self)
        ch.tick()
        r = ch.tick()                                   # same 15-minute slot: skipped
        self.assertTrue(r["skipped"], r)
        self.assertEqual(len(ch.parents()), 1)
        self.assertEqual(ch.refreshes, 1)

    def test_restart_with_a_new_loop_on_the_same_journal_one_order(self):
        ch = Chain(self)
        ch.tick()
        ch.loop = ch.make_loop()
        ch.clock.advance(15)
        ch.world.ticker.time = ch.clock()
        r = ch.tick()
        self.assertEqual(r["entry_block"], "max_open_trades")
        self.assertEqual(len(ch.parents()), 1)

    def test_lost_ack_is_not_resubmitted(self):
        ch = Chain(self)
        ch.world.fail_place_at = 1                      # parent reaches the broker, the TP fails
        r = ch.tick()
        self.assertEqual(r["entry_block"], "submit_outcome_unknown")
        n = len(ch.parents())
        for _ in range(3):
            ch.clock.advance(15)
            ch.world.ticker.time = ch.clock()
            ch.world.fail_place_at = None
            ch.loop = ch.make_loop()                   # restart each time
            r = ch.tick()
            self.assertIn(r["entry_block"], ("max_open_trades", "unresolved_pending_submit",
                                             "journal_needs_review"), r)
        self.assertEqual(n, 1)
        self.assertEqual(len(ch.parents()), n)
        self.assertEqual(len(ch.orders()), 1)


# ─── missing / stale data ───────────────────────────────────────────────────
class MissingData(_E2E):
    def _blocked(self, ch, frag):
        r = ch.tick()
        self.assertIn(frag, r["entry_block"] or "", r)
        self.assertNoOrder(ch)
        self.assertEqual([d for d in ch.decisions() if d[1] == "allow"], [])
        return r

    def test_exposure_snapshot_not_ready(self):
        ch = Chain(self)
        ch.world.snapshot_ready = False
        self._blocked(ch, "risk_refresh_block:exposure_refresh_rc_1")

    def test_missing_commission_report(self):
        ch = Chain(self)
        ch.world.add_execution("AAA", 111, "BOT", 1, 100.0, report="default")
        self._blocked(ch, "risk_refresh_block:pnl_refresh_rc_1")

    def test_commission_report_not_matched_to_its_execution(self):
        ch = Chain(self)
        ch.world.add_execution("CCC", 333, "SLD", 1, 40.0, realized=-5.0)
        ch.world.executions[-1].commissionReport.execId = ""    # report not yet attributed
        self._blocked(ch, "risk_refresh_block:pnl_refresh_rc_1")

    def test_reqpnl_unavailable(self):
        ch = Chain(self)
        ch.world.pnl_available = False
        self._blocked(ch, "risk_refresh_block:pnl_refresh_rc_1")

    def test_reqpnl_disagrees_on_a_zero_trade_day(self):
        ch = Chain(self)
        ch.world.ib_realized = -250.0                    # IB says a loss our fills do not show
        self._blocked(ch, "risk_refresh_block:pnl_refresh_rc_1")

    def test_reqpnl_disagrees_with_the_fills(self):
        ch = Chain(self)
        ch.world.add_execution("CCC", 333, "SLD", 1, 40.0, realized=-5.0)
        ch.world.ib_realized -= 250.0                    # an execution the gateway did not list
        self._blocked(ch, "risk_refresh_block:pnl_refresh_rc_1")

    def test_corrected_execution(self):
        ch = Chain(self)
        ch.world.add_execution("AAA", 111, "SLD", 1, 99.0, realized=-5.0)
        ex = ch.world.executions[-1]
        corrected = SimpleNamespace(**{**ex.__dict__})
        corrected.execution = SimpleNamespace(**{**ex.execution.__dict__,
                                                 "execId": ex.execution.execId[:-2] + "02"})
        corrected.commissionReport = SimpleNamespace(**{**ex.commissionReport.__dict__,
                                                        "execId": corrected.execution.execId})
        ch.world.executions.append(corrected)
        ch.world.ib_realized = -10.0                     # even if IB's total matched both copies
        self._blocked(ch, "risk_refresh_block:pnl_refresh_rc_1")

    def test_refresh_that_writes_nothing_meets_m14_age_check(self):
        ch = Chain(self)
        with sqlite3.connect(ch.signals) as c:
            F._seed_fresh_state(c, scope="ibkr_paper",
                                fetched_at=(ch.now - timedelta(minutes=10)).isoformat())
        ch.loop = ch.make_loop(refresher=lambda: (True, "refreshed"))
        self._blocked(ch, "risk_authority_block:exposure_stale")

    def test_monitor_flags_repeated_data_blocks(self):
        from bot.monitoring.risk_data import check_risk_data_blocks
        ch = Chain(self)
        ch.world.pnl_available = False
        ch.tick()
        ch.clock.advance(15)
        ch.world.ticker.time = ch.clock()
        ch.loop = ch.make_loop()
        ch.tick()
        res = check_risk_data_blocks(ch.cycles(), market_open=True)
        self.assertEqual(res.status, "ALARM", [c.get("reason") for c in ch.cycles()])


# ─── startup and recovery failures ──────────────────────────────────────────
class StartupAndRecovery(_E2E):
    def test_gateway_not_ready(self):
        ch = Chain(self)
        ch.world.health = dict(HEALTHY, ready_for_ibkr_trading=False, status="service_inactive")
        r = ch.tick()
        self.assertEqual(r["entry_block"], "risk:risk_refresh_block:exposure_refresh_rc_1", r)
        self.assertNoOrder(ch)
        self.assertEqual(ch.world.connects, [])          # no read-only session either

    def test_gateway_drops_between_the_two_reads(self):
        ch = Chain(self)
        ch.world.pnl_health = dict(HEALTHY, ready_for_ibkr_trading=False, status="service_inactive")
        r = ch.tick()
        self.assertEqual(r["entry_block"], "risk:risk_refresh_block:pnl_refresh_rc_1", r)
        self.assertNoOrder(ch)
        self.assertEqual([c for c in ch.world.connects if c[0] == 16], [])   # refused before connect

    def test_gateway_in_live_mode(self):
        ch = Chain(self)
        ch.world.health = dict(HEALTHY, mode="live", expected_port=4001)
        r = ch.tick()
        self.assertIn("risk_refresh_block", r["entry_block"] or "", r)
        self.assertNoOrder(ch)
        self.assertEqual([c for c in ch.world.connects if c[2] != 4002], [])

    def test_kill_switch_stops_before_any_broker_read_for_risk(self):
        ch = Chain(self)
        ch.kill = True
        r = ch.tick()
        self.assertEqual(r["entry_block"], "kill_switch_active")
        self.assertEqual(ch.refreshes, 0)
        self.assertNoOrder(ch)

    def test_market_closed_no_refresh_no_order(self):
        ch = Chain(self)
        ch.loop = ch.make_loop(d1_gate=lambda: (False, "market_closed"))
        r = ch.tick()
        self.assertTrue(r["entry_block"].startswith("d1_blocked"), r)
        self.assertEqual(ch.refreshes, 0)
        self.assertNoOrder(ch)

    def test_expired_d0_window(self):
        ch = Chain(self, lane=False, policy_overrides={
            "ibkr_paper.auto_trading_enabled": True,
            "ibkr_paper.auto_trading_enabled_until_utc": (utc_now() - timedelta(minutes=1)).isoformat()})
        r = ch.tick()
        self.assertTrue(r["entry_block"].startswith("d0_blocked"), r)
        self.assertNoOrder(ch)

    def test_refresh_crash_during_startup_blocks(self):
        ch = Chain(self)

        def boom():
            raise RuntimeError("gateway socket reset")
        ch.loop = ch.make_loop(refresher=boom)
        r = ch.tick()
        self.assertEqual(r["entry_block"], "risk:risk_refresh_error:RuntimeError")
        self.assertNoOrder(ch)


if __name__ == "__main__":
    unittest.main()

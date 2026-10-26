#!/usr/bin/env python3
"""M21.1extra-D2.3 / D2.4 / D5 — unattended IBKR-paper learning loop.

Everything runs against the deterministic SimulatedPaperBroker or small fakes
(no gateway, no network, no real orders; C5a isolation is installed). Proves:
  * scheduling: one entry per slot, re-triggered slots do maintenance only,
    overlap lock, interrupted cycles;
  * outcome capture: entry fill -> OPEN, take-profit / stop-loss / time-stop
    exit -> CLOSED with realised P&L written atomically; no invented exits;
  * idempotency: client_ref / cycle_key keys, one active order per symbol,
    no duplicate submission across crashes and restarts;
  * crash recovery for every gap (before submit, lost ack, process death,
    broker restart, ambiguous broker state);
  * fail-closed entry gates: kill switch, D0, D1, Risk Authority (real engine
    on a temp DB), equity, caps, broker state, candidates;
  * broker equity snapshots; the IBKR wrapper and its read-only equity/fills
    readers against fake IB clients; CLI refusals;
  * the outcome table satisfies the D3 dashboard contract, and simulated
    outcomes never count as genuine there.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import _test_isolation  # noqa: E402
_test_isolation.install()

from bot.paper_loop_d2.broker_port import (  # noqa: E402
    AccountSnapshot, BracketRequest, BrokerUnavailable, FillView, OrderLookup, OrderView,
    OwnedTrade, PaperModeRefused, PositionView, Quote, SubmitAck, attribute_fills,
    classify_owned_orders, flatten_ref, make_order_ref,
)
from bot.paper_loop_d2.ibkr_adapter import IbkrLoopBroker  # noqa: E402
from bot.paper_loop_d2.candidates import (  # noqa: E402
    InvalidCandidate, LoopCandidate, candidate_from_scored_signal, select_candidate,
)
from bot.paper_loop_d2.journal import JournalConflict, LoopJournal  # noqa: E402
from bot.paper_loop_d2.scheduler import LoopConfig, PaperLearningLoop  # noqa: E402
from bot.paper_loop_d2.sim_broker import SimulatedPaperBroker  # noqa: E402

REPO = Path(__file__).resolve().parent
T0 = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
CLI = "tools/paper_loop/m21_1extra_d2_3_scheduler.py"
REAL_LOOP_FLAG_TEXT = "--i-understand-this-runs-unattended-" + "ibkr-paper-orders"


class LoopClock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t = self.t + timedelta(minutes=minutes)


def cand(symbol="AAA", score=80.0, direction="long", entry=100.0, sl=95.0, tp=110.0,
         strategy="trend_v1"):
    return LoopCandidate.build(symbol=symbol, direction=direction, score=score,
                               strategy_id=strategy, entry_ref_price=entry,
                               stop_loss=sl, take_profit=tp)


def entry_orders(broker):
    return len([o for o in broker.raw_state()["orders"] if o["kind"] == "entry"])


class LoopHarness:
    """Temp journal + lock + simulated broker + recording gates."""

    def __init__(self, testcase, *, config=None, candidates=None, prices=None, state_path=None,
                 broker=None, once=False):
        self.dir = tempfile.mkdtemp(prefix="d23_")
        testcase.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "paper_lifecycles.db")
        self.lock = os.path.join(self.dir, "loop.lock")
        self.clock = LoopClock()
        self.prices = dict(prices or {"AAA": 100.0, "BBB": 50.0})
        self.candidates = candidates if candidates is not None else [cand()]
        self.broker = broker or SimulatedPaperBroker(
            price_fn=lambda s, now: self.prices[s], clock=self.clock,
            commission_per_share=0.005, min_commission=1.0, state_path=state_path)
        self.calls = {"d0": 0, "d1": 0, "risk": 0, "kill": 0}
        self.kill = False
        self.d0 = (True, "authorized")
        self.d1 = (True, "market_open")
        self.risk = (True, "allow", "dec-1")
        self.config = config or LoopConfig(interval_minutes=15, max_hold_minutes=600)
        self.once = once
        self.provided = 0
        self.loop = self.make_loop()

    def _provide(self, now):
        """Fresh signals each slot (signal_ts = now), optionally only once."""
        self.provided += 1
        if self.once and self.provided > 1:
            return []
        return [dataclasses.replace(c, signal_ts_utc=now.isoformat())
                if isinstance(c, LoopCandidate) and c.signal_ts_utc is None else c
                for c in self.candidates]

    def _d0(self):
        self.calls["d0"] += 1
        return self.d0

    def _d1(self):
        self.calls["d1"] += 1
        return self.d1

    def _risk(self, **kw):
        self.calls["risk"] += 1
        self.last_risk_kw = kw
        return self.risk

    def _kill(self):
        self.calls["kill"] += 1
        return self.kill

    def make_loop(self, **overrides):
        kw = dict(journal=LoopJournal(self.db), broker=self.broker, d0_gate=self._d0,
                  d1_gate=self._d1, kill_switch_active=self._kill, risk_gate=self._risk,
                  candidate_provider=self._provide, lock_path=self.lock,
                  config=self.config, clock=self.clock, pid=os.getpid())
        kw.update(overrides)
        return PaperLearningLoop(**kw)

    def tick(self, minutes_after=None):
        if minutes_after:
            self.clock.advance(minutes_after)
        return self.loop.tick()

    def orders(self):
        return LoopJournal(self.db).orders()

    def outcomes(self):
        return LoopJournal(self.db).outcomes()

    def submits(self):
        return self.broker.calls.count("submit_bracket")


# ─────────────────────────────────────────────────────────────────────────────

class D23Candidates(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(cand().symbol, "AAA")
        for bad in (dict(symbol="aaa"), dict(direction="flat"), dict(entry=90.0),
                    dict(direction="short"), dict(score=float("nan")), dict(sl=-1.0),
                    dict(strategy=" ")):
            with self.assertRaises(InvalidCandidate, msg=bad):
                cand(**bad)
        self.assertEqual(cand(direction="short", entry=100, sl=105, tp=90).direction, "short")

    def test_select(self):
        items = [cand("AAA", 70), cand("BBB", 90, entry=50, sl=45, tp=60),
                 {"symbol": "bad"}, cand("CCC", 99, direction="short", entry=10, sl=11, tp=9)]
        pick, notes = select_candidate(items, min_score=60, allow_short=False, excluded_symbols=set())
        self.assertEqual(pick.symbol, "BBB")
        self.assertTrue(any(n.startswith("invalid_candidate") for n in notes))
        self.assertIn("short_disabled:CCC", notes)
        pick, notes = select_candidate(items, min_score=60, allow_short=False, excluded_symbols={"BBB"})
        self.assertEqual(pick.symbol, "AAA")
        self.assertIsNone(select_candidate(items, min_score=95, allow_short=False,
                                           excluded_symbols=set())[0])

    def test_from_scored_signal(self):
        sig = {"symbol": "AAA", "direction": "long", "entry_price": 100.0, "stop_loss": 95.0,
               "target_price": 115.0, "strategy_version": 3, "timestamp": "2026-10-26T14:00:00+00:00"}
        c = candidate_from_scored_signal(sig, {"final_score_100": 72.5})
        self.assertEqual((c.strategy_id, c.score, c.take_profit), ("scanner_v3", 72.5, 115.0))


class D23HappyPathAndOutcomes(unittest.TestCase):
    def test_take_profit_round_trip(self):
        h = LoopHarness(self)
        r1 = h.tick()
        self.assertEqual(r1["entry"]["state"], "SUBMITTED")
        self.assertEqual(h.submits(), 1)
        self.assertEqual(h.calls["risk"], 1)
        self.assertEqual(h.last_risk_kw["notional_usd"], 100.0)
        r2 = h.tick(15)                                   # entry fill observed
        self.assertEqual(r2["opened"], [r1["entry"]["client_ref"]])
        self.assertEqual(r2["entry_block"], "max_open_trades")
        h.prices["AAA"] = 111.0                           # crosses TP 110
        r3 = h.tick(15)
        self.assertEqual(r3["closed"][0]["exit_reason"], "take_profit")
        (o,) = h.outcomes()
        self.assertEqual(o["entry_price"], 100.0)
        self.assertEqual(o["exit_price"], 110.0)          # limit fills at the limit
        self.assertAlmostEqual(o["gross_pnl"], 10.0)
        self.assertAlmostEqual(o["fees"], 2.0)            # 2 x min commission
        self.assertAlmostEqual(o["realized_pnl"], 8.0)
        self.assertAlmostEqual(o["r_multiple"], 2.0)
        self.assertEqual((o["is_edge_outcome"], o["data_source"], o["strategy_id"]),
                         (1, "simulated", "trend_v1"))
        self.assertEqual(h.orders()[0]["state"], "CLOSED")
        self.assertEqual(h.orders()[0]["risk_decision_id"], "dec-1")

    def test_stop_loss_gap_fills_at_market(self):
        h = LoopHarness(self)
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 93.0                            # gaps through SL 95
        r = h.tick(15)
        self.assertEqual(r["closed"][0]["exit_reason"], "stop_loss")
        o = h.outcomes()[0]
        self.assertEqual(o["exit_price"], 93.0)
        self.assertAlmostEqual(o["realized_pnl"], -9.0)

    def test_short_take_profit(self):
        h = LoopHarness(self, config=LoopConfig(allow_short=True, max_hold_minutes=600),
                        candidates=[cand(direction="short", entry=100, sl=105, tp=90)])
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 89.0
        h.tick(15)
        o = h.outcomes()[0]
        self.assertEqual((o["direction"], o["exit_reason"], o["exit_price"]), ("short", "take_profit", 90.0))
        self.assertAlmostEqual(o["gross_pnl"], 10.0)

    def test_time_stop_flattens_and_records(self):
        h = LoopHarness(self, config=LoopConfig(max_hold_minutes=60), once=True)
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 101.5
        r = h.tick(75)
        self.assertEqual(r["closed"][0]["exit_reason"], "time_stop")
        self.assertAlmostEqual(h.outcomes()[0]["gross_pnl"], 1.5)
        self.assertNotIn("AAA", {p.symbol for p in h.broker.positions()})

    def test_equity_snapshots_every_tick(self):
        h = LoopHarness(self)
        h.tick()
        h.tick(15)
        con = sqlite3.connect(h.db)
        rows = con.execute("SELECT data_source, net_liquidation FROM paper_equity_snapshots").fetchall()
        con.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r[0] == "simulated" and r[1] > 0 for r in rows))

    def test_no_exit_invented_when_position_vanishes(self):
        class HidesExits(SimulatedPaperBroker):
            def fills(self, since_utc):
                return [f for f in super().fills(since_utc) if f.order_kind == "entry"]
        h = LoopHarness(self)
        h.broker = HidesExits(price_fn=lambda s, now: h.prices[s], clock=h.clock)
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 120.0
        h.tick(15)
        self.assertEqual(h.outcomes(), [])
        self.assertEqual(h.orders()[0]["state"], "NEEDS_REVIEW")
        r = h.tick(15)
        self.assertEqual(r["entry_block"], "journal_needs_review")


class D23Idempotency(unittest.TestCase):
    def test_same_slot_twice_never_reenters(self):
        h = LoopHarness(self, config=LoopConfig(max_open_trades=3, max_hold_minutes=600),
                        candidates=[cand("AAA"), cand("BBB", entry=50, sl=45, tp=60)])
        h.tick()
        r = h.tick(5)                                     # still the same 15-min slot
        self.assertEqual(r["skipped"], "slot_already_processed")
        self.assertIsNone(r["entry"])
        self.assertEqual(h.submits(), 1)

    def test_one_active_order_per_symbol(self):
        h = LoopHarness(self, config=LoopConfig(max_open_trades=3, max_hold_minutes=600))
        h.tick()
        r = h.tick(15)
        self.assertEqual(r["entry_block"], "no_candidate")
        self.assertIn("symbol_busy:AAA", r["candidate_notes"])
        j = LoopJournal(h.db)
        with self.assertRaises(sqlite3.IntegrityError):
            j.plan_order(client_ref="x|AAA", cycle_key="x", strategy_id="s", symbol="AAA",
                         direction="long", qty=1, entry_ref_price=100, stop_loss=95, take_profit=110,
                         score=80, data_source="simulated", risk_decision_id=None,
                         equity_at_entry=None, now=T0)

    def test_outcome_written_once(self):
        h = LoopHarness(self, once=True)
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        h.tick(15)
        self.assertEqual(len(h.outcomes()), 1)

    def test_daily_entry_cap(self):
        h = LoopHarness(self, config=LoopConfig(max_open_trades=5, max_entries_per_day=1,
                                                max_hold_minutes=600),
                        candidates=[cand("AAA"), cand("BBB", entry=50, sl=45, tp=60)])
        h.tick()
        r = h.tick(15)
        self.assertEqual(r["entry_block"], "daily_entry_cap")


class D23EntryGates(unittest.TestCase):
    def _blocked(self, h, reason_prefix):
        r = h.tick()
        self.assertTrue(str(r["entry_block"]).startswith(reason_prefix), r["entry_block"])
        self.assertEqual(h.submits(), 0)
        self.assertEqual([o for o in h.orders() if o["state"] != "NOT_SUBMITTED"], [])
        return r

    def test_kill_switch(self):
        h = LoopHarness(self)
        h.kill = True
        self._blocked(h, "kill_switch_active")
        self.assertEqual(h.calls["d0"], 0)

    def test_kill_switch_unknown_blocks(self):
        h = LoopHarness(self)
        h.kill = None
        self._blocked(h, "kill_switch_active")

    def test_kill_switch_raising_blocks(self):
        h = LoopHarness(self)
        h.loop = h.make_loop(kill_switch_active=lambda: 1 / 0)
        self._blocked(h, "kill_switch_error")

    def test_d0_and_d1(self):
        h = LoopHarness(self)
        h.d0 = (False, "not_authorized")
        self._blocked(h, "d0_blocked:not_authorized")
        h2 = LoopHarness(self)
        h2.d1 = (False, "market_closed")
        self._blocked(h2, "d1_blocked:market_closed")
        self.assertEqual(h2.calls["risk"], 0)

    def test_risk_authority_block(self):
        h = LoopHarness(self)
        h.risk = (False, "risk_authority_block:global_kill", "dec-9")
        self._blocked(h, "risk:risk_authority_block:global_kill")

    def test_risk_gate_exception(self):
        h = LoopHarness(self)
        h.loop = h.make_loop(risk_gate=lambda **kw: 1 / 0)
        self._blocked(h, "risk_gate_error")

    def test_equity_unavailable_and_minimum(self):
        h = LoopHarness(self)
        h.broker.fail_always["account_snapshot"] = BrokerUnavailable("down")
        self._blocked(h, "equity_unavailable")
        h2 = LoopHarness(self, config=LoopConfig(min_equity_usd=10**9))
        self._blocked(h2, "equity_below_minimum")

    def test_notional_cap_by_equity(self):
        h = LoopHarness(self, config=LoopConfig(max_notional_pct_of_equity=0.0005))
        self._blocked(h, "notional_cap")

    def test_candidate_provider_error_and_none(self):
        h = LoopHarness(self)
        h.loop = h.make_loop(candidate_provider=lambda now: 1 / 0)
        self._blocked(h, "candidate_provider_error")
        h2 = LoopHarness(self, candidates=[cand(score=10.0)])
        self._blocked(h2, "no_candidate")

    def test_unattributed_broker_orders_block(self):
        class Ambiguous(SimulatedPaperBroker):
            def open_orders(self):
                return super().open_orders() + [OrderView("77", None, "BUY", 1)]
        h = LoopHarness(self)
        h.broker = Ambiguous(price_fn=lambda s, now: 100.0, clock=h.clock)
        h.loop = h.make_loop()
        self._blocked(h, "unattributed_open_orders")

    def test_existing_broker_position_excludes_symbol(self):
        h = LoopHarness(self)
        h.broker.manual_fill("AAA", "BUY", 1, 100.0)                # pre-existing manual exposure
        r = self._blocked(h, "no_candidate")
        self.assertIn("symbol_busy:AAA", r["candidate_notes"])

    def test_broker_rejection(self):
        h = LoopHarness(self)
        h.broker.reject_next_submit = "insufficient_margin"
        r = h.tick()
        self.assertEqual(r["entry_block"], "broker_rejected:rejected")
        self.assertEqual(h.orders()[0]["state"], "REJECTED")
        self.assertEqual(h.outcomes(), [])

    def test_stale_or_untimestamped_signal_rejected(self):
        old = dataclasses.replace(cand(), signal_ts_utc=(T0 - timedelta(hours=2)).isoformat())
        h = LoopHarness(self, candidates=[old])
        r = self._blocked(h, "no_candidate")
        self.assertIn("stale_signal:AAA", r["candidate_notes"])
        naive = dataclasses.replace(cand(), signal_ts_utc="2026-10-26T14:00:00")
        h2 = LoopHarness(self, candidates=[naive])
        r2 = self._blocked(h2, "no_candidate")
        self.assertIn("signal_ts_unusable:AAA", r2["candidate_notes"])

    def test_entry_drift_blocks(self):
        h = LoopHarness(self, prices={"AAA": 103.0})          # 3% away from the signal's 100
        self._blocked(h, "entry_drift:AAA")
        h2 = LoopHarness(self)
        h2.broker.fail_always["quote"] = BrokerUnavailable("no quote")
        self._blocked(h2, "live_price_unavailable")

    def test_qty_fixed_at_one(self):
        with self.assertRaises(ValueError):
            LoopConfig(qty=2).validate()


class D23CrashRecovery(unittest.TestCase):
    def test_crash_before_broker_received_entry(self):
        h = LoopHarness(self)
        h.broker.fail_once["submit_bracket"] = ConnectionError("process lost mid-call")
        r1 = h.tick()
        self.assertEqual(r1["entry_block"], "submit_outcome_unknown")
        self.assertEqual(h.orders()[0]["state"], "PENDING_SUBMIT")
        r2 = h.tick(15)
        self.assertEqual(r2["recovered"][0]["resolution"], "absence_check_1")
        self.assertEqual(r2["entry_block"], "unresolved_pending_submit")
        self.assertEqual(h.orders()[0]["state"], "PENDING_SUBMIT")
        r3 = h.tick(15)
        self.assertEqual(r3["recovered"][0]["resolution"], "not_submitted")
        self.assertEqual(h.orders()[0]["state"], "NOT_SUBMITTED")
        self.assertEqual(r3["entry"]["state"], "SUBMITTED")    # symbol free again, new slot
        self.assertEqual(entry_orders(h.broker), 1)

    def test_lost_ack_is_adopted_not_resubmitted(self):
        h = LoopHarness(self)
        real = h.broker.submit_bracket

        def accept_then_crash(req):
            real(req)
            raise TimeoutError("ack lost")
        h.broker.submit_bracket = accept_then_crash
        h.tick()
        self.assertEqual(h.orders()[0]["state"], "PENDING_SUBMIT")
        h.broker.submit_bracket = real
        r2 = h.tick(15)
        self.assertEqual(r2["recovered"][0]["resolution"], "adopted")
        self.assertEqual(r2["opened"], [h.orders()[0]["client_ref"]])
        self.assertEqual(entry_orders(h.broker), 1)                  # never resubmitted
        h.prices["AAA"] = 112.0
        h.tick(15)
        self.assertEqual(h.outcomes()[0]["exit_reason"], "take_profit")

    def test_process_death_mid_cycle_and_same_slot_retrigger(self):
        h = LoopHarness(self)
        dead_pid = 999999
        j = LoopJournal(h.db)
        key = h.loop.cycle_key(h.clock())
        j.begin_cycle(key, dead_pid, T0)
        h.loop = h.make_loop(pid_alive=lambda pid: pid != dead_pid)
        r = h.tick(1)
        self.assertEqual(r["interrupted_cycles"], [key])
        self.assertEqual(r["skipped"], "slot_already_processed")
        self.assertEqual(h.submits(), 0)
        self.assertEqual(j.get_cycle(key)["state"], "INTERRUPTED")
        r2 = h.tick(15)
        self.assertEqual(r2["entry"]["state"], "SUBMITTED")

    def test_lock_live_vs_stale(self):
        h = LoopHarness(self)
        with open(h.lock, "w") as fh:
            json.dump({"pid": os.getpid()}, fh)
        self.assertEqual(h.tick()["skipped"], "lock_held")
        with open(h.lock, "w") as fh:
            json.dump({"pid": 999999}, fh)
        h.loop = h.make_loop(pid_alive=lambda pid: pid == os.getpid())
        self.assertIsNone(h.tick()["skipped"])
        self.assertFalse(os.path.exists(h.lock))

    def test_restart_with_persisted_broker_state(self):
        h = LoopHarness(self, once=True)
        state = os.path.join(h.dir, "broker.json")
        mk = lambda: SimulatedPaperBroker(price_fn=lambda s, now: h.prices[s],  # noqa: E731
                                          clock=h.clock, state_path=state)
        h.broker = mk()
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        # "restart": new broker object from disk + new loop object, same journal DB
        h.broker = mk()
        h.loop = h.make_loop()
        h.prices["AAA"] = 94.0
        r = h.tick(15)
        self.assertEqual(r["closed"][0]["exit_reason"], "stop_loss")
        self.assertEqual(len(h.outcomes()), 1)
        self.assertEqual(h.submits(), 0)          # the restarted broker never re-submitted


    def test_broker_outage_during_maintenance_is_retried(self):
        h = LoopHarness(self)
        h.tick()
        h.broker.fail_once["fills"] = BrokerUnavailable("gateway restart")
        r = h.tick(15)
        self.assertTrue(any(e.startswith("capture_broker_unavailable") for e in r["errors"]))
        self.assertEqual(r["entry_block"], "maintenance_incomplete")   # never trade on stale state
        self.assertEqual(h.orders()[0]["state"], "SUBMITTED")
        r = h.tick(15)
        self.assertEqual(len(r["opened"]), 1)

    def test_internal_maintenance_error_blocks_entry_but_loop_survives(self):
        h = LoopHarness(self)
        h.tick()
        real = h.loop.journal.close_with_outcome
        h.loop.journal.close_with_outcome = lambda *a, **k: 1 / 0
        h.tick(15)
        h.prices["AAA"] = 111.0
        r = h.tick(15)
        self.assertIn("capture_internal:ZeroDivisionError", r["errors"])
        self.assertEqual(r["entry_block"], "maintenance_incomplete")
        self.assertEqual(LoopJournal(h.db).get_cycle(r["cycle_key"])["state"], "DONE")
        h.loop.journal.close_with_outcome = real
        r = h.tick(15)
        self.assertEqual(r["closed"][0]["exit_reason"], "take_profit")

    def test_entry_without_any_broker_trace_needs_review_not_rejected(self):
        class NoTrace(SimulatedPaperBroker):
            def submit_bracket(self, req):
                self.calls.append("submit_bracket")
                return SubmitAck(accepted=True, status="accepted", broker_order_id="9")
        h = LoopHarness(self, config=LoopConfig(entry_fill_timeout_minutes=30, max_hold_minutes=600))
        h.broker = NoTrace(price_fn=lambda s, now: 100.0, clock=h.clock)
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        self.assertEqual(h.orders()[0]["state"], "SUBMITTED")
        h.tick(30)
        self.assertEqual(h.orders()[0]["state"], "NEEDS_REVIEW")     # absence of a trace is not proof
        self.assertIn("cannot be proven", h.orders()[0]["note"])

    def test_entry_cancelled_at_broker_is_proven_rejected(self):
        h = LoopHarness(self, config=LoopConfig(entry_fill_timeout_minutes=30, max_hold_minutes=600))
        h.broker.cancel_entry_next_submit = True
        h.tick()
        h.tick(15)
        self.assertEqual(h.orders()[0]["state"], "SUBMITTED")
        h.tick(30)
        o = h.orders()[0]
        self.assertEqual(o["state"], "REJECTED")
        self.assertIn("Cancelled", o["note"])

    def test_unattended_run_loop(self):
        h = LoopHarness(self)
        sleeps = []

        def fake_sleep(sec):
            sleeps.append(sec)
            h.clock.advance(sec / 60.0)
        out = h.loop.run(max_ticks=3, sleep=fake_sleep)
        self.assertEqual(len(out), 3)
        self.assertEqual(len({r["cycle_key"] for r in out}), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(h.submits(), 1)


class D23RiskAuthorityReal(unittest.TestCase):
    """The real M14 engine on a temp signals DB decides the entry."""

    def _db(self, policy_overrides):
        import test_m14_f_preflight as F
        from bot.broker_allocation import save_policy
        fx = F._DB()
        self.addCleanup(fx.cleanup)
        with fx.conn() as c:
            save_policy(c, F._valid_policy(**policy_overrides))
            F._seed_fresh_all_scopes(c)
        return fx.path

    def test_allow_and_block(self):
        from bot.paper_loop_d2.risk_gate import make_risk_authority_gate
        ok_db = self._db({})
        g = make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(ok_db))
        allowed, why, did = g(symbol="AAA", side="long", notional_usd=100.0, stop_loss=95.0,
                              take_profit=110.0)
        self.assertEqual((allowed, why), (True, "risk_authority_allow"))
        self.assertTrue(did)
        h = LoopHarness(self)
        h.loop = h.make_loop(risk_gate=g)
        r = h.tick()
        self.assertEqual(r["entry"]["state"], "SUBMITTED")
        self.assertEqual(h.orders()[0]["risk_decision_id"] is not None, True)
        kill_db = self._db({"global.kill_switch": True})
        g2 = make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(kill_db))
        allowed, why, _ = g2(symbol="AAA", side="long", notional_usd=100.0, stop_loss=95.0,
                             take_profit=110.0)
        self.assertFalse(allowed)
        self.assertIn("global_kill", why)
        low = make_risk_authority_gate(conn_factory=lambda: sqlite3.connect(ok_db),
                                       authority_name="PAPER_ONLY")
        self.assertFalse(low(symbol="AAA", side="long", notional_usd=1.0, stop_loss=0.5,
                             take_profit=2.0)[0])
        broken = make_risk_authority_gate(conn_factory=lambda: 1 / 0)
        self.assertEqual(broken(symbol="AAA", side="long", notional_usd=1.0, stop_loss=0.5,
                                take_profit=2.0)[:2], (False, "risk_authority_error:ZeroDivisionError"))
        with self.assertRaises(ValueError):
            make_risk_authority_gate(conn_factory=lambda: None, broker_scope="ibkr_live")


class FakeIBValue(SimpleNamespace):
    pass


# ═════════════════════════════════════════════════════════════════════════════
# Correction batch (ChatGPT WS2 review): ownership, IBKR identities, safe
# flatten, live-quote drift, crash uncertainty.
# ═════════════════════════════════════════════════════════════════════════════

def open_trade(testcase, **kw):
    """Harness with one OPEN loop trade on AAA (entry 100, SL 95, TP 110)."""
    h = LoopHarness(testcase, once=True, **kw)
    h.tick()
    h.tick(15)
    testcase.assertEqual(h.orders()[0]["state"], "OPEN")
    return h


class D23OwnershipRegression(unittest.TestCase):
    """Finding 1: never attribute by symbol / side / time."""

    def test_manual_buy_on_same_symbol_does_not_change_the_outcome(self):
        h = open_trade(self)
        h.broker.manual_fill("AAA", "BUY", 1, 101.0)              # operator buys AAA by hand
        h.prices["AAA"] = 111.0
        r = h.tick(15)
        self.assertEqual(r["closed"][0]["exit_reason"], "take_profit")
        o = h.outcomes()[0]
        self.assertEqual((o["entry_price"], o["exit_price"]), (100.0, 110.0))
        self.assertAlmostEqual(o["realized_pnl"], 8.0)
        self.assertEqual([p.qty for p in h.broker.positions()], [1.0])   # manual share untouched
        claims = LoopJournal(h.db).exec_claims()
        manual = [f for f in h.broker.raw_state()["fills"] if f["order_ref"] is None]
        self.assertTrue(manual and all(f["exec_id"] not in claims for f in manual))

    def test_manual_exit_lookalike_is_never_taken_as_the_exit(self):
        h = open_trade(self)
        # same symbol, exit side, after the entry, at a take-profit-like price
        h.broker.manual_fill("AAA", "SELL", 1, 109.9)
        r = h.tick(15)
        self.assertEqual(r["closed"], [])
        self.assertEqual(h.outcomes(), [])
        o = h.orders()[0]
        self.assertEqual(o["state"], "NEEDS_REVIEW")              # broker flat, no own exit
        self.assertIn("flat without an exit execution", o["note"])
        self.assertEqual(h.tick(15)["entry_block"], "journal_needs_review")

    def test_recovery_never_adopts_manual_exposure(self):
        h = LoopHarness(self)
        h.broker.fail_once["submit_bracket"] = ConnectionError("lost before the broker")
        h.tick()
        h.broker.manual_fill("AAA", "BUY", 1, 100.0)              # symbol+side+time all "match"
        r2 = h.tick(15)
        self.assertEqual(r2["recovered"][0]["resolution"], "absence_check_1")
        r3 = h.tick(15)
        self.assertEqual(r3["recovered"][0]["resolution"], "not_submitted")
        self.assertEqual(h.orders()[0]["state"], "NOT_SUBMITTED")
        self.assertEqual(entry_orders(h.broker), 0)               # nothing ever reached the broker
        self.assertEqual(r3["entry_block"], "no_candidate")       # manual AAA keeps the symbol busy
        self.assertIn("symbol_busy:AAA", r3["candidate_notes"])
        self.assertEqual(LoopJournal(h.db).exec_claims(), {})

    def test_order_ref_is_written_ahead_and_unique_per_journal(self):
        h = LoopHarness(self)
        h.broker.fail_once["submit_bracket"] = ConnectionError("lost")
        h.tick()
        o = h.orders()[0]
        j = LoopJournal(h.db)
        self.assertEqual(o["order_ref"], make_order_ref(j.journal_id, o["client_ref"]))
        self.assertTrue(o["order_ref"].startswith("M21L:%s:" % j.journal_id))
        self.assertNotEqual(LoopJournal(os.path.join(h.dir, "other.db")).journal_id, j.journal_id)
        with self.assertRaises(sqlite3.IntegrityError):
            j.plan_order(client_ref="other|BBB", cycle_key="other", strategy_id="s", symbol="BBB",
                         direction="long", qty=1, entry_ref_price=50, stop_loss=45, take_profit=60,
                         score=80, data_source="simulated", risk_decision_id=None,
                         equity_at_entry=None, now=T0, order_ref=o["order_ref"])

    def test_identities_are_persisted_for_every_leg(self):
        h = open_trade(self)
        o = h.orders()[0]
        legs = {x["kind"]: x for x in h.broker.raw_state()["orders"] if x["order_ref"] == o["order_ref"]}
        self.assertEqual(o["broker_perm_id"], legs["entry"]["perm_id"])
        self.assertEqual(o["tp_perm_id"], legs["take_profit"]["perm_id"])
        self.assertEqual(o["sl_perm_id"], legs["stop_loss"]["perm_id"])
        self.assertEqual((o["con_id"], o["account"]), (legs["entry"]["con_id"], "SIM-PAPER"))
        self.assertEqual(len(LoopJournal(h.db).exec_claims()), 1)  # the entry execution


class D23IdentityConflicts(unittest.TestCase):
    """Finding 1 / 5: matches that disagree are conflicts -> NEEDS_REVIEW."""

    ORDER = {"client_ref": "c1", "order_ref": "M21L:j:x|AAA", "symbol": "AAA", "direction": "long",
             "qty": 1, "con_id": "111", "broker_perm_id": "P1", "tp_perm_id": "P2", "sl_perm_id": "P3"}

    def fill(self, **kw):
        base = dict(exec_id="e1", symbol="AAA", side="BUY", qty=1.0, price=100.0,
                    time_utc=T0.isoformat(), perm_id="P1", order_ref="M21L:j:x|AAA",
                    account="ACC", con_id="111")
        base.update(kw)
        return FillView(**base)

    def att(self, *fills, claims=None, order=None):
        return attribute_fills(order or self.ORDER, fills, account="ACC", claims=claims or {})

    def test_verified_identity_is_attributed(self):
        a = self.att(self.fill(), self.fill(exec_id="e2", side="SELL", perm_id="P2", price=110.0))
        self.assertEqual((a.conflicts, a.entry_qty, a.exit_qty), ([], 1.0, 1.0))
        self.assertEqual(a.legs, {"e1": "entry", "e2": "take_profit"})

    def test_foreign_executions_are_ignored_not_attributed(self):
        a = self.att(self.fill(exec_id="m1", perm_id="P9", order_ref=None),
                     self.fill(exec_id="m2", perm_id=None, order_ref="SOMEONE-ELSE", side="SELL"))
        self.assertEqual((a.entry, a.exits, a.conflicts), ([], [], []))

    def test_each_kind_of_disagreement_is_a_conflict(self):
        cases = {
            "permId is ours but orderRef": self.fill(order_ref="OTHER"),
            "account OTHER": self.fill(account="OTHER"),
            "contract 999": self.fill(con_id="999"),
            "symbol BBB": self.fill(symbol="BBB"),
            "does not fit leg": self.fill(perm_id="P2"),                 # BUY on the TP leg
            "not the recorded leg permId": self.fill(perm_id="P77"),     # our ref, unknown perm
        }
        for needle, f in cases.items():
            with self.subTest(needle):
                a = self.att(f)
                self.assertEqual((a.entry, a.exits), ([], []))
                self.assertTrue(any(needle in c for c in a.conflicts), a.conflicts)
        self.assertTrue(any("already attributed" in c
                            for c in self.att(self.fill(), claims={"e1": "c0"}).conflicts))
        over = self.att(self.fill(), self.fill(exec_id="e2", perm_id=None))
        self.assertTrue(any("over-filled" in c for c in over.conflicts))

    def test_order_classification_conflicts(self):
        o = {k: v for k, v in self.ORDER.items() if not k.endswith("perm_id")}
        two_tp = [OrderView("1", "AAA", "SELL", 1, "Submitted", order_ref=o["order_ref"], perm_id="X1",
                            con_id="111", order_type="LMT"),
                  OrderView("2", "AAA", "SELL", 1, "Submitted", order_ref=o["order_ref"], perm_id="X2",
                            con_id="111", order_type="LMT")]
        _legs, _owned, conflicts = classify_owned_orders(o, two_tp, account="ACC")
        self.assertTrue(any("two broker orders claim leg take_profit" in c for c in conflicts))
        other_acct = [OrderView("1", "AAA", "BUY", 1, "Submitted", order_ref=o["order_ref"],
                                account="OTHER", order_type="MKT")]
        self.assertTrue(classify_owned_orders(o, other_acct, account="ACC")[2])

    def test_journal_refuses_to_claim_an_execution_twice(self):
        h = open_trade(self)
        j = LoopJournal(h.db)
        entry_exec = next(iter(j.exec_claims()))
        j.plan_order(client_ref="x|BBB", cycle_key="x", strategy_id="s", symbol="BBB",
                     direction="long", qty=1, entry_ref_price=50, stop_loss=45, take_profit=60,
                     score=80, data_source="simulated", risk_decision_id=None,
                     equity_at_entry=None, now=T0, order_ref="R2")
        with self.assertRaises(JournalConflict):
            j.transition("x|BBB", "PENDING_SUBMIT", "SUBMITTED", T0, claims=[(entry_exec, "entry")])
        self.assertEqual(j.get_order("x|BBB")["state"], "PENDING_SUBMIT")   # rolled back

    def test_conflicting_execution_during_capture_sends_trade_to_review(self):
        class Tampered(SimulatedPaperBroker):
            inject = None

            def fills(self, since_utc):
                return super().fills(since_utc) + ([self.inject] if self.inject else [])
        h = LoopHarness(self, once=True)
        h.broker = Tampered(price_fn=lambda s, now: h.prices[s], clock=h.clock)
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        o = h.orders()[0]
        # an execution carrying OUR take-profit permId but someone else's orderRef
        h.broker.inject = FillView(exec_id="x9", symbol="AAA", side="SELL", qty=1.0, price=110.0,
                                   time_utc=(T0 + timedelta(minutes=20)).isoformat(),
                                   perm_id=o["tp_perm_id"], order_ref="MANUAL-REF",
                                   account="SIM-PAPER", con_id=o["con_id"])
        r = h.tick(15)
        self.assertEqual(r["closed"], [])
        self.assertEqual(h.orders()[0]["state"], "NEEDS_REVIEW")
        self.assertIn("identity conflict", h.orders()[0]["note"])
        self.assertEqual(h.outcomes(), [])

    def test_ambiguous_recovery_evidence_needs_review_not_adoption(self):
        class OtherAccount(SimulatedPaperBroker):
            def lookup_order_ref(self, order_ref, since_utc):
                lk = super().lookup_order_ref(order_ref, since_utc)
                return OrderLookup(order_ref, lk.complete, lk.orders,
                                   [dataclasses.replace(f, account="SOMEONE-ELSE") for f in lk.fills])
        h = LoopHarness(self)
        h.broker = OtherAccount(price_fn=lambda s, now: h.prices[s], clock=h.clock)
        h.loop = h.make_loop()
        h.broker.lose_ack_next_submit = True
        h.tick()
        r = h.tick(15)
        self.assertEqual(r["recovered"], [])
        self.assertEqual(r["review"][0]["client_ref"], h.orders()[0]["client_ref"])
        self.assertEqual(h.orders()[0]["state"], "NEEDS_REVIEW")
        self.assertIn("identity conflict", h.orders()[0]["note"])
        self.assertEqual(entry_orders(h.broker), 1)               # never resubmitted


class D23SafeFlatten(unittest.TestCase):
    """Finding 3: the time stop closes only provably-owned quantity."""

    def h(self):
        return open_trade(self, config=LoopConfig(max_hold_minutes=60))

    def own_legs(self, h):
        ref = h.orders()[0]["order_ref"]
        return {x["kind"]: x for x in h.broker.raw_state()["orders"] if x["order_ref"] == ref}

    def test_refuses_when_broker_position_is_not_the_loops(self):
        h = self.h()
        h.broker.manual_fill("AAA", "BUY", 1, 100.0)              # position 2 = loop 1 + manual 1
        r = h.tick(75)
        self.assertEqual(r["closed"], [])
        o = h.orders()[0]
        self.assertEqual(o["state"], "NEEDS_REVIEW")
        self.assertIn("not the loop's own", o["note"])
        self.assertNotIn("flatten_owned", h.broker.calls)
        self.assertEqual([p.qty for p in h.broker.positions()], [2.0])
        self.assertEqual({k: v["status"] for k, v in self.own_legs(h).items()},
                         {"entry": "Filled", "take_profit": "Submitted", "stop_loss": "Submitted"})

    def test_refuses_and_never_cancels_a_foreign_order_on_the_contract(self):
        h = self.h()
        manual = h.broker.manual_order("AAA", "SELL", 1, order_type="LMT", price=104.0)
        r = h.tick(75)
        self.assertEqual(r["closed"], [])
        self.assertIn("foreign working order", h.orders()[0]["note"])
        self.assertEqual(h.broker.order_by_perm(manual)["status"], "Submitted")
        self.assertEqual(h.broker.cancelled, [])

    def test_closes_only_own_quantity_and_cancels_only_own_legs(self):
        h = self.h()
        h.broker.manual_fill("BBB", "BUY", 3, 50.0)
        manual = h.broker.manual_order("BBB", "SELL", 3, order_type="STP", price=45.0)
        h.prices["AAA"] = 101.5
        r = h.tick(75)
        self.assertEqual(r["closed"][0]["exit_reason"], "time_stop")
        legs = self.own_legs(h)
        self.assertEqual(sorted(h.broker.cancelled),
                         sorted([legs["take_profit"]["perm_id"], legs["stop_loss"]["perm_id"]]))
        close = [x for x in h.broker.raw_state()["orders"] if x["kind"] == "flatten"]
        self.assertEqual(len(close), 1)
        self.assertEqual((close[0]["qty"], close[0]["order_ref"]),
                         (1.0, flatten_ref(h.orders()[0]["order_ref"])))
        self.assertEqual(h.orders()[0]["flatten_perm_id"], close[0]["perm_id"])
        self.assertEqual(h.broker.order_by_perm(manual)["status"], "Submitted")
        self.assertEqual({p.symbol: p.qty for p in h.broker.positions()}, {"BBB": 3.0})
        self.assertAlmostEqual(h.outcomes()[0]["gross_pnl"], 1.5)

    def test_broker_side_refusal_is_review_and_no_action(self):
        h = self.h()
        o = h.orders()[0]
        h.broker.manual_fill("AAA", "SELL", 1, 100.0)            # position now 0 != loop's 1
        legs = (o["tp_perm_id"], o["sl_perm_id"])
        res = h.broker.flatten_owned(OwnedTrade(o["client_ref"], o["order_ref"], "AAA", o["con_id"],
                                                "SIM-PAPER", "long", 1.0, legs, legs))
        self.assertTrue(res.refused)
        self.assertEqual(res.detail["reason"], "position_not_owned")
        self.assertEqual(h.broker.cancelled, [])
        self.assertFalse([x for x in h.broker.raw_state()["orders"] if x["kind"] == "flatten"])
        h.broker.manual_fill("AAA", "BUY", 1, 100.0)             # position back to the loop's 1
        manual = h.broker.manual_order("AAA", "SELL", 1, order_type="LMT", price=104.0)
        res = h.broker.flatten_owned(OwnedTrade(o["client_ref"], o["order_ref"], "AAA", o["con_id"],
                                                "SIM-PAPER", "long", 1.0, legs, legs))
        self.assertEqual((res.refused, res.detail["reason"]), (True, "foreign_orders_on_contract"))
        self.assertEqual(h.broker.order_by_perm(manual)["status"], "Submitted")
        self.assertEqual(h.broker.cancelled, [])

    def test_exit_that_lands_during_the_time_stop_is_captured_not_reviewed(self):
        class LateTp(SimulatedPaperBroker):
            armed = False

            def positions(self):
                if self.armed:                    # TP fills right after the fills read
                    self.armed = False
                    h.prices["AAA"] = 111.0
                return super().positions()
        h = LoopHarness(self, once=True, config=LoopConfig(max_hold_minutes=60))
        h.broker = LateTp(price_fn=lambda s, now: h.prices[s], clock=h.clock)
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        h.broker.armed = True
        r = h.tick(75)
        self.assertEqual(r["closed"][0]["exit_reason"], "take_profit")
        self.assertEqual(r["review"], [])
        self.assertNotIn("flatten_owned", h.broker.calls)

    def test_refusal_reported_by_the_broker_goes_to_review(self):
        class Refuses(SimulatedPaperBroker):
            def flatten_owned(self, trade):
                self.calls.append("flatten_owned")
                from bot.paper_loop_d2.broker_port import FlattenResult
                return FlattenResult(confirmed=False, refused=True, detail={"reason": "position_changed"})
        h = LoopHarness(self, once=True, config=LoopConfig(max_hold_minutes=60))
        h.broker = Refuses(price_fn=lambda s, now: h.prices[s], clock=h.clock)
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        h.tick(75)
        self.assertEqual(h.orders()[0]["state"], "NEEDS_REVIEW")
        self.assertIn("position_changed", h.orders()[0]["note"])


class D23LiveQuote(unittest.TestCase):
    """Finding 4: a fresh live quote is mandatory; drift is always enforced."""

    def blocked(self, prefix, *, quote=None, broker_cls=None, config=None):
        h = LoopHarness(self, config=config)
        if broker_cls is not None:
            h.broker = broker_cls(price_fn=lambda s, now: h.prices[s], clock=h.clock)
            h.loop = h.make_loop()
        if quote is not None:
            h.broker.quote_override = quote
        r = h.tick()
        self.assertTrue(str(r["entry_block"]).startswith(prefix), r["entry_block"])
        self.assertEqual((h.submits(), h.orders()), (0, []))
        return h

    def q(self, price=100.0, age=0.0, delayed=False, symbol="AAA"):
        return lambda s, now: Quote(symbol=symbol, price=price,
                                    as_of_utc=(now - timedelta(seconds=age)).isoformat(),
                                    delayed=delayed, source="test")

    def test_missing_or_bad_price_blocks(self):
        self.blocked("live_price_unavailable", quote=self.q(price=None))
        self.blocked("live_price_unavailable", quote=self.q(price=float("nan")))
        self.blocked("live_price_unavailable", quote=self.q(price=0.0))
        self.blocked("quote_symbol_mismatch", quote=self.q(symbol="ZZZ"))

    def test_broker_without_a_quote_source_blocks_instead_of_skipping(self):
        class NoQuote(SimulatedPaperBroker):
            quote = None
        self.blocked("quote_unsupported", broker_cls=NoQuote)

    def test_stale_future_or_untimed_quote_blocks(self):
        self.blocked("quote_stale", quote=self.q(age=120))
        self.blocked("quote_stale", quote=self.q(age=-60))
        self.blocked("quote_time_unusable",
                     quote=lambda s, now: Quote("AAA", 100.0, None, False, "test"))
        self.blocked("quote_time_unusable",
                     quote=lambda s, now: Quote("AAA", 100.0, "2026-10-26T14:00:00", False, "test"))

    def test_delayed_quote_blocks_unless_explicitly_allowed(self):
        self.blocked("quote_delayed", quote=self.q(delayed=True))
        h = LoopHarness(self, config=LoopConfig(allow_delayed_quotes=True, max_hold_minutes=600))
        h.broker.quote_override = self.q(delayed=True)
        self.assertEqual(h.tick()["entry"]["state"], "SUBMITTED")

    def test_price_outside_bracket_and_drift(self):
        self.blocked("price_outside_bracket", quote=self.q(price=94.0))
        self.blocked("price_outside_bracket", quote=self.q(price=111.0))
        self.blocked("entry_drift:AAA", quote=self.q(price=102.5))
        h = LoopHarness(self)
        h.broker.quote_override = self.q(price=101.0)            # 1% drift: allowed
        r = h.tick()
        self.assertEqual((r["entry"]["state"], r["quote"]), ("SUBMITTED", 101.0))


class D23CrashUncertainty(unittest.TestCase):
    """Finding 5: an incomplete broker view never proves NOT_SUBMITTED."""

    def crashed(self, **kw):
        h = LoopHarness(self, **kw)
        h.broker.fail_once["submit_bracket"] = ConnectionError("lost")
        r = h.tick()
        self.assertEqual(r["entry_block"], "submit_outcome_unknown")
        return h

    def test_incomplete_lookup_never_concludes_absence(self):
        h = self.crashed()
        h.broker.incomplete_lookup = True
        for _ in range(3):                                         # 15, 30, 45 minutes
            r = h.tick(15)
            self.assertTrue(r["recovered"][0]["resolution"].startswith("unresolved:incomplete"))
            self.assertEqual(r["entry_block"], "unresolved_pending_submit")
            self.assertEqual(h.orders()[0]["state"], "PENDING_SUBMIT")
        r = h.tick(30)                                             # 75 > pending_review_after 60
        self.assertEqual(h.orders()[0]["state"], "NEEDS_REVIEW")
        self.assertIn("cannot be proven", h.orders()[0]["note"])
        self.assertEqual(h.submits(), 1)                           # the original attempt only
        self.assertNotIn("NOT_SUBMITTED", [o["state"] for o in h.orders()])

    def test_absence_needs_consecutive_complete_lookups(self):
        h = self.crashed()
        self.assertEqual(h.tick(15)["recovered"][0]["resolution"], "absence_check_1")
        h.broker.incomplete_lookup = True
        self.assertTrue(h.tick(15)["recovered"][0]["resolution"].startswith("unresolved"))
        self.assertEqual(h.orders()[0]["absence_checks"], 0)       # streak broken
        h.broker.incomplete_lookup = False
        self.assertEqual(h.tick(15)["recovered"][0]["resolution"], "absence_check_1")
        self.assertEqual(h.tick(15)["recovered"][0]["resolution"], "not_submitted")

    def test_grace_period_before_any_absence_check(self):
        h = self.crashed(config=LoopConfig(recovery_absence_grace_minutes=20, max_hold_minutes=600))
        self.assertEqual(h.tick(15)["recovered"][0]["resolution"], "unresolved:within_grace")
        self.assertEqual(h.tick(15)["recovered"][0]["resolution"], "absence_check_1")

    def test_lookup_failure_keeps_the_entry_pending(self):
        h = self.crashed()
        h.broker.fail_once["lookup_order_ref"] = BrokerUnavailable("gateway restarting")
        r = h.tick(15)
        self.assertTrue(any(e.startswith("recovery_broker_unavailable") for e in r["errors"]))
        self.assertEqual(r["entry_block"], "maintenance_incomplete")
        self.assertEqual(h.orders()[0]["state"], "PENDING_SUBMIT")

    def test_lost_ack_with_positive_evidence_is_adopted_with_leg_identities(self):
        h = LoopHarness(self, once=True)
        h.broker.lose_ack_next_submit = True
        h.tick()
        r = h.tick(15)
        self.assertEqual(r["recovered"][0]["resolution"], "adopted")
        o = h.orders()[0]
        self.assertEqual(o["state"], "OPEN")
        self.assertTrue(o["tp_perm_id"] and o["sl_perm_id"] and o["broker_perm_id"] and o["con_id"])
        h.prices["AAA"] = 94.0
        self.assertEqual(h.tick(15)["closed"][0]["exit_reason"], "stop_loss")
        self.assertEqual(entry_orders(h.broker), 1)

    def test_config_refuses_single_confirmation(self):
        with self.assertRaises(ValueError):
            LoopConfig(recovery_absence_confirmations=1).validate()


class D23JournalMigration(unittest.TestCase):
    def test_pre_correction_database_is_migrated_in_place(self):
        d = tempfile.mkdtemp(prefix="d23mig_")
        self.addCleanup(shutil.rmtree, d, True)
        db = os.path.join(d, "old.db")
        con = sqlite3.connect(db)
        con.executescript("""CREATE TABLE loop_orders (client_ref TEXT PRIMARY KEY, cycle_key TEXT NOT NULL,
            strategy_id TEXT, symbol TEXT NOT NULL, direction TEXT NOT NULL, qty REAL NOT NULL,
            entry_ref_price REAL, stop_loss REAL, take_profit REAL, score REAL, state TEXT NOT NULL,
            data_source TEXT NOT NULL, broker_order_id TEXT, broker_perm_id TEXT, submit_status TEXT,
            risk_decision_id TEXT, equity_at_entry REAL, planned_at_utc TEXT NOT NULL,
            submitted_at_utc TEXT, entry_fill_price REAL, entry_filled_at_utc TEXT, entry_fees REAL,
            exit_fill_price REAL, exit_filled_at_utc TEXT, exit_reason TEXT, exit_fees REAL,
            time_stop_requested_at_utc TEXT, note TEXT, updated_at_utc TEXT NOT NULL);""")
        con.close()
        j = LoopJournal(db)
        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(loop_orders)")}
        con.close()
        self.assertTrue({"order_ref", "tp_perm_id", "sl_perm_id", "con_id", "account",
                         "absence_checks"} <= cols)
        self.assertEqual(len(j.journal_id), 8)


# ── IBKR adapter over a fake IB (no gateway) ────────────────────────────────

ACCT = "DUP623346"


class FakeWorld:
    """Shared broker state behind every fake IB connection."""

    def __init__(self):
        self.accounts = [ACCT]
        self.trades = []
        self.positions = []          # SimpleNamespace(account, contract, position, avgCost)
        self.executions = []
        self.placed = []
        self.cancelled = []
        self.calls = []
        self.next_id = 100
        self.next_perm = 9000
        self.parent_status = "Submitted"
        self.fill_market = False
        self.fail_place_at = None
        self.fail_completed = False
        self.cancel_ok = True
        self.on_cancel = None
        self.position_lags = False          # positions() does not reflect a close fill yet
        self.ticker = SimpleNamespace(last=100.0, bid=99.9, ask=100.1, time=T0, marketDataType=1)
        self.account_values = [FakeIBValue(account=ACCT, tag="NetLiquidation", value="1000000",
                                           currency="USD")]
        self.disconnects = 0

    def contract(self, symbol, con_id):
        return SimpleNamespace(symbol=symbol, conId=con_id, secType="STK", currency="USD")

    def add_trade(self, symbol, con_id, action, qty, *, ref="", order_type="LMT", status="Submitted",
                  account=ACCT, perm=None):
        self.next_perm += 1
        o = SimpleNamespace(orderId=len(self.trades) + 1, permId=perm or self.next_perm, action=action,
                            totalQuantity=qty, orderRef=ref, orderType=order_type, parentId=0,
                            account=account)
        tr = SimpleNamespace(contract=self.contract(symbol, con_id) if symbol else None, order=o,
                             orderStatus=SimpleNamespace(status=status, permId=o.permId), log=[])
        self.trades.append(tr)
        return tr

    def add_execution(self, symbol, con_id, side, qty, price, *, ref="", perm=1, acct=ACCT, t=T0,
                      report="match", commission=1.0):
        ex = SimpleNamespace(execId="x%d" % (len(self.executions) + 1), acctNumber=acct, time=t,
                             side=side, shares=qty, price=price, orderId=1, permId=perm, orderRef=ref)
        rep = {"match": SimpleNamespace(execId=ex.execId, commission=commission),
               "default": SimpleNamespace(execId="", commission=0.0),     # ib_insync placeholder
               "other": SimpleNamespace(execId="zz", commission=commission),
               "none": None}[report]
        self.executions.append(SimpleNamespace(execution=ex, contract=self.contract(symbol, con_id),
                                               commissionReport=rep, time=t))

    def set_position(self, symbol, con_id, qty, account=ACCT):
        self.positions = [p for p in self.positions if p.contract.conId != con_id or p.account != account]
        if qty:
            self.positions.append(SimpleNamespace(account=account, contract=self.contract(symbol, con_id),
                                                  position=qty, avgCost=100.0))


class FakeIB:
    def __init__(self, w):
        self.w = w
        self.client = SimpleNamespace(getReqId=self._req_id)

    def _req_id(self):
        self.w.next_id += 1
        return self.w.next_id

    def managedAccounts(self):
        return list(self.w.accounts)

    def positions(self, account=""):
        return list(self.w.positions)

    def reqAllOpenOrders(self):
        self.w.calls.append("reqAllOpenOrders")
        return [t.order for t in self.openTrades()]

    def openTrades(self):
        return [t for t in self.w.trades
                if t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")]

    def reqCompletedOrders(self, apiOnly):
        if self.w.fail_completed:
            raise RuntimeError("completed orders unavailable")
        return [t for t in self.w.trades
                if t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")]

    def reqExecutions(self, execFilter=None):
        self.w.calls.append(("reqExecutions", getattr(execFilter, "acctCode", None)))
        return list(self.w.executions)

    def accountValues(self, account=""):
        return list(self.w.account_values)

    def qualifyContracts(self, *cs):
        c = cs[0]
        sym = getattr(c, "symbol", "") or {"111": "AAA", "222": "BBB"}.get(str(getattr(c, "conId", "")), "")
        return [self.w.contract(sym, {"AAA": 111, "BBB": 222}.get(sym, 333))] if sym else []

    def reqMarketDataType(self, t):
        self.w.calls.append(("mdt", t))

    def reqTickers(self, *cs):
        return [self.w.ticker]

    def placeOrder(self, contract, order):
        if self.w.fail_place_at is not None and len(self.w.placed) == self.w.fail_place_at:
            raise ConnectionError("socket dropped mid-placement")
        self.w.next_perm += 1
        order.permId = self.w.next_perm
        status = self.w.parent_status if not getattr(order, "parentId", 0) else "PreSubmitted"
        if order.orderType == "MKT" and getattr(order, "parentId", 0) == 0 and self.w.fill_market:
            status = "Filled"
            sign = 1 if order.action == "BUY" else -1
            cur = sum(p.position for p in self.w.positions if p.contract.conId == contract.conId)
            if not self.w.position_lags:
                self.w.set_position(contract.symbol, contract.conId, cur + sign * order.totalQuantity)
            self.w.add_execution(contract.symbol, contract.conId, "BOT" if sign > 0 else "SLD",
                                 order.totalQuantity, 100.0, ref=order.orderRef, perm=order.permId)
        tr = SimpleNamespace(contract=contract, order=order,
                             orderStatus=SimpleNamespace(status=status, permId=order.permId), log=[])
        self.w.placed.append(order)
        self.w.trades.append(tr)
        return tr

    def cancelOrder(self, order):
        self.w.cancelled.append(order.permId)
        if self.w.on_cancel is not None:
            self.w.on_cancel()
        if self.w.cancel_ok:
            for t in self.w.trades:
                if t.order is order:
                    t.orderStatus.status = "Cancelled"

    def sleep(self, s):
        pass

    def disconnect(self):
        self.w.disconnects += 1


class D23IbkrAdapter(unittest.TestCase):
    PARAMS = staticmethod(lambda: ("127.0.0.1", 4002, ACCT, 1))

    def adapter(self, world=None, **kw):
        self.w = world or FakeWorld()
        state = {"kill": False, "policy": (False, "ok"), "gw": True, "live": False,
                 "params": ("127.0.0.1", 4002, ACCT, 1)}
        self.state = state
        args = dict(ib_factory=lambda: FakeIB(self.w), get_params=lambda: state["params"],
                    is_live=lambda: state["live"], kill_switch_active=lambda: state["kill"],
                    runtime_policy=lambda: state["policy"],
                    gateway_available=lambda h, p: state["gw"], clock=lambda: T0,
                    ack_wait_seconds=0, cancel_wait_seconds=1, close_fill_wait_seconds=1,
                    poll_seconds=0.5)
        args.update(kw)
        return IbkrLoopBroker(**args)

    REQ = BracketRequest("slot:x|AAA", "AAA", "long", 1, 100.0, 95.0, 110.0, "s", order_ref="M21L:j:x|AAA")

    def test_refuses_non_paper(self):
        for kw in (dict(is_live=lambda: True), dict(get_params=lambda: ("h", 4001, ACCT, 1)),
                   dict(get_params=lambda: ("h", 4002, "U17402165", 1))):
            with self.assertRaises(PaperModeRefused):
                self.adapter(**kw)

    def test_submit_sets_order_ref_on_every_leg_and_returns_identities(self):
        b = self.adapter()
        ack = b.submit_bracket(self.REQ)
        self.assertTrue(ack.accepted, ack)
        parent, tp, sl = self.w.placed
        self.assertEqual({o.orderRef for o in (parent, tp, sl)}, {"M21L:j:x|AAA"})
        self.assertEqual({o.account for o in (parent, tp, sl)}, {ACCT})
        self.assertEqual((tp.parentId, sl.parentId), (parent.orderId, parent.orderId))
        self.assertEqual((parent.orderType, tp.orderType, sl.orderType), ("MKT", "LMT", "STP"))
        self.assertEqual((ack.order_ref, ack.con_id, ack.account), ("M21L:j:x|AAA", "111", ACCT))
        self.assertEqual({k: v.perm_id for k, v in ack.legs.items()},
                         {"entry": str(parent.permId), "take_profit": str(tp.permId),
                          "stop_loss": str(sl.permId)})

    def test_every_preflight_blocks_before_any_order(self):
        cases = [("kill", True, "kill_switch_active"), ("kill", None, "kill_switch_active"),
                 ("policy", (True, "signal_only"), "signal_only_skipped"),
                 ("live", True, "live_mode_refused"),
                 ("params", ("h", 4001, ACCT, 1), "paper_port_refused"),
                 ("params", ("h", 4002, "OTHER", 1), "account_changed"),
                 ("gw", False, "connection_failed")]
        for key, val, status in cases:
            with self.subTest(key=key, val=val):
                b = self.adapter()
                self.state[key] = val
                ack = b.submit_bracket(self.REQ)
                self.assertFalse(ack.accepted)
                self.assertEqual(ack.status, status)
                self.assertEqual(self.w.placed, [])
        b = self.adapter()
        self.w.accounts = ["U17402165"]
        self.assertEqual(b.submit_bracket(self.REQ).status, "account_mismatch")
        self.assertEqual(self.w.placed, [])
        self.assertFalse(self.adapter().submit_bracket(dataclasses.replace(self.REQ, order_ref="")).accepted)

    def test_unknown_outcome_after_placement_raises_dead_parent_is_rejected(self):
        b = self.adapter()
        self.w.fail_place_at = 1                                  # parent placed, TP fails
        with self.assertRaises(ConnectionError):
            b.submit_bracket(self.REQ)
        b = self.adapter()
        self.w.parent_status = "PendingSubmit"
        with self.assertRaises(BrokerUnavailable):
            b.submit_bracket(self.REQ)
        b = self.adapter()
        self.w.parent_status = "Inactive"
        ack = b.submit_bracket(self.REQ)
        self.assertEqual((ack.accepted, ack.status), (False, "broker_rejected"))

    def test_reads_are_account_scoped_and_contract_aware(self):
        w = FakeWorld()
        w.set_position("AAA", 111, 1)
        w.set_position("AAA", 111, 5, account="OTHER")
        w.add_trade("AAA", 111, "SELL", 1, ref="M21L:j:x|AAA", order_type="LMT")
        w.add_trade("AAA", 111, "SELL", 1, ref="", order_type="STP")          # manual
        w.add_trade("AAA", 111, "BUY", 1, ref="", account="OTHER")
        w.add_trade(None, None, "BUY", 1, ref="")                             # no contract
        w.add_execution("AAA", 111, "BOT", 1, 100.0, ref="M21L:j:x|AAA", perm=7)
        w.add_execution("AAA", 111, "BOT", 1, 100.0, acct="OTHER")
        b = self.adapter(w)
        self.assertEqual([(p.symbol, p.qty, p.con_id, p.account) for p in b.positions()],
                         [("AAA", 1.0, "111", ACCT)])
        oo = b.open_orders()
        self.assertEqual([(o.order_ref, o.con_id, o.order_type) for o in oo],
                         [("M21L:j:x|AAA", "111", "LMT"), (None, "111", "STP"), (None, None, "LMT")])
        self.assertIsNone(oo[2].symbol)                          # unattributed -> blocks entries
        self.assertIn("reqAllOpenOrders", w.calls)
        f, = b.fills(T0.isoformat())
        self.assertEqual((f.order_ref, f.perm_id, f.con_id, f.account, f.side),
                         ("M21L:j:x|AAA", "7", "111", ACCT, "BUY"))
        self.assertIn(("reqExecutions", ACCT), w.calls)
        w.add_execution("AAA", 111, "BOT", 1, 100.0, t=datetime(2026, 10, 26, 14))
        with self.assertRaises(BrokerUnavailable):
            b.fills(T0.isoformat())
        w.accounts = ["OTHER"]
        with self.assertRaises(BrokerUnavailable):
            b.positions()

    def test_lookup_reports_completeness_honestly(self):
        w = FakeWorld()
        ref = "M21L:j:x|AAA"
        w.add_trade("AAA", 111, "BUY", 1, ref=ref, order_type="MKT", status="Filled")
        w.add_trade("AAA", 111, "SELL", 1, ref=ref, order_type="LMT")
        w.add_trade("AAA", 111, "SELL", 1, ref="OTHER")
        w.add_execution("AAA", 111, "BOT", 1, 100.0, ref=ref, perm=5)
        w.add_execution("AAA", 111, "SLD", 1, 101.0, ref=flatten_ref(ref), perm=6)
        b = self.adapter(w)
        lk = b.lookup_order_ref(ref, T0.isoformat())
        self.assertTrue(lk.complete)
        self.assertEqual(sorted(o.status for o in lk.orders), ["Filled", "Submitted"])
        self.assertEqual(sorted(f.order_ref for f in lk.fills), [ref, flatten_ref(ref)])
        w.fail_completed = True
        lk = b.lookup_order_ref(ref, T0.isoformat())
        self.assertFalse(lk.complete)
        self.assertTrue(any(r.startswith("completed_orders_unavailable") for r in lk.reasons))
        w.fail_completed = False
        old = (T0 - timedelta(hours=13)).isoformat()
        self.assertEqual(b.lookup_order_ref(ref, old).reasons, ["outside_execution_history_window"])

    def test_quote(self):
        b = self.adapter()
        q = b.quote("AAA")
        self.assertEqual((q.price, q.delayed, q.as_of_utc), (100.0, False, T0.isoformat()))
        self.assertIn(("mdt", 1), self.w.calls)
        self.w.ticker = SimpleNamespace(last=float("nan"), bid=99.0, ask=101.0, time=T0, marketDataType=3)
        q = b.quote("AAA")
        self.assertEqual((q.price, q.delayed), (100.0, True))
        for bad in (SimpleNamespace(last=float("nan"), bid=float("nan"), ask=float("nan"), time=T0,
                                    marketDataType=1),
                    SimpleNamespace(last=100.0, bid=None, ask=None, time=None, marketDataType=1),
                    SimpleNamespace(last=100.0, bid=None, ask=None, time=datetime(2026, 10, 26, 14),
                                    marketDataType=1)):
            self.w.ticker = bad
            with self.assertRaises(BrokerUnavailable):
                b.quote("AAA")
        with self.assertRaises(BrokerUnavailable):
            b.quote("ZZZZ")                                      # cannot qualify

    def owned(self, w, qty=1.0):
        ref = "M21L:j:x|AAA"
        tp = w.add_trade("AAA", 111, "SELL", 1, ref=ref, order_type="LMT")
        sl = w.add_trade("AAA", 111, "SELL", 1, ref=ref, order_type="STP")
        perms = (str(tp.order.permId), str(sl.order.permId))
        return OwnedTrade("slot:x|AAA", ref, "AAA", "111", ACCT, "long", qty, perms, perms), tp, sl

    def test_flatten_refuses_unowned_positions_and_foreign_orders_without_any_action(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 2)                            # loop 1 + manual 1
        b = self.adapter(w)
        res = b.flatten_owned(trade)
        self.assertTrue(res.refused)
        self.assertEqual(res.detail["reason"], "position_not_owned")
        self.assertEqual((w.cancelled, w.placed), ([], []))
        w.set_position("AAA", 111, 1)
        manual = w.add_trade("AAA", 111, "SELL", 1, ref="", order_type="LMT")
        res = b.flatten_owned(trade)
        self.assertEqual((res.refused, res.detail["reason"]), (True, "foreign_orders_on_contract"))
        self.assertEqual((w.cancelled, w.placed), ([], []))
        self.assertEqual(manual.orderStatus.status, "Submitted")
        for t in (dataclasses.replace(trade, con_id=None), dataclasses.replace(trade, account="OTHER")):
            self.assertTrue(b.flatten_owned(t).refused)
        self.state["kill"] = True
        res = b.flatten_owned(trade)
        self.assertEqual((res.confirmed, res.refused, res.detail["reason"]),
                         (False, False, "kill_switch_active"))

    def test_flatten_cancels_only_own_legs_and_closes_own_quantity(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)
        w.set_position("BBB", 222, 4)
        manual_other = w.add_trade("BBB", 222, "SELL", 4, ref="", order_type="STP")
        w.fill_market = True
        b = self.adapter(w)
        res = b.flatten_owned(trade)
        self.assertTrue(res.confirmed, res)
        self.assertEqual(sorted(w.cancelled), sorted([tp.order.permId, sl.order.permId]))
        close, = w.placed
        self.assertEqual((close.action, close.totalQuantity, close.orderRef, close.account),
                         ("SELL", 1.0, flatten_ref(trade.order_ref), ACCT))
        self.assertEqual(res.close_order_ref, flatten_ref(trade.order_ref))
        self.assertEqual(manual_other.orderStatus.status, "Submitted")
        self.assertEqual([(p.contract.symbol, p.position) for p in w.positions], [("BBB", 4)])

    def test_flatten_stops_when_cancel_is_unconfirmed_or_a_leg_filled(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)
        w.cancel_ok = False
        b = self.adapter(w)
        res = b.flatten_owned(trade)
        self.assertEqual((res.confirmed, res.refused, res.detail["reason"]),
                         (False, False, "cancel_unconfirmed"))
        self.assertEqual(w.placed, [])
        tp.orderStatus.status = "Filled"                         # position read may lag the fill
        w.cancel_ok = True
        w.cancelled.clear()
        res = b.flatten_owned(trade)
        self.assertEqual((res.confirmed, res.refused, res.detail["reason"]),
                         (False, False, "exit_leg_filled"))
        self.assertEqual((w.placed, w.cancelled), ([], []))     # nothing touched at all
        tp.orderStatus.status = "Submitted"
        w.fail_completed = True
        self.assertEqual(b.flatten_owned(trade).detail["reason"], "leg_state_unknown")
        self.assertEqual(w.placed, [])

    def test_loop_end_to_end_over_fake_ibkr_ignores_manual_activity(self):
        w = FakeWorld()
        w.fill_market = True
        clock = LoopClock()
        w.ticker = SimpleNamespace(last=100.0, bid=None, ask=None, time=T0, marketDataType=1)
        b = self.adapter(w, clock=clock)
        d = tempfile.mkdtemp(prefix="d23ib_")
        self.addCleanup(shutil.rmtree, d, True)
        loop = PaperLearningLoop(
            journal=LoopJournal(os.path.join(d, "j.db")), broker=b, d0_gate=lambda: (True, "ok"),
            d1_gate=lambda: (True, "ok"), kill_switch_active=lambda: False,
            risk_gate=lambda **kw: (True, "allow", "d1"),
            candidate_provider=lambda now: [dataclasses.replace(cand(), signal_ts_utc=now.isoformat())],
            lock_path=os.path.join(d, "l.lock"), config=LoopConfig(max_hold_minutes=600),
            clock=clock, pid=os.getpid())
        r = loop.tick()
        self.assertEqual(r["entry"]["state"], "SUBMITTED")
        o = loop.journal.orders()[0]
        self.assertEqual((o["con_id"], o["account"]), ("111", ACCT))
        self.assertTrue(o["tp_perm_id"] and o["sl_perm_id"])
        clock.advance(15)
        w.ticker.time = clock()
        w.add_execution("AAA", 111, "SLD", 1, 110.0, t=clock())     # manual sell lookalike
        r = loop.tick()
        self.assertEqual(r["opened"], [o["client_ref"]])
        self.assertEqual(r["closed"], [])
        # the real take-profit leg fills at the broker
        tp_trade = next(t for t in w.trades if str(t.order.permId) == o["tp_perm_id"])
        tp_trade.orderStatus.status = "Filled"
        clock.advance(15)
        w.add_execution("AAA", 111, "SLD", 1, 110.0, ref=o["order_ref"],
                        perm=int(o["tp_perm_id"]), t=clock())
        r = loop.tick()
        self.assertEqual(r["closed"][0]["exit_reason"], "take_profit")
        out = loop.journal.outcomes()[0]
        self.assertEqual((out["entry_price"], out["exit_price"], out["data_source"]),
                         (100.0, 110.0, "ibkr_paper"))


# ═════════════════════════════════════════════════════════════════════════════
# PR #1 review: P0 duplicate-close risk, P0 fail-open exit-leg recheck,
# P1 commission integrity.
# ═════════════════════════════════════════════════════════════════════════════

class PaperTaggedSim(SimulatedPaperBroker):
    """Contract-test broker: tags rows like IBKR paper so D3 would count them."""
    data_source = "ibkr_paper"

    def account_snapshot(self):
        s = super().account_snapshot()
        return AccountSnapshot(s.account, s.net_liquidation, s.cash, s.currency, s.as_of_utc,
                               "ibkr_paper")


def time_stop_harness(testcase, broker_cls=SimulatedPaperBroker, **broker_kw):
    """OPEN trade on AAA at T0+15; the time stop (max_hold 60) fires at T0+90."""
    h = LoopHarness(testcase, once=True, config=LoopConfig(max_hold_minutes=60))
    h.broker = broker_cls(price_fn=lambda s, now: h.prices[s], clock=h.clock, **broker_kw)
    h.loop = h.make_loop()
    h.tick()
    h.tick(15)
    testcase.assertEqual(h.orders()[0]["state"], "OPEN")
    return h


def close_orders(broker):
    return [o for o in broker.raw_state()["orders"] if o["kind"] == "flatten"]


class D23CloseIntent(unittest.TestCase):
    """P0-1: a close that may exist is never sent again."""

    def test_intent_is_durable_before_the_broker_is_called(self):
        seen = {}

        class Spy(SimulatedPaperBroker):
            journal_db = None

            def flatten_owned(self, trade):
                seen["row"] = LoopJournal(self.journal_db).get_order(trade.client_ref)
                return super().flatten_owned(trade)
        h = time_stop_harness(self, Spy)
        h.broker.journal_db = h.db
        h.prices["AAA"] = 101.0
        r = h.tick(75)
        self.assertEqual(seen["row"]["close_state"], "INTENT")
        self.assertIsNotNone(seen["row"]["time_stop_requested_at_utc"])
        self.assertEqual(seen["row"]["close_attempts"], 1)
        self.assertEqual(r["closed"][0]["exit_reason"], "time_stop")

    def test_late_filled_close_is_captured_and_never_resent(self):
        h = time_stop_harness(self)
        h.broker.close_fill_not_before = T0 + timedelta(minutes=100)
        h.prices["AAA"] = 101.0
        r = h.tick(75)                                            # T0+90: close placed, not filled
        self.assertTrue(any(e.startswith("time_stop_close_unconfirmed") for e in r["errors"]))
        o = h.orders()[0]
        (close,) = close_orders(h.broker)
        self.assertEqual((o["close_state"], o["flatten_perm_id"], o["flatten_order_id"]),
                         ("SUBMITTED", close["perm_id"], close["order_id"]))
        r = h.tick(15)                                            # T0+105: the close fills late
        self.assertEqual(r["closed"][0]["exit_reason"], "time_stop")
        self.assertEqual(h.broker.calls.count("flatten_owned"), 1)
        self.assertEqual(len(close_orders(h.broker)), 1)
        self.assertEqual(h.broker.positions(), [])                # flat, not short

    def test_broker_timeout_after_placing_is_reconciled_not_resent(self):
        h = time_stop_harness(self)
        h.broker.close_fill_not_before = T0 + timedelta(minutes=115)
        h.broker.lose_flatten_ack = True
        r = h.tick(75)                                            # T0+90: placed, then timeout
        self.assertTrue(any(e.startswith("time_stop_close_outcome_unknown") for e in r["errors"]))
        o = h.orders()[0]
        self.assertEqual((o["close_state"], o["flatten_perm_id"]), ("INTENT", None))
        r = h.tick(15)                                            # T0+105: still working
        o = h.orders()[0]
        (close,) = close_orders(h.broker)
        self.assertEqual((o["close_state"], o["flatten_perm_id"]), ("SUBMITTED", close["perm_id"]))
        self.assertTrue(any(e.startswith("time_stop_close_pending") for e in r["errors"]))
        r = h.tick(15)                                            # T0+120: filled
        self.assertEqual(r["closed"][0]["exit_reason"], "time_stop")
        self.assertEqual((h.broker.calls.count("flatten_owned"), len(close_orders(h.broker))), (1, 1))

    def test_unknown_close_that_never_appears_goes_to_review_unsent(self):
        h = time_stop_harness(self)
        h.broker.fail_once["flatten_owned"] = TimeoutError("lost before anything was known")
        h.tick(75)
        self.assertEqual(h.orders()[0]["close_state"], "INTENT")
        for _ in range(2):                                        # +15, +30: inside the window
            r = h.tick(15)
            self.assertIn("time_stop_close_pending:AAA:no_close_order_seen", r["errors"])
            self.assertEqual(h.orders()[0]["state"], "OPEN")
        h.tick(15)                                                # +45 > 30 min
        o = h.orders()[0]
        self.assertEqual(o["state"], "NEEDS_REVIEW")
        self.assertIn("no close order or execution found", o["note"])
        self.assertEqual(h.broker.calls.count("flatten_owned"), 1)
        self.assertEqual(close_orders(h.broker), [])

    def test_dead_close_order_goes_to_review_unsent(self):
        h = time_stop_harness(self)
        h.broker.close_fill_not_before = T0 + timedelta(days=1)
        h.tick(75)
        (close,) = close_orders(h.broker)
        h.broker.order_by_perm(close["perm_id"])["status"] = "Cancelled"   # rejected/cancelled at broker
        h.tick(15)
        o = h.orders()[0]
        self.assertEqual(o["state"], "NEEDS_REVIEW")
        self.assertIn("close order Cancelled without an execution", o["note"])
        self.assertEqual(len(close_orders(h.broker)), 1)

    def test_proven_not_placed_clears_the_intent_and_retries(self):
        from bot.paper_loop_d2.broker_port import FlattenResult

        class OnceNotPlaced(SimulatedPaperBroker):
            first = True

            def flatten_owned(self, trade):
                if self.first:
                    self.first = False
                    self.calls.append("flatten_owned")
                    return FlattenResult(confirmed=False, close_placed=False,
                                         detail={"reason": "cancel_unconfirmed"})
                return super().flatten_owned(trade)
        h = time_stop_harness(self, OnceNotPlaced)
        r = h.tick(75)
        self.assertIn("time_stop_not_placed:AAA:cancel_unconfirmed", r["errors"])
        o = h.orders()[0]
        self.assertEqual((o["close_state"], o["time_stop_requested_at_utc"], o["close_attempts"]),
                         (None, None, 1))
        r = h.tick(15)
        self.assertEqual(r["closed"][0]["exit_reason"], "time_stop")
        self.assertEqual(h.orders()[0]["close_attempts"], 2)
        self.assertEqual(len(close_orders(h.broker)), 1)

    def test_simulator_itself_refuses_a_second_close_while_one_works(self):
        h = time_stop_harness(self)
        h.broker.close_fill_not_before = T0 + timedelta(days=1)
        h.tick(75)
        o = h.orders()[0]
        legs = (o["tp_perm_id"], o["sl_perm_id"])
        res = h.broker.flatten_owned(OwnedTrade(o["client_ref"], o["order_ref"], "AAA", o["con_id"],
                                                "SIM-PAPER", "long", 1.0, legs, legs))
        self.assertEqual((res.close_placed, res.detail["reason"]), (False, "close_already_working"))
        self.assertEqual(len(close_orders(h.broker)), 1)

    def test_unknown_placement_is_never_treated_as_not_placed(self):
        from bot.paper_loop_d2.broker_port import FlattenResult

        class Unknown(SimulatedPaperBroker):
            def flatten_owned(self, trade):
                self.calls.append("flatten_owned")
                return FlattenResult(confirmed=False, close_placed=None,
                                     detail={"reason": "close_outcome_unknown:ConnectionError"})
        h = time_stop_harness(self, Unknown)
        h.tick(75)
        self.assertEqual(h.orders()[0]["close_state"], "INTENT")
        h.tick(15)
        h.tick(15)
        self.assertEqual(h.broker.calls.count("flatten_owned"), 1)


class D23AdapterCloseOutcomes(unittest.TestCase):
    """P0-1 / P0-2 at the IBKR adapter."""
    adapter = D23IbkrAdapter.adapter          # shared fake-IB fixtures (no inherited tests)
    owned = D23IbkrAdapter.owned

    def test_close_placed_but_unfilled_reports_placed_with_ids(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)
        res = self.adapter(w).flatten_owned(trade)                # fill_market False: stays working
        close, = w.placed
        self.assertEqual((res.confirmed, res.close_placed), (False, True))
        self.assertEqual((res.close_perm_id, res.close_order_ref), (str(close.permId),
                                                                    flatten_ref(trade.order_ref)))

    def test_stale_position_after_fill_is_placed_but_unconfirmed(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)
        w.fill_market = True
        w.position_lags = True
        res = self.adapter(w).flatten_owned(trade)
        self.assertEqual((res.confirmed, res.close_placed), (False, True))
        self.assertEqual(res.detail["remaining_qty"], 1.0)

    def test_place_order_failure_is_unknown_not_not_placed(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)
        w.fail_place_at = 0
        res = self.adapter(w).flatten_owned(trade)
        self.assertIsNone(res.close_placed)
        self.assertFalse(res.confirmed)
        self.assertTrue(res.detail["reason"].startswith("close_outcome_unknown"))
        self.assertEqual(res.close_order_ref, flatten_ref(trade.order_ref))

    def test_every_pre_placement_exit_reports_not_placed(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 2)
        b = self.adapter(w)
        self.assertIs(b.flatten_owned(trade).close_placed, False)          # not owned
        self.state["kill"] = True
        self.assertIs(b.flatten_owned(trade).close_placed, False)          # kill switch
        self.state["kill"] = False
        w.set_position("AAA", 111, 1)
        w.add_trade("AAA", 111, "SELL", 1, ref=flatten_ref(trade.order_ref), order_type="MKT")
        res = b.flatten_owned(trade)                                       # a close already works
        self.assertEqual((res.close_placed, res.detail["reason"]), (False, "close_already_working"))
        self.assertEqual((w.placed, w.cancelled), ([], []))

    def test_unknown_exit_leg_state_after_cancellation_blocks_the_close(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)

        def completed_orders_fail():
            w.fail_completed = True                               # read fails AFTER cancelling
        w.on_cancel = completed_orders_fail
        res = self.adapter(w).flatten_owned(trade)
        self.assertEqual((res.confirmed, res.close_placed, res.detail["reason"]),
                         (False, False, "leg_state_unknown_after_cancel"))
        self.assertEqual(w.placed, [])                            # no market close
        self.assertEqual(sorted(w.cancelled), sorted([tp.order.permId, sl.order.permId]))

    def test_leg_fill_reported_only_after_cancellation_blocks_the_close(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)                             # position read lags the fill

        def late_fill_report():
            if not any(t.orderStatus.status == "Filled" for t in w.trades):
                w.add_trade("AAA", 111, "SELL", 1, ref=trade.order_ref, order_type="STP",
                            status="Filled")                      # an own exit leg, seen only now
        w.on_cancel = late_fill_report
        res = self.adapter(w).flatten_owned(trade)
        self.assertEqual((res.close_placed, res.detail["reason"]), (False, "exit_leg_filled"))
        self.assertEqual(w.placed, [])

    def test_exit_leg_filled_during_cancellation_blocks_the_close(self):
        w = FakeWorld()
        trade, tp, sl = self.owned(w)
        w.set_position("AAA", 111, 1)
        w.cancel_ok = False

        def tp_fills():
            tp.orderStatus.status = "Filled"
            sl.orderStatus.status = "Cancelled"                   # OCA
        w.on_cancel = tp_fills
        res = self.adapter(w).flatten_owned(trade)
        self.assertEqual((res.close_placed, res.detail["reason"]), (False, "exit_leg_filled"))
        self.assertEqual(w.placed, [])


class D23CommissionIntegrity(unittest.TestCase):
    """P1-3: unknown fees are never zero; net P&L only when proven."""

    def test_adapter_distinguishes_unknown_from_proven_zero(self):
        w = FakeWorld()
        kinds = [("none", 1.0), ("default", 0.0), ("other", 1.0), ("match", float("nan")),
                 ("match", 1.7976931348623157e308), ("match", -1.0), ("match", 0.0), ("match", 1.25)]
        for i, (rep, c) in enumerate(kinds):
            w.add_execution("AAA", 111, "BOT", 1, 100.0, ref="R%d" % i, report=rep, commission=c)
        b = D23IbkrAdapter.adapter(self, w)                      # fake IB, no gateway
        got = [f.commission for f in sorted(b.fills(T0.isoformat()), key=lambda f: f.order_ref)]
        self.assertEqual(got, [None, None, None, None, None, None, 0.0, 1.25])

    def run_round_trip(self, broker_cls=PaperTaggedSim, **kw):
        h = LoopHarness(self, once=True)
        h.broker = broker_cls(price_fn=lambda s, now: h.prices[s], clock=h.clock, **kw)
        h.loop = h.make_loop()
        return h

    def test_unknown_fees_keep_net_pnl_unproven_until_reported(self):
        from bot.runtime.paper_outcome_metrics import compute_paper_metrics
        h = self.run_round_trip()
        h.broker.hold_commissions = True
        h.tick()
        h.tick(15)
        self.assertIsNone(h.orders()[0]["entry_fees"])            # not 0.0
        h.prices["AAA"] = 111.0
        r = h.tick(15)
        self.assertEqual(r["closed"][0]["fees_final"], 0)
        o = h.outcomes()[0]
        self.assertEqual((o["gross_pnl"], o["fees"], o["realized_pnl"], o["fees_final"]),
                         (10.0, None, None, 0))
        m = compute_paper_metrics(h.db)
        self.assertEqual((m["edge_outcomes"]["rows_total"], m["edge_outcomes"]["rows_genuine"]), (1, 0))
        self.assertIsNone(m["edge_outcomes"]["overall"]["total_pnl"])
        r = h.tick(15)                                            # reports still missing
        self.assertEqual(r["fees_finalized"], [])
        h.broker.release_commissions()
        r = h.tick(15)
        self.assertEqual(r["fees_finalized"], [o["trade_id"]])
        o = h.outcomes()[0]
        self.assertEqual((o["fees"], o["realized_pnl"], o["fees_final"]), (2.0, 8.0, 1))
        self.assertEqual((h.orders()[0]["entry_fees"], h.orders()[0]["exit_fees"]), (1.0, 1.0))
        m = compute_paper_metrics(h.db)
        self.assertEqual(m["edge_outcomes"]["rows_genuine"], 1)
        self.assertAlmostEqual(m["edge_outcomes"]["overall"]["total_pnl"], 8.0)
        self.assertEqual(h.tick(15)["fees_finalized"], [])        # finalized once

    def test_proven_zero_commission_is_final(self):
        h = self.run_round_trip(commission_per_share=0.0, min_commission=0.0)
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        o = h.outcomes()[0]
        self.assertEqual((o["fees"], o["realized_pnl"], o["fees_final"]), (0.0, 10.0, 1))

    def test_fees_that_never_arrive_stay_unfinalized(self):
        h = self.run_round_trip()
        h.broker.hold_commissions = True
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        h.clock.advance(73 * 60)                                  # past the 72 h window
        h.broker.release_commissions()
        self.assertEqual(h.tick()["fees_finalized"], [])
        self.assertEqual(h.outcomes()[0]["fees_final"], 0)

    def test_outcomes_table_migration_adds_fees_final(self):
        d = tempfile.mkdtemp(prefix="d23fees_")
        self.addCleanup(shutil.rmtree, d, True)
        db = os.path.join(d, "old.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE paper_trade_outcomes (trade_id TEXT PRIMARY KEY, realized_pnl REAL)")
        con.close()
        LoopJournal(db)
        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(paper_trade_outcomes)")}
        con.close()
        self.assertIn("fees_final", cols)


class D23DashboardContract(unittest.TestCase):
    def test_outcomes_table_matches_d3_contract_and_sim_is_not_genuine(self):
        from bot.runtime.paper_outcome_metrics import compute_paper_metrics
        h = LoopHarness(self)
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        m = compute_paper_metrics(h.db)
        self.assertEqual(m["edge_outcomes"]["rows_total"], 1)
        self.assertEqual(m["edge_outcomes"]["rows_genuine"], 0)       # simulated never counts
        self.assertIsNone(m["edge_outcomes"]["overall"]["win_rate"])
        self.assertIsNone(m["broker_equity"]["latest_equity"])

    def test_broker_paper_outcomes_feed_d3(self):
        from bot.runtime.paper_outcome_metrics import compute_paper_metrics

        class PaperTagged(SimulatedPaperBroker):     # contract test only: tags rows like IBKR paper
            data_source = "ibkr_paper"

            def account_snapshot(self):
                s = super().account_snapshot()
                return AccountSnapshot(s.account, s.net_liquidation, s.cash, s.currency,
                                       s.as_of_utc, "ibkr_paper")
        h = LoopHarness(self)
        h.broker = PaperTagged(price_fn=lambda s, now: h.prices[s], clock=h.clock)
        h.loop = h.make_loop()
        h.tick()
        h.tick(15)
        h.prices["AAA"] = 111.0
        h.tick(15)
        m = compute_paper_metrics(h.db)
        ov = m["edge_outcomes"]["overall"]
        self.assertEqual((ov["trades"], ov["wins"], ov["win_rate"]), (1, 1, 1.0))
        self.assertAlmostEqual(ov["total_pnl"], 8.0)
        self.assertEqual(list(m["edge_outcomes"]["by_strategy"]), ["trend_v1"])
        self.assertEqual(m["broker_equity"]["snapshots"], 3)


class D23CliAndStatic(unittest.TestCase):
    def _cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args], cwd=str(REPO),
                              capture_output=True, text=True, timeout=120)

    def test_real_mode_refused_without_flag(self):
        r = self._cli("--broker", "ibkr-paper", "--once")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("REFUSED", r.stderr)

    def test_simulated_mode_tmp_only(self):
        d = tempfile.mkdtemp(prefix="d23cli_", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        r = self._cli("--once", "--min-score", "0", "--db", d + "/l.db", "--lock", d + "/l.lock",
                      "--sim-state", d + "/b.json", "--json-out", d + "/t.json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ticks = json.loads(Path(d, "t.json").read_text())
        self.assertEqual(ticks[0]["data_source"], "simulated")
        r = self._cli("--once", "--db", str(REPO / "data" / "x.db"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only under /tmp/", r.stderr + r.stdout)

    def test_no_test_passes_the_real_loop_flag(self):
        for p in REPO.glob("test_*.py"):
            src = p.read_text(encoding="utf-8")
            self.assertNotIn(REAL_LOOP_FLAG_TEXT, src, p.name)
        cli = (REPO / CLI).read_text(encoding="utf-8")
        self.assertIn(REAL_LOOP_FLAG_TEXT, cli)

    def _imports(self, rel):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
        return mods

    def test_layering(self):
        forbidden_everywhere = ("bot.etoro", "tools.etoro_live_write", "requests", "urllib",
                                "socket", "bot.notifier")
        for rel in ("bot/paper_loop_d2/scheduler.py", "bot/paper_loop_d2/journal.py",
                    "bot/paper_loop_d2/sim_broker.py", "bot/paper_loop_d2/candidates.py",
                    "bot/paper_loop_d2/risk_gate.py", "bot/paper_loop_d2/broker_port.py",
                    "bot/paper_loop_d2/ibkr_adapter.py"):
            mods = self._imports(rel)
            bad = [m for m in mods for f in forbidden_everywhere if m == f or m.startswith(f + ".")]
            self.assertEqual(bad, [], rel)
        # only the IBKR adapter reaches bot.brokers / ib_insync
        for rel in ("bot/paper_loop_d2/scheduler.py", "bot/paper_loop_d2/journal.py",
                    "bot/paper_loop_d2/sim_broker.py", "bot/paper_loop_d2/candidates.py",
                    "bot/paper_loop_d2/broker_port.py"):
            mods = self._imports(rel)
            self.assertFalse(any(m.startswith("bot.brokers") or m.startswith("ib_insync")
                                 for m in mods), rel)

    def test_no_symbol_wide_flatten_or_symbol_only_ownership(self):
        """The loop has no symbol-wide close path and never calls the legacy
        whole-symbol flatten; ownership code never matches on symbol alone."""
        for rel in ("bot/paper_loop_d2/scheduler.py", "bot/paper_loop_d2/sim_broker.py",
                    "bot/paper_loop_d2/ibkr_adapter.py", "bot/paper_loop_d2/broker_port.py"):
            src = (REPO / rel).read_text(encoding="utf-8")
            self.assertNotIn("flatten_paper_position(", src, rel)
            self.assertNotIn("def flatten(", src, rel)
            self.assertNotIn("reqGlobalCancel", src, rel)
        sched = (REPO / "bot/paper_loop_d2/scheduler.py").read_text(encoding="utf-8")
        self.assertNotIn("last_price", sched)            # drift needs a real quote, never skipped


if __name__ == "__main__":
    unittest.main(verbosity=2)

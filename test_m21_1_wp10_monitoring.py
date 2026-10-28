#!/usr/bin/env python3
"""M21 WP10 — unattended monitoring groundwork (offline fixtures, mock sinks).

Proves: a failed gateway, a missing heartbeat, stale inputs, a stalled or
crashed scheduler, unresolved execution and duplicate / unattributed orders
each produce the right alert event; UNKNOWN never counts as healthy; WARN
alarms recover with an explicit recovery record; CRITICAL alarms latch and
can only be acknowledged once their condition has cleared, so a recovery
never silently erases an unresolved risk; state survives a restart without
re-notifying; a failing sink is recorded and changes nothing; and against
the REAL WS2 loop, a restart with an unresolved order raises the alarm.
No Telegram, no network, no real alert.
"""
import ast
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _test_isolation  # noqa: E402  (M21 C5a)
_test_isolation.install()

from bot.monitoring import (  # noqa: E402
    ACKED, ACTIVE, ALARM, CLEARED_PENDING_ACK, CRITICAL, OK, RECOVERED, UNKNOWN, WARN,
    CheckResult, MemorySink, Monitor, check_data_freshness, check_duplicate_orders,
    check_gateway, check_loop_heartbeat, check_scheduler_liveness,
    check_unresolved_execution, read_journal, run_checks,
)

REPO = Path(__file__).resolve().parent
UTC = timezone.utc
T0 = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)
GOOD_GW = {"as_of_utc": T0.isoformat(), "status": "service_active_api_port_open",
           "expected_port": 4002}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def state_db(tc):
    d = tempfile.mkdtemp(prefix="wp10_")
    tc.addCleanup(shutil.rmtree, d, True)
    return os.path.join(d, "monitor.db")


class M1_Checks(unittest.TestCase):
    def test_gateway(self):
        self.assertEqual(check_gateway(GOOD_GW, T0).status, OK)
        down = dict(GOOD_GW, status="service_down")
        r = check_gateway(down, T0)
        self.assertEqual((r.status, r.severity), (ALARM, CRITICAL))
        self.assertIn("service_down", r.detail)
        self.assertEqual(check_gateway(dict(GOOD_GW, status="service_active_login_error"),
                                       T0).status, ALARM)
        self.assertEqual(check_gateway(dict(GOOD_GW, expected_port=4001), T0).status, ALARM)
        self.assertEqual(check_gateway(None, T0).status, UNKNOWN)
        self.assertEqual(check_gateway(GOOD_GW, T0 + timedelta(minutes=6)).status, UNKNOWN)

    def test_heartbeat_and_scheduler(self):
        self.assertEqual(check_loop_heartbeat((T0 - timedelta(minutes=10)).isoformat(), T0,
                                              max_age=timedelta(minutes=32)).status, OK)
        r = check_loop_heartbeat((T0 - timedelta(minutes=40)).isoformat(), T0,
                                 max_age=timedelta(minutes=32))
        self.assertEqual(r.status, ALARM)
        self.assertEqual(check_loop_heartbeat(None, T0, max_age=timedelta(minutes=32)).status,
                         UNKNOWN)
        self.assertEqual(check_loop_heartbeat("2026-11-03T15:00:00", T0,
                                              max_age=timedelta(minutes=32)).status, UNKNOWN)
        iv = timedelta(minutes=15)
        cyc = [{"cycle_key": "a", "started_at_utc": (T0 - timedelta(minutes=10)).isoformat(),
                "state": "DONE"}]
        self.assertEqual(check_scheduler_liveness(cyc, T0, interval=iv, market_open=True).status, OK)
        stale = [dict(cyc[0], started_at_utc=(T0 - timedelta(minutes=40)).isoformat())]
        self.assertEqual(check_scheduler_liveness(stale, T0, interval=iv, market_open=True).status,
                         ALARM)
        self.assertEqual(check_scheduler_liveness(stale, T0, interval=iv, market_open=False).status,
                         OK)
        self.assertEqual(check_scheduler_liveness(stale, T0, interval=iv, market_open=None).status,
                         UNKNOWN)
        crashed = cyc + [{"cycle_key": "b", "started_at_utc": T0.isoformat(),
                          "state": "INTERRUPTED"}]
        r = check_scheduler_liveness(crashed, T0, interval=iv, market_open=False)
        self.assertEqual(r.status, ALARM)
        self.assertIn("interrupted", r.detail)
        self.assertEqual(check_scheduler_liveness([], T0, interval=iv, market_open=True).status,
                         ALARM)

    def test_data_freshness(self):
        good = {"AAA": {"1D": "VALID", "15m": "VALID"}, "BBB": {"1D": "VALID", "15m": "VALID"}}
        self.assertEqual(check_data_freshness(good, market_open=True).status, OK)
        stale = {"AAA": {"1D": "STALE", "15m": "MISSING"}, "BBB": {"1D": "VALID", "15m": "VALID"}}
        r = check_data_freshness(stale, market_open=True)
        self.assertEqual(r.status, ALARM)
        self.assertIn("2/4", r.detail)
        self.assertEqual(check_data_freshness({}, market_open=True).status, UNKNOWN)
        self.assertEqual(check_data_freshness({}, market_open=False).status, OK)

    def test_unresolved_and_duplicates(self):
        old = (T0 - timedelta(hours=1)).isoformat()
        self.assertEqual(check_unresolved_execution([{"state": "OPEN", "updated_at_utc": old}],
                                                    T0).status, OK)
        r = check_unresolved_execution([{"client_ref": "x", "state": "NEEDS_REVIEW",
                                         "updated_at_utc": T0.isoformat()}], T0)
        self.assertEqual((r.status, r.severity), (ALARM, CRITICAL))
        r = check_unresolved_execution([{"client_ref": "p", "state": "PENDING_SUBMIT",
                                         "updated_at_utc": old}], T0)
        self.assertEqual(r.status, ALARM)
        self.assertEqual(check_unresolved_execution(
            [{"client_ref": "p", "state": "PENDING_SUBMIT", "updated_at_utc": T0.isoformat()}],
            T0).status, OK)
        r = check_unresolved_execution([{"client_ref": "c", "state": "OPEN",
                                         "close_state": "INTENT", "updated_at_utc": old}], T0)
        self.assertEqual(r.status, ALARM)
        orders = [{"client_ref": "a", "symbol": "AAA", "state": "OPEN", "order_ref": "r1"}]
        self.assertEqual(check_duplicate_orders(orders, [{"order_ref": "r1", "leg": "tp"}]).status,
                         OK)
        self.assertEqual(check_duplicate_orders(orders, None).status, UNKNOWN)
        dup = orders + [{"client_ref": "b", "symbol": "AAA", "state": "SUBMITTED",
                         "order_ref": "r1"}]
        r = check_duplicate_orders(dup, [])
        self.assertEqual(r.status, ALARM)
        self.assertIn("symbol AAA", r.detail)
        self.assertIn("order_ref r1", r.detail)
        r = check_duplicate_orders(orders, [{"order_ref": None, "order_id": 9}])
        self.assertIn("unattributed", r.detail)
        r = check_duplicate_orders(orders, [{"order_ref": "r1", "leg": "tp"},
                                            {"order_ref": "r1", "leg": "tp"}])
        self.assertIn("broker has 2 orders", r.detail)


class M2_AlarmStateMachine(unittest.TestCase):
    def mon(self, path, sink=None, clock=None):
        return Monitor(path, sinks=[sink] if sink else [], clock=clock or Clock())

    def test_raise_once_and_recover_with_record(self):
        path, sink, clk = state_db(self), MemorySink(), Clock()
        m = self.mon(path, sink, clk)
        warn = CheckResult("data_freshness", ALARM, WARN, "3/4 stale")
        self.assertEqual(m.evaluate([warn])[0]["transition"], "RAISED")
        clk.t += timedelta(minutes=15)
        self.assertEqual(m.evaluate([warn]), [])                     # no repeat alert
        clk.t += timedelta(minutes=15)
        t = m.evaluate([CheckResult("data_freshness", OK, WARN, "4/4 usable")])
        self.assertEqual(t[0]["transition"], "RECOVERED")
        self.assertEqual(m.open_alarms(), [])
        self.assertEqual([e["kind"] for e in sink.events], ["RAISED", "RECOVERED"])
        kinds = [e["kind"] for e in m.events()]
        self.assertEqual(kinds, ["RAISED", "RECOVERED"])

    def test_unknown_is_never_healthy(self):
        m = self.mon(state_db(self))
        m.evaluate([CheckResult("gateway", UNKNOWN, CRITICAL, "no reading")])
        (a,) = m.open_alarms()
        self.assertEqual(a.state, ACTIVE)
        self.assertIn("cannot verify", a.detail)

    def test_critical_latches_until_acked_after_clearing(self):
        path, sink, clk = state_db(self), MemorySink(), Clock()
        m = self.mon(path, sink, clk)
        bad = CheckResult("unresolved_execution", ALARM, CRITICAL, "NEEDS_REVIEW: x")
        m.evaluate([bad])
        (a,) = m.open_alarms()
        self.assertFalse(m.acknowledge(a.alarm_id, operator="mike", reason="checked"))  # still bad
        m.evaluate([CheckResult("unresolved_execution", OK, CRITICAL, "none")])
        (a,) = m.open_alarms()
        self.assertEqual(a.state, CLEARED_PENDING_ACK)               # not silently erased
        self.assertFalse(m.acknowledge(a.alarm_id, operator=" ", reason="x"))
        self.assertFalse(m.acknowledge(a.alarm_id, operator="mike", reason=""))
        m.evaluate([bad])                                           # condition is back
        (a,) = m.open_alarms()
        self.assertEqual(a.state, ACTIVE)
        m.evaluate([CheckResult("unresolved_execution", OK, CRITICAL, "none")])
        self.assertTrue(m.acknowledge(a.alarm_id, operator="mike", reason="order reconciled"))
        self.assertEqual(m.open_alarms(), [])
        kinds = [e["kind"] for e in m.events(a.alarm_id)]
        self.assertEqual(kinds, ["RAISED", "ACK_REFUSED", "CLEARED_PENDING_ACK", "ACK_REFUSED",
                                 "ACK_REFUSED", "REOPENED", "CLEARED_PENDING_ACK", "ACKED"])
        self.assertFalse(m.acknowledge("nope", operator="mike", reason="x"))

    def test_gateway_recovery_does_not_erase_the_unresolved_order(self):
        m = self.mon(state_db(self), MemorySink())
        m.evaluate([CheckResult("gateway", ALARM, CRITICAL, "gateway service_down"),
                    CheckResult("unresolved_execution", ALARM, CRITICAL, "pending_submit=x")])
        m.evaluate([CheckResult("gateway", OK, CRITICAL, "port open"),
                    CheckResult("unresolved_execution", ALARM, CRITICAL, "pending_submit=x")])
        states = {a.check: a.state for a in m.open_alarms()}
        self.assertEqual(states, {"gateway": CLEARED_PENDING_ACK, "unresolved_execution": ACTIVE})

    def test_restart_keeps_state_without_renotifying(self):
        path, sink, clk = state_db(self), MemorySink(), Clock()
        m = self.mon(path, sink, clk)
        bad = CheckResult("gateway", ALARM, CRITICAL, "gateway service_down")
        m.evaluate([bad])
        sink2 = MemorySink()
        m2 = self.mon(path, sink2, clk)                              # process restart
        self.assertEqual([a.check for a in m2.open_alarms()], ["gateway"])
        self.assertEqual(m2.evaluate([bad]), [])
        self.assertEqual(sink2.events, [])                           # no duplicate alert

    def test_failing_sink_is_recorded_and_changes_nothing(self):
        class Boom:
            def notify(self, ev):
                raise ConnectionError("down")
        m = Monitor(state_db(self), sinks=[Boom(), MemorySink()], clock=Clock())
        m.evaluate([CheckResult("gateway", ALARM, CRITICAL, "down")])
        self.assertEqual(len(m.open_alarms()), 1)
        self.assertIn("NOTIFY_FAILED", [e["kind"] for e in m.events()])


class M3_AgainstTheRealLoop(unittest.TestCase):
    def test_restart_with_an_unresolved_order_raises_the_alarm(self):
        from test_m21_1extra_d2_3_learning_loop import LoopHarness
        h = LoopHarness(self)
        real = h.broker.submit_bracket

        def lost_ack(req):
            real(req)
            raise TimeoutError("ack lost")
        with mock.patch.object(h.broker, "submit_bracket", side_effect=lost_ack):
            r = h.tick()
        self.assertEqual(r["entry_block"], "submit_outcome_unknown")
        now = h.clock() + timedelta(minutes=45)                       # still unresolved
        journal = read_journal(h.db)
        sink = MemorySink()
        mon = Monitor(state_db(self), sinks=[sink], clock=lambda: now)
        results = run_checks(journal=journal, now=now, interval=timedelta(minutes=15),
                             market_open=True, gateway_health=dict(GOOD_GW, as_of_utc=now.isoformat()),
                             observations={"AAA": {"15m": "VALID"}}, broker_open_orders=[])
        mon.evaluate(results)
        by = {a.check: a for a in mon.open_alarms()}
        self.assertIn("unresolved_execution", by)
        self.assertIn("pending_submit", by["unresolved_execution"].detail)
        self.assertIn("loop_heartbeat", by)                           # no tick for 45 min
        self.assertEqual(by["unresolved_execution"].severity, CRITICAL)
        self.assertTrue(any(e["check"] == "unresolved_execution" for e in sink.events))

    def test_read_journal_is_read_only(self):
        from test_m21_1extra_d2_3_learning_loop import LoopHarness
        h = LoopHarness(self)
        h.tick()
        before = Path(h.db).read_bytes()
        read_journal(h.db)
        self.assertEqual(Path(h.db).read_bytes(), before)


class M4_Boundaries(unittest.TestCase):
    def test_no_messaging_broker_or_network_imports(self):
        for p in sorted((REPO / "bot" / "monitoring").glob("*.py")):
            tree = ast.parse(p.read_text())
            mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            for m in mods:
                self.assertFalse(any(b in m for b in ("telegram", "notifier", "requests", "socket",
                                                      "urllib", "ib_insync", "brokers",
                                                      "kill_switch", "subprocess")), (p.name, m))


if __name__ == "__main__":
    unittest.main()

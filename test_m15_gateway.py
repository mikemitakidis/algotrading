"""
M15.1 — Gateway Watchdog tests.
Run: python3 test_m15_gateway.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.recovery_executor import (
    RecoveryController, RecoveryExecutor,
    EVENT_RESTART_DISABLED_M15_1, EVENT_RESTART_NOT_IMPLEMENTED_M15_1,
    EVENT_ALERT_ONLY,
)
from bot.gateway_watchdog import (
    GatewayWatchdog, WatchdogConfig, ProbeResult,
    STATE_API_UP_HEALTHY, STATE_TCP_UP_API_DOWN,
    STATE_SERVICE_RUNNING_TCP_DOWN, STATE_SERVICE_DOWN,
)


def _utc():
    return datetime.now(timezone.utc)


# ============================================================
# RecoveryController — eligibility, cooldown, backoff
# ============================================================
class TestRecoveryController(unittest.TestCase):

    def test_alert_only_blocks_all(self):
        c = RecoveryController('alert_only', 30, 2)
        ok, reason = c.is_eligible(STATE_TCP_UP_API_DOWN)
        self.assertFalse(ok)
        self.assertEqual(reason, 'mode_alert_only')

    def test_unknown_mode_blocks(self):
        c = RecoveryController('turbo', 30, 2)
        ok, reason = c.is_eligible(STATE_TCP_UP_API_DOWN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('unknown_mode'))

    def test_systemd_restart_eligible_first_time(self):
        c = RecoveryController('systemd_restart', 30, 2)
        ok, reason = c.is_eligible(STATE_TCP_UP_API_DOWN)
        self.assertTrue(ok)
        self.assertEqual(reason, 'eligible')

    def test_min_interval_blocks(self):
        c = RecoveryController('systemd_restart', 30, 5)
        c.record_attempt()
        ok, reason = c.is_eligible(STATE_TCP_UP_API_DOWN)
        self.assertFalse(ok)
        self.assertIn('cooldown_active', reason)

    def test_max_per_hour_blocks(self):
        c = RecoveryController('systemd_restart', 0, 2)
        c.record_attempt()
        c.record_attempt()
        ok, reason = c.is_eligible(STATE_TCP_UP_API_DOWN)
        self.assertFalse(ok)
        self.assertIn('max_restarts_per_hour', reason)

    def test_stats_returns_summary(self):
        c = RecoveryController('systemd_restart', 30, 2)
        c.record_attempt()
        s = c.stats()
        self.assertEqual(s['mode'], 'systemd_restart')
        self.assertEqual(s['attempts_total'], 1)
        self.assertEqual(s['attempts_last_hour'], 1)


# ============================================================
# RecoveryExecutor — INERT proof
# ============================================================
class TestRecoveryExecutorInert(unittest.TestCase):

    def test_alert_only_returns_alert_only_event(self):
        self.assertEqual(
            RecoveryExecutor('alert_only').execute(STATE_TCP_UP_API_DOWN),
            EVENT_ALERT_ONLY,
        )

    def test_systemd_restart_returns_disabled_marker(self):
        self.assertEqual(
            RecoveryExecutor('systemd_restart').execute(STATE_TCP_UP_API_DOWN),
            EVENT_RESTART_DISABLED_M15_1,
        )

    def test_unknown_mode_returns_not_implemented(self):
        self.assertEqual(
            RecoveryExecutor('unknown').execute(STATE_TCP_UP_API_DOWN),
            EVENT_RESTART_NOT_IMPLEMENTED_M15_1,
        )

    def test_no_restart_call_in_real_code(self):
        """AST-based proof of M15.1 contract: no subprocess.{run,call,Popen,
        check_call,check_output} call anywhere in real code passes a string
        literal 'restart' (and/or 'systemctl' + 'restart' together).

        Why AST and not grep/tokenize-strip-strings:
        - tokenize that strips STRING tokens would mask a real call like
          subprocess.run(['systemctl', 'restart', 'ibgateway'])
          because the dangerous 'restart' lives INSIDE a string literal.
        - AST traversal naturally excludes comments and docstrings (those
          are not children of Call nodes), but DOES inspect string
          constants passed as arguments. That is exactly what we want.
        """
        import ast

        SUBPROCESS_FUNCS = {
            'run', 'call', 'check_call', 'check_output', 'Popen',
        }

        def _is_subprocess_call(node):
            if not isinstance(node, ast.Call):
                return False
            f = node.func
            # subprocess.run(...), subprocess.Popen(...), etc.
            if isinstance(f, ast.Attribute) and f.attr in SUBPROCESS_FUNCS:
                obj = f.value
                if isinstance(obj, ast.Name) and obj.id == 'subprocess':
                    return True
            # Bare run(...) after `from subprocess import run` — flag too
            if isinstance(f, ast.Name) and f.id in SUBPROCESS_FUNCS:
                return True
            return False

        def _string_constants_in(node):
            out = []
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    out.append(child.value)
            return out

        offenders = []
        for fname in ('bot/recovery_executor.py', 'bot/gateway_watchdog.py'):
            path = os.path.join(os.path.dirname(__file__), fname)
            with open(path) as f:
                tree = ast.parse(f.read(), filename=fname)
            for node in ast.walk(tree):
                if not _is_subprocess_call(node):
                    continue
                strs = [s.lower() for s in _string_constants_in(node)]
                # Hard fail: 'restart' literal anywhere in subprocess call args
                if any(s == 'restart' for s in strs):
                    offenders.append(
                        (fname, node.lineno, "subprocess call with 'restart' literal arg")
                    )
                # Belt-and-braces: any string literal containing 'restart'
                # token AND any other arg containing 'systemctl'
                joined = ' '.join(strs)
                if 'restart' in joined and 'systemctl' in joined:
                    offenders.append(
                        (fname, node.lineno, "subprocess call combines systemctl + restart")
                    )
        if offenders:
            self.fail(
                'M15.1 contract violated — restart call(s) found:\n  ' +
                '\n  '.join(f'{f}:{ln} — {msg}' for f, ln, msg in offenders)
            )

    def test_subprocess_only_used_for_is_active_probe(self):
        """Positive assertion: the ONLY subprocess call in the watchdog/
        recovery code is the read-only `systemctl is-active <unit>` probe."""
        import ast
        allowed_combos = [{'systemctl', 'is-active'}]

        for fname in ('bot/recovery_executor.py', 'bot/gateway_watchdog.py'):
            path = os.path.join(os.path.dirname(__file__), fname)
            with open(path) as f:
                tree = ast.parse(f.read(), filename=fname)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {'run', 'call', 'check_call',
                                               'check_output', 'Popen'}):
                    continue
                obj = node.func.value
                if not (isinstance(obj, ast.Name) and obj.id == 'subprocess'):
                    continue
                strs = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        strs.add(child.value)
                # The call's string args must be a subset of an allowed combo
                ok = any(strs.issubset(allowed) or strs == allowed
                         for allowed in allowed_combos) or strs == set()
                # Allow the variable arg `unit` (not a constant) — strs only
                # contains literal constants. The is-active probe has
                # ['systemctl', 'is-active', unit] -> literals = {'systemctl','is-active'}
                if not ok:
                    self.fail(
                        f'{fname}:{node.lineno} subprocess call has unexpected '
                        f'string literals: {strs}'
                    )


# ============================================================
# Probe truth table → derived state
# ============================================================
class TestProbeStateMachine(unittest.TestCase):

    def test_service_down(self):
        p = ProbeResult(_utc(), service_running=False, tcp_ok=False, api_ok=False)
        self.assertEqual(p.derive_state(), STATE_SERVICE_DOWN)

    def test_service_running_tcp_down(self):
        p = ProbeResult(_utc(), service_running=True, tcp_ok=False, api_ok=False)
        self.assertEqual(p.derive_state(), STATE_SERVICE_RUNNING_TCP_DOWN)

    def test_tcp_up_api_down(self):
        p = ProbeResult(_utc(), service_running=True, tcp_ok=True, api_ok=False)
        self.assertEqual(p.derive_state(), STATE_TCP_UP_API_DOWN)

    def test_api_up_healthy(self):
        p = ProbeResult(_utc(), service_running=True, tcp_ok=True, api_ok=True)
        self.assertEqual(p.derive_state(), STATE_API_UP_HEALTHY)

    def test_service_unknown_treated_as_running(self):
        # service_running=None means systemctl unreadable; we don't fail-block.
        p = ProbeResult(_utc(), service_running=None, tcp_ok=True, api_ok=True)
        self.assertEqual(p.derive_state(), STATE_API_UP_HEALTHY)


# ============================================================
# Watchdog hysteresis + transitions + alert dedup
# ============================================================
class _FakeFlywheel:
    def __init__(self):
        self.events = []
        self.states = []

    def write_gateway_event(self, event_type, broker_mode,
                            status_before, status_after, details):
        self.events.append({
            'event_type': event_type, 'broker_mode': broker_mode,
            'status_before': status_before, 'status_after': status_after,
            'details': details,
        })
        return len(self.events)

    def write_gateway_state(self, state):
        self.states.append(state)


def _make_watchdog(mode='alert_only', failures_to_down=2,
                   alert_cooldown_min=15, manual_action_after_min=5):
    cfg = WatchdogConfig(
        enabled=True, mode=mode, interval_sec=999,
        failures_to_down=failures_to_down,
        alert_cooldown_min=alert_cooldown_min,
        manual_action_after_min=manual_action_after_min,
        host='127.0.0.1', port=4002, broker_mode='paper',
    )
    fw = _FakeFlywheel()
    sent = []
    wd = GatewayWatchdog(
        cfg, fw,
        notifier_send_fn=lambda sev, txt, pl: sent.append((sev, txt, pl)),
    )
    return wd, fw, sent


def _force_probe(wd, *, service_running=True, tcp_ok=True, api_ok=True):
    with patch('bot.gateway_watchdog.systemd_probe', return_value=service_running), \
         patch('bot.gateway_watchdog.tcp_probe', return_value=tcp_ok), \
         patch('bot.gateway_watchdog.api_probe',
               return_value=(api_ok, 12 if api_ok else None,
                             None if api_ok else 'TimeoutError')):
        wd._tick()


class TestWatchdogHysteresis(unittest.TestCase):

    def test_single_failure_does_not_transition(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=True)
        _force_probe(wd, api_ok=True)
        self.assertEqual(wd.current_state()['state'], STATE_API_UP_HEALTHY)
        _force_probe(wd, api_ok=False)
        # 1 failure < failures_to_down=2 → still healthy
        self.assertEqual(wd.current_state()['state'], STATE_API_UP_HEALTHY)

    def test_two_failures_transition_to_api_down(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=True)
        _force_probe(wd, api_ok=False)
        _force_probe(wd, api_ok=False)
        self.assertEqual(wd.current_state()['state'], STATE_TCP_UP_API_DOWN)

    def test_one_success_recovers(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=False)
        _force_probe(wd, api_ok=False)
        self.assertEqual(wd.current_state()['state'], STATE_TCP_UP_API_DOWN)
        _force_probe(wd, api_ok=True)
        self.assertEqual(wd.current_state()['state'], STATE_API_UP_HEALTHY)

    def test_service_down_classification(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, service_running=False)
        _force_probe(wd, service_running=False)
        self.assertEqual(wd.current_state()['state'], STATE_SERVICE_DOWN)

    def test_state_persisted_every_tick(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=True)
        _force_probe(wd, api_ok=True)
        self.assertEqual(len(fw.states), 2)

    def test_event_logged_only_on_transition(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=True)  # UNKNOWN → healthy = transition
        n0 = len(fw.events)
        _force_probe(wd, api_ok=True)  # healthy → healthy = no transition
        self.assertEqual(len(fw.events), n0)

    def test_alert_dedup_within_cooldown(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=True)  # establish healthy
        sent.clear()
        _force_probe(wd, api_ok=False)
        _force_probe(wd, api_ok=False)  # transition → 1 alert
        n_after_first = len(sent)
        # Stay in failure state — no new transition, no new alert
        _force_probe(wd, api_ok=False)
        self.assertEqual(len(sent), n_after_first)

    def test_recovery_alert_always_sends(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=False)
        _force_probe(wd, api_ok=False)
        sent.clear()
        _force_probe(wd, api_ok=True)
        self.assertGreaterEqual(len(sent), 1)
        self.assertEqual(sent[-1][0], 'info')

    def test_health_payload_required_fields(self):
        wd, fw, sent = _make_watchdog()
        _force_probe(wd, api_ok=True)
        p = wd.gateway_health_payload()
        for k in ('service_running', 'tcp_ok', 'api_ok',
                  'last_success_ts', 'failure_count', 'watchdog_status'):
            self.assertIn(k, p, f'gateway_health_payload missing {k}')

    def test_is_healthy_for_submission_only_when_api_up(self):
        wd, fw, sent = _make_watchdog()
        self.assertFalse(wd.is_healthy_for_submission())  # UNKNOWN
        _force_probe(wd, api_ok=True)
        self.assertTrue(wd.is_healthy_for_submission())
        _force_probe(wd, api_ok=False)
        _force_probe(wd, api_ok=False)
        self.assertFalse(wd.is_healthy_for_submission())


# ============================================================
# Watchdog → recovery_executor wiring (inert in M15.1)
# ============================================================
class TestWatchdogRecoveryWiring(unittest.TestCase):

    def test_systemd_restart_mode_logs_disabled_marker(self):
        cfg = WatchdogConfig(
            enabled=True, mode='systemd_restart',
            failures_to_down=2, interval_sec=999,
            min_restart_interval_min=0, max_restarts_per_hour=10,
        )
        fw = _FakeFlywheel()
        ctrl = RecoveryController('systemd_restart', 0, 10)
        execu = RecoveryExecutor('systemd_restart')
        wd = GatewayWatchdog(cfg, fw,
                             recovery_controller=ctrl, recovery_executor=execu)
        with patch('bot.gateway_watchdog.systemd_probe', return_value=True), \
             patch('bot.gateway_watchdog.tcp_probe', return_value=True), \
             patch('bot.gateway_watchdog.api_probe',
                   return_value=(False, None, 'Timeout')):
            wd._tick()  # 1st failure (no transition yet)
            wd._tick()  # 2nd failure → transition + recovery decision
        types = [e['event_type'] for e in fw.events]
        self.assertIn(
            EVENT_RESTART_DISABLED_M15_1, types,
            'Watchdog must log restart_eligible_but_disabled_m15_1',
        )
        self.assertNotIn(
            'restart_executed', types,
            'M15.1 contract violated: a restart_executed event was logged',
        )

    def test_alert_only_mode_logs_alert_only_event(self):
        cfg = WatchdogConfig(
            enabled=True, mode='alert_only',
            failures_to_down=2, interval_sec=999,
        )
        fw = _FakeFlywheel()
        ctrl = RecoveryController('alert_only', 30, 2)
        execu = RecoveryExecutor('alert_only')
        wd = GatewayWatchdog(cfg, fw,
                             recovery_controller=ctrl, recovery_executor=execu)
        with patch('bot.gateway_watchdog.systemd_probe', return_value=True), \
             patch('bot.gateway_watchdog.tcp_probe', return_value=True), \
             patch('bot.gateway_watchdog.api_probe',
                   return_value=(False, None, 'Timeout')):
            wd._tick()
            wd._tick()
        types = [e['event_type'] for e in fw.events]
        # alert_only mode → controller blocks → recovery_skipped is logged
        self.assertIn('recovery_skipped', types)


# ============================================================
# Flywheel gateway helpers
# ============================================================
class TestFlywheelGatewayHelpers(unittest.TestCase):

    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix='.db'); f.close()
        self.db = f.name
        from bot.flywheel import init_flywheel_tables
        c = sqlite3.connect(self.db)
        init_flywheel_tables(c)
        c.close()

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def test_write_and_read_gateway_state(self):
        from bot.flywheel import write_gateway_state, read_gateway_state
        write_gateway_state({'state': 'api_up_healthy', 'tcp_ok': True}, db_path=self.db)
        s = read_gateway_state(db_path=self.db)
        self.assertEqual(s.get('state'), 'api_up_healthy')
        self.assertEqual(s.get('tcp_ok'), True)
        self.assertIn('_persisted_at', s)

    def test_state_upsert_overwrites(self):
        from bot.flywheel import write_gateway_state, read_gateway_state
        write_gateway_state({'state': 'service_down'}, db_path=self.db)
        write_gateway_state({'state': 'api_up_healthy'}, db_path=self.db)
        s = read_gateway_state(db_path=self.db)
        self.assertEqual(s.get('state'), 'api_up_healthy')

    def test_write_and_read_gateway_events(self):
        from bot.flywheel import write_gateway_event, read_gateway_events
        write_gateway_event('state_transition', 'paper',
                            status_before='unknown', status_after='api_up_healthy',
                            details={'foo': 'bar'}, db_path=self.db)
        write_gateway_event('state_transition', 'paper',
                            status_before='api_up_healthy', status_after='tcp_up_api_down',
                            db_path=self.db)
        events = read_gateway_events(limit=10, db_path=self.db)
        self.assertEqual(len(events), 2)
        # newest first
        self.assertEqual(events[0]['status_after'], 'tcp_up_api_down')
        self.assertEqual(events[1]['details'].get('foo'), 'bar')

    def test_gateway_events_table_has_expected_indexes(self):
        c = sqlite3.connect(self.db)
        idx = c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gateway_events'"
        ).fetchall()
        c.close()
        names = {r[0] for r in idx}
        self.assertIn('idx_gateway_events_ts', names)
        self.assertIn('idx_gateway_events_event_type', names)
        self.assertIn('idx_gateway_events_broker_mode', names)
        # M14 lesson: signal_id/symbol indexes MUST NOT exist
        self.assertNotIn('idx_gateway_events_signal_id', names)
        self.assertNotIn('idx_gateway_events_symbol', names)


# ============================================================
# Controlled status: broker_unready is documented in INTENT_SCHEMA
# ============================================================
class TestBrokerUnreadyStatusDocumented(unittest.TestCase):

    def test_intent_schema_lists_broker_unready(self):
        from bot.flywheel import INTENT_SCHEMA
        self.assertIn('broker_unready', INTENT_SCHEMA,
                      'broker_unready must be documented in INTENT_SCHEMA comment')


if __name__ == '__main__':
    unittest.main(verbosity=2)

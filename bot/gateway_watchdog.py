"""
M15.1 — IB Gateway Watchdog.

Single in-process timed prober. Detects, classifies, alerts on Gateway health.
Does NOT execute restarts in M15.1 (Option B; recovery_executor is inert).

Probes (per tick, on a daemon background thread):
  1. systemctl is-active <unit>     -> service_running (None if unreadable)
  2. TCP socket connect to host:port -> tcp_ok
  3. Independent ib_insync IB() with reserved clientId,
     connect -> reqCurrentTime -> disconnect, hard timeout -> api_ok

State machine (concrete, derived from probe truth table):
  not service_running           -> service_down
  service_running, !tcp_ok      -> service_running_tcp_down
  service_running, tcp, !api    -> tcp_up_api_down
  service_running, tcp, api     -> api_up_healthy

Hysteresis:
  - N consecutive failures (default 2) required to leave api_up_healthy.
  - 1 successful probe is enough to declare recovery (asymmetric on purpose).

Derived flags:
  - degraded               = rolling failure rate > threshold over window
  - manual_action_required = stuck in tcp_up_api_down for > N min (2FA signature)

Notes:
  - TCP probe is duplicated from bot/brokers/ibkr_broker.py::_gateway_available.
    Extraction was deferred to avoid changing M12 broker code; a future M15.x
    cleanup may centralise it.
  - The probe uses a SEPARATE ib_insync client (reserved clientId=99) so a
    wedged trading client does not contaminate the verdict.
  - systemd config (Restart=always, RestartSec=30, max 3/300s,
    AutoRestartTime=23:59) is intentionally LEFT UNCHANGED in M15.1.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional, Tuple

log = logging.getLogger(__name__)

# Controlled state strings. Used in dashboard, gateway_events, alerts.
STATE_SERVICE_DOWN = 'service_down'
STATE_SERVICE_RUNNING_TCP_DOWN = 'service_running_tcp_down'
STATE_TCP_UP_API_DOWN = 'tcp_up_api_down'
STATE_API_UP_HEALTHY = 'api_up_healthy'
STATE_UNKNOWN = 'unknown'

CONCRETE_STATES = {
    STATE_SERVICE_DOWN, STATE_SERVICE_RUNNING_TCP_DOWN,
    STATE_TCP_UP_API_DOWN, STATE_API_UP_HEALTHY, STATE_UNKNOWN,
}

WATCHDOG_CLIENT_ID = int(os.getenv('GATEWAY_WATCHDOG_CLIENT_ID', '99'))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec='seconds') if dt else None


def _bool_env(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class WatchdogConfig:
    enabled: bool = True
    mode: str = 'alert_only'
    interval_sec: int = 60
    api_timeout_sec: int = 5
    alert_cooldown_min: int = 15
    min_restart_interval_min: int = 30
    max_restarts_per_hour: int = 2
    failures_to_down: int = 2
    degraded_window_min: int = 10
    degraded_fail_rate: float = 0.30
    manual_action_after_min: int = 5
    host: str = '127.0.0.1'
    port: int = 4002
    broker_mode: str = 'paper'
    systemd_unit: str = 'ibgateway'

    @classmethod
    def from_env(cls, broker_mode: str, host: str, port: int,
                 systemd_unit: str = 'ibgateway') -> 'WatchdogConfig':
        return cls(
            enabled=_bool_env('GATEWAY_WATCHDOG_ENABLED', True),
            mode=os.getenv('GATEWAY_WATCHDOG_MODE', 'alert_only').strip(),
            interval_sec=_int_env('GATEWAY_WATCHDOG_INTERVAL_SEC', 60),
            api_timeout_sec=_int_env('GATEWAY_WATCHDOG_API_TIMEOUT_SEC', 5),
            alert_cooldown_min=_int_env('GATEWAY_WATCHDOG_ALERT_COOLDOWN_MIN', 15),
            min_restart_interval_min=_int_env(
                'GATEWAY_WATCHDOG_MIN_RESTART_INTERVAL_MIN', 30
            ),
            max_restarts_per_hour=_int_env('GATEWAY_WATCHDOG_MAX_RESTARTS_PER_HOUR', 2),
            failures_to_down=_int_env('GATEWAY_WATCHDOG_FAILURES_TO_DOWN', 2),
            degraded_window_min=_int_env('GATEWAY_WATCHDOG_DEGRADED_WINDOW_MIN', 10),
            degraded_fail_rate=_float_env('GATEWAY_WATCHDOG_DEGRADED_FAIL_RATE', 0.30),
            manual_action_after_min=_int_env(
                'GATEWAY_WATCHDOG_MANUAL_ACTION_AFTER_MIN', 5
            ),
            host=host, port=port,
            broker_mode=broker_mode, systemd_unit=systemd_unit,
        )


@dataclass
class ProbeResult:
    ts: datetime
    service_running: Optional[bool]
    tcp_ok: bool
    api_ok: bool
    api_latency_ms: Optional[int] = None
    error: Optional[str] = None

    def derive_state(self) -> str:
        if self.service_running is False:
            return STATE_SERVICE_DOWN
        if not self.tcp_ok:
            return STATE_SERVICE_RUNNING_TCP_DOWN
        if not self.api_ok:
            return STATE_TCP_UP_API_DOWN
        return STATE_API_UP_HEALTHY


# --- TCP probe (mirrors bot/brokers/ibkr_broker.py::_gateway_available) ------
def tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- systemctl probe (READ-ONLY: is-active, never restart) -------------------
def systemd_probe(unit: str) -> Optional[bool]:
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=3,
        )
        out = r.stdout.strip()
        if out == 'active':
            return True
        if out in ('inactive', 'failed', 'deactivating', 'activating'):
            return False
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


# --- API probe (independent IB client; never reuses trading client) ----------
def api_probe(host: str, port: int, client_id: int,
              timeout_sec: int) -> Tuple[bool, Optional[int], Optional[str]]:
    try:
        from ib_insync import IB
    except ImportError:
        return False, None, 'ib_insync_not_installed'
    ib = IB()
    t0 = time.monotonic()
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout_sec, readonly=True)
        ib.reqCurrentTime()
        latency_ms = int((time.monotonic() - t0) * 1000)
        return True, latency_ms, None
    except Exception as e:
        return False, None, f'{type(e).__name__}: {e}'[:200]
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


class GatewayWatchdog:
    """Background-threaded prober. One instance per process."""

    def __init__(self, config: WatchdogConfig, flywheel,
                 notifier_send_fn: Optional[Callable[[str, str, dict], None]] = None,
                 recovery_controller=None, recovery_executor=None):
        self.config = config
        self.flywheel = flywheel
        self.notifier_send_fn = notifier_send_fn
        self.recovery_controller = recovery_controller
        self.recovery_executor = recovery_executor

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._declared_state = STATE_UNKNOWN
        self._last_probe: Optional[ProbeResult] = None
        self._last_success_ts: Optional[datetime] = None
        self._last_failure_ts: Optional[datetime] = None
        self._stuck_in_api_down_since: Optional[datetime] = None
        self._roll: deque = deque(maxlen=512)

        self._last_alert_state: Optional[str] = None
        self._last_alert_ts: Optional[datetime] = None
        self._alerts_suppressed: int = 0

    # ---------- public API ----------
    def start(self) -> None:
        if not self.config.enabled:
            log.info('[GW-WATCHDOG] disabled by config; not starting.')
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='gw-watchdog', daemon=True,
        )
        self._thread.start()
        log.info(
            '[GW-WATCHDOG] started: mode=%s interval=%ds host=%s port=%d broker=%s',
            self.config.mode, self.config.interval_sec,
            self.config.host, self.config.port, self.config.broker_mode,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def current_state(self) -> dict:
        with self._lock:
            p = self._last_probe
            stuck_min = (
                int((_utc_now() - self._stuck_in_api_down_since).total_seconds() / 60)
                if self._stuck_in_api_down_since else 0
            )
            return {
                'state': self._declared_state,
                'service_running': p.service_running if p else None,
                'tcp_ok': p.tcp_ok if p else None,
                'api_ok': p.api_ok if p else None,
                'api_latency_ms': p.api_latency_ms if p else None,
                'last_success_ts': _utc_iso(self._last_success_ts),
                'last_probe_ts': _utc_iso(p.ts) if p else None,
                'probe_age_seconds': (
                    int((_utc_now() - p.ts).total_seconds()) if p else None
                ),
                'failure_count': self._consecutive_failures,
                'watchdog_status': self._declared_state,
                'degraded': self._is_degraded(),
                'manual_action_required': self._is_manual_action_required(),
                'stuck_in_api_down_min': stuck_min,
                'broker_mode': self.config.broker_mode,
                'mode': self.config.mode,
            }

    def is_healthy_for_submission(self) -> bool:
        with self._lock:
            return self._declared_state == STATE_API_UP_HEALTHY

    def gateway_health_payload(self) -> dict:
        """Full health payload for risk_checks JSON on a broker_unready row.

        Returns the complete watchdog snapshot (same as current_state()):
          - REQUIRED 6 fields:    service_running, tcp_ok, api_ok,
                                  last_success_ts, failure_count, watchdog_status
          - Plus richer context:  state, last_probe_ts, probe_age_seconds,
                                  api_latency_ms, degraded,
                                  manual_action_required, broker_mode, mode,
                                  stuck_in_api_down_min
        Returning the full state improves audit fidelity for blocked
        intents — preferred over a compact subset.
        """
        return self.current_state()

    # ---------- internal ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception('[GW-WATCHDOG] tick error')
            self._stop.wait(self.config.interval_sec)

    def _tick(self) -> None:
        ts = _utc_now()
        service_running = systemd_probe(self.config.systemd_unit)
        if service_running is False:
            tcp_ok, api_ok, api_lat, err = False, False, None, 'service_inactive'
        else:
            tcp_ok = tcp_probe(self.config.host, self.config.port)
            if tcp_ok:
                api_ok, api_lat, err = api_probe(
                    self.config.host, self.config.port,
                    WATCHDOG_CLIENT_ID, self.config.api_timeout_sec,
                )
            else:
                api_ok, api_lat, err = False, None, 'tcp_unreachable'

        probe = ProbeResult(
            ts=ts, service_running=service_running,
            tcp_ok=tcp_ok, api_ok=api_ok,
            api_latency_ms=api_lat, error=err,
        )
        new_concrete = probe.derive_state()
        success = (new_concrete == STATE_API_UP_HEALTHY)

        with self._lock:
            prev = self._declared_state
            self._last_probe = probe
            self._roll.append((ts, success))

            if success:
                self._consecutive_failures = 0
                self._consecutive_successes += 1
                self._last_success_ts = ts
                self._declared_state = STATE_API_UP_HEALTHY
                self._stuck_in_api_down_since = None
            else:
                self._consecutive_failures += 1
                self._consecutive_successes = 0
                self._last_failure_ts = ts
                if self._consecutive_failures >= self.config.failures_to_down:
                    self._declared_state = new_concrete
                # else: hold previous declared state (hysteresis)
                if new_concrete == STATE_TCP_UP_API_DOWN:
                    if self._stuck_in_api_down_since is None:
                        self._stuck_in_api_down_since = ts
                else:
                    self._stuck_in_api_down_since = None

            declared = self._declared_state
            transitioned = (declared != prev)

        # Persist state every tick (dashboard freshness)
        try:
            self.flywheel.write_gateway_state(self.current_state())
        except Exception:
            log.exception('[GW-WATCHDOG] write_gateway_state failed')

        # Log transition
        if transitioned:
            try:
                self.flywheel.write_gateway_event(
                    event_type='state_transition',
                    broker_mode=self.config.broker_mode,
                    status_before=prev, status_after=declared,
                    details=self.current_state(),
                )
            except Exception:
                log.exception('[GW-WATCHDOG] write_gateway_event failed')

        # Recovery decision (inert in M15.1)
        if transitioned and declared != STATE_API_UP_HEALTHY and self.recovery_controller:
            self._consider_recovery(declared)

        # Alert
        if transitioned:
            self._maybe_alert(prev, declared)

    def _consider_recovery(self, state: str) -> None:
        eligible, reason = self.recovery_controller.is_eligible(state)
        if not eligible:
            try:
                self.flywheel.write_gateway_event(
                    event_type='recovery_skipped',
                    broker_mode=self.config.broker_mode,
                    status_before=state, status_after=state,
                    details={'reason': reason, 'mode': self.config.mode},
                )
            except Exception:
                log.exception('[GW-WATCHDOG] recovery_skipped event write failed')
            return
        try:
            event_type = self.recovery_executor.execute(state)
            self.flywheel.write_gateway_event(
                event_type=event_type,
                broker_mode=self.config.broker_mode,
                status_before=state, status_after=state,
                details={'mode': self.config.mode, 'executor': 'M15.1_inert'},
            )
            self.recovery_controller.record_attempt()
        except Exception:
            log.exception('[GW-WATCHDOG] recovery_executor.execute failed')

    def _maybe_alert(self, prev: str, new: str) -> None:
        if not self.notifier_send_fn:
            return
        now = _utc_now()
        is_recovery = (new == STATE_API_UP_HEALTHY and prev != STATE_API_UP_HEALTHY)
        is_escalation = self._is_manual_action_required()

        if is_recovery or is_escalation:
            self._send(prev, new, 'info' if is_recovery else 'critical')
            return

        within_cd = (
            self._last_alert_ts is not None and
            (now - self._last_alert_ts) <
            timedelta(minutes=self.config.alert_cooldown_min)
        )
        if self._last_alert_state == new and within_cd:
            self._alerts_suppressed += 1
            return

        if self._alerts_suppressed >= 20:
            text = (
                f'\u26a0\ufe0f {self._alerts_suppressed + 1} gateway transitions in '
                f'{self.config.alert_cooldown_min} min — see dashboard'
            )
            self._send(prev, new, 'warning', text)
            self._alerts_suppressed = 0
            return

        self._send(prev, new, 'warning')

    def _send(self, prev: str, new: str, severity: str,
              override_text: Optional[str] = None) -> None:
        try:
            text = override_text or self._format(prev, new, severity)
            self.notifier_send_fn(severity, text, self.current_state())
            self._last_alert_state = new
            self._last_alert_ts = _utc_now()
            if not override_text:
                self._alerts_suppressed = 0
        except Exception:
            log.exception('[GW-WATCHDOG] notifier_send_fn failed')

    def _format(self, prev: str, new: str, severity: str) -> str:
        emo = {'info': '\u2705', 'warning': '\u26a0\ufe0f',
               'critical': '\U0001F6A8'}.get(severity, '\u2139\ufe0f')
        s = self.current_state()
        lines = [
            f'{emo} <b>Gateway:</b> {prev} \u2192 {new}',
            f'broker={s["broker_mode"]} mode={s["mode"]}',
            f'service={s["service_running"]} tcp={s["tcp_ok"]} api={s["api_ok"]}',
        ]
        if s['last_success_ts']:
            lines.append(f'last_success={s["last_success_ts"]}')
        if s['manual_action_required']:
            lines.append('\u26a0\ufe0f manual action likely required (2FA / re-login)')
        return '\n'.join(lines)

    def _is_degraded(self) -> bool:
        cutoff = _utc_now() - timedelta(minutes=self.config.degraded_window_min)
        recent = [s for ts, s in self._roll if ts >= cutoff]
        if not recent:
            return False
        return (
            sum(1 for s in recent if not s) / len(recent)
        ) >= self.config.degraded_fail_rate

    def _is_manual_action_required(self) -> bool:
        if not self._stuck_in_api_down_since:
            return False
        stuck_min = (_utc_now() - self._stuck_in_api_down_since).total_seconds() / 60
        return stuck_min >= self.config.manual_action_after_min

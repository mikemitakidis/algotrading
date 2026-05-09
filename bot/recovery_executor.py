"""
M15.1 — Recovery Controller + Executor.

CONTRACT (Option B, locked):
- mode=alert_only is the runtime default.
- mode=systemd_restart is RECOGNISED but INERT in M15.1.
- Eligibility, cooldown, and backoff logic is fully implemented and tested.
- execute() NEVER calls subprocess in M15.1. M15.2 is the only place that
  may add the `subprocess.run(["systemctl", "restart", ...])` call, and it
  must add it inside the marked block in `_perform_restart()`.

Why split from the watchdog:
- The watchdog OWNS health detection.
- The recovery controller OWNS the policy (eligibility / cooldown / backoff).
- The executor OWNS the action (log-only in M15.1; real action in M15.2).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Tuple


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Event-type strings written to gateway_events when the executor runs.
# These are CONTROLLED — do not invent new ones without review.
EVENT_RESTART_DISABLED_M15_1 = 'restart_eligible_but_disabled_m15_1'
EVENT_RESTART_NOT_IMPLEMENTED_M15_1 = 'restart_not_implemented_m15_1'
EVENT_ALERT_ONLY = 'alert_only_mode_no_action'


class RecoveryController:
    """
    Decides whether a restart attempt is eligible, given:
    - mode (alert_only blocks all attempts)
    - min interval since last attempt
    - max attempts per rolling hour
    """

    def __init__(self, mode: str, min_restart_interval_min: int,
                 max_restarts_per_hour: int):
        self.mode = mode
        self.min_restart_interval_min = int(min_restart_interval_min)
        self.max_restarts_per_hour = int(max_restarts_per_hour)
        self._lock = threading.RLock()
        self._attempts: deque = deque(maxlen=64)

    def is_eligible(self, state: str) -> Tuple[bool, str]:
        """Return (eligible, reason_code). Reason is informational/audit."""
        with self._lock:
            if self.mode == 'alert_only':
                return False, 'mode_alert_only'
            if self.mode != 'systemd_restart':
                return False, f'unknown_mode:{self.mode}'
            now = _utc_now()
            # Min interval guard
            if self._attempts:
                since_last = (now - self._attempts[-1]).total_seconds() / 60
                if since_last < self.min_restart_interval_min:
                    return False, (
                        f'cooldown_active_{int(since_last)}min_of_'
                        f'{self.min_restart_interval_min}'
                    )
            # Rolling-hour rate guard
            cutoff = now - timedelta(hours=1)
            recent = [t for t in self._attempts if t >= cutoff]
            if len(recent) >= self.max_restarts_per_hour:
                return False, (
                    f'max_restarts_per_hour_{len(recent)}_of_'
                    f'{self.max_restarts_per_hour}'
                )
            return True, 'eligible'

    def record_attempt(self) -> None:
        with self._lock:
            self._attempts.append(_utc_now())

    def stats(self) -> dict:
        with self._lock:
            now = _utc_now()
            cutoff = now - timedelta(hours=1)
            return {
                'mode': self.mode,
                'attempts_total': len(self._attempts),
                'attempts_last_hour': sum(1 for t in self._attempts if t >= cutoff),
                'last_attempt_ts': (
                    self._attempts[-1].isoformat(timespec='seconds')
                    if self._attempts else None
                ),
                'min_restart_interval_min': self.min_restart_interval_min,
                'max_restarts_per_hour': self.max_restarts_per_hour,
            }


class RecoveryExecutor:
    """
    Executes (or, in M15.1, does NOT execute) a recovery action.

    M15.1 contract:
      - When called, returns one of the EVENT_* constants.
      - Does NOT import subprocess.
      - Does NOT shell out.
      - Does NOT touch systemd.
    """

    def __init__(self, mode: str, systemd_unit: str = 'ibgateway'):
        self.mode = mode
        self.systemd_unit = systemd_unit

    def execute(self, state: str) -> str:
        """
        Returns the event_type to log in gateway_events.
        NEVER calls subprocess in M15.1.
        """
        if self.mode == 'alert_only':
            return EVENT_ALERT_ONLY
        if self.mode == 'systemd_restart':
            # M15.1: explicitly inert.
            # M15.2 will replace this return with a guarded subprocess call.
            return self._perform_restart(dry_run=True)
        return EVENT_RESTART_NOT_IMPLEMENTED_M15_1

    def _perform_restart(self, dry_run: bool) -> str:
        # ====================================================================
        # M15.2 IMPLEMENTATION POINT — DO NOT MODIFY IN M15.1
        # ====================================================================
        # In M15.2, when ready to enable real restart, replace the body of
        # this function with:
        #     import subprocess
        #     subprocess.run(
        #         ['systemctl', 'restart', self.systemd_unit],
        #         check=True, timeout=30,
        #     )
        #     return 'restart_executed'
        #
        # M15.1 MUST keep this function purely declarative (no side effects).
        # ====================================================================
        if dry_run:
            return EVENT_RESTART_DISABLED_M15_1
        return EVENT_RESTART_NOT_IMPLEMENTED_M15_1

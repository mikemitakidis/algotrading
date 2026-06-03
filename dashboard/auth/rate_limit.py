"""Login rate-limiter — in-memory sliding-window.

Approved per Q-A.1 (Option A: in-memory).

Threat model: brute-force against /api/login. Acceptable trade-off
that lockout state resets on dashboard restart because:
  * The auth_events audit log still records every attempt regardless.
  * bcrypt-verify is ~250ms (cost factor 12), itself rate-limiting at
    scale: an attacker doing 1000 wrong-password guesses per IP from
    multiple sources takes minutes of CPU per attempt.
  * In a single-operator system, the operator notices a lockout
    quickly and can investigate auth_events.

Default policy (configurable via env):
  * Window: 10 minutes (LOGIN_LOCKOUT_WINDOW_SEC)
  * Threshold: 5 failures (LOGIN_LOCKOUT_THRESHOLD)
  * Lockout duration: 15 minutes (LOGIN_LOCKOUT_DURATION_SEC)

Key shape: client_ip (raw, per Q-A.2). The RateLimiter takes a key
parameter so tests can inject mock IPs.

NOT thread-safe by default — Flask under typical WSGI is single-
threaded per worker process. If multiple workers/threads are used,
each maintains independent state (slightly weaker; documented as a
known limitation in the runbook). For the M15.3.A scope of a single
gunicorn/Flask process this is sufficient.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple


# Default policy parameters. Read at module-load time so tests can
# patch via env between test cases (each test instantiates its own
# RateLimiter with explicit args anyway).
def _read_int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, str(default)).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


DEFAULT_WINDOW_SEC    = _read_int_env("LOGIN_LOCKOUT_WINDOW_SEC", 600)
DEFAULT_THRESHOLD     = _read_int_env("LOGIN_LOCKOUT_THRESHOLD", 5)
DEFAULT_LOCKOUT_SEC   = _read_int_env("LOGIN_LOCKOUT_DURATION_SEC", 900)


class LoginRateLimited(Exception):
    """Raised by RateLimiter.check_locked() when the key is currently
    locked out. Carries the remaining seconds for the response body."""
    def __init__(self, retry_after_sec: int):
        super().__init__(
            f"login_rate_limited:retry_after_sec={retry_after_sec}"
        )
        self.retry_after_sec = retry_after_sec


class RateLimiter:
    """Sliding-window failure counter per key (typically client_ip).

    State per key:
      * failures: deque[float] of failure-timestamps within the window
      * locked_until: float | None — unix timestamp the lockout expires

    Methods:
      * check_locked(key) -> None | raises LoginRateLimited
          Call before bcrypt-verify to short-circuit attackers.
      * record_failure(key) -> None
          Append a failure timestamp; trigger lockout if threshold
          reached within the window.
      * record_success(key) -> None
          Reset failure counter and any active lockout for that key.
      * clear(key) -> None
          Admin/test escape — reset a key entirely.

    Returns None for all mutator methods (state is internal).
    """

    def __init__(
        self,
        *,
        window_sec: int = DEFAULT_WINDOW_SEC,
        threshold: int = DEFAULT_THRESHOLD,
        lockout_sec: int = DEFAULT_LOCKOUT_SEC,
        clock=time.time,
    ):
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if window_sec < 1:
            raise ValueError("window_sec must be >= 1")
        if lockout_sec < 1:
            raise ValueError("lockout_sec must be >= 1")
        self._window_sec    = window_sec
        self._threshold     = threshold
        self._lockout_sec   = lockout_sec
        self._clock         = clock
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._locked_until: Dict[str, float]    = {}

    # ── Public API ──────────────────────────────────────────────────

    def check_locked(self, key: str) -> None:
        """Raise LoginRateLimited if the key is currently locked out."""
        if not isinstance(key, str) or not key:
            # Defensive: empty / non-string keys treated as not locked
            # (the caller should always provide a real IP; if it can't,
            # we don't want to deny everything).
            return
        now = self._clock()
        locked_until = self._locked_until.get(key)
        if locked_until is not None:
            if now >= locked_until:
                # Lockout expired — clean up.
                del self._locked_until[key]
                self._failures[key].clear()
            else:
                retry = int(locked_until - now) + 1
                raise LoginRateLimited(retry_after_sec=retry)

    def record_failure(self, key: str) -> None:
        """Record a failure. May trigger lockout if threshold reached."""
        if not isinstance(key, str) or not key:
            return
        now = self._clock()
        q = self._failures[key]
        # Drop failures outside the sliding window.
        cutoff = now - self._window_sec
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)
        if len(q) >= self._threshold:
            self._locked_until[key] = now + self._lockout_sec
            # Clear the deque so a subsequent lockout-expiry test
            # doesn't immediately re-trigger.
            q.clear()

    def record_success(self, key: str) -> None:
        """Successful login — clear all state for this key."""
        if not isinstance(key, str) or not key:
            return
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def clear(self, key: str) -> None:
        """Admin/test escape. Same effect as record_success."""
        self.record_success(key)

    # ── Inspection (tests + auth_events extras) ─────────────────────

    def failure_count(self, key: str) -> int:
        """Failures-in-current-window. Drops stale entries."""
        if not isinstance(key, str) or not key:
            return 0
        now = self._clock()
        q = self._failures.get(key, deque())
        cutoff = now - self._window_sec
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def locked_until(self, key: str) -> Optional[float]:
        """Returns the lockout-expiry unix-ts, or None if not locked."""
        return self._locked_until.get(key)

    def policy(self) -> Dict[str, int]:
        """Returns the active policy parameters — for audit extras."""
        return {
            "window_sec":  self._window_sec,
            "threshold":   self._threshold,
            "lockout_sec": self._lockout_sec,
        }


# Module-level singleton used by dashboard/app.py. Tests instantiate
# their own RateLimiter with explicit args; the singleton is only for
# the running Flask process.
_default_limiter: Optional[RateLimiter] = None


def get_default_limiter() -> RateLimiter:
    global _default_limiter
    if _default_limiter is None:
        _default_limiter = RateLimiter()
    return _default_limiter


def reset_default_limiter() -> None:
    """Test helper — drop the module-level singleton so the next
    get_default_limiter() reads fresh env values."""
    global _default_limiter
    _default_limiter = None

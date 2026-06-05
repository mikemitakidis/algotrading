"""bot/runtime_policy.py — TTL-cached M13.4A policy check at submit
time (audit P0-3, 2026-06-05).

Background
──────────
Before P0-3, `bot.brokers.__init__.get_broker()` consulted the
M13.4A broker-allocation policy ONCE, at scanner startup. The
SignalOnlyBroker wrap (if applied) was then frozen for the process
lifetime. Operator dashboard toggles of the global kill_switch or
per-broker kill_switch took effect only after a scanner restart.

P0-3 makes the runtime check per-submit, with a TTL cache to avoid
a DB read on every single signal in the scanner hot path. Default
TTL is 5 seconds — short enough that operator-toggled kill_switches
take effect well within "without restart" semantics, long enough
that a 30-second scan cycle issuing 20 signals causes at most a
handful of DB reads.

The TTL is overridable via env var `M13_4A_RUNTIME_POLICY_TTL_SEC`.
Set to 0 for strictest behaviour (re-check every submit, no cache).

Fail-safe semantics (audit Correction A)
────────────────────────────────────────
On DB read failure the module returns the safest available answer:

  * If a cached policy exists from a prior successful read AND that
    cache entry is within `STALE_CACHE_MAX_AGE_SEC` (5 minutes) of
    the failed read → return the cached (skip, reason) and emit
    a warning log. Operator gets coverage even during transient DB
    blips. After 5 minutes of continuous failure the cache itself
    is considered too stale to rely on.
  * If NO cached policy exists, OR the cache is too stale →
    fail-SAFE: return (True, REASON_POLICY_UNAVAILABLE). The broker
    submit paths interpret this as "skip — policy state unknown".
    Never fail-OPEN: a safety surface must not assume "ok to trade"
    when it cannot confirm policy state.

Public API
──────────
get_signal_only_reason(broker_name, *, db_path=None, ttl_sec=None,
                         now=None) -> (skip: bool, reason: str)
clear_cache()   — test helper.

Thread safety
─────────────
Module-level RLock guards the cache. Brokers may be invoked from
different threads in some test/operator paths; this keeps the cache
consistent without serialising the actual DB read for too long.

Hard constraint
───────────────
This module imports NOTHING from broker submit paths or live-write
code. It only consults `bot.broker_allocation.load_policy` and
`bot.etoro.signal_only_broker.determine_signal_only_reason` — the
existing primitives `get_broker()` already used at startup.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from bot.etoro.signal_only_broker import (
    REASON_POLICY_UNAVAILABLE,
    determine_signal_only_reason,
)

log = logging.getLogger(__name__)


# Default TTL — 5 seconds. Operator can override via env at process
# start. Tests inject explicit ttl_sec into get_signal_only_reason().
_DEFAULT_TTL_SEC = 5.0

# Maximum age the cache can be relied on during a DB failure (5
# minutes). After this, the cache is considered too stale and the
# module fail-safes instead.
STALE_CACHE_MAX_AGE_SEC = 5 * 60


def _read_default_ttl_from_env() -> float:
    raw = os.getenv("M13_4A_RUNTIME_POLICY_TTL_SEC", "").strip()
    if not raw:
        return _DEFAULT_TTL_SEC
    try:
        v = float(raw)
        return v if v >= 0 else _DEFAULT_TTL_SEC
    except ValueError:
        log.warning("[runtime_policy] M13_4A_RUNTIME_POLICY_TTL_SEC=%r "
                      "is not a number; using default %.1fs",
                      raw, _DEFAULT_TTL_SEC)
        return _DEFAULT_TTL_SEC


# ─────────────────────────────────────────────────────────────────────────────
# Cache state — module-level, RLock-guarded
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.RLock()

# Per-broker cache entries: broker_name -> {value: (skip, reason),
#                                            fetched_at: float}
# We key per-broker because the (skip, reason) computed by
# determine_signal_only_reason depends on the broker.
_cache: dict[str, dict] = {}


def clear_cache() -> None:
    """Test helper: drop all cached entries."""
    with _lock:
        _cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# DB read — isolated so tests can patch it
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_db_path(db_path: Optional[str]) -> str:
    """Resolve the signals.db path the same way the rest of the
    system does."""
    if db_path:
        return db_path
    env = os.environ.get("SIGNALS_DB_PATH")
    if env:
        return env
    from bot.config import BASE_DIR
    return str(BASE_DIR / "data" / "signals.db")


def _read_policy_from_db(db_path: str) -> Optional[dict]:
    """Open the DB and call bot.broker_allocation.load_policy.

    Returns the policy dict on success, None on any failure. Never
    raises — caller relies on None to trigger fail-safe logic.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            from bot.broker_allocation import load_policy
            policy = load_policy(conn)
        finally:
            conn.close()
        return policy if isinstance(policy, dict) else None
    except Exception as e:
        log.debug("[runtime_policy] DB read failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_signal_only_reason(
    broker_name: str,
    *,
    db_path: Optional[str] = None,
    ttl_sec: Optional[float] = None,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Return (skip, reason) for whether the broker should be
    treated as signal-only at this moment.

    skip=True  → broker.submit() should return signal_only_skipped
                  with the given reason.
    skip=False → broker.submit() should proceed normally (reason="").

    Behaviour
    ─────────
    1. Look up cache entry for `broker_name`.
    2. If cache fresh (age < ttl_sec) → return cached value.
    3. Otherwise read policy from DB.
       3a. Read succeeds → compute (skip, reason) via
           determine_signal_only_reason, update cache, return.
       3b. Read fails → CORRECTION A:
           * if a cache entry exists AND its age <
             STALE_CACHE_MAX_AGE_SEC → return cached value, log
             warning ("DB unavailable, using cached policy").
           * else → return (True, REASON_POLICY_UNAVAILABLE). Never
             fail-open.

    Parameters
    ──────────
    broker_name : str
        e.g. 'paper', 'ibkr_live', 'etoro_paper'. Cache is keyed
        per-broker.
    db_path : Optional[str]
        Override the signals.db location (tests). None → resolve
        from env or bot.config.
    ttl_sec : Optional[float]
        Override the freshness window. None → read from env, then
        default 5.0s.
    now : Optional[float]
        Override the clock (tests). None → time.monotonic().
    """
    if ttl_sec is None:
        ttl_sec = _read_default_ttl_from_env()
    if now is None:
        now = time.monotonic()

    with _lock:
        entry = _cache.get(broker_name)
        # 2. Fresh-cache fast path.
        if entry is not None:
            age = now - entry["fetched_at"]
            if age < ttl_sec:
                return entry["value"]

        # 3. Refresh.
        resolved_path = _resolve_db_path(db_path)
        policy = _read_policy_from_db(resolved_path)

        if policy is None:
            # DB read failed. Correction A: prefer cached, else fail-safe.
            if entry is not None:
                age = now - entry["fetched_at"]
                if age < STALE_CACHE_MAX_AGE_SEC:
                    log.warning("[runtime_policy] DB read failed; "
                                 "using cached policy for %s (age=%.1fs)",
                                 broker_name, age)
                    return entry["value"]
                log.warning("[runtime_policy] DB read failed and "
                             "cached policy for %s is too stale "
                             "(age=%.1fs > %ds); failing safe "
                             "(policy_unavailable)",
                             broker_name, age, STALE_CACHE_MAX_AGE_SEC)
            else:
                log.warning("[runtime_policy] DB read failed and no "
                             "cached policy for %s; failing safe "
                             "(policy_unavailable)", broker_name)
            return (True, REASON_POLICY_UNAVAILABLE)

        # DB read succeeded → compute fresh value, update cache.
        skip, reason = determine_signal_only_reason(policy, broker_name)
        _cache[broker_name] = {
            "value":      (skip, reason),
            "fetched_at": now,
        }
        return (skip, reason)


__all__ = [
    "get_signal_only_reason",
    "clear_cache",
    "STALE_CACHE_MAX_AGE_SEC",
]

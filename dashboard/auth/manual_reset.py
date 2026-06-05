"""dashboard/auth/manual_reset.py — M15.3.B operator-reset primitives.

This module contains the PURE helper logic for the operator's manual_reset
flow (M15.3.B). It is intentionally separated from `dashboard/app.py`'s
route glue so the security-critical primitives are isolated, testable,
and AST-checkable.

What's in here:
  * `PreviewTokenStore`         — session-bound, 60s-TTL, single-use tokens
                                  issued by GET /api/manual-reset/preview
                                  and consumed by POST /api/manual-reset.
  * `make_manual_reset_limiter()` — dedicated RateLimiter (3/3600s/3600s)
                                  per client IP. Tighter than the login
                                  limiter — manual_reset should be rare.
  * `read_kill_switch_state()`  — read the M13.4A allocation policy and
                                  return a dict of scope→kill_switch.
                                  Uses bot.broker_allocation.load_policy
                                  (the M13.4A canonical reader).
  * `prepare_cleared_policy()`  — produce a *new* policy dict with all
                                  kill_switch flags set to False; return
                                  also the list of scopes that were
                                  cleared (idempotent: empty list if
                                  none were set).
  * `verify_step_up_totp()`     — fresh-TOTP check using the M15.3.A.2
                                  primitives. Returns (ok, hint) where
                                  hint is ONLY ever 'recently_used' or '';
                                  this is the operator-approved C1 surface.
  * `validate_reason()`         — 10..500 char operator-reason validator.
  * `validate_confirm()`        — server-side `confirm == "RESET"` check.

What's NOT in here (hard constraints):
  * NO broker imports (ib_insync, ibapi, bot.broker_*, bot.etoro.*,
    bot.gateway_*, bot.risk_authority.ibkr_paper_reader).
  * NO broker method names as string literals or function calls.
  * NO scanner/strategy imports.
  * NO M14 engine/governor/snapshot/preflight imports. The ONLY
    bot.risk_authority import is `audit_decisions.write_manual_reset_decision`
    (a thin audit-row writer, no engine logic).
  * NO live-trading code.
  * NO direct SQL against M14 tables — the kill_switch live in the
    M13.4A policy (`portfolio_risk_state.broker_allocation_policy`),
    which is read/written via `bot.broker_allocation`.

These constraints are enforced by `TestNoBrokerImports` in
`test_m15_3_b_manual_reset.py` (AST scan).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Preview-token store — session-bound, 60s TTL, single-use
# ─────────────────────────────────────────────────────────────────────────────

PREVIEW_TOKEN_TTL_SECONDS = 60


class PreviewTokenStore:
    """Issues and consumes single-use preview tokens.

    Each token is tied to a *session* (we use the hashed session id, the
    same one auth_events uses, so we never store raw session material).
    Tokens expire after 60 seconds and are single-use.

    Why session-bound: if attacker steals the token (e.g. via referer
    leak), they still can't use it from a different session.

    Storage: in-memory dict, lazy GC on every access. Tokens are
    cryptographically random 32-byte values, base64url-encoded — they're
    not secrets in the cryptographic sense (no derivation), just
    unpredictable nonces.
    """

    def __init__(self, ttl_seconds: int = PREVIEW_TOKEN_TTL_SECONDS,
                 clock=time.time):
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._tokens: Dict[Tuple[str, str], float] = {}
        self._lock = Lock()

    def issue(self, session_hash: str) -> str:
        """Generate a new token bound to the given (hashed) session.

        Returns the token string. The session_hash MUST be the hashed
        form (from dashboard.auth.audit.hash_session_id); never pass the
        raw session id here.
        """
        if not isinstance(session_hash, str) or not session_hash:
            raise ValueError("session_hash must be a non-empty string")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._gc_locked()
            self._tokens[(session_hash, token)] = self._clock() + self._ttl
        return token

    def consume(self, session_hash: str, token: str) -> bool:
        """Validate + consume a token. Returns True on success.

        Token is removed from the store on success (single-use).
        Returns False if:
          * session_hash/token are malformed
          * the (session_hash, token) pair was never issued
          * the token has expired
        """
        if not isinstance(session_hash, str) or not session_hash:
            return False
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            self._gc_locked()
            key = (session_hash, token)
            if key not in self._tokens:
                return False
            del self._tokens[key]
            return True

    def size(self) -> int:
        """Diagnostic — current number of live tokens. After GC."""
        with self._lock:
            self._gc_locked()
            return len(self._tokens)

    def _gc_locked(self) -> None:
        """Remove expired tokens. Caller MUST hold self._lock."""
        now = self._clock()
        expired = [k for k, exp in self._tokens.items() if exp <= now]
        for k in expired:
            del self._tokens[k]


# Module-level singleton — created on import; replaced by tests via
# injection. The dashboard app uses this instance.
_preview_token_store = PreviewTokenStore()


def get_preview_token_store() -> PreviewTokenStore:
    """Public accessor for the module-level singleton."""
    return _preview_token_store


def set_preview_token_store(store: PreviewTokenStore) -> None:
    """Replace the module-level singleton (tests only)."""
    global _preview_token_store
    _preview_token_store = store


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiter — dedicated, tighter than login
# ─────────────────────────────────────────────────────────────────────────────

MANUAL_RESET_RATE_LIMIT_THRESHOLD = 3       # attempts
MANUAL_RESET_RATE_LIMIT_WINDOW_SEC = 3600   # 60 minutes
MANUAL_RESET_RATE_LIMIT_LOCKOUT_SEC = 3600  # 60 minutes


def make_manual_reset_limiter():
    """Build a fresh RateLimiter for manual_reset. Tests build their own."""
    # Local import — keeps this module importable without triggering
    # the rest of dashboard.auth at import time. Also keeps the class
    # identity correct (the same RateLimiter the dashboard app uses).
    from dashboard.auth.rate_limit import RateLimiter
    return RateLimiter(
        threshold=MANUAL_RESET_RATE_LIMIT_THRESHOLD,
        window_sec=MANUAL_RESET_RATE_LIMIT_WINDOW_SEC,
        lockout_sec=MANUAL_RESET_RATE_LIMIT_LOCKOUT_SEC,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Kill-switch state I/O — uses bot.broker_allocation primitives (M13.4A)
# ─────────────────────────────────────────────────────────────────────────────

# Closed list of scope names where kill_switch may exist in the M13.4A
# policy. Aligned with bot.broker_allocation.DEFAULT_POLICY. If new broker
# scopes are added later, this list must be extended in lockstep.
_KILL_SWITCH_SCOPES = ("global", "ibkr", "etoro")


def read_kill_switch_state(conn: sqlite3.Connection) -> Dict[str, bool]:
    """Read the current M13.4A allocation policy and return
    {scope_name: kill_switch_bool} for every scope that has a kill_switch.

    Uses `bot.broker_allocation.load_policy()` — the M13.4A canonical
    reader. Does not mutate anything. If the policy is missing/corrupt,
    `load_policy` returns DEFAULT_POLICY (kill switches all False).
    """
    from bot.broker_allocation import load_policy
    policy = load_policy(conn)
    out: Dict[str, bool] = {}
    for scope in _KILL_SWITCH_SCOPES:
        block = policy.get(scope)
        if isinstance(block, dict) and "kill_switch" in block:
            out[scope] = bool(block["kill_switch"])
    return out


def prepare_cleared_policy(
    policy: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Return (new_policy, cleared_scopes).

    new_policy is a deep-copy of `policy` with every kill_switch flag
    set to False. cleared_scopes is the list of scope names where the
    flag was True (and is now False). Idempotent: if all flags were
    already False, cleared_scopes is empty.
    """
    import copy
    new_policy = copy.deepcopy(policy)
    cleared: List[str] = []
    for scope in _KILL_SWITCH_SCOPES:
        block = new_policy.get(scope)
        if not isinstance(block, dict):
            continue
        if block.get("kill_switch") is True:
            block["kill_switch"] = False
            cleared.append(scope)
    return new_policy, cleared


# ─────────────────────────────────────────────────────────────────────────────
# Step-up TOTP — fresh-code check at reset time
# ─────────────────────────────────────────────────────────────────────────────

def verify_step_up_totp(provided: Any, *, clock=None) -> Tuple[bool, str]:
    """Verify a step-up TOTP code submitted with /api/manual-reset.

    Returns (ok, hint). On success: (True, ''). On failure:
      * (False, 'recently_used') — the code was correct cryptographically
        but is in the replay cache (e.g. just used for login). This is
        the ONLY hint the API ever exposes — it's operator-friendly
        because the login flow can naturally cause it.
      * (False, '') — anything else (wrong format, wrong code, no
        secret configured, pyotp missing). Per operator C1, the API
        returns these all as generic `totp_invalid`.

    `clock` is an optional callable returning a unix timestamp. Tests
    use this to advance time deterministically without sleeping.
    Production callers leave it None and the M15.3.A.2 verify_code
    default (real time.time) is used.

    Reuses `dashboard.auth.totp.verify_code` which already enforces:
      * constant-time pyotp.TOTP.verify under the hood
      * the M15.3.A.2 replay cache (per-secret, ±VALID_WINDOW horizon)
      * extras-blacklist (no raw code/secret/URI ever returned)
    """
    from dashboard.auth.totp import verify_code, totp_enabled
    if not totp_enabled():
        # Hard refusal: manual_reset over a dashboard without TOTP
        # configured would be a hole — it removes the step-up barrier
        # the operator explicitly approved (C1). Tests cover this.
        log.warning("manual_reset: step-up TOTP requested but TOTP is not "
                    "enabled on the dashboard. Refusing.")
        return False, ""
    kwargs: Dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    ok, info = verify_code(provided, **kwargs)
    if ok:
        return True, ""
    # Only 'replay' maps to the user-visible hint. Everything else is
    # collapsed to the generic failure.
    reason = info.get("reason") if isinstance(info, dict) else None
    if reason == "replay":
        return False, "recently_used"
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Reason and confirm validators
# ─────────────────────────────────────────────────────────────────────────────

REASON_MIN_CHARS = 10
REASON_MAX_CHARS = 500
CONFIRM_LITERAL = "RESET"


def validate_reason(reason: Any) -> Tuple[bool, str]:
    """Validate the operator-supplied reason field.

    Rules:
      * MUST be a string
      * MUST be 10..500 characters after .strip()
      * MUST NOT consist entirely of whitespace

    Returns (ok, error_code). error_code is a short closed-set tag
    suitable for audit `extras_json.reason`:
      * 'reason_missing'   — not a string, empty, or None
      * 'reason_too_short' — < 10 chars after strip
      * 'reason_too_long'  — > 500 chars after strip
      * ''                 — ok
    """
    if not isinstance(reason, str):
        return False, "reason_missing"
    stripped = reason.strip()
    if not stripped:
        return False, "reason_missing"
    if len(stripped) < REASON_MIN_CHARS:
        return False, "reason_too_short"
    if len(stripped) > REASON_MAX_CHARS:
        return False, "reason_too_long"
    return True, ""


def validate_confirm(value: Any) -> bool:
    """Strict server-side confirm check: must equal 'RESET' exactly."""
    return value == CONFIRM_LITERAL


# ─────────────────────────────────────────────────────────────────────────────
# Audit helpers — small wrappers around dashboard.auth.audit.record_auth_event
# ─────────────────────────────────────────────────────────────────────────────

# Keep the extras-payload schema closed and tight. Operators reading
# audit logs should see consistent shape per kind.

def make_attempt_extras(*, has_csrf: bool, has_preview_token: bool,
                        has_totp: bool, has_reason: bool,
                        confirm_ok: bool) -> Dict[str, Any]:
    """Build the extras dict for a manual_reset_attempt row.

    Captures which inputs were present (NOT their values). Never
    contains: passwords, TOTP codes, TOTP secrets, otpauth URIs, raw
    session ids, broker credentials, the operator reason text.
    """
    return {
        "has_csrf": bool(has_csrf),
        "has_preview_token": bool(has_preview_token),
        "has_totp": bool(has_totp),
        "has_reason": bool(has_reason),
        "confirm_ok": bool(confirm_ok),
    }


def make_failure_extras(*, reason_code: str,
                         extra: Optional[Dict[str, Any]] = None,
                         ) -> Dict[str, Any]:
    """Build the extras dict for a manual_reset_failure row.

    reason_code is a short closed-set tag describing the failure mode.
    extra is an optional small dict of non-secret diagnostic data.
    """
    out: Dict[str, Any] = {"reason": reason_code}
    if extra:
        # Defensive: drop anything that looks like a secret.
        for k, v in extra.items():
            if isinstance(k, str) and k.lower() in _FORBIDDEN_EXTRA_KEYS:
                continue
            out[k] = v
    return out


def make_success_extras(*, switches_cleared: List[str],
                         before_state: Dict[str, bool],
                         after_state: Dict[str, bool],
                         reason_text: str) -> Dict[str, Any]:
    """Build the extras dict for a manual_reset_success row.

    Includes operator-visible audit data: which switches were cleared,
    before/after state, the operator's reason. The reason field is
    plain text and could theoretically contain operator-pasted secrets
    — we trust the UI helper text (per operator C3) to deter that
    and don't add server-side filtering (would be too aggressive).
    """
    return {
        "switches_cleared": list(switches_cleared),
        "noop": len(switches_cleared) == 0,
        "before_state": dict(before_state),
        "after_state": dict(after_state),
        "reason": str(reason_text),
    }


def make_preview_extras(*, kill_switch_state: Dict[str, bool],
                         token_issued: bool) -> Dict[str, Any]:
    """Build the extras dict for a manual_reset_preview row."""
    return {
        "kill_switch_state": dict(kill_switch_state),
        "token_issued": bool(token_issued),
    }


# Defensive denylist used by make_failure_extras to filter accidental
# leakage of secret-keyed values. The dashboard's audit schema already
# enforces this invariant at higher layers (record_auth_event and the
# test_m15_3_a_2_totp / test_m15_3_b regression suites), but a defence
# at the source is cheap and good.
_FORBIDDEN_EXTRA_KEYS = frozenset({
    "password", "passwd", "pwd",
    "totp_code", "totp", "code",
    "totp_secret", "secret", "otpauth", "otpauth_uri",
    "session_id", "session_raw",
    "api_key", "user_key", "api-key", "user-key",
    "etoro_api_key", "etoro_user_key",
    "authorization", "cookie",
})


# ─────────────────────────────────────────────────────────────────────────────
# Idempotent reset — the atomic write block
# ─────────────────────────────────────────────────────────────────────────────

def execute_atomic_reset(
    conn: sqlite3.Connection,
    *,
    actor: str,
    reason_text: str,
    client_ip: Optional[str],
    user_agent: Optional[str],
    session_id: Optional[str],
) -> Dict[str, Any]:
    """Perform the kill-switch clear + dual audit writes atomically.

    Single SQLite transaction (BEGIN IMMEDIATE / COMMIT / ROLLBACK on
    error). All three writes succeed together, or none do:
      1. INSERT OR REPLACE into portfolio_risk_state (the policy row)
      2. INSERT into risk_decisions with source='manual_reset'
      3. INSERT into auth_events with kind='manual_reset_success'

    Returns a dict suitable for the JSON response:
      {
        "before_state": {scope: bool, ...},
        "after_state":  {scope: bool, ...},
        "switches_cleared": [scope, ...],
        "noop": bool,
        "auth_event_id": int,
        "decision_id": str,
      }

    Raises sqlite3.Error on DB failure; caller catches and writes a
    manual_reset_failure audit row outside this transaction.

    NOTE: this function does NOT call record_auth_event for the success
    row — it inlines the INSERT so all three writes share one
    transaction. record_auth_event commits internally, which would
    break atomicity.
    """
    from bot.broker_allocation import load_policy, validate_policy, POLICY_KEY
    from bot.risk_authority.audit_decisions import (
        write_manual_reset_decision,
    )
    from dashboard.auth.audit import (
        ALLOWED_KINDS, ensure_auth_events_schema,
        hash_session_id, _truncate_ua, _now_utc_iso,
    )

    # Defence-in-depth: confirm the allowed kind is registered.
    if "manual_reset_success" not in ALLOWED_KINDS:
        raise RuntimeError(
            "manual_reset_success is not in ALLOWED_KINDS; "
            "dashboard/auth/audit.py is out of sync with M15.3.B."
        )

    # Ensure both auth_events schema exists (idempotent) BEFORE we
    # open the IMMEDIATE transaction, since CREATE TABLE inside an
    # IMMEDIATE write transaction can deadlock against other readers.
    ensure_auth_events_schema(conn)

    # 1. Read current policy + compute the diff.
    current_policy = load_policy(conn)
    new_policy, cleared = prepare_cleared_policy(current_policy)
    before_state = read_kill_switch_state(conn)
    # after_state is computed from new_policy directly so we don't have
    # to re-query after the write.
    after_state: Dict[str, bool] = {}
    for scope in _KILL_SWITCH_SCOPES:
        block = new_policy.get(scope)
        if isinstance(block, dict) and "kill_switch" in block:
            after_state[scope] = bool(block["kill_switch"])

    # 2. Validate the proposed policy (so we don't write garbage).
    validation = validate_policy(new_policy)
    if not validation.ok:
        raise ValueError(
            f"manual_reset: prepared policy fails validation: "
            f"{validation.errors}"
        )

    now_iso = _now_utc_iso()
    policy_json = json.dumps(new_policy, sort_keys=True)
    success_extras = make_success_extras(
        switches_cleared=cleared,
        before_state=before_state,
        after_state=after_state,
        reason_text=reason_text,
    )

    # 3. Begin atomic write. SQLite default isolation is "deferred"
    #    — we want IMMEDIATE so we don't get a SQLITE_BUSY between
    #    the policy write and the audit writes.
    conn.isolation_level = None  # we manage transactions explicitly
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        # 3a. Policy upsert (M13.4A).
        cur.execute(
            "INSERT OR REPLACE INTO portfolio_risk_state "
            "(key, value, updated_at) VALUES (?, ?, ?)",
            (POLICY_KEY, policy_json, now_iso),
        )
        # 3b. Risk decisions audit (M14 vocabulary).
        decision_id = write_manual_reset_decision(
            conn,
            switches_cleared=cleared,
            reason_text=reason_text,
            actor=actor,
            now_iso=now_iso,
        )
        # 3c. Auth events audit (M15.3.A vocabulary, M15.3.B kind).
        cur.execute(
            """
            INSERT INTO auth_events
                (ts_utc, kind, client_ip, user_agent,
                  session_id, success, extras_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso, "manual_reset_success", client_ip or "",
             _truncate_ua(user_agent),
             hash_session_id(session_id),
             1,
             json.dumps(success_extras, separators=(",", ":"))),
        )
        auth_event_id = int(cur.lastrowid or 0)
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        # Restore default isolation for the rest of the connection's
        # lifetime. (Connections are short-lived per request, so this
        # is defensive.)
        conn.isolation_level = ""

    return {
        "before_state": before_state,
        "after_state": after_state,
        "switches_cleared": cleared,
        "noop": len(cleared) == 0,
        "auth_event_id": auth_event_id,
        "decision_id": decision_id,
    }

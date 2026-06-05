"""auth_events — append-only audit log for dashboard authentication.

Approved schema per Q-A.8:
  id          INTEGER PRIMARY KEY AUTOINCREMENT
  ts_utc      TEXT NOT NULL        — ISO-8601 with timezone
  kind        TEXT NOT NULL        — closed-set classifier
  client_ip   TEXT                 — raw IPv4/v6 per Q-A.2
  user_agent  TEXT                 — truncated to 200 chars per Q-A.8
  session_id  TEXT                 — sha256-hashed per Q-A.8 ("never raw")
  success     INTEGER NOT NULL     — 0 or 1
  extras_json TEXT                 — application-controlled, no secrets

Append-only:
  * The DAO never offers UPDATE/DELETE methods.
  * Schema migration is idempotent (CREATE TABLE IF NOT EXISTS).
  * A SQL CHECK constraint enforces ts_utc non-empty and success ∈ {0,1}.

Closed kind set:
  * "login_success"  — /api/login with correct password
  * "login_failure"  — /api/login with wrong password
  * "login_locked"   — /api/login while client_ip is rate-limited
  * "login_unconfigured" — /api/login while no password is configured
  * "logout"         — /api/logout
  * "session_rotate" — explicit rotation by operator (M15.3.B will use)
  * "csrf_invalid"   — request rejected by csrf_required decorator
  * "session_expired" — enforce_session_timeout cleared a session

The auth_events table lives in the same SQLite DB as the rest of the
audit trail (data/signals.db). ensure_auth_events_schema is called
once at app startup AND idempotently before each insert (defensive).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List


CREATE_AUTH_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS auth_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    client_ip   TEXT,
    user_agent  TEXT,
    session_id  TEXT,
    success     INTEGER NOT NULL,
    extras_json TEXT,
    CHECK (ts_utc <> ''),
    CHECK (success IN (0, 1))
);
"""

CREATE_INDEX_TS = (
    "CREATE INDEX IF NOT EXISTS idx_auth_events_ts "
    "ON auth_events(ts_utc DESC);"
)
CREATE_INDEX_IP = (
    "CREATE INDEX IF NOT EXISTS idx_auth_events_client_ip "
    "ON auth_events(client_ip);"
)
CREATE_INDEX_KIND = (
    "CREATE INDEX IF NOT EXISTS idx_auth_events_kind "
    "ON auth_events(kind);"
)


# Closed kind set — enforced by the DAO (NOT by the schema, since
# SQLite has no native enum and CHECK against a long IN-list is
# brittle to schema migration).
ALLOWED_KINDS = frozenset({
    "login_success",
    "login_failure",
    "login_locked",
    "login_unconfigured",
    "logout",
    "session_rotate",
    "csrf_invalid",
    "session_expired",
    # M15.3.A.2 — TOTP / Google Authenticator 2FA additions:
    "totp_success",                  # TOTP code verified during login
    "totp_failure",                  # TOTP code wrong / replay / format error
    "totp_required_not_provided",    # login with correct password but no totp_code
    "totp_setup",                    # operator enabled TOTP via tool
    "totp_disabled",                 # operator disabled TOTP via tool
    # M15.3.B — manual_reset operator flow additions:
    "manual_reset_preview",          # GET /api/manual-reset/preview
    "manual_reset_attempt",          # POST attempt (always written first)
    "manual_reset_success",          # POST success (kill switches cleared)
    "manual_reset_failure",          # POST failure (with reason in extras)
})


def ensure_auth_events_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the auth_events table + indexes.

    Safe to call on every insert; SQLite's IF NOT EXISTS makes it
    effectively a no-op once the table exists."""
    cur = conn.cursor()
    cur.execute(CREATE_AUTH_EVENTS_SQL)
    cur.execute(CREATE_INDEX_TS)
    cur.execute(CREATE_INDEX_IP)
    cur.execute(CREATE_INDEX_KIND)
    conn.commit()


def hash_session_id(session_id: Optional[str]) -> str:
    """SHA-256 hash of the session ID (per Q-A.8 "never raw").

    Empty / None → empty string (so the audit row's session_id column
    can be a stable empty value, easier to query than NULL)."""
    if not session_id:
        return ""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _truncate_ua(ua: Optional[str], max_chars: int = 200) -> str:
    """Truncate the User-Agent header to <= max_chars (per Q-A.8)."""
    if not ua:
        return ""
    if not isinstance(ua, str):
        ua = str(ua)
    return ua[:max_chars]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def record_auth_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    client_ip: Optional[str],
    user_agent: Optional[str],
    session_id: Optional[str],
    success: bool,
    extras: Optional[Dict[str, Any]] = None,
    ts_utc: Optional[str] = None,
) -> int:
    """Append one row to auth_events. Returns the new row id.

    `session_id` is hashed automatically — pass the raw ID. The raw
    value is never persisted.

    `extras` is application-controlled metadata serialized to JSON.
    The caller MUST NOT include passwords, password hashes, raw IPs
    in nested objects, or any secret material. Common extras:
      * failure_reason (e.g. "wrong_password", "csrf_invalid")
      * rate_limit_policy (the policy dict from RateLimiter.policy())
      * password_path ("bcrypt" or "plaintext") — NOT the password
      * lockout_retry_after_sec (int)
    """
    if kind not in ALLOWED_KINDS:
        # Refuse rather than silently coerce — catches bugs.
        raise ValueError(
            f"auth_events: kind={kind!r} not in ALLOWED_KINDS"
        )
    ensure_auth_events_schema(conn)
    row = {
        "ts_utc":      ts_utc or _now_utc_iso(),
        "kind":        kind,
        "client_ip":   client_ip or "",
        "user_agent":  _truncate_ua(user_agent),
        "session_id":  hash_session_id(session_id),
        "success":     1 if success else 0,
        "extras_json": json.dumps(extras, separators=(",", ":"))
                       if extras else None,
    }
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO auth_events
            (ts_utc, kind, client_ip, user_agent,
              session_id, success, extras_json)
        VALUES (:ts_utc, :kind, :client_ip, :user_agent,
                 :session_id, :success, :extras_json)
        """,
        row,
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def read_auth_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    kind: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read recent auth_events. Read-only, no auth side-effects.

    Used by a future /api/auth/events endpoint (M15.3.B will wire
    that up if needed — for M15.3.A we just need write-and-test).

    Returns a list of dicts; extras_json is parsed back to a dict."""
    ensure_auth_events_schema(conn)
    where = []
    params: Dict[str, Any] = {"lim": min(max(1, limit), 1000)}
    if kind is not None:
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"read_auth_events: kind={kind!r} not allowed")
        where.append("kind = :kind")
        params["kind"] = kind
    if client_ip:
        where.append("client_ip = :client_ip")
        params["client_ip"] = client_ip
    sql = """
        SELECT id, ts_utc, kind, client_ip, user_agent,
               session_id, success, extras_json
        FROM auth_events
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT :lim"
    cur = conn.cursor()
    rows = cur.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        extras = None
        if r[7]:
            try:
                extras = json.loads(r[7])
            except (json.JSONDecodeError, TypeError):
                extras = {"_raw_unparseable": True}
        out.append({
            "id":          r[0],
            "ts_utc":      r[1],
            "kind":        r[2],
            "client_ip":   r[3],
            "user_agent":  r[4],
            "session_id":  r[5],
            "success":     bool(r[6]),
            "extras":      extras,
        })
    return out

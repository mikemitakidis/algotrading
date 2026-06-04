"""dashboard.auth — M15.3.A dashboard auth/security hardening primitives.

Five narrow modules:
  * passwords    — bcrypt verify + plaintext fallback (no secrets logged)
  * rate_limit   — in-memory sliding-window login lockout
  * csrf         — per-session CSRF token issue + verify + decorator
  * sessions     — cookie-flag hardening (env-gated), idle+absolute timeouts
  * audit        — auth_events DAO (append-only, raw IP per Q-A.2)

All re-exported here for convenience. dashboard/app.py imports from this
package only — does not reach into individual modules — so the surface
stays small and easy to AST-scan.

M15.3.A scope:
  * No order paths, no broker imports, no IB API
  * No M14 engine/governor changes
  * Does NOT modify Caddy/TLS install (operator step per the runbook)
  * Does NOT touch protected files outside dashboard/app.py +
    bot/flywheel.py (schema-only)
"""
from dashboard.auth.passwords import verify_password
from dashboard.auth.rate_limit import RateLimiter, LoginRateLimited
from dashboard.auth.csrf import (
    issue_csrf_token,
    verify_csrf_token,
    csrf_required,
    rotate_csrf_token,
)
from dashboard.auth.sessions import (
    harden_app_config,
    rotate_session,
    enforce_session_timeout,
    is_secure_cookie_mode,
)
from dashboard.auth.audit import (
    ensure_auth_events_schema,
    record_auth_event,
    read_auth_events,
    hash_session_id,
)
from dashboard.auth.totp import (
    totp_enabled,
    generate_secret as totp_generate_secret,
    build_otpauth_uri,
    render_qr_terminal,
    verify_code as totp_verify_code,
    ReplayCache as TOTPReplayCache,
    get_default_replay_cache as totp_get_default_replay_cache,
)

__all__ = [
    "verify_password",
    "RateLimiter", "LoginRateLimited",
    "issue_csrf_token", "verify_csrf_token", "csrf_required",
    "rotate_csrf_token",
    "harden_app_config", "rotate_session", "enforce_session_timeout",
    "is_secure_cookie_mode",
    "ensure_auth_events_schema", "record_auth_event", "read_auth_events",
    "hash_session_id",
    # M15.3.A.2 — TOTP:
    "totp_enabled", "totp_generate_secret", "build_otpauth_uri",
    "render_qr_terminal", "totp_verify_code",
    "TOTPReplayCache", "totp_get_default_replay_cache",
]

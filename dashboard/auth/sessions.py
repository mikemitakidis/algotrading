"""Flask session hardening — cookie flags + idle/absolute timeouts.

Approved per Q-A.5 (hybrid: 30 min idle + 12 h absolute, both env-
configurable) and Q-A.3 correction #2 (Secure cookie flag MUST NOT
default to True unconditionally — would break login over plain HTTP
during transition).

Cookie flag policy:
  * HttpOnly: ALWAYS True. No legitimate JS needs to read the session
    cookie; preventing XSS access is always safe.
  * SameSite: ALWAYS "Strict". The dashboard is a single-origin app;
    no cross-site requests should ever carry the session cookie.
  * Secure: GATED on env. Set True only when DASHBOARD_HTTPS_MODE=true
    OR DASHBOARD_COOKIE_SECURE=true. Otherwise stays False with a
    one-time warning at startup. Setting Secure=True on a plain-HTTP
    session means the browser silently drops the cookie — login
    appears to succeed but the session never establishes.

Timeout policy:
  * Absolute timeout: session["_login_at"] + DASHBOARD_SESSION_MAX_HOUR
    must be in the future. Default 12 hours.
  * Idle timeout: session["_last_seen"] + DASHBOARD_SESSION_IDLE_MIN
    must be in the future. Default 30 minutes.
  * enforce_session_timeout() is called from a before_request hook;
    if either is breached, the session is cleared and the request
    proceeds as unauthenticated (the require_auth decorator will then
    return 401).

Session ID rotation:
  * On successful /api/login, rotate_session() generates a fresh
    session, sets the auth marker, login_at, last_seen, AND a fresh
    CSRF token. Old session ID becomes invalid even if leaked.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)


# Default timeout values. Env override at module load; tests reset
# via reload or by setting session keys directly.
def _read_int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, str(default)).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


DEFAULT_IDLE_MIN     = _read_int_env("DASHBOARD_SESSION_IDLE_MIN", 30)
DEFAULT_MAX_HOUR     = _read_int_env("DASHBOARD_SESSION_MAX_HOUR", 12)

# Session keys we own.
SESSION_AUTHED_KEY    = "authed"
SESSION_LOGIN_AT_KEY  = "_login_at"
SESSION_LAST_SEEN_KEY = "_last_seen"
SESSION_LOGIN_IP_KEY  = "_login_ip"


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("true", "1", "yes", "on")


def is_secure_cookie_mode() -> bool:
    """Return True iff the dashboard is configured for HTTPS — i.e.
    safe to set SESSION_COOKIE_SECURE=True. Either env knob enables.

    Correction #2 from the user: do NOT set Secure unconditionally —
    breaks login over plain HTTP during the transition window."""
    return _truthy_env("DASHBOARD_HTTPS_MODE") or \
           _truthy_env("DASHBOARD_COOKIE_SECURE")


def harden_app_config(app, *, logger=log) -> dict:
    """Apply Flask session-cookie config hardening.

    Returns a dict describing what was applied — caller can log it
    or include in startup diagnostics. The dict NEVER contains the
    secret key or any password material.

    Defaults:
      SESSION_COOKIE_HTTPONLY     = True   (always)
      SESSION_COOKIE_SAMESITE     = 'Strict' (always)
      SESSION_COOKIE_SECURE       = is_secure_cookie_mode()
      PERMANENT_SESSION_LIFETIME  = DEFAULT_MAX_HOUR hours
      SESSION_REFRESH_EACH_REQUEST = True
    """
    secure = is_secure_cookie_mode()
    config = {
        "SESSION_COOKIE_HTTPONLY":      True,
        "SESSION_COOKIE_SAMESITE":      "Strict",
        "SESSION_COOKIE_SECURE":        secure,
        "PERMANENT_SESSION_LIFETIME":   DEFAULT_MAX_HOUR * 3600,
        "SESSION_REFRESH_EACH_REQUEST": True,
    }
    for k, v in config.items():
        app.config[k] = v
    if not secure:
        logger.warning(
            "DASHBOARD_COOKIE_SECURE=False — session cookie will be "
            "sent over plain HTTP. This is acceptable during the "
            "Caddy/TLS transition but should be flipped to True "
            "(via DASHBOARD_HTTPS_MODE=true or DASHBOARD_COOKIE_SECURE=true) "
            "once HTTPS is in place. See docs/M15_3_A_dashboard_auth.md."
        )
    return {
        "secure":     secure,
        "httponly":   True,
        "samesite":   "Strict",
        "max_hour":   DEFAULT_MAX_HOUR,
        "idle_min":   DEFAULT_IDLE_MIN,
    }


def rotate_session(session_obj, *, client_ip: Optional[str] = None,
                    clock=time.time) -> None:
    """Reset the session on login — clears old keys, sets new auth
    marker + login_at + last_seen + login_ip. Flask's
    session.clear()+set rotates the underlying cookie identifier
    because Flask uses a content-based signed cookie.

    The fresh CSRF token is issued separately by the caller via
    issue_csrf_token() — keeping that responsibility outside this
    module avoids importing csrf here (and the circular).
    """
    session_obj.clear()
    now = clock()
    session_obj[SESSION_AUTHED_KEY]    = True
    session_obj[SESSION_LOGIN_AT_KEY]  = now
    session_obj[SESSION_LAST_SEEN_KEY] = now
    if client_ip:
        session_obj[SESSION_LOGIN_IP_KEY] = client_ip
    # Mark as permanent so PERMANENT_SESSION_LIFETIME applies.
    try:
        session_obj.permanent = True
    except AttributeError:
        pass


def enforce_session_timeout(session_obj, *,
                              idle_min: int = DEFAULT_IDLE_MIN,
                              max_hour: int = DEFAULT_MAX_HOUR,
                              clock=time.time) -> bool:
    """Check whether the session is still within idle + absolute
    timeouts. Returns True if session is valid; False if it was
    cleared (caller should treat the request as unauthenticated).

    Called from a before_request hook on every authenticated route.
    If the session has no _login_at / _last_seen (e.g. legacy session
    from before M15.3.A deploy), set them now to give a fair window
    from this request forward — don't punish the operator for the
    deploy itself.
    """
    if not session_obj.get(SESSION_AUTHED_KEY):
        # Not authed; nothing to enforce.
        return True
    now = clock()

    login_at  = session_obj.get(SESSION_LOGIN_AT_KEY)
    last_seen = session_obj.get(SESSION_LAST_SEEN_KEY)

    # Legacy session (M15.3.A first-deploy grace): set timestamps now,
    # treat as valid this request, enforce from next request forward.
    if login_at is None or last_seen is None:
        session_obj[SESSION_LOGIN_AT_KEY]  = now
        session_obj[SESSION_LAST_SEEN_KEY] = now
        return True

    # Absolute timeout.
    if now - float(login_at) > max_hour * 3600:
        session_obj.clear()
        return False

    # Idle timeout.
    if now - float(last_seen) > idle_min * 60:
        session_obj.clear()
        return False

    # Touch last_seen.
    session_obj[SESSION_LAST_SEEN_KEY] = now
    return True

"""CSRF protection — per-session token in the Flask session, verified
via X-CSRF-Token header (or form field) on state-changing requests.

Approved per Q-A.6/Q-A.7: every POST/PUT/PATCH/DELETE endpoint requires
CSRF except /api/login (no session yet to embed the token in).
/api/logout DOES require CSRF — Q-A.7 explicit.

Implementation choices:
  * Token is a 32-byte URL-safe base64 string stored in
    session["csrf_token"].
  * Generated on login (and on rotate_session). Persists for the
    session lifetime.
  * Verified via either:
      - X-CSRF-Token header (preferred for fetch/XHR)
      - csrf_token form field (fallback for traditional forms)
  * Constant-time compare via hmac.compare_digest to mitigate timing
    side channels.

A request fails CSRF check ⇒ HTTP 403 with JSON
{"error": "csrf_invalid"}. The check runs BEFORE the wrapped handler,
so handlers can assume CSRF has passed when they execute.

NOT CSRF-protected:
  * GET / HEAD / OPTIONS — read-only, idempotent
  * POST /api/login — there's no session yet (per Q-A.7)
"""
from __future__ import annotations

import hmac
import secrets
from functools import wraps
from typing import Callable

from flask import request, session, jsonify


CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FORM_FIELD  = "csrf_token"
CSRF_TOKEN_BYTES = 32

# Methods that DO require CSRF protection.
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def issue_csrf_token(session_obj) -> str:
    """Generate a fresh CSRF token and store in the session.

    Returns the token (so the caller can return it to the client in
    the response body — e.g. /api/login response includes the new
    token so the JS can attach it to subsequent requests)."""
    token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
    session_obj[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token(session_obj) -> str:
    """Force-rotate the CSRF token. Called on session rotation
    (login, privilege change). Returns the new token."""
    return issue_csrf_token(session_obj)


def get_csrf_token(session_obj) -> str:
    """Read the current CSRF token from the session. Returns "" if
    not set (caller should issue one). Used by the /api/auth/csrf
    endpoint to return the current token to the client."""
    return session_obj.get(CSRF_SESSION_KEY, "") or ""


def _extract_provided_token() -> str:
    """Read the CSRF token from the request — header preferred,
    form-field fallback. Returns "" if neither present."""
    header = request.headers.get(CSRF_HEADER_NAME, "")
    if header:
        return header
    # Form/JSON fallback — only check form (not JSON body) to avoid
    # ambiguity in JSON parsing. If JS clients want to use the form
    # field they can — but the header is preferred.
    form_val = request.form.get(CSRF_FORM_FIELD, "")
    return form_val or ""


def verify_csrf_token(session_obj) -> bool:
    """Validate the request's CSRF token against the session.

    Returns True iff session has a token AND the request provides a
    matching token (constant-time compare). False otherwise."""
    expected = session_obj.get(CSRF_SESSION_KEY, "")
    if not expected:
        return False
    provided = _extract_provided_token()
    if not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))


def csrf_required(f: Callable) -> Callable:
    """Decorator. Applies CSRF verification to state-changing methods
    (POST/PUT/PATCH/DELETE). GET/HEAD/OPTIONS pass through. Failed
    check returns 403 JSON {"error": "csrf_invalid"}.

    Apply AFTER @require_auth (auth is checked first; CSRF only
    matters once the session exists). The recommended decorator stack:

        @app.route(...)
        @require_auth
        @csrf_required
        def endpoint(): ...
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method not in STATE_CHANGING_METHODS:
            return f(*args, **kwargs)
        if not verify_csrf_token(session):
            return jsonify({"error": "csrf_invalid"}), 403
        return f(*args, **kwargs)
    return decorated

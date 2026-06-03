"""Password verification with bcrypt + plaintext fallback.

Approved per Q-A.4 (bcrypt cost factor 12) and Q1/Q2 (support both
DASHBOARD_PASSWORD_HASH and DASHBOARD_PASSWORD during transition).

Verification order:
  1. If DASHBOARD_PASSWORD_HASH is set: bcrypt-verify against it.
     If match → return (True, {"path": "bcrypt"}).
     If mismatch → return (False, {"path": "bcrypt"}).
  2. Else if DASHBOARD_PASSWORD is set: plaintext-compare.
     If match → return (True, {"path": "plaintext", "warning":
                                 "plaintext_fallback_in_use"}).
     If mismatch → return (False, {"path": "plaintext"}).
  3. Else: return (False, {"path": "none"}).

Critical invariants:
  * NEVER include the provided password or any password material in
    the returned dict, log statements, or exception messages.
  * NEVER include the bcrypt hash in the returned dict (the caller
    might log the dict for audit; the hash is recoverable across
    DB backups but must not appear in app logs).
  * The "warning" key tells the caller to emit a deprecation message;
    it does NOT include the password.
"""
from __future__ import annotations

import os
import logging
from typing import Tuple, Dict, Any

log = logging.getLogger(__name__)

# bcrypt cost factor — Q-A.4 approved.
BCRYPT_COST_FACTOR = 12

# Sentinel for "no password configured anywhere".
_NO_PASSWORD = {"path": "none"}


def _bcrypt_available() -> bool:
    try:
        import bcrypt  # noqa: F401
        return True
    except ImportError:
        return False


def hash_password(plaintext: str) -> str:
    """Generate a bcrypt hash with the approved cost factor.

    Used by tools/set_dashboard_password.py — NOT by request handlers.
    The plaintext is never logged. Returns the bcrypt hash string
    (which itself contains salt + cost + digest)."""
    if not _bcrypt_available():
        raise RuntimeError(
            "bcrypt not installed — install via pip install bcrypt"
        )
    import bcrypt
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext must be a non-empty string")
    salt = bcrypt.gensalt(rounds=BCRYPT_COST_FACTOR)
    return bcrypt.hashpw(plaintext.encode("utf-8"), salt).decode("utf-8")


def verify_password(provided: str) -> Tuple[bool, Dict[str, Any]]:
    """Verify a candidate password against the configured backend.

    Returns (matched, info_dict). info_dict contains:
      * "path": "bcrypt" | "plaintext" | "none"
      * "warning": optional string — emit-log-warning advisory

    Never logs the provided password. Never returns the configured
    password or its hash."""
    if not isinstance(provided, str):
        # Reject non-string inputs (e.g. JSON null, numbers). Treat as
        # mismatch with the "none" path so audit logging is honest.
        return (False, dict(_NO_PASSWORD))

    bcrypt_hash = os.getenv("DASHBOARD_PASSWORD_HASH", "").strip()
    plaintext   = os.getenv("DASHBOARD_PASSWORD", "")

    # Preferred path: bcrypt hash.
    if bcrypt_hash:
        if not _bcrypt_available():
            log.error(
                "DASHBOARD_PASSWORD_HASH is set but bcrypt is not installed; "
                "refusing to fall back to plaintext for safety."
            )
            return (False, {"path": "bcrypt", "error": "bcrypt_not_installed"})
        import bcrypt
        try:
            matched = bcrypt.checkpw(
                provided.encode("utf-8"),
                bcrypt_hash.encode("utf-8"),
            )
            return (bool(matched), {"path": "bcrypt"})
        except (ValueError, TypeError):
            # Malformed hash. Log without revealing the hash.
            log.error(
                "DASHBOARD_PASSWORD_HASH is malformed (not a valid "
                "bcrypt hash). Run tools/set_dashboard_password.py "
                "to reset."
            )
            return (False, {"path": "bcrypt", "error": "hash_malformed"})

    # Transitional fallback: plaintext compare.
    # The "changeme" default is treated as "no password configured"
    # to prevent accidental access on a fresh deploy.
    if plaintext and plaintext != "changeme":
        # Constant-time compare to mitigate timing attacks. The
        # provided string is the only secret-equivalent value, so use
        # hmac.compare_digest.
        import hmac
        matched = hmac.compare_digest(provided, plaintext)
        return (matched, {"path": "plaintext",
                           "warning": "plaintext_fallback_in_use"})

    return (False, dict(_NO_PASSWORD))


def password_configured() -> bool:
    """True iff a real password is configured via either backend.

    Used at startup to emit a clear "DASHBOARD HAS NO PASSWORD" warning
    when both env vars are empty/default — preferable to a silent
    'changeme'-accepts-anything failure mode."""
    bcrypt_hash = os.getenv("DASHBOARD_PASSWORD_HASH", "").strip()
    if bcrypt_hash:
        return True
    plaintext = os.getenv("DASHBOARD_PASSWORD", "")
    return bool(plaintext) and plaintext != "changeme"

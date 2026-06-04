"""dashboard.auth.totp — M15.3.A.2 TOTP / Google Authenticator 2FA primitives.

Public surface:
  * totp_enabled() -> bool
      Reads DASHBOARD_TOTP_SECRET; True iff set and non-empty.
  * generate_secret() -> str
      New base32 secret (20 bytes / 32 chars). Used by --enable-totp.
  * build_otpauth_uri(secret, account, issuer) -> str
      otpauth:// URI for QR encoding. NEVER logged. NEVER returned in API.
  * render_qr_terminal(uri) -> str
      Unicode-block QR for stdout. Caller is responsible for stdout
      being the operator's TTY only.
  * verify_code(provided, *, secret=None, replay_cache=None, clock=time.time)
      -> (matched, info)
      Validates a 6-digit code against the env-configured (or injected)
      secret with ±1 window tolerance. Replay cache rejects re-use of
      already-accepted time-steps within a 120-sec TTL.
  * ReplayCache
      In-memory (sha256(secret_fp), time_step) cache with TTL pruning.
      Per Q-A.10 correction: no raw codes ever stored.
  * get_default_replay_cache()
      Module-level singleton for the dashboard process.

Hard invariants (per Q-A.10 + Correction 4):
  * No raw OTP codes in memory. Cache stores time-step ints keyed by
    sha256(secret)[:16] fingerprint.
  * No code/secret/URI/QR data ever returned in info_dict.
  * verify_code accepts an injectable clock for tests.
  * Secret can be injected per-call OR read from env (default).

NOT covered by this module:
  * Login-endpoint wiring (in dashboard/app.py)
  * --enable-totp / --disable-totp tool flags (in tools/set_dashboard_password.py)
  * Audit-log integration (callers decide what audit kind to write)

Replay model per Q-A.10:
  pyotp's TOTP.verify(code, valid_window=1) accepts the current 30-sec
  window plus ±1 (90 sec total). An attacker who shoulder-surfs a code
  has up to 90 sec to use it. The replay cache prevents the SAME
  time-step+secret combination from succeeding twice within TTL — so
  a sniffed code can be used by the legitimate operator OR the attacker,
  whoever uses it first, but not both. Combined with the rate-limiter
  (5 failures → 15 min lockout), this is the M15.3.A.2 trade-off.

The cache reset on dashboard restart is the same in-memory trade-off
as the M15.3.A rate-limiter. A DB-backed variant can be added as a
future `M15.3.A.2.persist` carry-forward item if a real incident
materializes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

log = logging.getLogger(__name__)


# Time-window tolerance for pyotp.TOTP.verify (in units of 30-sec steps).
# RFC 6238 + Google Authenticator default.
VALID_WINDOW = 1

# Replay cache TTL — slightly longer than the maximum valid-window span
# (3 × 30 sec = 90 sec) to handle minor clock skew.
REPLAY_TTL_SEC = 120

# Secret-fingerprint length (truncated sha256). Long enough to avoid
# collisions across realistic operator key rotations; short enough to
# keep cache memory bounded.
SECRET_FINGERPRINT_LEN = 16


def _pyotp_available() -> bool:
    try:
        import pyotp  # noqa: F401
        return True
    except ImportError:
        return False


def totp_enabled() -> bool:
    """Returns True iff DASHBOARD_TOTP_SECRET is set to a non-empty value.

    Reads env at call time. Callers can flip TOTP on/off by setting the
    env var without restarting (the env load happens at dashboard start
    via load_dotenv; mid-process flips require operator action through
    the disable tool, which triggers a service restart)."""
    secret = os.getenv("DASHBOARD_TOTP_SECRET", "").strip()
    return bool(secret)


def _read_secret_from_env() -> str:
    return os.getenv("DASHBOARD_TOTP_SECRET", "").strip()


def generate_secret() -> str:
    """Generate a fresh base32 TOTP secret (20 bytes / 32 base32 chars).

    Returns the secret string. The caller is responsible for ensuring
    the secret never reaches stdout outside the operator's interactive
    setup terminal, and never reaches stderr / logs / API responses."""
    if not _pyotp_available():
        raise RuntimeError(
            "pyotp not installed — install via pip install pyotp"
        )
    import pyotp
    return pyotp.random_base32(length=32)


def build_otpauth_uri(secret: str, *, account_name: str = "operator",
                        issuer: str = "Algo Trader") -> str:
    """Build the otpauth:// URI per RFC 6238 / Key URI Format spec.

    Used as input to render_qr_terminal. NEVER logged. NEVER returned
    in any API response. NEVER persisted to disk (the secret in .env
    is the base32 secret, not the URI — the URI is reconstructed at
    setup time only)."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    # URI format: otpauth://totp/<issuer>:<account>?secret=...&issuer=...
    label = f"{quote(issuer)}:{quote(account_name)}"
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret}"
        f"&issuer={quote(issuer)}"
        f"&algorithm=SHA1"
        f"&digits=6"
        f"&period=30"
    )


def render_qr_terminal(otpauth_uri: str) -> str:
    """Render an otpauth:// URI as a QR code in Unicode-block characters
    suitable for stdout. Returns the rendered QR as a multi-line string.

    The caller MUST ensure the destination is the operator's interactive
    terminal — never a log file, never journald. The URI contains the
    secret in plaintext (base32), so any persistent capture of the QR
    is a credential leak."""
    try:
        import qrcode
    except ImportError as e:
        raise RuntimeError(
            "qrcode not installed — install via pip install qrcode"
        ) from e
    if not isinstance(otpauth_uri, str) or not otpauth_uri.startswith("otpauth://"):
        raise ValueError("otpauth_uri must start with otpauth://")
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=1,
    )
    qr.add_data(otpauth_uri)
    qr.make(fit=True)
    # Build Unicode-block representation. Two QR rows fit into one
    # terminal row via half-block characters (U+2580 ▀, U+2584 ▄,
    # U+2588 █). Result: ~25 lines × 25 columns for a typical secret.
    matrix = qr.get_matrix()
    rows = len(matrix)
    out_lines = []
    # Pad to even number of rows.
    if rows % 2 == 1:
        matrix.append([False] * len(matrix[0]))
        rows += 1
    for r in range(0, rows, 2):
        line = []
        for c in range(len(matrix[0])):
            top = matrix[r][c]
            bot = matrix[r + 1][c]
            if top and bot:
                line.append("█")
            elif top and not bot:
                line.append("▀")
            elif not top and bot:
                line.append("▄")
            else:
                line.append(" ")
        out_lines.append("".join(line))
    return "\n".join(out_lines)


def _secret_fingerprint(secret: str) -> str:
    """sha256-truncated fingerprint of the secret. Used as the cache
    key. Never the raw secret itself — protects against memory-dump
    secret leaks."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:SECRET_FINGERPRINT_LEN]


class ReplayCache:
    """In-memory (secret_fp, time_step) cache with TTL pruning.

    Per Q-A.10 correction: no raw OTP codes ever stored. The cache
    key is (sha256(secret)[:16], time_step) where time_step is an
    integer (typically `now // 30`). Reset on process restart — same
    trade-off as the M15.3.A rate-limiter (in-memory by design).
    """

    def __init__(self, *, ttl_sec: int = REPLAY_TTL_SEC,
                  clock=time.time):
        if ttl_sec < 1:
            raise ValueError("ttl_sec must be >= 1")
        self._ttl = ttl_sec
        self._clock = clock
        self._used: Dict[Tuple[str, int], float] = {}

    def _prune(self) -> None:
        """Drop entries older than TTL. Called before each lookup."""
        now = self._clock()
        cutoff = now - self._ttl
        for k in list(self._used.keys()):
            if self._used[k] < cutoff:
                del self._used[k]

    def is_replay(self, secret: str, time_step: int) -> bool:
        """True iff (secret_fp, time_step) was accepted within TTL."""
        if not isinstance(secret, str) or not secret:
            return False
        self._prune()
        key = (_secret_fingerprint(secret), int(time_step))
        return key in self._used

    def record_accepted(self, secret: str, time_step: int) -> None:
        """Mark (secret_fp, time_step) as used."""
        if not isinstance(secret, str) or not secret:
            return
        key = (_secret_fingerprint(secret), int(time_step))
        self._used[key] = self._clock()

    def size(self) -> int:
        """Cache entry count — exposed for tests/diagnostics."""
        self._prune()
        return len(self._used)

    def clear(self) -> None:
        """Test/admin hook."""
        self._used.clear()


# Module-level singleton for dashboard process.
_default_replay_cache: Optional[ReplayCache] = None


def get_default_replay_cache() -> ReplayCache:
    global _default_replay_cache
    if _default_replay_cache is None:
        _default_replay_cache = ReplayCache()
    return _default_replay_cache


def reset_default_replay_cache() -> None:
    """Test helper — drop the singleton so the next get_*() rebuilds."""
    global _default_replay_cache
    _default_replay_cache = None


def verify_code(
    provided: str,
    *,
    secret: Optional[str] = None,
    replay_cache: Optional[ReplayCache] = None,
    clock=time.time,
) -> Tuple[bool, Dict[str, Any]]:
    """Verify a 6-digit TOTP code.

    Args:
      provided: the 6-digit code string from the login form.
      secret: base32 TOTP secret. If None, read from DASHBOARD_TOTP_SECRET.
      replay_cache: ReplayCache instance. If None, uses the module singleton.
      clock: callable -> unix-ts. Default time.time. Injectable for tests.

    Returns (matched, info_dict). info_dict contains:
      * "reason": "wrong_format" | "no_secret" | "pyotp_not_installed"
                  | "wrong_code" | "replay" | "ok"
      * "window": int in {-1, 0, 1} on success — which time-window matched
      * NEVER contains the provided code, the secret, the otpauth URI,
        or any password/session material.

    Constant-time mismatch is delegated to pyotp.TOTP.verify, which uses
    HMAC-SHA1 + constant-time compare internally.
    """
    # 1. Validate input shape.
    if not isinstance(provided, str):
        return (False, {"reason": "wrong_format"})
    provided = provided.strip()
    if not provided.isdigit() or len(provided) != 6:
        return (False, {"reason": "wrong_format"})

    # 2. Get secret.
    if secret is None:
        secret = _read_secret_from_env()
    if not secret:
        return (False, {"reason": "no_secret"})

    # 3. pyotp must be installed.
    if not _pyotp_available():
        log.error(
            "DASHBOARD_TOTP_SECRET is set but pyotp is not installed; "
            "refusing TOTP verification for safety."
        )
        return (False, {"reason": "pyotp_not_installed"})
    import pyotp
    totp = pyotp.TOTP(secret)

    # 4. Compute the time-step that the provided code would correspond
    #    to. We need to figure out WHICH window matched so we can store
    #    the right time-step in the replay cache.
    now = clock()
    # pyotp's window arithmetic: timecode(now) = int(now / 30) for the
    # default 30-sec period. We probe ±VALID_WINDOW.
    base_step = int(now // 30)
    matched_step: Optional[int] = None
    matched_window: Optional[int] = None
    for delta in range(-VALID_WINDOW, VALID_WINDOW + 1):
        candidate_step = base_step + delta
        expected_at = candidate_step * 30
        # pyotp.TOTP.at() generates the code for a specific timestamp.
        expected_code = totp.at(expected_at)
        # Constant-time compare to mitigate timing attacks. Both are
        # 6-digit strings so the comparison cost is uniform.
        if _ct_eq(provided, expected_code):
            matched_step = candidate_step
            matched_window = delta
            break
    if matched_step is None:
        return (False, {"reason": "wrong_code"})

    # 5. Replay check — reject if this (secret_fp, time_step) was
    #    already accepted within TTL.
    if replay_cache is None:
        replay_cache = get_default_replay_cache()
    if replay_cache.is_replay(secret, matched_step):
        return (False, {"reason": "replay"})

    # 6. Record acceptance + success.
    replay_cache.record_accepted(secret, matched_step)
    return (True, {"reason": "ok", "window": matched_window})


def _ct_eq(a: str, b: str) -> bool:
    """Constant-time string equality. Both inputs must be strings."""
    import hmac
    return hmac.compare_digest(str(a), str(b))


def current_time_step(*, clock=time.time) -> int:
    """Returns the current pyotp 30-sec time-step. For tests only."""
    return int(clock() // 30)

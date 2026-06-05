"""dashboard/auth/trusted_proxy.py — XFF trust-boundary (audit P0-1).

Resolves the real client IP for rate-limiting and audit logging.
Mitigates the M1–M16 audit P0-1 finding: the previous
`_m153a_client_ip()` blindly took `X-Forwarded-For[0]` from any
caller, so an attacker could rotate the spoofed XFF on every request
to bypass the per-IP login rate limiter and corrupt audit IPs.

Trust-boundary policy
─────────────────────

A request's `X-Forwarded-For` header is trusted ONLY when
`request.remote_addr` is in the env-configured allowlist
`DASHBOARD_TRUSTED_PROXIES` (comma-separated). Default allowlist is
`127.0.0.1,::1` — matching the M15.3.A.cutover deployment of
Caddy-on-same-host bound to loopback.

When `remote_addr` is trusted:
  * Honour `X-Forwarded-For`.
  * Use the LAST entry, not the first. The last entry is the hop
    immediately before our trusted proxy — i.e. the actual client.
    Earlier entries were attacker-supplied at the time they were
    added and CANNOT be trusted even when the final hop is.
  * If XFF is empty, malformed, or contains no usable IP, fall back
    to `request.remote_addr`.

When `remote_addr` is NOT trusted:
  * Ignore XFF entirely. Use `request.remote_addr`. This is the only
    way to defeat per-request XFF spoofing from an untrusted source.

The function is intentionally tolerant on malformed input (empty
strings, whitespace) and returns "" only when no IP is derivable
from any source — never raises.

Tested in test_m15_3_a_dashboard_auth_xff.py.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional


# Default allowlist — loopback (IPv4 + IPv6). Matches the
# M15.3.A.cutover Caddy-on-same-host deployment.
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = ("127.0.0.1", "::1")


def _read_trusted_proxies_env() -> tuple[str, ...]:
    """Parse the DASHBOARD_TRUSTED_PROXIES env var.

    Comma-separated. Whitespace tolerant. Empty/missing → defaults.
    No IP validation here — the strings are compared literally with
    `request.remote_addr`, which Flask gives us as a stringified
    address; an unparseable entry just never matches.
    """
    raw = os.getenv("DASHBOARD_TRUSTED_PROXIES", "").strip()
    if not raw:
        return DEFAULT_TRUSTED_PROXIES
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts if parts else DEFAULT_TRUSTED_PROXIES


def _is_trusted_proxy(remote_addr: Optional[str],
                       trusted: Iterable[str]) -> bool:
    """True iff remote_addr is in the trusted set. Defensive about
    None / empty inputs (treated as untrusted)."""
    if not remote_addr:
        return False
    return remote_addr in trusted


def _pick_last_real_ip_from_xff(xff_header: str) -> Optional[str]:
    """Extract the last non-empty entry from an XFF header.

    XFF format: `client, proxy1, proxy2`. The leftmost entry is the
    original client AS DECLARED — but anyone in the path can prepend
    a fake entry. The rightmost entry is the host that hit our
    trusted proxy. When the trusted proxy adds the real client IP,
    the rightmost entry is the most reliable.

    Returns None if no usable entry is present.
    """
    if not xff_header:
        return None
    parts = [p.strip() for p in xff_header.split(",")]
    parts = [p for p in parts if p]   # drop empties
    if not parts:
        return None
    return parts[-1]


def resolve_client_ip(
    *,
    remote_addr: Optional[str],
    xff_header: Optional[str],
    trusted_proxies: Optional[Iterable[str]] = None,
) -> str:
    """Resolve the client IP for rate-limiting + audit purposes.

    Pure function — accepts injected `remote_addr`, `xff_header`,
    and `trusted_proxies` so unit tests don't need a Flask request
    context.

    Returns the resolved IP as a string, or "" if no IP is
    derivable from any source.

    Policy
    ──────

    1. If `remote_addr` is in `trusted_proxies` AND `xff_header` is
       non-empty: return the LAST non-empty XFF entry. (If XFF
       contains no usable entries, fall through to remote_addr.)
    2. Otherwise (untrusted source, or no XFF): return
       `remote_addr` (or "" if also missing).
    """
    trusted = (tuple(trusted_proxies) if trusted_proxies is not None
                else _read_trusted_proxies_env())

    if _is_trusted_proxy(remote_addr, trusted):
        last = _pick_last_real_ip_from_xff(xff_header or "")
        if last:
            return last
        # XFF header was empty or malformed — fall through.

    return remote_addr or ""


__all__ = [
    "DEFAULT_TRUSTED_PROXIES",
    "resolve_client_ip",
]

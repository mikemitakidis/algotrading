"""
bot/etoro/nonce.py — M13.5.B per-payload nonce.

The operator must echo a per-payload nonce before any live POST is
emitted. A static phrase is not acceptable (M13.4B §10).

Design:
  - Nonce digest binds (canonical_payload, timestamp_ms).
  - 8-char hex digest displayed to the operator (sha256 first 8).
  - Operator echoes 'CONFIRM <NONCE>' exactly.
  - TTL: 60 seconds by default; configurable per call.
  - Single-use: once consumed by validate() it is rejected on replay.
  - The store is in-process only; CLI exits after one POST, so a
    long-lived store is unnecessary.

This module has no I/O, no network, no DB writes. It is pure logic
plus an in-memory set. Tests use injectable clock for determinism.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Set


Clock = Callable[[], float]


def canonical_payload(payload: dict) -> str:
    """Stable JSON serialisation for digesting.

    Sort keys, no extra whitespace, ensure ASCII so the digest is
    reproducible across machines/runs."""
    if not isinstance(payload, dict):
        raise TypeError("canonical_payload requires a dict")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def compute_digest(payload: dict, timestamp_ms: int) -> str:
    """Return the 8-char hex digest used as the operator-echoed nonce."""
    if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool):
        raise TypeError("timestamp_ms must be int")
    if timestamp_ms < 0:
        raise ValueError("timestamp_ms must be non-negative")
    canon = canonical_payload(payload)
    h = hashlib.sha256()
    h.update(canon.encode("ascii"))
    h.update(b"|")
    h.update(str(timestamp_ms).encode("ascii"))
    return h.hexdigest()[:8]


@dataclass
class NonceRecord:
    """A nonce issued for a specific payload at a specific time."""
    digest: str
    payload_canon: str
    issued_at_ms: int
    ttl_seconds: int = 60


@dataclass
class NonceStore:
    """In-memory single-use nonce tracker.

    Not thread-safe — the CLI is single-threaded. Tests inject `clock`."""
    clock: Clock = field(default=lambda: time.time())
    _issued: dict = field(default_factory=dict)   # digest -> NonceRecord
    _consumed: Set[str] = field(default_factory=set)

    def issue(self, payload: dict, ttl_seconds: int = 60,
              timestamp_ms: Optional[int] = None) -> NonceRecord:
        """Issue a fresh nonce bound to `payload`.

        Returns a NonceRecord. The digest is what the operator must
        echo (as `CONFIRM <digest>`)."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if timestamp_ms is None:
            timestamp_ms = int(self.clock() * 1000)
        canon = canonical_payload(payload)
        digest = compute_digest(payload, timestamp_ms)
        rec = NonceRecord(
            digest=digest,
            payload_canon=canon,
            issued_at_ms=timestamp_ms,
            ttl_seconds=ttl_seconds,
        )
        self._issued[digest] = rec
        return rec

    def validate(self, echoed: str, payload: dict) -> tuple[bool, str]:
        """Validate an operator-echoed confirmation against the payload.

        Returns (ok, reason). On ok=True, the nonce is consumed and
        cannot be re-used.

        Reasons:
          - "ok"
          - "format_invalid"        — echoed string not 'CONFIRM <8hex>'
          - "nonce_unknown"         — never issued
          - "nonce_expired"         — past TTL
          - "nonce_consumed"        — replayed
          - "payload_mismatch"      — payload changed since issue
        """
        if not isinstance(echoed, str):
            return False, "format_invalid"
        parts = echoed.strip().split()
        if len(parts) != 2 or parts[0] != "CONFIRM":
            return False, "format_invalid"
        digest = parts[1].strip()
        if len(digest) != 8 or not all(c in "0123456789abcdef" for c in digest):
            return False, "format_invalid"

        if digest in self._consumed:
            return False, "nonce_consumed"
        rec = self._issued.get(digest)
        if rec is None:
            return False, "nonce_unknown"

        # Payload binding check — recompute against the canonical
        # serialisation of the supplied payload.
        try:
            canon_now = canonical_payload(payload)
        except (TypeError, ValueError):
            return False, "payload_mismatch"
        if canon_now != rec.payload_canon:
            return False, "payload_mismatch"

        # TTL check (use the same clock used at issue time).
        now_ms = int(self.clock() * 1000)
        age_ms = now_ms - rec.issued_at_ms
        if age_ms < 0:
            # Clock skew — treat as unknown rather than valid.
            return False, "nonce_expired"
        if age_ms > rec.ttl_seconds * 1000:
            return False, "nonce_expired"

        # Consume.
        self._consumed.add(digest)
        del self._issued[digest]
        return True, "ok"


__all__ = [
    "canonical_payload",
    "compute_digest",
    "NonceRecord",
    "NonceStore",
]

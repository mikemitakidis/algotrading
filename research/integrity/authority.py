"""research.integrity.authority — separately held signing authority (local model).

Integrity decisions that a research process must not be able to grant itself
(releasing a sealed holdout, attesting when a pre-registration was frozen)
require a signature from an AUTHORITY whose key the research process does
not hold. Research code is only ever given a ``Verifier``.

Local-model caveat: this uses HMAC-SHA256 from the standard library, so the
verifier also holds the shared key. In the operational design the verifier
side would hold only a public key (asymmetric signature) or call the
authority's API, and the key would live outside every research runner. The
policy logic (what must be signed, what is checked) is what this module and
its tests pin down; it is NOT itself the security boundary until connected
to such a separately permissioned authority (PR #5 review, P1).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict

from research.common.hashing import canonical_json

TRUST_AUTHORITY_SIGNED = "authority_signed"
TRUST_UNTRUSTED_DECLARED = "untrusted_declared"


class Authority:
    """Signer. Lives with the operator / an independent CI verifier, never in a research runner."""

    def __init__(self, authority_id: str, key: bytes):
        if not authority_id or len(key) < 16:
            raise ValueError("authority needs an id and a key of at least 16 bytes")
        self.authority_id = authority_id
        self._key = bytes(key)

    def sign(self, purpose: str, payload: Dict[str, Any]) -> str:
        msg = canonical_json({"purpose": purpose, "authority": self.authority_id, "payload": payload})
        return hmac.new(self._key, msg.encode("utf-8"), hashlib.sha256).hexdigest()

    def verifier(self) -> "Verifier":
        return Verifier(self.authority_id, self._key)


class Verifier:
    """What research code receives: can check, cannot (by policy) be used to sign."""

    def __init__(self, authority_id: str, key: bytes):
        self.authority_id = authority_id
        self._key = bytes(key)

    def verify(self, purpose: str, payload: Dict[str, Any], signature: str) -> bool:
        if not isinstance(signature, str) or not signature:
            return False
        msg = canonical_json({"purpose": purpose, "authority": self.authority_id, "payload": payload})
        good = hmac.new(self._key, msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, signature)


__all__ = ["TRUST_AUTHORITY_SIGNED", "TRUST_UNTRUSTED_DECLARED", "Authority", "Verifier"]

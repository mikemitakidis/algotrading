"""M21 WP14 — operator approval tokens (MOCK service, local HMAC model).

An approval is a token bound to ONE proposal: its id, the digest of its exact
content, its tier, the approver and an expiry. The service that issues
tokens holds the key (operator side); the promotion book only receives a
``ApprovalVerifier``. A token for another proposal, another content digest,
a lower tier, after expiry, or signed with another key does not verify.
Automation cannot approve (the same non-human names WP9 refuses).

This is a local policy model, like ``research.integrity.authority``: whoever
holds the verifier key could forge tokens, so it proves policy compliance
inside a trusted process, NOT independent authorisation. A production
approval needs the dashboard's TOTP-authenticated operator session (a later,
approved dashboard change).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional, Tuple

from bot.promotion.tiers import _RANK
from bot.templates.controls import NON_HUMAN_APPROVERS

MODEL = "local_hmac_mock_approval_service"


@dataclass(frozen=True)
class ApprovalToken:
    proposal_id: str
    proposal_digest: str
    tier: str
    approver: str
    issued_utc: str
    expires_utc: str
    service_id: str
    signature: str = ""

    def body(self) -> dict:
        d = asdict(self)
        d.pop("signature")
        return d


def _sign(key: bytes, body: dict) -> str:
    return hmac.new(key, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


class ApprovalService:
    def __init__(self, service_id: str, key: bytes):
        if not service_id or len(key) < 16:
            raise ValueError("approval service needs an id and a key of at least 16 bytes")
        self.service_id, self._key = service_id, bytes(key)

    def issue(self, *, proposal_id: str, proposal_digest: str, tier: str, approver: str, issued: datetime,
              expires: datetime) -> ApprovalToken:
        if not str(approver or "").strip() or str(approver).strip().lower() in NON_HUMAN_APPROVERS:
            raise PermissionError("approval must come from a named human operator")
        if expires <= issued:
            raise ValueError("expiry must be after issue")
        t = ApprovalToken(proposal_id, proposal_digest, tier, approver, issued.isoformat(), expires.isoformat(),
                          self.service_id)
        return ApprovalToken(**{**t.body(), "signature": _sign(self._key, t.body())})

    def verifier(self) -> "ApprovalVerifier":
        return ApprovalVerifier(self.service_id, self._key)


class ApprovalVerifier:
    model = MODEL
    independent = False

    def __init__(self, service_id: str, key: bytes):
        self.service_id, self._key = service_id, bytes(key)

    def verify(self, token: Optional[ApprovalToken], *, proposal_id: str, proposal_digest: str, tier: str,
               now: datetime) -> Tuple[bool, str]:
        if token is None:
            return False, "no_token"
        if token.service_id != self.service_id:
            return False, "wrong_service"
        if not hmac.compare_digest(_sign(self._key, token.body()), token.signature or ""):
            return False, "bad_signature"
        if token.proposal_id != proposal_id or token.proposal_digest != proposal_digest:
            return False, "token_for_another_proposal"
        if _RANK.get(token.tier, -1) < _RANK[tier]:
            return False, "token_tier_%s_below_%s" % (token.tier, tier)
        if str(token.approver).strip().lower() in NON_HUMAN_APPROVERS:
            return False, "non_human_approver"
        if now > datetime.fromisoformat(token.expires_utc) or now < datetime.fromisoformat(token.issued_utc):
            return False, "token_outside_validity"
        return True, "ok"


__all__ = ["MODEL", "ApprovalToken", "ApprovalService", "ApprovalVerifier"]

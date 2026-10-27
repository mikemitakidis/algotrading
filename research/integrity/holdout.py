"""research.integrity.holdout — sealed holdout with a one-use, authority-approved release log.

Design (Issue #2 critique #3): the holdout must live in a SEPARATELY
PERMISSIONED store and workflow that research runners and agents cannot
read; CODEOWNERS is not a security boundary. This module implements the
policy that such a store enforces, so it can be tested offline:

* the vault splits a time-indexed dataset at ``seal_ts``. Research code gets
  only ``research_view()`` (strictly before the seal).
* ``release`` hands the sealed part to ONE frozen spec of ONE family, once.
  It requires (PR #5 review, P1):
    - a pre-registration whose ``family_id`` AND ``spec_digest`` both match
      the release request (a pre-registration from family A can never open
      the holdout for family B);
    - an authority-signed (trusted) attestation of that pre-registration
      that predates the release;
    - an approval token signed by the separately held release authority
      over the exact request (vault, family, spec, pre-registration,
      requester); a caller-supplied approver name is not an access control;
    - the requester is not the authority;
    - the family AND the pre-registration have not been used before.
* Every denied attempt is ledgered (``holdout_access_denied``).

Local-model caveat: in production the sealed frame never enters a research
process and the authority key never exists there (see authority.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, Optional

import pandas as pd

from research.common.hashing import frame_digest
from research.integrity.authority import Verifier
from research.integrity.ledger import Ledger, LedgerError
from research.integrity.prereg import trusted_attestations

RELEASE_PURPOSE = "holdout_release"


class HoldoutDenied(PermissionError):
    pass


def release_request(vault: str, family_id: str, spec_digest: str, prereg_digest: str, requester: str) -> Dict:
    """The exact payload the release authority signs."""
    return {"vault": vault, "family_id": family_id, "spec_digest": spec_digest, "prereg_digest": prereg_digest,
            "requester": requester}


def _utc_now() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


class HoldoutVault:
    def __init__(self, ledger: Ledger, name: str, frame: pd.DataFrame, seal_ts_utc: str, *,
                 authority: Verifier, ts_column: str = "ts_utc", clock: Callable[[], str] = _utc_now):
        ts = pd.to_datetime(frame[ts_column], utc=True)
        self._seal = pd.Timestamp(seal_ts_utc)
        if self._seal.tzinfo is None:
            raise ValueError("seal timestamp must be timezone-aware")
        if authority is None:
            raise ValueError("a holdout vault needs a release-authority verifier")
        self.name = name
        self._ledger = ledger
        self._authority = authority
        self._clock = clock
        self._research = frame.loc[ts < self._seal].reset_index(drop=True)
        self._sealed = frame.loc[ts >= self._seal].reset_index(drop=True)
        self.sealed_digest = frame_digest(self._sealed)
        if ledger.entries("holdout_seal", name):
            raise LedgerError("holdout %r already sealed" % name)
        ledger.append("holdout_seal", {"seal_ts_utc": self._seal.isoformat(), "sealed_rows": len(self._sealed),
                                       "research_rows": len(self._research), "sealed_digest": self.sealed_digest,
                                       "authority": authority.authority_id}, ref_id=name)

    def research_view(self) -> pd.DataFrame:
        return self._research.copy()

    def _deny(self, why: str, **ctx) -> None:
        self._ledger.append("holdout_access_denied", {"reason": why, **ctx}, ref_id=self.name)
        raise HoldoutDenied(why)

    def releases(self) -> Dict[str, Dict]:
        return {e.payload["family_id"]: e.payload for e in self._ledger.entries("holdout_release", self.name)}

    def release(self, *, family_id: str, spec_digest: str, prereg_digest: str, requester: str,
                approval_token: Optional[str]) -> pd.DataFrame:
        ctx = {"family_id": family_id, "spec_digest": spec_digest, "prereg_digest": prereg_digest,
               "requester": requester}
        req = release_request(self.name, family_id, spec_digest, prereg_digest, requester)
        if not approval_token or not self._authority.verify(RELEASE_PURPOSE, req, approval_token):
            self._deny("no valid release-authority approval for this exact request", **ctx)
        if requester == self._authority.authority_id:
            self._deny("the release authority cannot request a release for itself", **ctx)
        rows = self._ledger.entries("prereg", prereg_digest)
        if not rows:
            self._deny("no pre-registration for this release", **ctx)
        pre = rows[0].payload
        if pre["family_id"] != family_id:
            self._deny("pre-registration belongs to family %r, not %r" % (pre["family_id"], family_id), **ctx)
        if pre["spec_digest"] != spec_digest:
            self._deny("pre-registration is for a different spec", **ctx)
        att = trusted_attestations(self._ledger, prereg_digest)
        now = datetime.fromisoformat(self._clock())
        if not att or min(datetime.fromisoformat(a.payload["server_timestamp_utc"]) for a in att) > now:
            self._deny("pre-registration lacks a trusted attestation that predates the release", **ctx)
        if family_id in self.releases():
            self._deny("holdout already spent for this family", **ctx)
        if any(r.get("prereg_digest") == prereg_digest for r in self.releases().values()):
            self._deny("this pre-registration already opened the holdout", **ctx)
        self._ledger.append("holdout_release", {**ctx, "approval_token": approval_token,
                                                "authority": self._authority.authority_id,
                                                "sealed_digest": self.sealed_digest}, ref_id=self.name)
        return self._sealed.copy()


__all__ = ["RELEASE_PURPOSE", "HoldoutDenied", "HoldoutVault", "release_request"]

"""research.integrity.holdout — sealed holdout with a one-use release log.

Design (Issue #2 critique #3): the holdout must live in a SEPARATELY
PERMISSIONED store and workflow that research runners and agents cannot
read; CODEOWNERS is not a security boundary. This module implements the
policy that such a store enforces, so it can be tested offline:

* ``seal`` splits a time-indexed dataset at ``seal_ts``. Research code gets
  only ``research_view()`` (strictly before the seal).
* ``release`` hands the sealed part to ONE frozen spec of ONE family, once,
  with requester and approver recorded; requester and approver must differ.
* Every denied attempt is ledgered too (``holdout_access_denied``).

In production use the sealed frame would never be loaded into a research
process at all; here the vault object stands in for that store.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from research.common.hashing import frame_digest
from research.integrity.ledger import Ledger, LedgerError


class HoldoutDenied(PermissionError):
    pass


class HoldoutVault:
    def __init__(self, ledger: Ledger, name: str, frame: pd.DataFrame, seal_ts_utc: str,
                 ts_column: str = "ts_utc"):
        ts = pd.to_datetime(frame[ts_column], utc=True)
        self._seal = pd.Timestamp(seal_ts_utc)
        if self._seal.tzinfo is None:
            raise ValueError("seal timestamp must be timezone-aware")
        self.name = name
        self._ledger = ledger
        self._research = frame.loc[ts < self._seal].reset_index(drop=True)
        self._sealed = frame.loc[ts >= self._seal].reset_index(drop=True)
        self.sealed_digest = frame_digest(self._sealed)
        if ledger.entries("holdout_seal", name):
            raise LedgerError("holdout %r already sealed" % name)
        ledger.append("holdout_seal", {"seal_ts_utc": self._seal.isoformat(), "sealed_rows": len(self._sealed),
                                       "research_rows": len(self._research),
                                       "sealed_digest": self.sealed_digest}, ref_id=name)

    def research_view(self) -> pd.DataFrame:
        return self._research.copy()

    def _deny(self, why: str, **ctx) -> None:
        self._ledger.append("holdout_access_denied", {"reason": why, **ctx}, ref_id=self.name)
        raise HoldoutDenied(why)

    def releases(self) -> Dict[str, Dict]:
        return {e.payload["family_id"]: e.payload for e in self._ledger.entries("holdout_release", self.name)}

    def release(self, *, family_id: str, spec_digest: str, prereg_digest: str, requester: str,
                approver: Optional[str]) -> pd.DataFrame:
        ctx = {"family_id": family_id, "spec_digest": spec_digest, "requester": requester,
               "approver": approver or ""}
        if not approver:
            self._deny("release needs an explicit approver", **ctx)
        if approver == requester:
            self._deny("requester cannot approve their own release", **ctx)
        if not self._ledger.entries("prereg", prereg_digest):
            self._deny("no pre-registration for this release", **ctx)
        if self._ledger.entries("prereg", prereg_digest)[0].payload["spec_digest"] != spec_digest:
            self._deny("pre-registration is for a different spec", **ctx)
        if family_id in self.releases():
            self._deny("holdout already spent for this family", **ctx)
        self._ledger.append("holdout_release", {**ctx, "prereg_digest": prereg_digest,
                                                "sealed_digest": self.sealed_digest}, ref_id=self.name)
        return self._sealed.copy()


__all__ = ["HoldoutDenied", "HoldoutVault"]

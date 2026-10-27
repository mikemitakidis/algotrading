"""research.integrity.prereg — pre-registration with one-shot evaluation.

A pre-registration freezes WHAT will be tested before the data exists: the
spec digest, the metric, the decision threshold, the minimum effective sample
and the start of the forward window. Its canonical digest is the identifier.

Time proof (Issue #2 critique #3): Git commit/tag dates and the ledger's own
clock are client-set and prove nothing about time. A pre-registration only
counts as out-of-sample once an ATTESTATION records a server-side timestamp
for its digest (e.g. the `created_at` of a GitHub issue or PR comment that
contains the digest, read back from the API) and that timestamp precedes the
forward window. Posting the digest is a manual, reviewer-visible step.

Trust (PR #5 review, P1): an attestation is only TRUSTED when it carries a
signature from a separately held authority (``research.integrity.authority``)
over the digest, timestamp, source and evidence URL — e.g. an independent CI
verifier that read the GitHub comment's ``created_at`` through the API.
Attestations typed in by a caller are recorded as ``untrusted_declared`` and
can never make an evaluation pass (``require_trusted`` defaults to True).

The stored ``trust`` label is NEVER believed on its own (second review, P1):
``trusted_attestations`` re-verifies every attestation's signature against its
stored payload with the consumer's verifier at the moment of use, so an entry
appended directly to the ledger with ``trust='authority_signed'`` and a junk
or misplaced signature is ignored. Without a verifier nothing is trusted.

Until connected to an operational, independently secured authority this
module is a local policy model (``authority.LOCAL_POLICY_MODEL``), not proof
that a research agent could not see future data; outcomes record which
authority model verified them.

One-shot: a pre-registration can be evaluated exactly once. A second
evaluation is refused and the refusal is visible in the ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from research.common.hashing import digest
from research.integrity.authority import TRUST_AUTHORITY_SIGNED, TRUST_UNTRUSTED_DECLARED, Verifier
from research.integrity.ledger import Ledger, LedgerError

ATTESTATION_PURPOSE = "prereg_attestation"

COMPARATORS = (">", ">=", "<", "<=")


@dataclass(frozen=True)
class PreRegistration:
    family_id: str
    hypothesis_id: str
    spec_digest: str
    metric: str                      # e.g. "mean_net_R_lower95" or "dsr"
    comparator: str                  # ">", ">=", "<", "<="
    threshold: float
    min_effective_n: int
    forward_window_start_utc: str    # ISO-8601; data before this is NOT forward data
    analysis_plan: str               # free text: exactly how the metric is computed
    max_evaluations: int = 1

    def __post_init__(self):
        if self.comparator not in COMPARATORS:
            raise ValueError("comparator must be one of %s" % (COMPARATORS,))
        if self.max_evaluations != 1:
            raise ValueError("pre-registrations are one-shot (max_evaluations must be 1)")
        datetime.fromisoformat(self.forward_window_start_utc)

    @property
    def digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class PreregOutcome:
    prereg_digest: str
    passed: bool
    value: float
    effective_n: int
    reason: str
    authority_model: str = ""          # which verifier re-checked the attestation ("" = none)
    authority_independent: bool = False


def register(ledger: Ledger, p: PreRegistration) -> str:
    if not ledger.entries("hypothesis", p.hypothesis_id):
        raise LedgerError("pre-registration references unknown hypothesis %s" % p.hypothesis_id)
    d = p.digest
    if ledger.entries("prereg", d):
        raise LedgerError("pre-registration %s already exists" % d[:12])
    ledger.append("prereg", asdict(p), ref_id=d)
    return d


def attestation_payload(prereg_digest: str, server_timestamp_utc: str, source: str, evidence_url: str) -> Dict[str, Any]:
    return {"prereg_digest": prereg_digest, "server_timestamp_utc": server_timestamp_utc, "source": source,
            "evidence_url": evidence_url}


def attest(ledger: Ledger, prereg_digest: str, *, server_timestamp_utc: str, source: str,
           evidence_url: str, signature: str = "", verifier: Optional[Verifier] = None) -> str:
    """Record a server-side timestamp for a pre-registration digest; returns its trust level.

    Without a verifier and a valid authority signature the attestation is
    stored as ``untrusted_declared``. An invalid signature is refused. This
    function never contacts the network.
    """
    if not ledger.entries("prereg", prereg_digest):
        raise LedgerError("unknown pre-registration %s" % prereg_digest[:12])
    datetime.fromisoformat(server_timestamp_utc)
    payload = attestation_payload(prereg_digest, server_timestamp_utc, source, evidence_url)
    trust, authority = TRUST_UNTRUSTED_DECLARED, ""
    if verifier is not None or signature:
        if verifier is None or not verifier.verify(ATTESTATION_PURPOSE, payload, signature):
            raise LedgerError("attestation signature does not verify")
        trust, authority = TRUST_AUTHORITY_SIGNED, verifier.authority_id
    ledger.append("attestation", {**payload, "trust": trust, "authority": authority, "signature": signature},
                  ref_id=prereg_digest)
    return trust


def trusted_attestations(ledger: Ledger, prereg_digest: str, verifier: Optional[Verifier]):
    """Attestations whose signature RE-VERIFIES now, with this verifier, over
    the stored payload. The stored ``trust`` label alone counts for nothing."""
    if verifier is None:
        return []
    out = []
    for a in ledger.entries("attestation", prereg_digest):
        p = a.payload
        try:
            payload = attestation_payload(p["prereg_digest"], p["server_timestamp_utc"], p["source"],
                                          p["evidence_url"])
        except KeyError:
            continue
        if p.get("prereg_digest") != prereg_digest or p.get("authority") != verifier.authority_id:
            continue
        if verifier.verify(ATTESTATION_PURPOSE, payload, p.get("signature", "")):
            out.append(a)
    return out


def _compare(v: float, op: str, thr: float) -> bool:
    return {">": v > thr, ">=": v >= thr, "<": v < thr, "<=": v <= thr}[op]


def evaluate_once(ledger: Ledger, prereg_digest: str, *, value: float, effective_n: int,
                  data_first_ts_utc: str, data_digest: str, require_trusted: bool = True,
                  attestation_verifier: Optional[Verifier] = None) -> PreregOutcome:
    """Evaluate a pre-registration exactly once, fail-closed. Only attestations
    whose signature re-verifies with ``attestation_verifier`` count; without a
    verifier nothing is trusted. require_trusted=False is a local diagnostic
    whose outcome never passes."""
    rows = ledger.entries("prereg", prereg_digest)
    if not rows:
        raise LedgerError("unknown pre-registration %s" % prereg_digest[:12])
    if ledger.entries("prereg_evaluation", prereg_digest):
        raise LedgerError("pre-registration %s was already evaluated (one-shot)" % prereg_digest[:12])
    p = rows[0].payload
    all_att = ledger.entries("attestation", prereg_digest)
    att = trusted_attestations(ledger, prereg_digest, attestation_verifier)
    window = datetime.fromisoformat(p["forward_window_start_utc"])
    first = datetime.fromisoformat(data_first_ts_utc)
    reason = ""
    if not all_att:
        reason = "no server-side attestation: not provably out-of-sample"
    elif not att and require_trusted:
        reason = ("attestation is declared, not independently verified: local diagnostic only"
                  if attestation_verifier is not None else
                  "no attestation verifier supplied: attestations cannot be re-verified, local diagnostic only")
    elif not att:
        reason = "untrusted attestation (require_trusted=False): local diagnostic only, cannot pass"
    elif min(datetime.fromisoformat(a.payload["server_timestamp_utc"]) for a in att) >= window:
        reason = "attested after the forward window started: not out-of-sample"
    elif first < window:
        reason = "evaluation data starts before the forward window"
    elif effective_n < p["min_effective_n"]:
        reason = "effective N %d below pre-registered minimum %d" % (effective_n, p["min_effective_n"])
    passed = (not reason) and _compare(float(value), p["comparator"], float(p["threshold"]))
    if not reason:
        reason = "metric %s %s %s: %s" % (p["metric"], p["comparator"], p["threshold"],
                                          "met" if passed else "not met")
    model = getattr(attestation_verifier, "model", "") if att else ""
    indep = bool(getattr(attestation_verifier, "independent", False)) if att else False
    if passed and not indep:
        reason += " (attestation re-verified by %s; not an independent authority)" % model
    out = PreregOutcome(prereg_digest, bool(passed), float(value), int(effective_n), reason, model, indep)
    ledger.append("prereg_evaluation", {**asdict(out), "data_digest": data_digest,
                                        "data_first_ts_utc": data_first_ts_utc}, ref_id=prereg_digest)
    return out


__all__ = ["COMPARATORS", "ATTESTATION_PURPOSE", "PreRegistration", "PreregOutcome", "register",
           "attestation_payload", "attest", "trusted_attestations", "evaluate_once"]

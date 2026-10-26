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

One-shot: a pre-registration can be evaluated exactly once. A second
evaluation is refused and the refusal is visible in the ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from research.common.hashing import digest
from research.integrity.ledger import Ledger, LedgerError

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


def register(ledger: Ledger, p: PreRegistration) -> str:
    if not ledger.entries("hypothesis", p.hypothesis_id):
        raise LedgerError("pre-registration references unknown hypothesis %s" % p.hypothesis_id)
    d = p.digest
    if ledger.entries("prereg", d):
        raise LedgerError("pre-registration %s already exists" % d[:12])
    ledger.append("prereg", asdict(p), ref_id=d)
    return d


def attest(ledger: Ledger, prereg_digest: str, *, server_timestamp_utc: str, source: str,
           evidence_url: str) -> None:
    """Record a server-side timestamp for a pre-registration digest.

    `source` names the authority (e.g. "github_issue_comment.created_at");
    `evidence_url` lets a reviewer re-check it. This function never contacts
    the network: the caller supplies values read by a reviewer or an approved
    tool.
    """
    if not ledger.entries("prereg", prereg_digest):
        raise LedgerError("unknown pre-registration %s" % prereg_digest[:12])
    datetime.fromisoformat(server_timestamp_utc)
    ledger.append("attestation", {"server_timestamp_utc": server_timestamp_utc, "source": source,
                                  "evidence_url": evidence_url}, ref_id=prereg_digest)


def _compare(v: float, op: str, thr: float) -> bool:
    return {">": v > thr, ">=": v >= thr, "<": v < thr, "<=": v <= thr}[op]


def evaluate_once(ledger: Ledger, prereg_digest: str, *, value: float, effective_n: int,
                  data_first_ts_utc: str, data_digest: str) -> PreregOutcome:
    """Evaluate a pre-registration exactly once, fail-closed."""
    rows = ledger.entries("prereg", prereg_digest)
    if not rows:
        raise LedgerError("unknown pre-registration %s" % prereg_digest[:12])
    if ledger.entries("prereg_evaluation", prereg_digest):
        raise LedgerError("pre-registration %s was already evaluated (one-shot)" % prereg_digest[:12])
    p = rows[0].payload
    att = ledger.entries("attestation", prereg_digest)
    window = datetime.fromisoformat(p["forward_window_start_utc"])
    first = datetime.fromisoformat(data_first_ts_utc)
    reason = ""
    if not att:
        reason = "no server-side attestation: not provably out-of-sample"
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
    out = PreregOutcome(prereg_digest, bool(passed), float(value), int(effective_n), reason)
    ledger.append("prereg_evaluation", {**asdict(out), "data_digest": data_digest,
                                        "data_first_ts_utc": data_first_ts_utc}, ref_id=prereg_digest)
    return out


__all__ = ["COMPARATORS", "PreRegistration", "PreregOutcome", "register", "attest", "evaluate_once"]

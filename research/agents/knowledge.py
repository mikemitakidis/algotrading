"""research.agents.knowledge — the permanent research knowledge library (canonical B21).

The library is stored in the hash-chained research ledger
(``research.integrity.ledger``), so it is append-only and tamper-evident.
Every write passes the role gate (``research.agents.roles``).

What it holds:
- **Sources**, with explicit verification status. A fixture or AI-found
  citation is ``unverified`` until the operator verifies it.
- **Hypotheses.** Each must be falsifiable: a metric, a comparator, a
  threshold and kill criteria. It also needs a mechanism and the sources
  it cites.
- **The rejection graveyard**, with evidence and an explicit re-test
  trigger.
- **Lineage** between hypotheses, genomes and trials.
- **Reviews and reports.**

Guards:
- *Operator verification is authenticated* (second review, PR #9). A
  citation becomes ``operator_verified`` only through ``verify_source`` with
  an operator signature over the source's own content, checked by the
  operator ``Verifier`` the library was built with (lane B authority model).
  An identity string proves nothing: without a verifier the library cannot
  verify at all, and the status is re-derived by re-checking the stored
  signature whenever it is read. ``add_source`` refuses a self-declared
  ``operator_verified``. Local-model caveat: the HMAC verifier shares the
  key, so this is a policy model inside a trusted process, not independent
  authorisation (``independent`` is recorded as False).
- *Point-in-time knowledge.* A hypothesis tested on a window may cite only
  sources published BEFORE the window starts. Anything else is look-ahead
  through the literature.
- *Near-duplicate detection.* A proposal too close to an existing or
  rejected hypothesis is refused. Similarity is the larger of two Jaccard
  scores: content words (stop words removed, number words normalised) and
  word 3-shingles. A deliberately DISTINCT idea, such as the opposite
  direction, may proceed only with a written justification, and that
  justification is ledgered for reviewers. A graveyard match can be
  re-tested only once its trigger is met; no justification bypasses that.
  This is lexical, not semantic: a paraphrase in entirely different words
  is not caught. That limit is stated, not hidden.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from research.agents.roles import Gate
from research.common.hashing import digest
from research.integrity.authority import Verifier
from research.integrity.ledger import Ledger, LedgerError

VERIFICATION = ("unverified", "fixture", "operator_verified")
COMPARATORS = (">", ">=", "<", "<=")
DUPLICATE_THRESHOLD = 0.6
VERIFY_PURPOSE = "kb.verify_source"


class KnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    title: str
    authors: str
    venue: str
    published: str                  # ISO date (month precision is fine: YYYY-MM-01)
    locator: str                    # DOI or URL
    claim: str
    retrieved: str
    verification: str = "unverified"
    note: str = ""

    @property
    def source_id(self) -> str:
        return "SRC-" + digest({"title": self.title, "authors": self.authors, "published": self.published})[:10]


@dataclass(frozen=True)
class Prediction:
    metric: str
    comparator: str
    threshold: float


@dataclass(frozen=True)
class Proposal:
    statement: str
    mechanism: str
    prediction: Prediction
    kill_criteria: str
    source_ids: Tuple[str, ...]
    template: str
    proposed_by: str
    test_window_start: str          # ISO date: first day of data the test will use


STOP = frozenset("a an the of in on at to for and or with by from than that this these those is are be "
                 "enough over under into after before within its their it as".split())
NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "twenty": "20", "thirty": "30"}


def normalise(text: str) -> List[str]:
    return [NUMBER_WORDS.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def shingles(text: str, k: int = 3) -> set:
    w = normalise(text)
    return {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))}


def _jac(sa: set, sb: set) -> float:
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def similarity(a: str, b: str) -> float:
    content = _jac({w for w in normalise(a) if w not in STOP}, {w for w in normalise(b) if w not in STOP})
    return max(content, _jac(shingles(a), shingles(b)))


def verification_payload(src: Mapping) -> Dict:
    """What the operator signs: the source's identity AND content, so a signature cannot be moved to another
    source or survive an edited claim or locator."""
    return {"source_id": src["source_id"], "title": src["title"], "authors": src["authors"],
            "published": src["published"], "locator": src["locator"], "claim": src["claim"]}


class KnowledgeLibrary:
    def __init__(self, ledger: Ledger, gate: Gate, operator_verifier: Optional[Verifier] = None):
        self.ledger, self.gate = ledger, gate
        self.operator_verifier = operator_verifier

    # ── sources ──────────────────────────────────────────────────────────
    def add_source(self, role: str, s: Source) -> str:
        self.gate.require(role, "kb.write.source", s.title)
        if s.verification not in VERIFICATION:
            raise KnowledgeError("verification must be one of %s" % (VERIFICATION,))
        if s.verification == "operator_verified":
            raise KnowledgeError("operator_verified cannot be declared; it is granted only by an authenticated "
                                 "operator signature via verify_source")
        if not s.locator:
            raise KnowledgeError("a source needs a DOI or URL")
        if date.fromisoformat(s.published) > date.fromisoformat(s.retrieved):
            raise KnowledgeError("source published after it was retrieved")
        sid = s.source_id
        if self.ledger.entries("kb_source", sid):
            raise KnowledgeError("source %s already recorded" % sid)
        self.ledger.append("kb_source", {**asdict(s), "source_id": sid, "recorded_by": role}, ref_id=sid)
        return sid

    def source(self, sid: str) -> Dict:
        rows = self.ledger.entries("kb_source", sid)
        if not rows:
            raise KnowledgeError("unknown source %s" % sid)
        return rows[0].payload

    def verify_source(self, sid: str, *, operator: str, signature: str) -> None:
        """Record an operator verification. The caller must present the operator's signature over
        ``verification_payload``; the identity string alone is never trusted (second review, PR #9)."""
        src = self.source(sid)
        v = self.operator_verifier
        if v is None:
            raise KnowledgeError("this library has no operator verifier: citations cannot be operator-verified "
                                 "here (offline mock libraries stay unverified)")
        if operator != v.authority_id:
            raise KnowledgeError("%r is not the operator authority %r" % (operator, v.authority_id))
        if not v.verify(VERIFY_PURPOSE, verification_payload(src), signature):
            raise KnowledgeError("operator signature does not verify for source %s" % sid)
        self.ledger.append("kb_source_verification", {"source_id": sid, "by": operator, "signature": signature,
                                                      "authority_model": v.model, "independent": bool(v.independent)},
                           ref_id=sid)

    def verification_status(self, sid: str) -> str:
        """Re-derived at every read: ``operator_verified`` only if a stored verification still re-verifies
        against this library's operator verifier and the source content as recorded."""
        src = self.source(sid)
        v = self.operator_verifier
        if v is not None:
            for e in self.ledger.entries("kb_source_verification", sid):
                pl = e.payload
                if pl.get("by") == v.authority_id and v.verify(VERIFY_PURPOSE, verification_payload(src),
                                                               pl.get("signature", "")):
                    return "operator_verified"
        return src["verification"]

    # ── hypotheses ───────────────────────────────────────────────────────
    def hypotheses(self) -> List[Dict]:
        return [e.payload for e in self.ledger.entries("kb_hypothesis")]

    def similar(self, statement: str, k: int = 3) -> List[Tuple[str, float]]:
        scored = [(h["hypothesis_id"], similarity(statement, h["statement"])) for h in self.hypotheses()]
        return sorted(scored, key=lambda x: -x[1])[:k]

    def propose(self, p: Proposal, *, context: Optional[Mapping] = None,
                distinct_from: Optional[Mapping[str, str]] = None) -> str:
        self.gate.require(p.proposed_by, "kb.write.hypothesis", p.statement[:60])
        distinct_from = dict(distinct_from or {})
        problems = []
        if p.prediction.comparator not in COMPARATORS or not p.prediction.metric:
            problems.append("prediction is not falsifiable (metric, comparator and threshold required)")
        if not p.kill_criteria.strip():
            problems.append("kill criteria missing")
        if not p.mechanism.strip():
            problems.append("mechanism missing")
        if not p.source_ids:
            problems.append("no sources cited")
        win = date.fromisoformat(p.test_window_start)
        cutoff = None
        for sid in p.source_ids:
            try:
                s = self.source(sid)
            except KnowledgeError:
                problems.append("cites unknown source %s" % sid)
                continue
            pub = date.fromisoformat(s["published"])
            cutoff = pub if cutoff is None or pub > cutoff else cutoff
            if pub >= win:
                problems.append("look-ahead: source %s published %s, on or after the test window start %s"
                                % (sid, s["published"], p.test_window_start))
        overrides = []
        for hid, sim in self.similar(p.statement, k=5):
            if sim >= DUPLICATE_THRESHOLD:
                st = self.status(hid)
                if st == "rejected":
                    if self.retest_allowed(hid, context or {}):
                        continue
                    problems.append("duplicate of %s (similarity %.2f, in the graveyard; re-test trigger not met)"
                                    % (hid, sim))
                elif distinct_from.get(hid, "").strip():
                    overrides.append({"near": hid, "similarity": sim, "justification": distinct_from[hid]})
                else:
                    problems.append("duplicate of %s (similarity %.2f, status %s)" % (hid, sim, st))
        if problems:
            self.ledger.append("kb_refusal", {"proposal": asdict(p), "problems": problems}, ref_id=p.proposed_by)
            raise KnowledgeError("; ".join(problems))
        hid = "KH-" + digest({"statement": " ".join(normalise(p.statement)), "window": p.test_window_start})[:10]
        self.ledger.append("kb_hypothesis", {**asdict(p), "hypothesis_id": hid, "distinct_from": overrides,
                                             "knowledge_cutoff": cutoff.isoformat() if cutoff else None}, ref_id=hid)
        self.ledger.append("kb_status", {"status": "open", "reason": "proposed"}, ref_id=hid)
        return hid

    def status(self, hid: str) -> str:
        rows = self.ledger.entries("kb_status", hid)
        if not rows:
            raise KnowledgeError("unknown hypothesis %s" % hid)
        return rows[-1].payload["status"]

    # ── outcomes, graveyard, lineage ────────────────────────────────────
    def reject(self, role: str, hid: str, *, reason: str, evidence: Sequence[str],
               retest_trigger: Mapping) -> None:
        self.gate.require(role, "kb.write.rejection", hid)
        self.status(hid)
        if not retest_trigger:
            raise KnowledgeError("a rejection needs an explicit re-test trigger")
        self.ledger.assert_ledgered(evidence) if evidence else None
        self.ledger.append("kb_rejection", {"hypothesis_id": hid, "reason": reason, "evidence": list(evidence),
                                            "retest_trigger": dict(retest_trigger)}, ref_id=hid)
        self.ledger.append("kb_status", {"status": "rejected", "reason": reason}, ref_id=hid)

    def graveyard(self) -> List[Dict]:
        return [e.payload for e in self.ledger.entries("kb_rejection")]

    def retest_allowed(self, hid: str, context: Mapping) -> bool:
        """Triggers: {"data_after": ISO date}, {"min_symbols": n}, {"requires": [flags]}. All must hold."""
        rows = self.ledger.entries("kb_rejection", hid)
        if not rows:
            return False
        trig = rows[-1].payload["retest_trigger"]
        ok = True
        if "data_after" in trig:
            ok &= bool(context.get("data_end")) and date.fromisoformat(context["data_end"]) > date.fromisoformat(
                trig["data_after"])
        if "min_symbols" in trig:
            ok &= int(context.get("n_symbols", 0)) >= int(trig["min_symbols"])
        if "requires" in trig:
            ok &= set(trig["requires"]) <= set(context.get("flags", ()))
        return bool(ok)

    def record_lineage(self, role: str, *, hypothesis_id: str, genome_id: str, parent_genome_id: str = "",
                       trial_ids: Sequence[str] = ()) -> None:
        self.gate.require(role, "kb.write.lineage", genome_id)
        self.status(hypothesis_id)
        if trial_ids:
            self.ledger.assert_ledgered(trial_ids)
        self.ledger.append("kb_lineage", {"hypothesis_id": hypothesis_id, "genome_id": genome_id,
                                          "parent_genome_id": parent_genome_id, "trial_ids": list(trial_ids)},
                           ref_id=hypothesis_id)

    def review(self, role: str, hid: str, verdict: str, notes: Sequence[str]) -> None:
        self.gate.require(role, "kb.write.review", hid)
        self.ledger.append("kb_review", {"hypothesis_id": hid, "verdict": verdict, "notes": list(notes)}, ref_id=hid)

    def summary(self) -> Dict:
        hs = self.hypotheses()
        srcs = [e.payload["source_id"] for e in self.ledger.entries("kb_source")]
        return {"sources": len(srcs),
                "operator_verified_sources": sum(self.verification_status(x) == "operator_verified" for x in srcs), "hypotheses": len(hs),
                "rejected": len(self.graveyard()), "refusals": len(self.ledger.entries("kb_refusal")),
                "lineage": len(self.ledger.entries("kb_lineage")), "reviews": len(self.ledger.entries("kb_review")),
                "status": {h["hypothesis_id"]: self.status(h["hypothesis_id"]) for h in hs}}


__all__ = ["VERIFICATION", "VERIFY_PURPOSE", "verification_payload", "DUPLICATE_THRESHOLD", "KnowledgeError", "Source", "Prediction", "Proposal",
           "normalise", "shingles", "similarity", "KnowledgeLibrary"]

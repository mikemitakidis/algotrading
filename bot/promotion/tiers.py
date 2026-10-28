"""M21 WP14 (canonical B13) — change proposals and the GREEN / AMBER / RED tier classifier.

A proposal is a change from the ACTIVE paper-lane template configuration
(``bot.templates.controls``) to a candidate configuration, plus an optional
exit-policy change, plus the research evidence that motivates it.

Tier rules (the strictest rule that matches wins):

  RED    promoting a template to trade in the paper lane for the first time
         (``promote`` kind); any parameter moved by more than 50 % of its
         current value; evidence that FAILED walk-forward or the holdout
  AMBER  any exit-policy change (a tighter stop can RAISE losses, so it is
         never GREEN, and the state machine admits it only if the shadow
         comparison shows the loss rate did not rise); a choice parameter
         changed; a parameter moved 10-50 %;
         adding a template to the selection; a champion change
  GREEN  parameter moves of at most 10 % with walk-forward PASS evidence, or
         risk-reducing changes (removing a template from the selection)

Evidence that is missing or INCONCLUSIVE never makes a proposal eligible:
the classifier still assigns a tier (for the record) and ``eligible_evidence``
says why it cannot proceed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
_RANK = {GREEN: 0, AMBER: 1, RED: 2}
KINDS = ("tune", "select", "deselect", "champion", "exits", "promote")

GREEN_MAX_REL, AMBER_MAX_REL = 0.10, 0.50
# minimum live paper signals observed in SHADOW before a proposal is eligible
SHADOW_MIN_SIGNALS = {GREEN: 20, AMBER: 50, RED: 100}


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    kind: str
    current: Mapping[str, Any]                 # the active controls config
    proposed: Mapping[str, Any]                # the candidate controls config
    evidence: Mapping[str, Any] = field(default_factory=dict)   # wf_verdict, holdout_status, ...
    exits_current: Optional[Mapping[str, float]] = None         # e.g. {"stop_atr": 2.0, "target_atr": 3.0}
    exits_proposed: Optional[Mapping[str, float]] = None
    author: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("proposal kind must be one of %s" % (KINDS,))
        if not self.proposal_id or not str(self.author).strip():
            raise ValueError("proposal id and author are required")


def _rel(a: Any, b: Any) -> float:
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) or \
            not isinstance(b, (int, float)):
        return float("inf") if a != b else 0.0
    if a == b:
        return 0.0
    return abs(float(b) - float(a)) / abs(float(a)) if a else float("inf")


def param_changes(cur: Mapping[str, Any], new: Mapping[str, Any]) -> Dict[str, Dict[str, Tuple[Any, Any, float]]]:
    out: Dict[str, Dict[str, Tuple[Any, Any, float]]] = {}
    cp, np_ = cur.get("params", {}) or {}, new.get("params", {}) or {}
    for t in sorted(set(cp) & set(np_)):
        for k in sorted(set(cp[t]) | set(np_[t])):
            a, b = cp[t].get(k), np_[t].get(k)
            if a != b:
                out.setdefault(t, {})[k] = (a, b, _rel(a, b))
    return out


@dataclass(frozen=True)
class TierDecision:
    tier: str
    reasons: Tuple[str, ...]
    eligible_evidence: bool
    evidence_reasons: Tuple[str, ...]
    shadow_min_signals: int


def classify(p: Proposal) -> TierDecision:
    tier, reasons = GREEN, []

    def bump(t, why):
        nonlocal tier
        reasons.append(why)
        if _RANK[t] > _RANK[tier]:
            tier = t

    cur_sel, new_sel = list(p.current.get("selected", [])), list(p.proposed.get("selected", []))
    added = [t for t in new_sel if t not in cur_sel]
    removed = [t for t in cur_sel if t not in new_sel]
    if p.kind == "promote":
        bump(RED, "promote_template_to_paper_trading")
    for t in added:
        bump(AMBER, "template_added:%s" % t)
    if removed and not added:
        reasons.append("risk_reducing_deselect:%s" % ",".join(removed))
    if p.current.get("champion") != p.proposed.get("champion"):
        bump(AMBER, "champion_change:%s->%s" % (p.current.get("champion"), p.proposed.get("champion")))
    for t, ch in param_changes(p.current, p.proposed).items():
        for k, (a, b, rel) in ch.items():
            if math.isinf(rel) and not isinstance(a, (int, float)):
                bump(AMBER, "choice_change:%s.%s" % (t, k))
            elif rel > AMBER_MAX_REL:
                bump(RED, "large_param_move:%s.%s:%.0f%%" % (t, k, 100 * rel if not math.isinf(rel) else 999))
            elif rel > GREEN_MAX_REL:
                bump(AMBER, "param_move:%s.%s:%.0f%%" % (t, k, 100 * rel))
            else:
                reasons.append("small_param_move:%s.%s:%.0f%%" % (t, k, 100 * rel))
    if (p.exits_current or {}) != (p.exits_proposed or {}) and p.exits_proposed is not None:
        bump(AMBER, "exit_policy_change_can_raise_losses")
    ev = dict(p.evidence or {})
    if ev.get("wf_verdict") == "FAIL" or ev.get("holdout_status") == "FAIL":
        bump(RED, "evidence_failed")
    ev_reasons = []
    if ev.get("wf_verdict") != "PASS" and not (removed and not added and p.kind == "deselect"):
        ev_reasons.append("walk_forward_%s" % (ev.get("wf_verdict") or "missing"))
    if tier == RED and ev.get("holdout_status") != "PASS" and p.kind != "deselect":
        ev_reasons.append("holdout_%s" % (ev.get("holdout_status") or "missing"))
    if p.proposed == p.current and (p.exits_proposed is None or p.exits_proposed == p.exits_current):
        ev_reasons.append("no_change")
    return TierDecision(tier, tuple(reasons), not ev_reasons, tuple(ev_reasons), SHADOW_MIN_SIGNALS[tier])


__all__ = ["GREEN", "AMBER", "RED", "KINDS", "GREEN_MAX_REL", "AMBER_MAX_REL", "SHADOW_MIN_SIGNALS", "Proposal",
           "param_changes", "TierDecision", "classify"]

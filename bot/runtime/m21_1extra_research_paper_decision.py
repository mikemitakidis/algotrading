#!/usr/bin/env python3
"""M21.1extra — research-paper eligibility decision (simulation-only).

The M20 paper router requires a candidate that PASSED M19's hard gates
(hard_gate_passed=True). M21.1 RESEARCH rankings deliberately do NOT pass the
hard gate — model-readiness/calibration are downgraded to MANUAL_REVIEW so we
can rank by component quality while no trained model exists. Feeding such a
candidate into the M20 router would (correctly) reject it.

This module adds a SEPARATE, STRICT eligibility rule used ONLY for simulation
paper trading in M21.1extra. It never changes M19 truth fields and never sets
hard_gate_passed/execution_eligible. It only DECIDES whether a research-grade
candidate may be carried into the *simulated* paper pipeline.

Honesty contract:
  * never inspects or mutates M19 internals; reads the public ScoredSignalCandidate
  * a candidate is eligible ONLY when the sole reason it is not gate-passed is the
    known research manual-review state (model not ready / calibration unavailable)
  * any REAL safety block (missing context, stale data, liquidity, PIT/adjusted
    price, risk authority, production thinness, invalid data quality) -> NOT eligible
  * SHORT side -> NOT eligible (A is long-only)
  * execution_eligible True -> NOT eligible (that would be a live-approval path)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from bot.signal_scoring import ScoredSignalCandidate, SignalSide

# Blocking reasons that are REAL safety blocks (must reject). This list is
# documentation of intent; the primary guard is "hard_gate_failures and
# blocked_reasons are both empty", which a clean research candidate satisfies
# (model-readiness/calibration are MANUAL_REVIEW, surfaced in reason_codes,
# never in the blocking lists). Tests assert each of these rejects.
_REAL_SAFETY_BLOCKS = frozenset({
    "missing_context_key",
    "stale_data",
    "adjusted_price_pit_risk",
    "risk_authority_blocked",
    "production_thinness_blocked",
    "min_liquidity",
    "invalid_context_value",
    "data_quality_invalid",
})


@dataclass
class ResearchPaperDecision:
    symbol: str
    research_paper_eligible: bool
    reason: str
    rejected_blocks: List[str] = field(default_factory=list)


def decide_research_paper_eligibility(
    scored: ScoredSignalCandidate,
) -> ResearchPaperDecision:
    """Strict simulation-only eligibility. Returns a decision; never raises for
    normal candidates. Does not modify the candidate."""
    sym = scored.symbol

    # 1. Never allow anything M19 considers execution-eligible to take this
    #    research-only path (that lane is reserved for the real future flow).
    if scored.execution_eligible is not False:
        return ResearchPaperDecision(sym, False, "execution_eligible_not_false")

    # 2. Long-only in A.
    side_val = getattr(scored.side, "value", scored.side)
    if scored.side is not SignalSide.LONG and side_val != "LONG":
        return ResearchPaperDecision(sym, False, "not_long_side")

    # 3. No REAL hard-gate safety block may be present. Checked BEFORE the
    #    score test so a blocked candidate reports the actual block (a blocked
    #    candidate scores 0, which would otherwise mask the real reason). For a
    #    clean research candidate both lists are empty (model-readiness/
    #    calibration are manual-review, surfaced in reason_codes, NOT here).
    real_blocks = sorted(set(scored.hard_gate_failures) |
                         set(scored.blocked_reasons))
    if real_blocks:
        # classify: known safety blocks vs any other blocking reason — either
        # way we reject; the classification only enriches the audit trail.
        known = [b for b in real_blocks if b in _REAL_SAFETY_BLOCKS]
        reason = "real_safety_block" if known else "unexpected_block"
        return ResearchPaperDecision(
            sym, False, reason, rejected_blocks=real_blocks)

    # 4. Must have a real positive research score.
    if not (float(scored.final_score_100) > 0.0):
        return ResearchPaperDecision(sym, False, "non_positive_score")

    # 5. Defensive: if any blocking reason somehow appears that is NOT an
    #    allowed research-review reason, reject. (Belt-and-braces; real_blocks
    #    being empty already covers this.)
    return ResearchPaperDecision(sym, True, "research_paper_eligible")

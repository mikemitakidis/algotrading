"""M21.1 A02 — ledger stages, terminal kinds and the allowed order (pure).

Event stages, in pipeline order (``scored`` is optional: M19 is not on the
live submit path today):

    scanned -> [scored] -> risk_evaluated -> intent_logged -> broker_result
            -> outcome_resolved

``closed`` records the candidate's single terminal disposition. A candidate
without ``closed`` is in flight; the only legitimate long-lived in-flight
state is an accepted order awaiting its outcome.
"""
from __future__ import annotations

from typing import Dict, Tuple

STAGES: Tuple[str, ...] = (
    "scanned", "scored", "risk_evaluated", "intent_logged", "broker_result",
    "outcome_resolved", "closed",
)
_ORDER: Dict[str, int] = {s: i for i, s in enumerate(STAGES)}

# The stage that must already exist before each stage may be recorded.
REQUIRES: Dict[str, Tuple[str, ...]] = {
    "scanned": (),
    "scored": ("scanned",),
    "risk_evaluated": ("scanned",),
    "intent_logged": ("risk_evaluated",),
    "broker_result": ("intent_logged",),
    "outcome_resolved": ("broker_result",),
    "closed": ("scanned",),
}

TERMINAL_KINDS: Tuple[str, ...] = (
    "not_selected",           # scanner did not emit a signal (partial / watch / filtered)
    "not_persisted",          # final signal could not be stored
    "scoring_blocked",        # an M19 hard gate blocked it on a path that uses M19
    "risk_rejected",          # legacy RiskManager / PortfolioRiskPolicy or M14 block
    "broker_unready",         # gateway watchdog blocked submission
    "broker_not_accepted",    # submit returned a non-accepted status
    "cancelled",              # order cancelled before any outcome
    "outcome_resolved",       # WIN / LOSS / TIMEOUT recorded
    "untracked_after_signal", # legacy data gap: signal with no intent row
)

# broker statuses that mean "an order (or simulated paper order) exists"
ACCEPTED_STATUSES: Tuple[str, ...] = ("accepted", "filled", "paper_logged")
RESOLVED_OUTCOMES: Tuple[str, ...] = ("WIN", "LOSS", "TIMEOUT")


def stage_index(stage: str) -> int:
    if stage not in _ORDER:
        raise ValueError("unknown ledger stage %r" % stage)
    return _ORDER[stage]

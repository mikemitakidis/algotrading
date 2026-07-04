"""M21.1extra-D2.0 — dry-run loop orchestration (state machine + lock).

Pure dry-run: decides what a real cycle WOULD do given injected gates and
candidates. No orders, no broker/gateway, no D0/D1 imports, no network, no DB.
"""
from bot.paper_loop_d2.state import (
    LoopState, TERMINAL_STATES, DryRunCycleRecord,
)
from bot.paper_loop_d2.dryrun import (
    run_dry_cycle, MalformedCandidate,
)
from bot.paper_loop_d2.lock import CycleLock, LockHeld
from bot.paper_loop_d2.gates import make_d0_gate, make_d1_gate
from bot.paper_loop_d2.lifecycle import (
    run_supervised_cycle, SupervisedCycleResult,
)

__all__ = [
    "LoopState", "TERMINAL_STATES", "DryRunCycleRecord",
    "run_dry_cycle", "MalformedCandidate",
    "CycleLock", "LockHeld",
    "make_d0_gate", "make_d1_gate",
    "run_supervised_cycle", "SupervisedCycleResult",
]

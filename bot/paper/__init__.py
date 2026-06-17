"""bot.paper — M20 paper-trading firewall (contracts-first, M20.A).

Isolated simulation-only package. Consumes M19 public schema/enums as a
read-only analytical input. Never imports brokers/live/risk/etoro/flywheel/
main/dashboard or any network library. No execution, no persistence (M20.A/B),
no live semantics.
"""
from __future__ import annotations

M20_PHASE = "M20.A"

from bot.paper.lifecycle import (  # noqa: F401,E402
    PaperOrderStatus,
    InvalidPaperTransition,
    is_valid_transition,
    validate_transition,
    TERMINAL_STATES,
)
from bot.paper.schema import (  # noqa: F401,E402
    SCHEMA_VERSION,
    PaperContractViolation,
    PaperSide,
    PaperOrderType,
    PaperPositionStatus,
    PaperEventType,
    PaperRoutingDecision,
    PaperOrder,
    PaperFill,
    PaperPosition,
    PaperPnLSnapshot,
    PaperEvent,
    assert_m19_candidate_contract,
)
from bot.paper.config import (  # noqa: F401,E402
    PaperTradingConfig,
    default_paper_config,
)
from bot.paper import provenance  # noqa: F401,E402

__all__ = [
    "M20_PHASE",
    "SCHEMA_VERSION",
    # exceptions
    "PaperContractViolation",
    "InvalidPaperTransition",
    # enums
    "PaperOrderStatus",
    "PaperSide",
    "PaperOrderType",
    "PaperPositionStatus",
    "PaperEventType",
    # schemas
    "PaperRoutingDecision",
    "PaperOrder",
    "PaperFill",
    "PaperPosition",
    "PaperPnLSnapshot",
    "PaperEvent",
    # config
    "PaperTradingConfig",
    "default_paper_config",
    # lifecycle
    "is_valid_transition",
    "validate_transition",
    "TERMINAL_STATES",
    # ingestion guard
    "assert_m19_candidate_contract",
    # provenance module
    "provenance",
]

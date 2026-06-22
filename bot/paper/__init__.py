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
from bot.paper.routing import decide_paper_routing  # noqa: F401,E402
from bot.paper.sizing import (  # noqa: F401,E402
    compute_paper_sizing,
    PaperSizingPreview,
)
from bot.paper.orders import (  # noqa: F401,E402
    build_paper_order,
    PaperOrderResult,
)
from bot.paper.fills import (  # noqa: F401,E402
    simulate_paper_fill,
    PaperFillResult,
)
from bot.paper.positions import (  # noqa: F401,E402
    build_paper_position,
    PaperPositionResult,
)
from bot.paper.pnl import (  # noqa: F401,E402
    mark_paper_position,
    PaperPnLResult,
)
from bot.paper.closing import (  # noqa: F401,E402
    close_paper_position,
    PaperCloseResult,
)
from bot.paper.ledger import (  # noqa: F401,E402
    build_account_event,
    PaperLedgerResult,
)
from bot.paper.account import (  # noqa: F401,E402
    PaperAccountState,
    PaperAccountResult,
    new_account,
    open_position_in_account,
    mark_account,
    close_position_in_account,
)
from bot.paper.storage import (  # noqa: F401,E402
    PaperStorageResult,
    append_events,
    load_events,
    append_snapshots,
    load_snapshots,
    append_account_states,
    load_account_states,
    replay_events_summary,
)

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
    # routing (M20.B)
    "decide_paper_routing",
    # sizing (M20.C)
    "compute_paper_sizing",
    "PaperSizingPreview",
    # orders + fills (M20.D)
    "build_paper_order",
    "PaperOrderResult",
    "simulate_paper_fill",
    "PaperFillResult",
    # positions + pnl (M20.E)
    "build_paper_position",
    "PaperPositionResult",
    "mark_paper_position",
    "PaperPnLResult",
    # closing + realised pnl (M20.F)
    "close_paper_position",
    "PaperCloseResult",
    # account + ledger (M20.G)
    "PaperAccountState",
    "PaperAccountResult",
    "new_account",
    "open_position_in_account",
    "mark_account",
    "close_position_in_account",
    "build_account_event",
    "PaperLedgerResult",
    # storage (M20.H)
    "PaperStorageResult",
    "append_events",
    "load_events",
    "append_snapshots",
    "load_snapshots",
    "append_account_states",
    "load_account_states",
    "replay_events_summary",
]

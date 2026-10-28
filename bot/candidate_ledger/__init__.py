"""M21.1 A02 — candidate-decision ledger (offline implementation).

One stable candidate id (lane-A formula) carried through scanner -> M19 score
-> risk decision -> execution intent -> broker result -> outcome, with complete
stage accounting. Additive schema with an explicit migration; a flag-gated,
idempotent, never-raising writer that is OFF by default. Nothing in the
runtime calls it yet; deploying the schema or enabling the writer on any
production database requires operator approval.
"""
from bot.candidate_ledger.ids import (  # noqa: F401
    make_candidate_id, make_event_id, scanner_strategy_id,
)
from bot.candidate_ledger.stages import (  # noqa: F401
    ACCEPTED_STATUSES, REQUIRES, RESOLVED_OUTCOMES, STAGES, TERMINAL_KINDS,
)
from bot.candidate_ledger.schema import (  # noqa: F401
    LEDGER_TABLES, SCHEMA_VERSION, LedgerSchemaError, migrate, schema_present,
)
from bot.candidate_ledger.writer import (  # noqa: F401
    ENV_FLAG, CandidateLedgerWriter, ledger_enabled_from_env, risk_payload,
    safe_json, score_payload,
)
from bot.candidate_ledger.accounting import LedgerReport, account  # noqa: F401

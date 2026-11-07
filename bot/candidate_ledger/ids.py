"""M21.1 A02 — stable identifiers for the candidate-decision ledger (pure).

``make_candidate_id`` reproduces the lane-A research formula
(``research/evidence/candidates.make_candidate_id``) on the production side,
using the M19 canonical JSON encoder, so research and production records of
the same decision carry the same id:

    "C-" + sha256(canonical_json({"s": symbol, "t": decision_ts_iso,
                                  "d": direction, "k": strategy_id}))[:16]

The id is deterministic: replaying the same decision always yields the same
id, which is what makes ledger writes idempotent.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from bot.signal_scoring.provenance import canonical_json

DIRECTIONS = ("long", "short")


def _iso_utc(ts: Any) -> str:
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("unparseable decision timestamp %r" % ts)
    if not isinstance(ts, datetime):
        raise ValueError("decision timestamp required, got %r" % (ts,))
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("decision timestamp must be timezone-aware (no UTC assumed)")
    return ts.astimezone(timezone.utc).isoformat()


def scanner_strategy_id(strategy_version: Any) -> str:
    if isinstance(strategy_version, bool) or not isinstance(strategy_version, int) \
            or strategy_version < 0:
        raise ValueError("strategy_version must be a non-negative int")
    return "scanner:v%d" % strategy_version


def make_candidate_id(symbol: str, decision_ts_utc: Any, direction: str,
                      strategy_id: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol required")
    if direction not in DIRECTIONS:
        raise ValueError("direction must be long or short")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise ValueError("strategy_id required")
    body = canonical_json({"s": symbol, "t": _iso_utc(decision_ts_utc),
                           "d": direction, "k": strategy_id})
    return "C-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def make_event_id(candidate_id: str, stage: str, attempt: int = 0) -> str:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a non-negative int")
    body = canonical_json({"c": candidate_id, "s": stage, "a": attempt})
    return "E-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]

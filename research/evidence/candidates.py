"""research.evidence.candidates — point-in-time candidate events.

A CANDIDATE is one decision the system made (or could have made) about one
symbol and direction at one moment, whatever the outcome of that decision:
rejected, near-miss, eligible, blocked by a gate or the Risk Authority,
skipped for capacity, executed, or replayed from history. Every candidate can
be given a counterfactual path label (labeler.py) so the system learns from
all of them, not only from executed trades.

Only information available at ``decision_ts_utc`` may be stored on a
candidate (``atr_at_decision`` and ``features`` are as-of values).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from research.common.hashing import digest

STAGES: Tuple[str, ...] = (
    "scanned",            # evaluated, no TF agreement worth recording
    "rejected",           # failed the strategy rule (e.g. confluence)
    "near_miss",          # one TF short of the rule (partial confluence)
    "watch",              # M19 WATCH bucket
    "eligible",           # passed strategy + scoring
    "blocked_gate",       # eligible but blocked by a hard gate (reason codes)
    "blocked_risk",       # eligible but blocked by the Risk Authority
    "skipped_capacity",   # eligible, allowed, but no slot / daily cap reached
    "executed",           # an order was placed (paper or live)
    "simulated_entry",    # accepted and filled by a research backtest engine: simulated, never an order
    "unfillable",         # accepted by the rules but no position was possible (no fill bar, no ATR, ...):
                          # never executed, uses no capacity
    "historical_anchor",  # replayed from history (M18 anchors / scanner replica)
    "shadow",             # scored by a challenger in shadow mode
)
DIRECTIONS = ("long", "short")
SOURCES = ("scanner_live", "historical_replay", "synthetic", "paper_fill", "shadow")


@dataclass(frozen=True)
class Bracket:
    """Counterfactual exit rule: entry at the open of the first bar that starts
    at or after the decision; stop / target at multiples of the decision-time
    ATR from the ENTRY price; time stop at the close of the last bar of the
    holding window."""
    stop_atr_mult: float = 2.0
    target_atr_mult: float = 3.0
    max_holding_bars: int = 50

    def __post_init__(self):
        if not (self.stop_atr_mult > 0 and self.target_atr_mult > 0 and self.max_holding_bars >= 1):
            raise ValueError("bracket multiples must be > 0 and max_holding_bars >= 1")


@dataclass(frozen=True)
class CandidateEvent:
    candidate_id: str
    symbol: str
    decision_ts_utc: pd.Timestamp
    direction: str
    stage: str
    source: str
    timeframe: str
    strategy_id: str
    atr_at_decision: float
    bracket: Bracket = field(default_factory=Bracket)
    reasons: Tuple[str, ...] = ()
    sector: str = ""
    features: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.stage not in STAGES:
            raise ValueError("unknown stage %r" % self.stage)
        if self.direction not in DIRECTIONS:
            raise ValueError("direction must be long or short, got %r" % self.direction)
        if self.source not in SOURCES:
            raise ValueError("unknown source %r" % self.source)
        ts = pd.Timestamp(self.decision_ts_utc)
        if ts.tzinfo is None:
            raise ValueError("decision_ts_utc must be timezone-aware")
        object.__setattr__(self, "decision_ts_utc", ts.tz_convert("UTC"))


def make_candidate_id(symbol: str, decision_ts_utc, direction: str, strategy_id: str) -> str:
    return "C-" + digest({"s": symbol, "t": pd.Timestamp(decision_ts_utc).isoformat(),
                          "d": direction, "k": strategy_id})[:16]


def to_frame(events: Iterable[CandidateEvent]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for e in events:
        r = asdict(e)
        b = r.pop("bracket")
        r.update({"stop_atr_mult": b["stop_atr_mult"], "target_atr_mult": b["target_atr_mult"],
                  "max_holding_bars": b["max_holding_bars"], "reasons": "|".join(e.reasons)})
        rows.append(r)
    return pd.DataFrame(rows)


def stage_counts(events: Iterable[CandidateEvent]) -> Dict[str, int]:
    out = {s: 0 for s in STAGES}
    for e in events:
        out[e.stage] += 1
    return {k: v for k, v in out.items() if v}


__all__ = ["STAGES", "DIRECTIONS", "SOURCES", "Bracket", "CandidateEvent", "make_candidate_id",
           "to_frame", "stage_counts"]

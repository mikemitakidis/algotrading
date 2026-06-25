"""M20.UC2 quality-gate report dataclasses.

Structured, admin-callable result of applying quality gates to a UC1 snapshot.
Pure data; no I/O. Mirrors the UC1 report style so a future admin panel can
surface gate outcomes the same way.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

GATE_REPORT_SCHEMA_VERSION = "m20_quality_gate_report_v1"


@dataclass
class SymbolDecision:
    """Per-symbol gate outcome (what UC2 decided for one record)."""
    internal_symbol: str
    data_quality_status: str            # verified | failed | unverified
    scan_ready: bool
    min_liquidity_tier: Optional[str] = None
    avg_volume_20d: Optional[float] = None
    avg_dollar_volume_20d: Optional[float] = None
    median_spread_bps: Optional[float] = None
    last_verified_utc: Optional[str] = None
    liquidity_source: Optional[str] = None
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QualityGateReport:
    """Aggregate result of a UC2 gate run."""
    schema_version: str = GATE_REPORT_SCHEMA_VERSION
    mode: str = "report_only"           # report_only | write_back
    asof: Optional[str] = None
    snapshot_path: Optional[str] = None
    thresholds_digest: Optional[str] = None

    symbols_total: int = 0
    evaluated: int = 0
    verified_count: int = 0
    failed_count: int = 0
    unverified_count: int = 0
    scan_ready_count: int = 0

    # informational (never a failure): Alpaca IEX vs Yahoo volume divergence
    volume_semantics_divergence_count: int = 0

    # safety
    max_scan_ready_per_run: int = 0
    ceiling_exceeded: bool = False

    # breakdowns
    tier_counts: Dict[str, int] = field(default_factory=dict)
    fail_reason_counts: Dict[str, int] = field(default_factory=dict)
    would_scan_ready: List[str] = field(default_factory=list)

    decisions: List[SymbolDecision] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    started_at_utc: Optional[str] = None
    finished_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decisions"] = [
            x.to_dict() if isinstance(x, SymbolDecision) else x
            for x in self.decisions
        ]
        return d

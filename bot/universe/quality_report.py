"""M20.UC1 quality-collector structured report.

Plain, frozen result dataclasses returned by the admin-callable collector
backend (universe_quality_check / collect / validate). Kept separate from the
collector so the future admin panel and UC2 can import the result type WITHOUT
importing any network / provider code. No I/O, no network, no secrets.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "m20_quality_report_v1"


@dataclass(frozen=True)
class SourceSummary:
    """Per-source connectivity / coverage summary (no secrets, counts only)."""
    source: str
    creds_present: bool = False
    reachable: bool = False
    success_count: int = 0
    missing_count: int = 0
    error_count: int = 0
    rate_limit_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityCollectionReport:
    """Structured result for every collector mode (dry-run/collect/validate).

    Field set is stable so a future admin panel can render it directly. Contains
    only booleans / counts / paths — never credential values.
    """
    status: str                       # success | failed | partial
    mode: str                         # dry-run | collect | validate
    schema_version: str = SCHEMA_VERSION
    asof: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    symbols_total: int = 0
    symbols_checked: int = 0
    alpaca_success_count: int = 0
    yahoo_success_count: int = 0
    both_sources_success_count: int = 0
    missing_alpaca_count: int = 0
    missing_yahoo_count: int = 0
    source_disagreement_count: int = 0
    rate_limit_count: int = 0
    alpaca_creds_present: bool = False
    alpaca_reachable: bool = False
    yahoo_reachable: bool = False
    source_summaries: List[SourceSummary] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    snapshot_path: Optional[str] = None
    log_path: Optional[str] = None
    started_at_utc: Optional[str] = None
    finished_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source_summaries"] = [s.to_dict() if isinstance(s, SourceSummary)
                                 else s for s in self.source_summaries]
        return d

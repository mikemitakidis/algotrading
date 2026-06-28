"""M21.UQ quality model: reason codes + result dataclasses (read-only)."""
from dataclasses import dataclass, field
from typing import List, Optional

# ── Reason codes ──────────────────────────────────────────────────────────
# Provider / symbol integrity
PROVIDER_SYMBOL_MISSING = "provider_symbol_missing"
PROVIDER_SYMBOL_DUPLICATE = "provider_symbol_duplicate"
PROVIDER_SUFFIX_INVALID = "provider_suffix_invalid"
# OHLCV quality
OHLCV_EMPTY = "ohlcv_empty"
OHLCV_TOO_FEW_BARS = "ohlcv_too_few_bars"
OHLCV_STALE = "ohlcv_stale"
OHLCV_NON_FINITE = "ohlcv_non_finite"
# Liquidity
VOLUME_MISSING_OR_ZERO = "volume_missing_or_zero"
LIQUIDITY_UNKNOWN = "liquidity_unknown"
# Provider availability (distinct from data-quality problems)
PROVIDER_RATE_LIMITED = "provider_rate_limited"
PROVIDER_FETCH_ERROR = "provider_fetch_error"
# Aggregate
QUALITY_PASS = "quality_pass"
QUALITY_FAIL = "quality_fail"

ALL_REASON_CODES = frozenset({
    PROVIDER_SYMBOL_MISSING, PROVIDER_SYMBOL_DUPLICATE, PROVIDER_SUFFIX_INVALID,
    OHLCV_EMPTY, OHLCV_TOO_FEW_BARS, OHLCV_STALE, OHLCV_NON_FINITE,
    VOLUME_MISSING_OR_ZERO, LIQUIDITY_UNKNOWN, PROVIDER_RATE_LIMITED,
    PROVIDER_FETCH_ERROR, QUALITY_PASS, QUALITY_FAIL,
})

# Codes that, if present, make a candidate FAIL the gate. LIQUIDITY_UNKNOWN is a
# WARNING for inactive candidates (their liquidity fields are intentionally null
# at this stage), so it is non-fatal by default.
# Provider availability codes (rate-limit / fetch-error) are FATAL for a
# provider-backed live check (the candidate did not pass a live check) but are
# explicitly NOT data-quality verdicts — they say "could not evaluate", not
# "bad data". They are surfaced distinctly so a rate-limited symbol is never
# mislabelled ohlcv_empty / volume_missing_or_zero.
FATAL_CODES = frozenset({
    PROVIDER_SYMBOL_MISSING, PROVIDER_SYMBOL_DUPLICATE, PROVIDER_SUFFIX_INVALID,
    OHLCV_EMPTY, OHLCV_TOO_FEW_BARS, OHLCV_STALE, OHLCV_NON_FINITE,
    VOLUME_MISSING_OR_ZERO, PROVIDER_RATE_LIMITED, PROVIDER_FETCH_ERROR,
})
WARNING_CODES = frozenset({LIQUIDITY_UNKNOWN})
# Provider-availability codes: a live check could not be completed (NOT a
# statement about data quality).
PROVIDER_AVAILABILITY_CODES = frozenset({
    PROVIDER_RATE_LIMITED, PROVIDER_FETCH_ERROR,
})


@dataclass
class QualityResult:
    """Per-candidate quality evaluation result."""
    internal_symbol: str
    provider_symbol: Optional[str]
    region: str
    exchange: str
    passed: bool
    reason_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "internal_symbol": self.internal_symbol,
            "provider_symbol": self.provider_symbol,
            "region": self.region,
            "exchange": self.exchange,
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }


@dataclass
class OHLCVConfig:
    """Thresholds for OHLCV quality checks (deterministic, no network)."""
    min_bars: int = 20            # require >= this many bars
    max_staleness_days: int = 7   # last bar must be within this many days
                                  # of the evaluation "as_of" date


@dataclass
class ProviderBar:
    """One OHLCV bar. date is an ISO YYYY-MM-DD string for determinism."""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FetchResult:
    """Structured provider fetch outcome.

    error_kind is None on success. On failure it is one of:
      'rate_limited' -> maps to PROVIDER_RATE_LIMITED
      'fetch_error'  -> maps to PROVIDER_FETCH_ERROR
    bars is the raw payload (list[dict]) on success, [] for a true empty
    dataframe (no exception), or None when unfetchable with no specific error.
    """
    bars: Optional[List[dict]] = None
    error_kind: Optional[str] = None
    error_text: str = ""

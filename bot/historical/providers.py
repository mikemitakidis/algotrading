"""bot/data/providers.py — provider abstraction.

Reuses the M6 provider boundary in spirit but adds two M16-specific
concepts: a capability descriptor (what TFs/lookback the provider
supports, polite rate) and a unified return contract that the refresh
orchestrator depends on.

Provider implementations live in their own modules:
  bot.historical.providers_yfinance.YFinanceProvider

A FakeProvider for tests lives in test_m16_historical_data.py only —
never imported by production code.

Hard invariant: this module imports NOTHING broker-related. AST-asserted.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, FrozenSet, Optional, Tuple

import pandas as pd


log = logging.getLogger(__name__)


# Outcome categories for refresh fetches.
FETCH_OK = "ok"
FETCH_NO_DATA = "no_data"
FETCH_PROVIDER_ERROR = "provider_error"
FETCH_RATE_LIMITED = "rate_limited"

FETCH_OUTCOMES = (FETCH_OK, FETCH_NO_DATA, FETCH_PROVIDER_ERROR,
                    FETCH_RATE_LIMITED)


@dataclass(frozen=True)
class ProviderCapability:
    """Static per-provider metadata used by the refresh planner.

    `lookback_caps` maps timeframe -> max lookback as a string the planner
    parses ('60d', '730d', 'max'). 'max' means no provider-imposed cap.
    """
    name: str
    supported_timeframes: FrozenSet[str]
    lookback_caps: Dict[str, str]
    supports_adjusted: bool
    polite_calls_per_minute: int
    bulk_symbols_per_call: int = 1
    notes: str = ""


@dataclass
class FetchResult:
    """Provider fetch outcome — single (symbol, timeframe) request."""
    outcome: str                                 # one of FETCH_OUTCOMES
    df: Optional[pd.DataFrame] = None            # populated iff outcome=='ok'
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """All M16 providers conform to this minimal interface."""

    @property
    @abstractmethod
    def capability(self) -> ProviderCapability:
        ...

    @abstractmethod
    def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> FetchResult:
        """Return a FetchResult for the requested window.

        The returned DataFrame (when outcome=='ok') MUST have columns:
          ts_utc (tz-aware UTC), open, high, low, close, volume,
          adj_close (or NaN), provider (always set to this provider's name)
        Other columns may be present; storage will fill them in.

        Adapter responsibility:
          * convert provider-native timestamps to tz-aware UTC
          * clamp the window to the provider's lookback cap (caller
            already does this via the planner; this is belt-and-braces)
          * distinguish three failure modes: empty=ok-no-data,
            exception=provider_error, rate-limit signal=rate_limited
        """
        ...


# ---------------------------------------------------------------------------
# Planner — capability-aware
# ---------------------------------------------------------------------------

def parse_lookback_cap(cap: str) -> Optional[pd.Timedelta]:
    """'60d' -> 60 days; 'max' -> None (no cap); '730d' -> 730 days."""
    if cap == "max":
        return None
    if cap.endswith("d"):
        return pd.Timedelta(days=int(cap[:-1]))
    if cap.endswith("h"):
        return pd.Timedelta(hours=int(cap[:-1]))
    raise ValueError(f"unsupported lookback cap format: {cap!r}")


def clamp_to_lookback(
    want_from: datetime, want_to: datetime,
    *, timeframe: str, capability: ProviderCapability,
    now_utc: Optional[datetime] = None,
) -> Tuple[datetime, datetime, bool]:
    """Clamp a requested window to the provider's lookback cap.

    Returns (clamped_from, clamped_to, lookback_exceeded).
    `lookback_exceeded` is True iff the requested `want_from` was
    earlier than the capped earliest.
    """
    if timeframe not in capability.lookback_caps:
        return want_from, want_to, False
    cap_str = capability.lookback_caps[timeframe]
    cap = parse_lookback_cap(cap_str)
    if cap is None:
        return want_from, want_to, False
    now = now_utc if now_utc is not None else datetime.now(want_to.tzinfo)
    earliest = pd.Timestamp(now) - cap
    if pd.Timestamp(want_from) < earliest:
        return earliest.to_pydatetime(), want_to, True
    return want_from, want_to, False

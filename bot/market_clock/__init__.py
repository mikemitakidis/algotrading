"""M21.1extra-D1 — fail-closed market-clock guard (US_EQ slice).

Read-only market-status gate: given a market_calendar_id and instant, decide
whether an order MAY be submitted, defaulting to "do not submit" and only
returning may_submit=True on a fresh, supported, regular-session, affirmative
open from a trusted source. No order placement, no scheduler, no DB writes.
"""
from bot.market_clock.decision import (
    MarketClockDecision, VALID_REASONS, SUPPORTED_CALENDAR_IDS,
    REASON_MARKET_OPEN, REASON_MARKET_CLOSED, REASON_CALENDAR_UNSUPPORTED,
    REASON_NO_CREDENTIALS, REASON_SOURCE_UNAVAILABLE, REASON_SOURCE_STALE,
    REASON_SOURCE_MALFORMED, REASON_SOURCE_INCONSISTENT,
    REASON_EXTENDED_HOURS_REFUSED, REASON_INTERNAL_ERROR,
)
from bot.market_clock.sources import (
    MarketStatusSource, SourceResult, MockMarketStatusSource,
    IbkrMarketStatusSource, STATUS_OK, STATUS_NO_CREDENTIALS,
    STATUS_UNAVAILABLE, STATUS_MALFORMED,
)
from bot.market_clock.guard import (
    evaluate_market_clock, MarketClockCache, DEFAULT_MAX_AGE_SEC,
)
from bot.market_clock.alpaca_clock import AlpacaClockSource

__all__ = [
    "MarketClockDecision", "VALID_REASONS", "SUPPORTED_CALENDAR_IDS",
    "REASON_MARKET_OPEN", "REASON_MARKET_CLOSED", "REASON_CALENDAR_UNSUPPORTED",
    "REASON_NO_CREDENTIALS", "REASON_SOURCE_UNAVAILABLE", "REASON_SOURCE_STALE",
    "REASON_SOURCE_MALFORMED", "REASON_SOURCE_INCONSISTENT",
    "REASON_EXTENDED_HOURS_REFUSED", "REASON_INTERNAL_ERROR",
    "MarketStatusSource", "SourceResult", "MockMarketStatusSource",
    "IbkrMarketStatusSource", "STATUS_OK", "STATUS_NO_CREDENTIALS",
    "STATUS_UNAVAILABLE", "STATUS_MALFORMED",
    "evaluate_market_clock", "MarketClockCache", "DEFAULT_MAX_AGE_SEC",
    "AlpacaClockSource",
]

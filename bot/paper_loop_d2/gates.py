#!/usr/bin/env python3
"""M21.1extra-D2 — real D0 / D1 gate adapters.

These adapt the already-merged D0 authorization gate and D1 market-clock guard
to the callable shapes the D2.0 dry-run state machine already consumes:

    d0_gate() -> (allowed: bool, reason: str)
    d1_gate() -> (may_submit: bool, reason: str)

They are thin, read-only wrappers:
  * D0 -> runtime_policy.get_signal_only_reason("ibkr_paper", db_path, ttl_sec);
    it returns (skip, reason). allowed = (skip is False). D2 never mutates the
    policy; it only reads.
  * D1 -> evaluate_market_clock(market_calendar_id="US_EQ", sources=[...]).
    allowed = decision.may_submit. Sources are injected (an AlpacaClockSource in
    production, a mock/stubbed http_get in tests) so there is no hard network
    dependency here and no live credentials are required to import/use this.

This module places NO orders, opens NO gateway, writes NO DB, and imports NO
broker code. It is fail-closed: any exception in a gate yields the blocking
(not-allowed) shape so the state machine records a blocked/failed cycle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Callable, Sequence, Tuple, Any

# D0 + D1 are READ-ONLY consumed here (allowed by scope: adapt existing gates).
from bot.runtime_policy import get_signal_only_reason
from bot.market_clock import evaluate_market_clock, AlpacaClockSource
from bot.market_clock.sources import MarketStatusSource

# The lane D2 operates on. Paper only, forever, in this milestone.
_IBKR_PAPER_LANE = "ibkr_paper"
_US_EQ = "US_EQ"

# Reason returned when the adapter itself blows up (defensive; the underlying
# gates are already fail-closed, but never trust a call site).
_D0_ADAPTER_ERROR = "d0_adapter_error"
_D1_ADAPTER_ERROR = "d1_adapter_error"


def make_d0_gate(
    *,
    db_path: Optional[str] = None,
    ttl_sec: Optional[float] = None,
    now: Optional[float] = None,
) -> Callable[[], Tuple[bool, str]]:
    """Build a d0_gate() closure over the real D0 read. `db_path`/`ttl_sec` are
    injected (tests use a temp DB; production resolves the live path via the
    D0 layer). allowed = (skip is False)."""
    def _d0_gate() -> Tuple[bool, str]:
        try:
            skip, reason = get_signal_only_reason(
                _IBKR_PAPER_LANE, db_path=db_path, ttl_sec=ttl_sec, now=now)
        except Exception as exc:            # D0 is fail-closed, but be defensive
            return False, "%s:%s" % (_D0_ADAPTER_ERROR, type(exc).__name__)
        # authorized iff NOT signal-only-skipped.
        allowed = (skip is False)
        # when allowed, D0 returns reason="" — surface a stable positive token.
        return allowed, (reason if reason else ("authorized" if allowed
                                                else "d0_blocked"))
    return _d0_gate


def make_d1_gate(
    *,
    sources: Optional[Sequence[MarketStatusSource]] = None,
    now_utc: Optional[datetime] = None,
    max_age_sec: Optional[float] = None,
    symbol: Optional[str] = None,
) -> Callable[[], Tuple[bool, str]]:
    """Build a d1_gate() closure over the real D1 guard. `sources` is injected;
    when None, a default AlpacaClockSource() is used (which itself fails closed
    on missing credentials / network problems — no live creds required to build
    this gate). allowed = decision.may_submit."""
    def _d1_gate() -> Tuple[bool, str]:
        try:
            srcs = sources if sources is not None else [AlpacaClockSource()]
            kwargs: dict = {"market_calendar_id": _US_EQ, "sources": srcs}
            if now_utc is not None:
                kwargs["now_utc"] = now_utc
            if max_age_sec is not None:
                kwargs["max_age_sec"] = max_age_sec
            if symbol is not None:
                kwargs["symbol"] = symbol
            decision = evaluate_market_clock(**kwargs)
        except Exception as exc:            # D1 is fail-closed, but be defensive
            return False, "%s:%s" % (_D1_ADAPTER_ERROR, type(exc).__name__)
        return (decision.may_submit is True), decision.reason
    return _d1_gate


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)

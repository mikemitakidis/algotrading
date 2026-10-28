"""M21.1extra-D2.3 — Risk Authority gate for the paper learning loop.

Every new paper entry must pass the M14 Risk Authority (the same engine the
eToro operator path uses) before the broker is called:

    run_risk_preflight(conn, broker_scope='ibkr_paper', request=TradeRequest(...),
                       current_authority=Authority.AUTO_ALLOWED, actor='paper_learning_loop',
                       audit_source='auto')

The loop is the unattended (auto) lane, so it presents AUTO_ALLOWED; the
engine still blocks unless policy, kill switches, caps and state freshness
allow (trade_open needs >= ONE_SHOT_MANUAL; lower levels are always blocked).
The operator's time-boxed D0 authorization is checked separately, earlier.

The engine writes its own audit rows (risk_snapshots / risk_decisions) through
its single approved audit surface; this adapter writes nothing itself. Any
exception or unusable result is a BLOCK (fail closed). The decision id is
returned so the journal can tie each order to the decision that allowed it.

A09 — market state is verified HERE, at the authority boundary, on every call.
A09 hardening (R1-R3): the provider must be a ``TrustedMarketStateProvider``
built only from approved clock sources (exact ``AlpacaClockSource``); the
decision must name that source with status ok; the market-state age limit has
an absolute 30 s ceiling; and a valid, future, plausible session close is
required.
M14's ``market_open`` gate only trusts the flag its caller passes (and defaults
to True), and the scheduler's D1 check can be bypassed by calling this gate
directly. So the gate requires a ``market_state`` provider (no default) and,
before the preflight is even reached, demands a genuine D1 ``MarketClockDecision``
that is: affirmative open (may_submit / reason market_open), for US_EQ,
regular-session, not stale / inconsistent / unsupported, fetched within
``max_market_state_age_sec`` of the gate's clock and not in its future, with
any reported session close still ahead. Anything else — closed, holiday,
stale or unavailable clock, wrong type, provider error — is a BLOCK, and the
preflight is not called. ``market_open=True`` reaches M14 only after that.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Tuple

GateResult = Tuple[bool, str, Optional[str]]
RiskGate = Callable[..., GateResult]

PAPER_SCOPE = "ibkr_paper"
ACTOR = "paper_learning_loop"
MARKET_CALENDAR = "US_EQ"
MAX_MARKET_STATE_AGE_SEC = 30.0      # = D1 DEFAULT_MAX_AGE_SEC; absolute ceiling
_MAX_CLOCK_SKEW_SEC = 5.0
_MAX_SESSION_CLOSE_AHEAD = timedelta(hours=24)
TRUSTED_CLOCK_SOURCE_NAMES = frozenset({"alpaca"})


def _trusted_source_types():
    from bot.market_clock import AlpacaClockSource
    return (AlpacaClockSource,)


def _valid_age_limit(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value <= MAX_MARKET_STATE_AGE_SEC)


def _utc(value: Any) -> Optional[datetime]:
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def verify_market_open(market_state: Callable[[], Any], *, now: datetime,
                       max_age_sec: float = MAX_MARKET_STATE_AGE_SEC) -> Tuple[bool, str]:
    """Fail-closed check of a D1 market-clock decision. (True, 'market_open')
    only for a fresh, affirmative, regular-session US_EQ open from a trusted
    clock source, with a valid future session close. The age limit can only
    tighten the 30 s ceiling, never loosen it."""
    from bot.market_clock.decision import MarketClockDecision, REASON_MARKET_OPEN
    from bot.market_clock.sources import STATUS_OK
    if not _valid_age_limit(max_age_sec):
        return False, "market_state_age_limit_invalid"
    try:
        decision = market_state()
    except Exception as exc:
        return False, "market_state_error:%s" % type(exc).__name__
    if not isinstance(decision, MarketClockDecision):
        return False, "market_state_untrusted_type"
    if decision.may_submit is not True or decision.reason != REASON_MARKET_OPEN:
        return False, "market_not_open:%s" % decision.reason
    if (decision.source not in TRUSTED_CLOCK_SOURCE_NAMES
            or decision.source_status != STATUS_OK):
        return False, "market_state_untrusted_source"
    if decision.market_calendar_id != MARKET_CALENDAR:
        return False, "market_calendar_mismatch"
    if (decision.stale or decision.inconsistent or decision.unsupported
            or decision.regular_session_only is not True):
        return False, "market_state_flagged"
    now = _utc(now)
    fetched = _utc(decision.fetched_at_utc)
    if now is None or fetched is None:
        return False, "market_state_time_invalid"
    if fetched - now > timedelta(seconds=_MAX_CLOCK_SKEW_SEC):
        return False, "market_state_from_future"
    if now - fetched > timedelta(seconds=min(max_age_sec, MAX_MARKET_STATE_AGE_SEC)):
        return False, "market_state_stale"
    if decision.next_close_utc is None:
        return False, "market_session_close_missing"
    close = _utc(decision.next_close_utc)
    if close is None:
        return False, "market_session_close_invalid"
    if close <= now:
        return False, "market_session_ended"
    if close - now > _MAX_SESSION_CLOSE_AHEAD:
        return False, "market_session_close_implausible"
    return True, REASON_MARKET_OPEN


class TrustedMarketStateProvider:
    """market_state() over the real D1 guard, built ONLY from approved clock
    sources (exact AlpacaClockSource instances; production default is a fresh
    AlpacaClockSource, which fails closed without credentials or network).
    Returns the D1 guard's full decision; never raises. The D1 freshness limit
    is the 30 s ceiling."""

    __slots__ = ("_sources", "_clock")

    def __init__(self, *, sources=None, clock: Optional[Callable[[], datetime]] = None):
        trusted = _trusted_source_types()
        srcs = tuple(sources) if sources is not None else (trusted[0](),)
        if not srcs:
            raise ValueError("at least one trusted market clock source is required")
        for s in srcs:
            if type(s) not in trusted:
                raise ValueError("untrusted market clock source: %s" % type(s).__name__)
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self._sources = srcs
        self._clock = clock

    def __call__(self):
        from bot.market_clock import evaluate_market_clock
        from bot.market_clock.decision import REASON_INTERNAL_ERROR, do_not_submit
        try:
            kwargs: dict = {"market_calendar_id": MARKET_CALENDAR,
                            "sources": list(self._sources),
                            "max_age_sec": MAX_MARKET_STATE_AGE_SEC}
            if self._clock is not None:
                kwargs["now_utc"] = self._clock()
            return evaluate_market_clock(**kwargs)
        except Exception as exc:
            return do_not_submit(reason=REASON_INTERNAL_ERROR,
                                 market_calendar_id=MARKET_CALENDAR,
                                 source="market_state_adapter",
                                 raw_source_summary={"exc": type(exc).__name__})


def make_market_state_provider(*, sources=None,
                               clock: Optional[Callable[[], datetime]] = None
                               ) -> TrustedMarketStateProvider:
    """The only supported way to build a market_state provider for the gate."""
    return TrustedMarketStateProvider(sources=sources, clock=clock)


def make_risk_authority_gate(*, conn_factory: Callable[[], sqlite3.Connection],
                             market_state: TrustedMarketStateProvider,
                             authority_name: str = "AUTO_ALLOWED",
                             broker_scope: str = PAPER_SCOPE,
                             clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                             max_market_state_age_sec: float = MAX_MARKET_STATE_AGE_SEC
                             ) -> RiskGate:
    if broker_scope != PAPER_SCOPE:
        raise ValueError("the paper learning loop only ever uses the ibkr_paper scope")
    if type(market_state) is not TrustedMarketStateProvider:
        raise ValueError("market_state must be a TrustedMarketStateProvider (fail closed)")
    if not _valid_age_limit(max_market_state_age_sec):
        raise ValueError("max_market_state_age_sec must be in (0, %s]"
                         % MAX_MARKET_STATE_AGE_SEC)
    if not callable(clock):
        raise ValueError("clock must be callable")

    def gate(*, symbol: str, side: str, notional_usd: float, stop_loss: float,
             take_profit: float) -> GateResult:
        try:
            market_open_verified, market_why = verify_market_open(
                market_state, now=clock(), max_age_sec=max_market_state_age_sec)
        except Exception as exc:
            return False, "market_state_error:%s" % type(exc).__name__, None
        if market_open_verified is not True:
            return False, "market_state_block:%s" % market_why, None
        from bot.risk_authority import Authority, run_risk_preflight
        from bot.risk_authority.engine import TradeRequest
        conn = None
        try:
            conn = conn_factory()
            res = run_risk_preflight(
                conn, broker_scope=broker_scope,
                request=TradeRequest(symbol=symbol, amount_usd=float(notional_usd), side=side,
                                     sl=float(stop_loss), tp=float(take_profit)),
                current_authority=Authority.from_string(authority_name),
                actor=ACTOR, audit_source="auto", market_open=market_open_verified)
        except Exception as exc:
            return False, "risk_authority_error:%s" % type(exc).__name__, None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if res.allowed is not True:
            return False, "risk_authority_block:%s" % (",".join(res.reason_codes) or "unspecified"), \
                res.decision_id
        return True, "risk_authority_allow", res.decision_id

    return gate

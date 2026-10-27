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

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Tuple

GateResult = Tuple[bool, str, Optional[str]]
RiskGate = Callable[..., GateResult]

PAPER_SCOPE = "ibkr_paper"
ACTOR = "paper_learning_loop"
MARKET_CALENDAR = "US_EQ"
MAX_MARKET_STATE_AGE_SEC = 30.0      # = D1 DEFAULT_MAX_AGE_SEC
_MAX_CLOCK_SKEW_SEC = 5.0


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
    only for a genuine, fresh, affirmative, regular-session US_EQ open."""
    from bot.market_clock.decision import MarketClockDecision, REASON_MARKET_OPEN
    try:
        decision = market_state()
    except Exception as exc:
        return False, "market_state_error:%s" % type(exc).__name__
    if not isinstance(decision, MarketClockDecision):
        return False, "market_state_untrusted_type"
    if decision.may_submit is not True or decision.reason != REASON_MARKET_OPEN:
        return False, "market_not_open:%s" % decision.reason
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
    if now - fetched > timedelta(seconds=max_age_sec):
        return False, "market_state_stale"
    if decision.next_close_utc is not None:
        close = _utc(decision.next_close_utc)
        if close is None or close <= now:
            return False, "market_session_ended"
    return True, REASON_MARKET_OPEN


def make_market_state_provider(*, sources=None, max_age_sec: Optional[float] = None,
                               clock: Optional[Callable[[], datetime]] = None
                               ) -> Callable[[], Any]:
    """A market_state() closure returning the D1 guard's FULL decision for
    US_EQ (production default source: AlpacaClockSource, which itself fails
    closed without credentials or network). Never raises: an adapter
    exception becomes a do-not-submit decision."""
    def _market_state():
        from bot.market_clock import AlpacaClockSource, evaluate_market_clock
        from bot.market_clock.decision import REASON_INTERNAL_ERROR, do_not_submit
        try:
            srcs = sources if sources is not None else [AlpacaClockSource()]
            kwargs: dict = {"market_calendar_id": MARKET_CALENDAR, "sources": srcs}
            if clock is not None:
                kwargs["now_utc"] = clock()
            if max_age_sec is not None:
                kwargs["max_age_sec"] = max_age_sec
            return evaluate_market_clock(**kwargs)
        except Exception as exc:
            return do_not_submit(reason=REASON_INTERNAL_ERROR,
                                 market_calendar_id=MARKET_CALENDAR,
                                 source="market_state_adapter",
                                 raw_source_summary={"exc": type(exc).__name__})
    return _market_state


def make_risk_authority_gate(*, conn_factory: Callable[[], sqlite3.Connection],
                             market_state: Callable[[], Any],
                             authority_name: str = "AUTO_ALLOWED",
                             broker_scope: str = PAPER_SCOPE,
                             clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                             max_market_state_age_sec: float = MAX_MARKET_STATE_AGE_SEC
                             ) -> RiskGate:
    if broker_scope != PAPER_SCOPE:
        raise ValueError("the paper learning loop only ever uses the ibkr_paper scope")
    if not callable(market_state):
        raise ValueError("a market_state provider is required (fail closed)")
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

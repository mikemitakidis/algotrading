"""bot/risk_authority/governor.py — M14.E authority governor.

Stateless function over (current_state, decision) → new_state. The
governor enforces the **downgrade-only** invariant on the authority
ladder and applies the hysteresis / cooldown rules from M14.A §6.

Per ChatGPT M14.E corrections:

  #1  Pure function. No DB write. No file I/O. No broker call. The
      caller (`audit_decisions.decide_and_audit`) is responsible for
      persisting the resulting authority state.

  #5  Daily-loss latch and other day-keyed gates use the UTC trading
      day. If `decision.taken_at_utc` falls on a new UTC day vs the
      previous breach, the day-keyed latch is eligible for restore
      (still subject to its own restore condition; see cooldown table).

Hard invariants:
  * `transition_if_needed(...)` NEVER returns `after > before` unless
    the explicit `manual_reset` source is passed.
  * `apply_decision(...)` NEVER calls the engine. It consumes a
    RiskDecision and observes its `authority_after`.
  * The governor exposes a single property-testable surface:
        propose(authority_before, decision, prev_state) -> GovernorState
    No global state. No side effects.

Cooldown table (excerpted from M14.A §6 and the M14.E plan):

    trigger                          | target          | auto-restore?
    ---------------------------------|-----------------|----------------------
    global_kill                      | OFF             | no (manual_reset)
    broker_kill                      | OFF             | no (manual_reset)
    daily_loss_block_active          | SIGNAL_ONLY     | no, same UTC day
    broker_daily_loss_exceeded       | SIGNAL_ONLY     | no, same UTC day
    global_daily_loss_exceeded       | SIGNAL_ONLY     | no, same UTC day
    drawdown_throttle_hit            | SIGNAL_ONLY     | no (manual_reset)
    daily_pnl_unknown                | SIGNAL_ONLY     | yes, on N=3 fresh PnL
    exposure_unknown                 | SIGNAL_ONLY     | yes, on N=3 fresh exposure
    exposure_stale                   | SIGNAL_ONLY     | yes, on N=3 fresh exposure
    combined_exposure_unknown        | SIGNAL_ONLY     | yes, on N=3 fresh exposure (per-scope)
    global_open_positions_unknown    | SIGNAL_ONLY     | yes, on N=3 fresh exposure (per-scope)
    global_daily_loss_unknown        | SIGNAL_ONLY     | yes, on N=3 fresh PnL (per-scope)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from bot.risk_authority.authority import Authority, is_monotone_safe
from bot.risk_authority.engine import RiskDecision


# Reasons that latch for the rest of the UTC day even on fresh data.
_DAY_LATCH_REASONS = frozenset({
    "daily_loss_block_active",
    "broker_daily_loss_exceeded",
    "global_daily_loss_exceeded",
})

# Reasons that require explicit manual_reset to clear.
_MANUAL_RESET_ONLY_REASONS = frozenset({
    "global_kill",
    "broker_kill",
    "drawdown_throttle_hit",
})

# Reasons that auto-restore once N consecutive fresh reads land.
_AUTO_RESTORE_FRESH_REASONS = frozenset({
    "daily_pnl_unknown",
    "exposure_unknown",
    "exposure_stale",
    "combined_exposure_unknown",
    "global_open_positions_unknown",
    "global_daily_loss_unknown",
})


@dataclass(frozen=True)
class GovernorState:
    """Per-scope governor state. Persisted by the caller, not by us."""
    authority:                 Authority
    latched_day_utc:           Optional[str] = None      # UTC YYYY-MM-DD of latching event
    latched_reasons:           Tuple[str, ...] = field(default_factory=tuple)
    manual_reset_required:     bool = False              # True iff a manual-reset reason latched
    fresh_consecutive_count:   int = 0                   # for auto-restore eligibility


def _trading_day_utc(iso_ts: str) -> str:
    """Strip the UTC date prefix from an ISO-8601 timestamp."""
    return iso_ts[:10] if isinstance(iso_ts, str) else ""


def _classify_reasons(reasons: Tuple[str, ...]) -> Tuple[bool, bool, bool]:
    """Returns (any_manual_reset_only, any_day_latch, any_auto_restorable)."""
    any_manual = any(r in _MANUAL_RESET_ONLY_REASONS for r in reasons)
    any_latch  = any(r in _DAY_LATCH_REASONS for r in reasons)
    any_auto   = any(r in _AUTO_RESTORE_FRESH_REASONS for r in reasons)
    return any_manual, any_latch, any_auto


def propose(
    authority_before: Authority,
    decision: RiskDecision,
    prev_state: Optional[GovernorState] = None,
) -> GovernorState:
    """Propose the next GovernorState. Pure: no I/O, no mutation of inputs.

    Invariants:
      * Returned `authority` is ALWAYS <= `authority_before` (downgrade-only).
      * If the previous state latched on the same UTC day for a
        day-keyed reason, authority cannot rise above SIGNAL_ONLY
        regardless of the current decision result.
      * If a manual-reset-only reason was latched previously and
        no manual_reset has cleared it, authority stays clamped.
      * Once latched on `manual_reset_required=True`, only an explicit
        manual_reset action can clear (see `apply_manual_reset` below).
    """
    today = _trading_day_utc(decision.taken_at_utc)
    reasons = tuple(decision.reason_codes or ())
    any_manual, any_latch, any_auto = _classify_reasons(reasons)

    # Start from prev_state if present (carries day latch + manual flag).
    prev_auth   = prev_state.authority if prev_state else authority_before
    prev_day    = prev_state.latched_day_utc if prev_state else None
    prev_reasons = prev_state.latched_reasons if prev_state else ()
    prev_manual = prev_state.manual_reset_required if prev_state else False
    prev_fresh  = prev_state.fresh_consecutive_count if prev_state else 0

    # 1. If a manual-reset block is already in place, authority stays
    #    capped at SIGNAL_ONLY (or OFF if kill). It can never auto-clear.
    clamp_from_manual = Authority.AUTO_ALLOWED
    if prev_manual:
        # Kill switches drop to OFF; everything else to SIGNAL_ONLY.
        if any(r in ("global_kill", "broker_kill") for r in prev_reasons):
            clamp_from_manual = Authority.OFF
        else:
            clamp_from_manual = Authority.SIGNAL_ONLY

    # 2. Day-latch clamp. If prev state latched today on a day-keyed
    #    reason and we're still in the same UTC day, clamp to SIGNAL_ONLY.
    clamp_from_day_latch = Authority.AUTO_ALLOWED
    in_same_day = (prev_day is not None and prev_day == today
                   and any(r in _DAY_LATCH_REASONS for r in prev_reasons))
    if in_same_day:
        clamp_from_day_latch = Authority.SIGNAL_ONLY

    # 3. Engine's recommended authority_after is itself a downgrade-only
    #    move (computed in engine._compute_authority_after). We just take it.
    engine_after = decision.authority_after

    # 4. Compose: the final authority is the MIN of all clamps and the
    #    engine's recommendation. This guarantees downgrade-only.
    final_int = min(
        int(authority_before),
        int(engine_after),
        int(clamp_from_manual),
        int(clamp_from_day_latch),
    )
    final = Authority(final_int)

    # 5. Update latch state:
    #    - If current decision blocked on a manual-reset-only reason or
    #      a day-latch reason, latch now.
    #    - Else preserve prev latch UNLESS auto-restore eligible.
    if any_manual:
        latched_reasons = reasons
        latched_day = today
        manual_required = True
        fresh_count = 0
    elif any_latch:
        latched_reasons = reasons
        latched_day = today
        manual_required = False
        fresh_count = 0
    elif prev_manual:
        # Manual-reset latch persists until explicit clearing.
        latched_reasons = prev_reasons
        latched_day = prev_day
        manual_required = True
        fresh_count = 0
    elif in_same_day:
        # Day-latch persists for the rest of the UTC day.
        latched_reasons = prev_reasons
        latched_day = prev_day
        manual_required = False
        fresh_count = 0
    elif decision.result == "allow" and any_auto is False:
        # Fresh read with no auto-restorable issue: increment counter.
        # N=3 consecutive fresh reads required for full restore eligibility.
        fresh_count = min(prev_fresh + 1, 99)
        latched_reasons = () if fresh_count >= 3 else prev_reasons
        latched_day = None if fresh_count >= 3 else prev_day
        manual_required = False
    elif any_auto:
        # New auto-restorable downgrade today.
        latched_reasons = reasons
        latched_day = today
        manual_required = False
        fresh_count = 0
    else:
        # Block for a reason that isn't latch/manual/auto (e.g. authority_too_low,
        # market_closed, quote_stale). No state change, but reset fresh counter
        # so we require freshness streaks to be unbroken.
        latched_reasons = prev_reasons
        latched_day = prev_day
        manual_required = prev_manual
        fresh_count = 0

    # 6. Final monotone safety check. If somehow we computed an upgrade,
    #    that's a bug — fall back to authority_before.
    if not is_monotone_safe(authority_before, final, source="auto"):
        final = authority_before

    return GovernorState(
        authority=final,
        latched_day_utc=latched_day,
        latched_reasons=latched_reasons,
        manual_reset_required=manual_required,
        fresh_consecutive_count=fresh_count,
    )


def apply_manual_reset(
    prev_state: GovernorState,
    *,
    new_authority: Authority,
) -> GovernorState:
    """Explicit manual_reset action. The ONLY API that may raise
    authority. Caller is responsible for confirming operator intent
    before calling.

    Re-arms a previously latched scope. The caller is responsible for
    auditing this as `source='manual_reset'` per M14.A.
    """
    if not isinstance(new_authority, Authority):
        raise TypeError("new_authority must be an Authority")
    return GovernorState(
        authority=new_authority,
        latched_day_utc=None,
        latched_reasons=(),
        manual_reset_required=False,
        fresh_consecutive_count=0,
    )


__all__ = [
    "GovernorState",
    "propose",
    "apply_manual_reset",
]

"""
bot/etoro/signal_only_broker.py — M13.5.B SignalOnlyBroker wrapper.

The signal-only / manual-trading invariant (M13.5.A §1):

  Signal generation, signal storage, scanner analysis, and Telegram
  alerts run unconditionally. The Broker Allocation auto-trading
  switches and kill switches control ONLY whether a broker submission
  attempt is made.

This wrapper implements that invariant. When get_broker() (in
bot/brokers/__init__.py) detects that policy disables auto-trading for
the active broker, it constructs SignalOnlyBroker(wrapped=<real
broker>) instead of returning the broker directly.

The wrapper exposes the same BrokerAdapter interface. Its `submit()`
method NEVER calls the wrapped broker's submit() — it returns an
OrderResult with status='signal_only_skipped' and a named rejection
reason. main.py is not modified. The notifier path that runs *after*
broker.submit() is unaffected, which preserves Telegram alerting.
"""
from __future__ import annotations

import logging
from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)


# Rejection reason vocabulary for SignalOnlyBroker. These are explicit
# strings so dashboards and downstream code can branch on them.
REASON_GLOBAL_DISABLED       = "auto_trading_disabled_global"
REASON_GLOBAL_KILL_SWITCH    = "kill_switch_active_global"
REASON_BROKER_DISABLED       = "auto_trading_disabled_broker"
REASON_BROKER_KILL_SWITCH    = "kill_switch_active_broker"
REASON_BROKER_NOT_ALLOWED    = "broker_not_in_allowed_brokers"
REASON_ETORO_LIVE_DISABLED   = "etoro_live_disabled_policy"
REASON_POLICY_MISSING        = "policy_missing_or_invalid"
REASON_GENERIC               = "auto_trading_disabled"

# Reason codes -> human-readable labels used in the rejection_reason
# string. The exact strings are tested.
VALID_REASONS = {
    REASON_GLOBAL_DISABLED,
    REASON_GLOBAL_KILL_SWITCH,
    REASON_BROKER_DISABLED,
    REASON_BROKER_KILL_SWITCH,
    REASON_BROKER_NOT_ALLOWED,
    REASON_ETORO_LIVE_DISABLED,
    REASON_POLICY_MISSING,
    REASON_GENERIC,
}


class SignalOnlyBroker(BrokerAdapter):
    """No-op broker wrapper. Records intents as signal_only_skipped.

    The wrapped broker is held for reference (its `.name` is included
    in this broker's name) but is NEVER called. Construction does not
    open any connection, does not consult any policy, and does not
    perform any I/O.
    """

    def __init__(self, wrapped: BrokerAdapter, reason: str = REASON_GENERIC):
        if wrapped is None:
            raise ValueError("SignalOnlyBroker requires a wrapped broker")
        if reason not in VALID_REASONS:
            # Be strict — unknown reason codes hide bugs.
            raise ValueError(f"unknown SignalOnlyBroker reason {reason!r}; "
                             f"expected one of {sorted(VALID_REASONS)}")
        self._wrapped = wrapped
        self._reason = reason

    @property
    def name(self) -> str:
        try:
            wrapped_name = self._wrapped.name
        except Exception:
            wrapped_name = "unknown"
        return f"signal_only:{wrapped_name}"

    @property
    def is_live(self) -> bool:
        # Signal-only is never live.
        return False

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def wrapped(self) -> BrokerAdapter:
        return self._wrapped

    def submit(self, intent: OrderIntent) -> OrderResult:
        """Record the intent as signal_only_skipped. Never call wrapped."""
        log.info("[signal_only] skipping submission: symbol=%s direction=%s "
                 "wrapped=%s reason=%s",
                 getattr(intent, "symbol", "?"),
                 getattr(intent, "direction", "?"),
                 getattr(self._wrapped, "name", "?"),
                 self._reason)
        return OrderResult(
            intent=intent,
            status="signal_only_skipped",
            broker_order_id=None,
            reason=self._reason,
            filled_price=None,
        )

    def cancel(self, broker_order_id: str) -> bool:
        return False

    def get_positions(self) -> list:
        # Pass-through to the wrapped broker is acceptable for read
        # operations (no execution side effects). Defensive: catch any
        # error and return [].
        try:
            return list(self._wrapped.get_positions())
        except Exception as e:
            log.warning("[signal_only] get_positions on wrapped %s failed: %s",
                        getattr(self._wrapped, "name", "?"), e)
            return []


def determine_signal_only_reason(policy: dict, broker_name: str) -> tuple[bool, str]:
    """Inspect a policy dict and a broker name. Return (skip, reason).

    skip=True → SignalOnlyBroker should wrap the real broker.
    skip=False → reason='' and the real broker can be used.

    This function intentionally duplicates the gates of
    bot.broker_allocation.is_auto_trading_allowed() but emits the
    explicit signal-only reason codes used by this module. It uses
    `is True` (not `bool(...)`) for the etoro_live_enabled check, per
    ChatGPT audit finding.
    """
    if not isinstance(policy, dict):
        return True, REASON_POLICY_MISSING
    if not isinstance(broker_name, str) or not broker_name:
        return True, REASON_BROKER_NOT_ALLOWED

    g = policy.get("global")
    if not isinstance(g, dict):
        return True, REASON_POLICY_MISSING
    if g.get("kill_switch") is True:
        return True, REASON_GLOBAL_KILL_SWITCH
    if g.get("auto_trading_enabled") is not True:
        return True, REASON_GLOBAL_DISABLED

    routing = policy.get("routing")
    if not isinstance(routing, dict):
        return True, REASON_POLICY_MISSING
    allowed = routing.get("allowed_brokers")
    if not isinstance(allowed, list) or broker_name not in allowed:
        return True, REASON_BROKER_NOT_ALLOWED

    # eToro live policy flag (strict identity check).
    if broker_name == "etoro_real":
        # Strict: routing.etoro_live_enabled is True (not bool()).
        if routing.get("etoro_live_enabled") is not True:
            return True, REASON_ETORO_LIVE_DISABLED

    # Map broker_name → broker block key
    block_key = None
    if broker_name in ("ibkr", "ibkr_paper", "ibkr_live"):
        block_key = "ibkr"
    elif broker_name in ("etoro_paper", "etoro_real"):
        block_key = "etoro"

    if block_key is not None:
        block = policy.get(block_key)
        if not isinstance(block, dict):
            return True, REASON_POLICY_MISSING
        if block.get("kill_switch") is True:
            return True, REASON_BROKER_KILL_SWITCH
        if block.get("auto_trading_enabled") is not True:
            return True, REASON_BROKER_DISABLED

    # paper has no broker block — only global gates apply.
    return False, ""


__all__ = [
    "SignalOnlyBroker",
    "determine_signal_only_reason",
    "REASON_GLOBAL_DISABLED",
    "REASON_GLOBAL_KILL_SWITCH",
    "REASON_BROKER_DISABLED",
    "REASON_BROKER_KILL_SWITCH",
    "REASON_BROKER_NOT_ALLOWED",
    "REASON_ETORO_LIVE_DISABLED",
    "REASON_POLICY_MISSING",
    "REASON_GENERIC",
    "VALID_REASONS",
]

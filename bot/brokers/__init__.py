"""
bot/brokers/__init__.py
Broker factory — Milestone 10 (M13.5.B updated for SignalOnlyBroker wrap).

Select broker via BROKER env var in .env (default: paper).
  paper        — paper trading, logs intents, no real execution (M10 default)
  ibkr         — IBKR (placeholder, not implemented until M11)
  ibkr_paper   — IBKR paper trading (M11)
  ibkr_live    — IBKR live trading (M12)
  etoro_paper  — eToro paper / dry-run, no real eToro writes (M13.3)
  etoro_real   — STILL fails loudly here in M13.5.B. The live writer
                 EtoroLiveBroker is constructed ONLY by the operator
                 CLI (tools/etoro_live_write.py). get_broker() never
                 returns EtoroLiveBroker — that preserves the scanner-
                 isolation invariant documented in M13.5.A §1.4.

M13.5.B addition: when policy disables auto-trading for the active
broker, get_broker() returns SignalOnlyBroker(wrapped=<real broker>).
The wrapper has the same interface; .submit() returns
status='signal_only_skipped' with a named reason. main.py is NOT
modified. The Telegram alert path that runs after broker.submit() is
unaffected, so Telegram signals continue regardless of execution
switches.
"""
import os
import logging
import sqlite3
from typing import Optional

from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)


def _construct_concrete(name: str) -> BrokerAdapter:
    """Construct the underlying broker for `name`. Never wraps."""
    if name == 'paper':
        from bot.brokers.paper_broker import PaperBroker
        return PaperBroker()
    if name in ('ibkr', 'ibkr_paper', 'ibkr_live'):
        from bot.brokers.ibkr_broker import IBKRBroker
        return IBKRBroker()
    if name == 'etoro_paper':
        from bot.etoro.paper_broker import PaperEtoroBroker
        return PaperEtoroBroker()
    if name == 'etoro_real':
        # M13.5.B contract: get_broker() must NEVER construct
        # EtoroLiveBroker. The live writer is operator-CLI-only.
        # Continue to fail loudly here so misuse is visible.
        raise ValueError(
            'BROKER=etoro_real is not selectable via the broker registry. '
            'Live eToro writes are operator-only via '
            'tools/etoro_live_write.py.'
        )
    log.warning('[BROKER] Unknown BROKER=%r — defaulting to paper', name)
    from bot.brokers.paper_broker import PaperBroker
    return PaperBroker()


def _maybe_load_policy() -> Optional[dict]:
    """Load M13.4A broker allocation policy if a DB path is reachable.

    Returns None on any failure — the caller falls back to the unwrapped
    broker. We never block scanner startup on policy load.
    """
    try:
        # Resolve DB path the same way the dashboard / main.py do.
        from bot.config import BASE_DIR
        db_path = os.environ.get('SIGNALS_DB_PATH') or \
            str(BASE_DIR / 'data' / 'signals.db')
        conn = sqlite3.connect(db_path)
        try:
            from bot.broker_allocation import load_policy
            policy = load_policy(conn)
        finally:
            conn.close()
        return policy if isinstance(policy, dict) else None
    except Exception as e:
        log.debug('[BROKER] could not load broker allocation policy: %s', e)
        return None


def get_broker() -> BrokerAdapter:
    """Return the active broker, possibly wrapped in SignalOnlyBroker.

    Behaviour:
      1. If BROKER=etoro_real → raise ValueError (operator CLI only).
      2. Construct the concrete broker.
      3. Inspect M13.4A policy. If policy disables auto-trading for
         this broker (global disabled, global kill switch, broker
         disabled, broker kill switch, broker not in allowed_brokers),
         wrap in SignalOnlyBroker with a named reason.
      4. Otherwise return the concrete broker.

    The scanner / main.py is unchanged: it calls broker.submit() and
    main.py's Telegram alert path runs afterwards regardless.
    """
    name = os.getenv('BROKER', 'paper').lower().strip()
    concrete = _construct_concrete(name)

    # SignalOnlyBroker wrap decision. Only relevant for brokers the
    # registry returns — paper, ibkr*, etoro_paper. (etoro_real
    # already raised above.)
    try:
        from bot.etoro.signal_only_broker import (
            SignalOnlyBroker,
            determine_signal_only_reason,
            REASON_POLICY_UNAVAILABLE,
        )
    except Exception as e:
        log.debug('[BROKER] SignalOnlyBroker not available: %s', e)
        return concrete

    policy = _maybe_load_policy()
    if policy is None:
        # ISSUE-014: policy load failed / unavailable. Do NOT silently return
        # the bare concrete broker. Consult the runtime-policy fail-safe (the
        # same source the submit() paths use) so the factory-level decision
        # matches the submit-level fail-safe. Never fail OPEN.
        try:
            from bot.runtime_policy import get_signal_only_reason
            skip, reason = get_signal_only_reason(get_broker_name())
        except Exception as e:
            log.warning('[BROKER] policy unavailable and runtime-policy '
                        'fail-safe errored (%s) — wrapping %s in '
                        'SignalOnlyBroker(policy_unavailable)',
                        e, concrete.name)
            return SignalOnlyBroker(
                concrete, reason=REASON_POLICY_UNAVAILABLE)
        if skip:
            log.warning('[BROKER] policy unavailable — wrapping %s in '
                        'SignalOnlyBroker: reason=%s', concrete.name, reason)
            return SignalOnlyBroker(concrete, reason=reason)
        return concrete

    skip, reason = determine_signal_only_reason(policy, get_broker_name())
    if skip:
        log.info('[BROKER] wrapping %s in SignalOnlyBroker: reason=%s',
                 concrete.name, reason)
        return SignalOnlyBroker(concrete, reason=reason)
    return concrete


def get_broker_name() -> str:
    name = os.getenv('BROKER', 'paper').lower().strip()
    if name in ('ibkr', 'ibkr_paper'): return 'ibkr_paper'
    if name == 'ibkr_live': return 'ibkr_live'
    return name


__all__ = ['BrokerAdapter', 'OrderIntent', 'OrderResult', 'get_broker', 'get_broker_name']

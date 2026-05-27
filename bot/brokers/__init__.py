"""
bot/brokers/__init__.py
Broker factory — Milestone 10.

Select broker via BROKER env var in .env (default: paper).
  paper        — paper trading, logs intents, no real execution (M10 default)
  ibkr         — IBKR (placeholder, not implemented until M11)
  ibkr_paper   — IBKR paper trading (M11)
  ibkr_live    — IBKR live trading (M12)
  etoro_paper  — eToro paper / dry-run, no real eToro writes (M13.3)
  etoro_real   — RESERVED for M13.5 (live eToro execution). Currently
                 raises ValueError when selected, to fail loudly instead
                 of silently falling back to paper.
"""
import os
import logging
from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)


def get_broker() -> BrokerAdapter:
    name = os.getenv('BROKER', 'paper').lower().strip()
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
        # M13.3 contract: etoro_real must fail loudly, never silently
        # fall back to paper. Live eToro execution lands in M13.5.
        raise ValueError(
            'BROKER=etoro_real is not implemented in M13.3. '
            'Use BROKER=etoro_paper for dry-run, or wait for M13.5 '
            'production etoro broker.'
        )
    log.warning('[BROKER] Unknown BROKER=%r — defaulting to paper', name)
    from bot.brokers.paper_broker import PaperBroker
    return PaperBroker()


def get_broker_name() -> str:
    name = os.getenv('BROKER', 'paper').lower().strip()
    if name in ('ibkr', 'ibkr_paper'): return 'ibkr_paper'
    if name == 'ibkr_live': return 'ibkr_live'
    return name


__all__ = ['BrokerAdapter', 'OrderIntent', 'OrderResult', 'get_broker', 'get_broker_name']

"""
bot/brokers/__init__.py
Broker factory — Milestone 10.

Select broker via BROKER env var in .env (default: paper).
  paper  — paper trading, logs intents, no real execution (M10 default)
  ibkr   — IBKR (placeholder, not implemented until M11)
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
    log.warning('[BROKER] Unknown BROKER=%r — defaulting to paper', name)
    from bot.brokers.paper_broker import PaperBroker
    return PaperBroker()


def get_broker_name() -> str:
    name = os.getenv('BROKER', 'paper').lower().strip()
    if name in ('ibkr', 'ibkr_paper'): return 'ibkr_paper'
    if name == 'ibkr_live': return 'ibkr_live'
    return name


__all__ = ['BrokerAdapter', 'OrderIntent', 'OrderResult', 'get_broker', 'get_broker_name']

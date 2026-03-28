"""
bot/brokers/ibkr_broker.py
IBKR broker adapter — PLACEHOLDER for Milestone 11.

Not implemented. Returns status='not_implemented' on all submissions.
Full IBKR integration planned for Milestone 11 (paper trading) and
Milestone 12 (live trading).

Requires: ib_insync or ibapi
"""
import logging
from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)


class IBKRBroker(BrokerAdapter):
    """IBKR broker — NOT IMPLEMENTED. Placeholder for Milestone 11."""

    @property
    def name(self) -> str:
        return 'ibkr'

    @property
    def is_live(self) -> bool:
        return False   # will be True in M12

    def submit(self, intent: OrderIntent) -> OrderResult:
        log.warning('[IBKR] submit() called but IBKR is not implemented (Milestone 11)')
        return OrderResult(
            intent=intent,
            status='not_implemented',
            reason='IBKR broker not implemented until Milestone 11',
        )

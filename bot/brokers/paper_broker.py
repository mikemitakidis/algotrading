"""
bot/brokers/paper_broker.py
Paper trading broker — the M10 default.

Accepts every valid order intent, logs it to data/paper_orders.jsonl,
and returns status='paper_logged'. No real money. No real fills.

This is the shadow-mode execution layer: every signal that passes risk checks
gets logged as if it were a real order. This creates the execution-intent
data needed for the flywheel (signal → intent → outcome linkage).
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).resolve().parent.parent.parent
ORDERS_FILE = BASE_DIR / 'data' / 'paper_orders.jsonl'


class PaperBroker(BrokerAdapter):
    """
    Paper trading broker. Logs every intent, returns paper_logged.
    The logging here is the flywheel data source for later ML training.
    """

    @property
    def name(self) -> str:
        return 'paper'

    @property
    def is_live(self) -> bool:
        return False

    def submit(self, intent: OrderIntent) -> OrderResult:
        result = OrderResult(
            intent=intent,
            status='paper_logged',
            broker_order_id=f'PAPER-{intent.signal_id}-{intent.symbol}',
            reason='Shadow mode — paper broker, no real execution',
            submitted_at=datetime.now(timezone.utc).isoformat(),
        )
        self._log(intent, result)
        log.info('[PAPER] %s %s %s | entry=%.2f stop=%.2f target=%.2f | risk=%s',
                 intent.symbol, intent.direction.upper(), intent.route,
                 intent.entry_price, intent.stop_loss, intent.target_price,
                 intent.risk_checks.get('verdict', 'ok'))
        return result

    def _log(self, intent: OrderIntent, result: OrderResult) -> None:
        try:
            ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
            record = {
                'ts':             result.submitted_at,
                'signal_id':      intent.signal_id,
                'symbol':         intent.symbol,
                'direction':      intent.direction,
                'route':          intent.route,
                'entry_price':    intent.entry_price,
                'stop_loss':      intent.stop_loss,
                'target_price':   intent.target_price,
                'valid_count':    intent.valid_count,
                'strategy_version': intent.strategy_version,
                'position_size':  intent.position_size,
                'risk_usd':       intent.risk_usd,
                'risk_checks':    intent.risk_checks,
                'broker_order_id':result.broker_order_id,
                'status':         result.status,
            }
            with open(ORDERS_FILE, 'a') as f:
                f.write(json.dumps(record) + '\n')
        except Exception as e:
            log.warning('[PAPER] Log failed: %s', e)

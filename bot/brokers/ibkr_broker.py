"""
bot/brokers/ibkr_broker.py
IBKR broker adapter — Milestone 11: paper trading via IB Gateway.

Connection: IB Gateway on the same server, paper trading mode.
  Host : 127.0.0.1 (localhost — Gateway runs on same server)
  Port : 4002       (IB Gateway paper trading standard port)
  Account: DUP623346 (paper account)

Architecture:
  Uses ib_insync for clean synchronous-style interaction with the
  IB socket API. Each submit() call opens a fresh connection, places
  the order, and disconnects. This is safe for the signal frequency
  we operate at (a few orders per day).

Order type: Bracket order
  Parent : MKT (market order at open) — fills at next bar open
  Take-profit: LMT at target_price
  Stop-loss  : STP at stop_loss
  All three legs submitted as a single bracket to IB.

M11 scope: paper trading only. is_live=False.
M12 will flip is_live=True and switch account to live.

Fail-open: any connection or order failure returns status='error'
with a clear reason string. Never raises. Bot continues running.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)

# ── Connection defaults (override via .env) ────────────────────────────────────
DEFAULT_HOST    = '127.0.0.1'
DEFAULT_PORT    = 4002          # IB Gateway paper
DEFAULT_ACCOUNT = 'DUP623346'
CLIENT_ID       = 11            # unique client ID for this bot instance


def _get_connection_params() -> tuple[str, int, str]:
    host    = os.getenv('IBKR_HOST',    DEFAULT_HOST)
    port    = int(os.getenv('IBKR_PORT', DEFAULT_PORT))
    account = os.getenv('IBKR_ACCOUNT', DEFAULT_ACCOUNT)
    return host, port, account


class IBKRBroker(BrokerAdapter):
    """
    IBKR paper trading broker via ib_insync + IB Gateway.

    Paper mode: is_live=False. All orders go to account DUP623346.
    Bracket orders: MKT entry + LMT target + STP stop.
    """

    @property
    def name(self) -> str:
        return 'ibkr_paper'

    @property
    def is_live(self) -> bool:
        return False   # M12 will set this True for live account

    def _connect(self):
        """Create and return a connected IB instance. Raises on failure."""
        from ib_insync import IB
        host, port, _ = _get_connection_params()
        ib = IB()
        ib.connect(host, port, clientId=CLIENT_ID, timeout=10, readonly=False)
        if not ib.isConnected():
            raise ConnectionError(f'IB Gateway not reachable at {host}:{port}')
        return ib

    def _make_contract(self, symbol: str):
        """Build an IBKR STK contract for a US equity."""
        from ib_insync import Stock
        return Stock(symbol, 'SMART', 'USD')

    def _make_bracket(self, ib, contract, intent: OrderIntent):
        """
        Build a bracket order: MKT parent + LMT profit-taker + STP stop-loss.
        Quantity is rounded to nearest whole share (no fractional shares on paper).
        """
        from ib_insync import MarketOrder, LimitOrder, StopOrder

        qty = max(1, round(intent.position_size or 1))
        action = 'BUY' if intent.direction == 'long' else 'SELL'
        close_action = 'SELL' if intent.direction == 'long' else 'BUY'

        # Parent: MKT order — fills at next available price
        parent = MarketOrder(action, qty)
        parent.account      = _get_connection_params()[2]
        parent.tif          = 'DAY'
        parent.transmit     = False   # hold until children are ready

        # Take-profit: LMT
        take_profit = LimitOrder(
            close_action, qty,
            lmtPrice=round(intent.target_price, 2),
        )
        take_profit.account      = parent.account
        take_profit.tif          = 'GTC'
        take_profit.parentId     = parent.orderId
        take_profit.transmit     = False

        # Stop-loss: STP
        stop_loss = StopOrder(
            close_action, qty,
            stopPrice=round(intent.stop_loss, 2),
        )
        stop_loss.account      = parent.account
        stop_loss.tif          = 'GTC'
        stop_loss.parentId     = parent.orderId
        stop_loss.transmit     = True   # transmit all three together

        return parent, take_profit, stop_loss

    def submit(self, intent: OrderIntent) -> OrderResult:
        """
        Submit a bracket order to IB Gateway (paper account).
        Returns OrderResult — never raises.
        """
        host, port, account = _get_connection_params()
        log.info('[IBKR] Submitting %s %s %s | entry=%.2f stop=%.2f target=%.2f | acct=%s',
                 intent.symbol, intent.direction.upper(), intent.route,
                 intent.entry_price, intent.stop_loss, intent.target_price, account)

        ib = None
        try:
            ib = self._connect()
            log.info('[IBKR] Connected to Gateway at %s:%d', host, port)

            contract = self._make_contract(intent.symbol)

            # Qualify the contract (resolves conId)
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                return OrderResult(
                    intent=intent, status='error',
                    reason=f'Could not qualify contract for {intent.symbol}',
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )

            # Get next valid order ID for parent
            parent, take_profit, stop_loss = self._make_bracket(ib, contract, intent)
            ib.client.reqIds(-1)
            time.sleep(0.5)
            next_id = ib.client.getReqId()
            parent.orderId     = next_id
            take_profit.parentId = next_id
            stop_loss.parentId   = next_id

            # Place all three legs
            parent_trade      = ib.placeOrder(contract, parent)
            take_profit_trade = ib.placeOrder(contract, take_profit)
            stop_trade        = ib.placeOrder(contract, stop_loss)

            # Wait briefly for acknowledgement
            ib.sleep(2)

            parent_id  = str(parent_trade.order.orderId)
            tp_id      = str(take_profit_trade.order.orderId)
            sl_id      = str(stop_trade.order.orderId)
            broker_oid = f'IB-{parent_id}-{tp_id}-{sl_id}'

            log.info('[IBKR] Bracket placed: parent=%s TP=%s SL=%s qty=%s',
                     parent_id, tp_id, sl_id,
                     round(intent.position_size or 1))

            return OrderResult(
                intent=intent,
                status='accepted',
                broker_order_id=broker_oid,
                reason=f'Bracket order placed on paper account {account}',
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )

        except ConnectionError as e:
            log.warning('[IBKR] Connection failed: %s', e)
            return OrderResult(
                intent=intent, status='connection_failed',
                reason=str(e),
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            log.warning('[IBKR] Order failed: %s', str(e)[:120])
            return OrderResult(
                intent=intent, status='error',
                reason=str(e)[:200],
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def get_positions(self) -> list:
        """Return current open positions from IB Gateway."""
        ib = None
        try:
            ib = self._connect()
            _, _, account = _get_connection_params()
            positions = ib.positions(account=account)
            result = []
            for p in positions:
                result.append({
                    'symbol':    p.contract.symbol,
                    'position':  p.position,
                    'avg_cost':  p.avgCost,
                    'account':   p.account,
                })
            return result
        except Exception as e:
            log.warning('[IBKR] get_positions failed: %s', e)
            return []
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def cancel(self, broker_order_id: str) -> bool:
        """Cancel by parent order ID (first part of IB-parent-tp-sl string)."""
        ib = None
        try:
            parent_id = int(broker_order_id.split('-')[1])
            ib = self._connect()
            open_orders = ib.openOrders()
            for order in open_orders:
                if order.orderId == parent_id:
                    ib.cancelOrder(order)
                    log.info('[IBKR] Cancelled order %s', broker_order_id)
                    return True
            log.warning('[IBKR] Order %s not found for cancellation', broker_order_id)
            return False
        except Exception as e:
            log.warning('[IBKR] cancel failed: %s', e)
            return False
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def connection_status(self) -> dict:
        """Check connectivity to IB Gateway without placing an order."""
        host, port, account = _get_connection_params()
        ib = None
        try:
            ib = self._connect()
            server_version = ib.client.serverVersion()
            return {
                'connected':      True,
                'host':           host,
                'port':           port,
                'account':        account,
                'server_version': server_version,
                'is_live':        self.is_live,
            }
        except Exception as e:
            return {
                'connected': False,
                'host':      host,
                'port':      port,
                'account':   account,
                'error':     str(e)[:120],
            }
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

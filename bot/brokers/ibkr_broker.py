"""
bot/brokers/ibkr_broker.py
IBKR broker adapter — supports both paper (M11) and live (M12) modes.

Mode selection via .env:
  BROKER=ibkr_paper   → paper account, is_live=False  (M11 default)
  BROKER=ibkr_live    → live account,  is_live=True   (M12, requires safety config)

Live mode requires ALL of these in .env or startup is refused:
  IBKR_LIVE_ACCOUNT=<live_account_id>     must match connected account
  IBKR_LIVE_CONFIRMED=yes                 explicit human confirmation
  IBKR_LIVE_PORT=4001                     live Gateway port (different from paper 4002)
  RISK_MAX_POSITION_PCT must be <= 2.0    hard cap enforced at broker level

Connection: IB Gateway on the same server.
  Paper port: 4002  (default)
  Live port:  4001  (must be set explicitly for live)

Order type: Bracket
  Parent: MKT entry → fill at next bar open
  TP:     LMT at target_price (GTC)
  SL:     STP at stop_loss    (GTC)

Order lifecycle tracked in execution_intents via status updates.
Fail-open: any error returns OrderResult(status='error'), never raises.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from bot.brokers.base import BrokerAdapter, OrderIntent, OrderResult

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
PAPER_HOST      = '127.0.0.1'
PAPER_PORT      = 4002
LIVE_PORT       = 4001
PAPER_CLIENT_ID = 11
LIVE_CLIENT_ID  = 12    # separate client ID so paper/live never collide

# Hard limit enforced at broker level for live mode — cannot be overridden
LIVE_MAX_POSITION_PCT = 2.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_live_mode() -> bool:
    return os.getenv('BROKER', 'paper').lower().strip() == 'ibkr_live'


def _get_connection_params() -> tuple[str, int, str, int]:
    """Returns (host, port, account, client_id)."""
    if _is_live_mode():
        host    = os.getenv('IBKR_HOST',        PAPER_HOST)
        port    = int(os.getenv('IBKR_LIVE_PORT', LIVE_PORT))
        account = os.getenv('IBKR_LIVE_ACCOUNT', '').strip()
        return host, port, account, LIVE_CLIENT_ID
    else:
        host    = os.getenv('IBKR_HOST',    PAPER_HOST)
        port    = int(os.getenv('IBKR_PORT', PAPER_PORT))
        account = os.getenv('IBKR_ACCOUNT', 'DUP623346')
        return host, port, account, PAPER_CLIENT_ID


# ── Live safety gate ──────────────────────────────────────────────────────────

def _check_live_safety_config() -> tuple[bool, str]:
    """
    Verify all live safety requirements are met before ANY live submission.
    Returns (safe: bool, reason: str).
    Called on every live submit() — not just startup.
    """
    checks = []

    # 1. Explicit live account ID must be configured
    live_account = os.getenv('IBKR_LIVE_ACCOUNT', '').strip()
    if not live_account:
        checks.append('IBKR_LIVE_ACCOUNT not set in .env')

    # 2. Human must have explicitly confirmed live mode
    confirmed = os.getenv('IBKR_LIVE_CONFIRMED', '').strip().lower()
    if confirmed != 'yes':
        checks.append('IBKR_LIVE_CONFIRMED != yes in .env')

    # 3. Live port must be explicitly set (prevents accidentally using paper port)
    live_port = os.getenv('IBKR_LIVE_PORT', '').strip()
    if not live_port:
        checks.append('IBKR_LIVE_PORT not set in .env')

    # 4. Hard position size cap — cannot exceed 2% in live mode
    max_pos = float(os.getenv('RISK_MAX_POSITION_PCT', '2.0'))
    if max_pos > LIVE_MAX_POSITION_PCT:
        checks.append(
            f'RISK_MAX_POSITION_PCT={max_pos} exceeds live hard cap of {LIVE_MAX_POSITION_PCT}'
        )

    if checks:
        return False, ' | '.join(checks)
    return True, 'all live safety checks passed'


def _gateway_available(host: str, port: int, timeout: float = 3.0) -> bool:
    """Fast TCP check — returns True if port is listening, False otherwise."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class IBKRBroker(BrokerAdapter):
    """
    IBKR broker — paper (M11) and live (M12) modes via ib_insync + IB Gateway.
    """

    @property
    def name(self) -> str:
        return 'ibkr_live' if _is_live_mode() else 'ibkr_paper'

    @property
    def is_live(self) -> bool:
        return _is_live_mode()

    # ── Connection ─────────────────────────────────────────────────────────────

    def _connect(self):
        from ib_insync import IB
        host, port, _, client_id = _get_connection_params()
        ib = IB()
        ib.connect(host, port, clientId=client_id, timeout=10, readonly=False)
        if not ib.isConnected():
            raise ConnectionError(f'IB Gateway not reachable at {host}:{port}')
        return ib

    def _verify_account(self, ib) -> tuple[bool, str]:
        """
        Verify the connected account matches the configured account.
        Critical for live mode — prevents submitting to wrong account.
        """
        _, _, expected_account, _ = _get_connection_params()
        if not expected_account:
            return False, 'no account configured'
        try:
            managed = ib.managedAccounts()
            if expected_account not in managed:
                return False, (
                    f'Account mismatch: configured={expected_account} '
                    f'connected={managed}'
                )
            return True, f'account {expected_account} verified'
        except Exception as e:
            return False, f'account verification failed: {e}'

    # ── Reconciliation ─────────────────────────────────────────────────────────

    def reconcile(self) -> dict:
        """
        Pull open orders and positions from IB Gateway.
        Used before submitting live orders to detect stale/unknown positions.
        Returns dict with open_orders, positions, warnings.
        """
        ib = None
        result = {'open_orders': [], 'positions': [], 'warnings': []}
        try:
            ib = self._connect()
            _, _, account, _ = _get_connection_params()

            # Open orders
            orders = ib.openOrders()
            for o in orders:
                result['open_orders'].append({
                    'order_id': o.orderId,
                    'symbol':   getattr(o, 'symbol', '?'),
                    'action':   getattr(o, 'action', '?'),
                    'qty':      getattr(o, 'totalQuantity', 0),
                    'status':   getattr(o, 'status', '?'),
                })

            # Positions
            positions = ib.positions(account=account)
            for p in positions:
                result['positions'].append({
                    'symbol':   p.contract.symbol,
                    'position': p.position,
                    'avg_cost': p.avgCost,
                })
                if abs(p.position) > 0:
                    result['warnings'].append(
                        f'Open position: {p.contract.symbol} qty={p.position}'
                    )

        except Exception as e:
            result['warnings'].append(f'reconcile failed: {e}')
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass
        return result

    # ── Order submission ───────────────────────────────────────────────────────

    def _make_bracket(self, intent: OrderIntent, account: str):
        from ib_insync import MarketOrder, LimitOrder, StopOrder
        qty          = max(1, round(intent.position_size or 1))
        action       = 'BUY' if intent.direction == 'long' else 'SELL'
        close_action = 'SELL' if intent.direction == 'long' else 'BUY'

        parent = MarketOrder(action, qty)
        parent.account  = account
        parent.tif      = 'DAY'
        parent.transmit = False

        take_profit = LimitOrder(close_action, qty, lmtPrice=round(intent.target_price, 2))
        take_profit.account  = account
        take_profit.tif      = 'GTC'
        take_profit.transmit = False

        stop_loss = StopOrder(close_action, qty, stopPrice=round(intent.stop_loss, 2))
        stop_loss.account  = account
        stop_loss.tif      = 'GTC'
        stop_loss.transmit = True   # transmits all three

        return parent, take_profit, stop_loss

    def submit(self, intent: OrderIntent) -> OrderResult:
        """
        Submit bracket order. Live mode runs full safety gate before proceeding.
        Never raises — returns OrderResult with status='error' on any failure.
        """
        host, port, account, _ = _get_connection_params()

        # ── Kill switch (checked first — before any network probe) ─────────────
        from bot.kill_switch import is_kill_switch_active
        if is_kill_switch_active():
            log.error('[IBKR] KILL SWITCH ACTIVE — all submissions blocked')
            return OrderResult(
                intent=intent,
                status='kill_switch_active',
                reason='Kill switch is active. Deactivate via dashboard before submitting.',
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )

        # ── Runtime M13.4A broker-allocation policy (P0-3, audit 2026-06-05)
        # Re-checks the policy state at submit time so dashboard
        # toggles of the global / per-broker kill_switches take
        # effect without scanner restart. TTL-cached (default 5s)
        # so the hot path is not slowed by a DB read per signal.
        # Fail-safe per audit Correction A: no cached policy + DB
        # unavailable → signal_only_skipped (never trade on unknown
        # policy state).
        from bot.runtime_policy import get_signal_only_reason as _rt_policy
        _skip, _reason = _rt_policy(self.name)
        if _skip:
            log.warning('[IBKR] runtime policy says skip: reason=%s',
                          _reason)
            return OrderResult(
                intent=intent,
                status='signal_only_skipped',
                broker_order_id=None,
                reason=_reason,
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )

        # ── Live safety gate (checked before any network probe) ─────────────────
        # ORDER MATTERS: safety gate before _gateway_available() so that
        # incomplete live config returns live_safety_blocked, not connection_failed
        if self.is_live:
            safe, reason = _check_live_safety_config()
            if not safe:
                log.error('[IBKR-LIVE] BLOCKED — safety gate failed: %s', reason)
                return OrderResult(
                    intent=intent,
                    status='live_safety_blocked',
                    reason=f'Live safety gate: {reason}',
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )

        # ── Pre-submit gateway health check (after safety gate) ─────────────────
        if not _gateway_available(host, port):
            log.error('[IBKR] Gateway not reachable at %s:%d — order blocked. '
                      'Run: systemctl start ibgateway', host, port)
            return OrderResult(
                intent=intent,
                status='connection_failed',
                reason=f'IB Gateway not reachable at {host}:{port} — '
                       f'run: systemctl start ibgateway',
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )

        log.info('[IBKR%s] Submitting %s %s | entry=%.2f stop=%.2f target=%.2f | acct=%s',
                 '-LIVE' if self.is_live else '', intent.symbol,
                 intent.direction.upper(), intent.entry_price,
                 intent.stop_loss, intent.target_price, account)

        ib = None
        try:
            ib = self._connect()

            # ── Account verification (live mode: hard block on mismatch) ───────
            acct_ok, acct_msg = self._verify_account(ib)
            if not acct_ok:
                log.error('[IBKR] Account verification failed: %s', acct_msg)
                return OrderResult(
                    intent=intent,
                    status='account_mismatch',
                    reason=acct_msg,
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )
            log.info('[IBKR] %s', acct_msg)

            # ── Contract qualification ─────────────────────────────────────────
            from ib_insync import Stock
            contract  = Stock(intent.symbol, 'SMART', 'USD')
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                return OrderResult(
                    intent=intent, status='error',
                    reason=f'Could not qualify contract for {intent.symbol}',
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )

            # ── Bracket order ──────────────────────────────────────────────────
            parent, take_profit, stop_loss = self._make_bracket(intent, account)
            ib.client.reqIds(-1)
            time.sleep(0.5)
            next_id = ib.client.getReqId()
            parent.orderId       = next_id
            take_profit.parentId = next_id
            stop_loss.parentId   = next_id

            parent_trade = ib.placeOrder(contract, parent)
            tp_trade     = ib.placeOrder(contract, take_profit)
            sl_trade     = ib.placeOrder(contract, stop_loss)

            # Wait for broker acknowledgement — Error 321/Read-Only
            # arrives asynchronously in trade.log, not as an exception
            ib.sleep(3)

            # ── Verify parent order was genuinely accepted ─────────────────
            # Check trade.log for error entries (e.g. Error 321 Read-Only)
            error_entries = [
                e for e in (parent_trade.log or [])
                if getattr(e, 'errorCode', 0) and getattr(e, 'errorCode', 0) > 0
            ]
            if error_entries:
                err = error_entries[0]
                reason_str = (
                    f'IBKR rejected order: Error {err.errorCode} — {err.message}'
                )
                log.error('[IBKR] Order REJECTED by broker: %s', reason_str)
                return OrderResult(
                    intent=intent,
                    status='broker_rejected',
                    reason=reason_str,
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )

            # Check order status is genuinely active at broker
            from ib_insync import OrderStatus as _OS
            parent_status = parent_trade.orderStatus.status
            if parent_status == 'Inactive':
                reason_str = (
                    f'Order Inactive at broker — likely Read-Only mode or '
                    f'permissions issue. Check Gateway API settings.'
                )
                log.error('[IBKR] Order INACTIVE (broker rejected): %s', reason_str)
                return OrderResult(
                    intent=intent,
                    status='broker_rejected',
                    reason=reason_str,
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )

            if parent_status not in _OS.ActiveStates | {'Filled'}:
                reason_str = (
                    f'Unexpected order status from broker: {parent_status}. '
                    f'Expected one of {_OS.ActiveStates}.'
                )
                log.error('[IBKR] Unexpected order status: %s', reason_str)
                return OrderResult(
                    intent=intent,
                    status='broker_rejected',
                    reason=reason_str,
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                )

            # ── Genuinely accepted ─────────────────────────────────────────
            # Use broker-assigned permId as canonical order reference
            # permId is assigned server-side and is unique across sessions
            parent_perm = parent_trade.orderStatus.permId
            parent_id   = str(parent_trade.order.orderId)
            tp_id       = str(tp_trade.order.orderId)
            sl_id       = str(sl_trade.order.orderId)
            if parent_perm and parent_perm != 0:
                broker_oid = f'IB-PERM-{parent_perm}'
            else:
                broker_oid = f'IB-{parent_id}-{tp_id}-{sl_id}'

            mode_tag = 'LIVE' if self.is_live else 'PAPER'
            log.info('[IBKR-%s] Bracket CONFIRMED: parent=%s TP=%s SL=%s '
                     'status=%s qty=%d',
                     mode_tag, parent_id, tp_id, sl_id,
                     parent_status, round(intent.position_size or 1))

            return OrderResult(
                intent=intent,
                status='accepted',
                broker_order_id=broker_oid,
                reason=f'Bracket confirmed on {mode_tag} account {account} '
                       f'status={parent_status}',
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

    # ── Position / order queries ───────────────────────────────────────────────

    def get_positions(self) -> list:
        ib = None
        try:
            ib = self._connect()
            _, _, account, _ = _get_connection_params()
            return [
                {'symbol': p.contract.symbol, 'position': p.position,
                 'avg_cost': p.avgCost, 'account': p.account}
                for p in ib.positions(account=account)
            ]
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
        """Cancel an open order at IBKR by broker_order_id.

        Supports two ID formats — both produced by submit():
          * Canonical (preferred, since the M12 ChatGPT correction):
              'IB-PERM-{permId}'         e.g. 'IB-PERM-1735201840'
            Matched against `order.permId`. permId is broker-assigned
            and stable across sessions, which is why this is the
            preferred audit ref.
          * Legacy fallback (only used when permId failed to
            propagate at submit time):
              'IB-{orderId}-{tp}-{sl}'   e.g. 'IB-42-43-44'
            Matched against `order.orderId`.

        Returns True iff exactly one open order matched the ID and
        was successfully cancelled. Otherwise returns False (no
        match, broker error, or unknown ID format) and logs a
        clear warning.

        P0-2 fix (M1-M16 audit, 2026-06-05): the previous
        implementation did `int(broker_order_id.split('-')[1])`,
        which raised ValueError on the canonical 'IB-PERM-...'
        format ('PERM' is not an int). The exception was swallowed
        by a broad `except Exception` so cancel returned False
        silently — the order remained live at IBKR while the
        operator saw "cancel failed" with no actionable detail.
        """
        if not isinstance(broker_order_id, str) or not broker_order_id:
            log.warning('[IBKR] cancel: empty or non-string id %r',
                          broker_order_id)
            return False

        # Determine match mode + extract the lookup value.
        if broker_order_id.startswith('IB-PERM-'):
            suffix = broker_order_id[len('IB-PERM-'):]
            try:
                lookup_value = int(suffix)
            except ValueError:
                log.warning('[IBKR] cancel: malformed PERM id %r '
                              '(suffix %r not int)',
                              broker_order_id, suffix)
                return False
            match_attr = 'permId'
        elif broker_order_id.startswith('IB-'):
            # Legacy 'IB-{parent}-{tp}-{sl}' shape.
            parts = broker_order_id.split('-')
            if len(parts) < 2:
                log.warning('[IBKR] cancel: legacy id missing parent '
                              'segment: %r', broker_order_id)
                return False
            try:
                lookup_value = int(parts[1])
            except ValueError:
                log.warning('[IBKR] cancel: legacy id parent segment '
                              'not int: %r', broker_order_id)
                return False
            match_attr = 'orderId'
        else:
            log.warning('[IBKR] cancel: unknown broker_order_id format: %r',
                          broker_order_id)
            return False

        ib = None
        try:
            ib = self._connect()
            for order in ib.openOrders():
                candidate = getattr(order, match_attr, None)
                if candidate == lookup_value:
                    ib.cancelOrder(order)
                    log.info('[IBKR] Cancelled %s (%s=%s)',
                               broker_order_id, match_attr, lookup_value)
                    return True
            log.warning('[IBKR] cancel: no open order matches %s '
                          '(%s=%s)', broker_order_id, match_attr,
                          lookup_value)
            return False
        except Exception as e:
            log.warning('[IBKR] cancel failed for %s: %s',
                          broker_order_id, e)
            return False
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def flatten_paper_position(self, symbol: str,
                               confirm: bool = False) -> dict:
        """Paper-ONLY cleanup primitive (M21.1extra-B2flat).

        Safely flattens ONE explicit paper position and cancels its residual
        open orders, in a strict, no-retry order designed to avoid the
        fill/cleanup race that B2a proved the entry bracket could not handle:

          1. refuse live mode; assert paper account + port; require confirm=True
          2. check the kill switch BEFORE any broker action
          3. CANCEL the target symbol's open orders FIRST (so a still-live SL/TP
             leg cannot fill mid-flatten and flip the position)
          4. RE-READ the position for the target symbol
          5. if a residual position remains, place ONE offsetting MarketOrder
             sized to the ACTUAL remaining quantity (no retry loop)
          6. prove final state on the SAME connection (contract-aware
             openTrades() + positions()); flatten_confirmed only if no target/
             ambiguous open trades remain AND no residual target position

        Never raises. Returns a dict with truthful fields; flatten_confirmed is
        only True when the same-connection final proof shows the symbol
        genuinely flat. This method NEVER originates an ENTRY order — the only
        broker action it can take is cancelling the target's orders and placing
        a single offsetting close for an existing position. It does NOT call
        self.reconcile() (which would open a second IB connection with the same
        client id and collide with this method's open connection).
        """
        from datetime import datetime, timezone
        result = {
            'symbol': symbol,
            'live_mode_refused': False,
            'paper_asserted': False,
            'account_verified': False,
            'kill_switch_active': False,
            'confirmed_flag': bool(confirm),
            'already_flat': False,
            'cancelled_order_ids': [],
            'post_cancel_open_orders_cleared': None,
            'close_order_placed': False,
            'close_order_id': None,
            'flatten_confirmed': False,
            'remaining_positions': [],
            'remaining_open_orders': [],
            'warnings': [],
            'ts': datetime.now(timezone.utc).isoformat(),
        }

        # 1. paper-only + explicit target + explicit confirmation
        if _is_live_mode():
            result['live_mode_refused'] = True
            result['warnings'].append('live mode: flatten refused (paper only)')
            return result
        host, port, account, _ = _get_connection_params()
        if int(port) != int(PAPER_PORT):
            result['warnings'].append(
                'expected paper port %s, got %s: refused' % (PAPER_PORT, port))
            return result
        if str(account).strip() != 'DUP623346':
            result['warnings'].append(
                'expected paper account DUP623346, got %r: refused' % account)
            return result
        result['paper_asserted'] = True
        if not symbol or not isinstance(symbol, str):
            result['warnings'].append('explicit symbol required: refused')
            return result
        if not confirm:
            result['warnings'].append(
                'confirm=True required to flatten: refused')
            return result

        # 2. kill switch before any broker action
        from bot.kill_switch import is_kill_switch_active
        if is_kill_switch_active():
            result['kill_switch_active'] = True
            result['warnings'].append(
                'kill switch active: no broker action taken')
            return result

        ib = None
        try:
            from ib_insync import MarketOrder
            ib = self._connect()

            # 2b. verify account AFTER connect, BEFORE any cancel/close — mirror
            # submit()'s connected-account verification. Refuse on failure.
            acct_ok, acct_msg = self._verify_account(ib)
            result['account_verified'] = bool(acct_ok)
            if not acct_ok:
                result['warnings'].append(
                    'account verification failed: %s' % acct_msg)
                result['flatten_confirmed'] = False
                return result

            # 3. cancel the target symbol's OPEN ORDERS first, using a
            # contract-aware source. ib.openTrades() yields Trade objects that
            # reliably carry BOTH trade.contract and trade.order, unlike
            # ib.openOrders() whose Order entries may lack a contract. We match
            # on trade.contract.symbol and cancel by trade.order.
            def _target_open_trades():
                out = []
                ambiguous = False
                for tr in list(ib.openTrades()):
                    c = getattr(tr, 'contract', None)
                    o = getattr(tr, 'order', None)
                    csym = getattr(c, 'symbol', None)
                    if o is None or csym is None:
                        # cannot prove which symbol this order belongs to
                        ambiguous = True
                        continue
                    if csym == symbol:
                        out.append(tr)
                return out, ambiguous

            target_trades, ambiguous_before = _target_open_trades()
            if ambiguous_before:
                # a live order whose target we cannot prove -> fail closed
                result['post_cancel_open_orders_cleared'] = False
                result['warnings'].append(
                    'could not prove target open orders cleared: ambiguous '
                    'open trade without contract/order; refusing to close')
                result['flatten_confirmed'] = False
                return result
            for tr in target_trades:
                try:
                    ib.cancelOrder(tr.order)
                    result['cancelled_order_ids'].append(
                        str(getattr(tr.order, 'permId',
                                    getattr(tr.order, 'orderId', '?'))))
                except Exception as e:
                    result['warnings'].append(
                        'cancel of open order failed: %s' % e)
            ib.sleep(2)

            # 3b. RE-READ the SAME contract-aware source after cancel and PROVE
            # the target's orders cleared BEFORE placing any close. If any target
            # trade remains, or the source is ambiguous, do NOT place a market
            # close on top of live SL/TP legs — the exact race B2flat removes.
            post_target_trades, ambiguous_after = _target_open_trades()
            if ambiguous_after or post_target_trades:
                result['post_cancel_open_orders_cleared'] = False
                result['warnings'].append(
                    'could not prove target open orders cleared: refusing to '
                    'place a close (flatten not confirmed)')
                result['flatten_confirmed'] = False
                return result
            result['post_cancel_open_orders_cleared'] = True

            # 4. re-read the position for the target symbol
            target = None
            for pos in ib.positions(account=account):
                psym = getattr(getattr(pos, 'contract', None), 'symbol', None)
                if psym == symbol and float(getattr(pos, 'position', 0)) != 0:
                    target = pos
                    break


            # 5. offsetting close for the ACTUAL remaining qty (one order, no retry)
            if target is not None:
                net = float(target.position)
                qty = abs(net)
                close_action = 'SELL' if net > 0 else 'BUY'
                contract = target.contract
                close_order = MarketOrder(close_action, qty)
                if account:
                    close_order.account = account
                trade = ib.placeOrder(contract, close_order)
                result['close_order_placed'] = True
                result['close_order_id'] = str(
                    getattr(trade.order, 'orderId', '?'))
                ib.sleep(3)
            else:
                # no residual position; if we also cancelled nothing, the
                # target was already flat coming in.
                if not result['cancelled_order_ids']:
                    result['already_flat'] = True
                result['warnings'].append(
                    'no residual position for %s (nothing to close)' % symbol)

            # 6. FINAL state proof on the SAME active ib connection. We must NOT
            # call self.reconcile() here: reconcile() opens its OWN _connect()
            # with the same client id, which collides with this method's still-
            # open connection on the real gateway. Instead re-use `ib` directly.
            #
            # (a) contract-aware openTrades() — a remaining target trade or any
            #     ambiguous trade means we cannot claim confirmed flat.
            final_target_trades, ambiguous_final = _target_open_trades()
            if ambiguous_final or final_target_trades:
                result['post_cancel_open_orders_cleared'] = False
                result['warnings'].append(
                    'post-flatten open trades not cleared / ambiguous: '
                    'not confirmed flat')
                result['flatten_confirmed'] = False
                return result

            # (b) positions() on the SAME connection — residual target position
            #     means not flat.
            rem_pos = []
            for pos in ib.positions(account=account):
                psym = getattr(getattr(pos, 'contract', None), 'symbol', None)
                if psym == symbol and float(getattr(pos, 'position', 0)) != 0:
                    rem_pos.append({'symbol': psym,
                                    'position': float(pos.position)})

            # (c) openOrders() on the SAME connection as SECONDARY info only
            #     (openTrades in (a) is the authoritative order-state source).
            rem_ord = []
            for o in ib.openOrders():
                osym = getattr(getattr(o, 'contract', None), 'symbol', None)
                if osym == symbol:
                    rem_ord.append({'symbol': osym,
                                    'order_id': getattr(o, 'orderId', None)})

            result['remaining_positions'] = rem_pos
            result['remaining_open_orders'] = rem_ord
            # (d) confirmed flat only if openTrades clean/unambiguous (above) AND
            #     no residual target position. openOrders is secondary info and
            #     does not by itself flip a clean openTrades result, but a
            #     residual position always blocks confirmation.
            result['flatten_confirmed'] = (not rem_pos)
            if not result['flatten_confirmed']:
                result['warnings'].append(
                    'flatten NOT confirmed: residual target position remains')
            return result
        except Exception as e:
            result['warnings'].append('flatten error: %s' % e)
            result['flatten_confirmed'] = False
            return result
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def connection_status(self) -> dict:
        host, port, account, _ = _get_connection_params()
        ib = None
        try:
            ib = self._connect()
            acct_ok, acct_msg = self._verify_account(ib)
            recon = self.reconcile() if self.is_live else {}
            status = {
                'connected':      True,
                'host':           host,
                'port':           port,
                'account':        account,
                'account_verified': acct_ok,
                'account_msg':    acct_msg,
                'server_version': ib.client.serverVersion(),
                'is_live':        self.is_live,
                'mode':           'LIVE' if self.is_live else 'PAPER',
            }
            if self.is_live:
                status['open_positions'] = recon.get('positions', [])
                status['open_orders']    = len(recon.get('open_orders', []))
                status['warnings']       = recon.get('warnings', [])
                safe, safety_msg = _check_live_safety_config()
                status['live_safety_ok'] = safe
                status['live_safety_msg']= safety_msg
            return status
        except Exception as e:
            return {
                'connected': False, 'host': host, 'port': port,
                'account': account, 'error': str(e)[:120],
                'is_live': self.is_live,
            }
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except Exception:
                    pass

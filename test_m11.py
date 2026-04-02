#!/usr/bin/env python3
"""
test_m11.py — M11 controlled verification script.

Tests the full IBKR paper trading path:
  1. Connection test — can the bot reach IB Gateway?
  2. Contract qualification — does IBKR recognise AAPL?
  3. Bracket order submission — does a paper order land in TWS/Gateway?
  4. Flywheel logging — does execution_intents row get created?

This script does NOT use the live scanner.
It submits ONE real bracket order to your IB Gateway paper account (DUP623346).
The order will appear in IB Gateway's paper trading blotter.

Run ONLY when IB Gateway is running and connected in paper mode.

Usage: python test_m11.py
       python test_m11.py --dry-run    # connection test only, no order
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_env = BASE_DIR / '.env'
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        pass

sys.path.insert(0, str(BASE_DIR))

from bot.brokers.ibkr_broker import IBKRBroker
from bot.brokers.base import OrderIntent
from bot.risk import RiskManager
from bot.flywheel import init_flywheel_tables, log_intent


DB_PATH     = BASE_DIR / 'data' / 'signals.db'
TEST_SIG_ID = 888888   # clearly synthetic


def run(dry_run: bool = False):
    print('\n' + '='*60)
    print('M11 IBKR PAPER TRADING VERIFICATION')
    print('='*60)

    broker = IBKRBroker()
    print(f'Broker : {broker.name}')
    print(f'Is live: {broker.is_live}')

    # ── Step 1: Connection test ───────────────────────────────────────────
    print('\n[1] Testing IB Gateway connection...')
    status = broker.connection_status()
    print(f'    connected      : {status["connected"]}')
    print(f'    host:port      : {status.get("host")}:{status.get("port")}')
    print(f'    account        : {status.get("account")}')
    if status.get('server_version'):
        print(f'    server_version : {status.get("server_version")}')
    if not status['connected']:
        print(f'\n    ERROR: {status.get("error")}')
        print('\n    IB Gateway is not reachable.')
        print('    Make sure IB Gateway is:')
        print('      1. Running on the server')
        print('      2. Logged in to PAPER account DUP623346')
        print('      3. API connections enabled (port 4002)')
        print('      4. "Allow connections from localhost" checked')
        sys.exit(1)
    print('    PASS: IB Gateway connected')

    if dry_run:
        print('\nDry-run mode — skipping order submission.')
        print('Connection test passed. Run without --dry-run to test full order path.')
        return

    # ── Step 2: Build order intent ────────────────────────────────────────
    print('\n[2] Building test order intent (AAPL long, small size)...')
    intent = OrderIntent(
        signal_id      = TEST_SIG_ID,
        symbol         = 'AAPL',
        direction      = 'long',
        route          = 'IBKR',
        entry_price    = 213.50,
        stop_loss      = 207.50,
        target_price   = 224.00,
        valid_count    = 3,
        strategy_version = 1,
        position_size  = 1,          # 1 share only for test
        risk_usd       = 6.00,
    )
    print(f'    symbol={intent.symbol}  direction={intent.direction}')
    print(f'    entry={intent.entry_price}  stop={intent.stop_loss}  target={intent.target_price}')
    print(f'    qty=1 share (test — minimum size)')

    # Risk check
    rm = RiskManager()
    passed, checks, reason = rm.evaluate(intent)
    intent.risk_checks = checks
    print(f'\n[3] Risk check: verdict={checks["verdict"]}  reason={reason or "none"}')
    if not passed:
        if reason == 'market_closed_weekend':
            print('    WARNING: market is closed (weekend/holiday)')
            print('    Proceeding anyway — paper order will be queued for next open')
        else:
            print(f'    Risk check failed: {reason}')
            print('    Proceeding to test the broker path regardless...')

    # ── Step 3: Submit to IBKR paper ──────────────────────────────────────
    print('\n[4] Submitting bracket order to IB Gateway paper account...')
    result = broker.submit(intent)
    print(f'    status         : {result.status}')
    print(f'    broker_order_id: {result.broker_order_id}')
    print(f'    reason         : {result.reason}')
    print(f'    submitted_at   : {result.submitted_at}')

    if result.status == 'broker_rejected':
        print(f'\n    FAIL: broker REJECTED the order — {result.reason}')
        print('    This means Read-Only mode is active or API permissions are wrong.')
        print('    Fix: restart Gateway after setting ReadOnlyApi=no in config.ini')
        sys.exit(1)
    if result.status not in ('accepted',):
        print(f'\n    FAIL: expected accepted, got {result.status}')
        print(f'    Reason: {result.reason}')
        sys.exit(1)
    # Verify broker_order_id is a real unique ID, not a static fake
    if result.broker_order_id in (None, '', 'IB-4-5-6'):
        print(f'\n    WARN: broker_order_id={result.broker_order_id} — '
              f'may be a locally-generated ID not confirmed by broker')
    print(f'    PASS: order accepted by IB Gateway (status=accepted)')
    print(f'    broker_order_id={result.broker_order_id}')

    # ── Step 4: Log to flywheel ───────────────────────────────────────────
    print('\n[5] Logging execution intent to flywheel DB...')
    conn = sqlite3.connect(str(DB_PATH))
    init_flywheel_tables(conn)
    intent_id = log_intent(
        conn, TEST_SIG_ID,
        symbol='AAPL', direction='long', route='IBKR',
        entry_price=213.50, stop_loss=207.50, target_price=224.00,
        position_size=1, risk_usd=6.00,
        valid_count=3, strategy_version=1,
        broker=broker.name,
        status=result.status,
        broker_order_id=result.broker_order_id,
        rejection_reason=reason,
        risk_checks=checks,
    )
    assert intent_id, "execution_intents insert failed"

    row = conn.execute(
        'SELECT id, symbol, direction, status, broker_order_id, broker '
        'FROM execution_intents WHERE id=?', (intent_id,)
    ).fetchone()
    print(f'    DB row id={row[0]}  symbol={row[1]}  direction={row[2]}')
    print(f'    status={row[3]}  broker={row[5]}')
    print(f'    broker_order_id={row[4]}')
    print('    PASS: execution_intent logged')

    # ── Step 5: Check positions ───────────────────────────────────────────
    print('\n[6] Checking IB Gateway positions (after 3s)...')
    import time; time.sleep(3)
    positions = broker.get_positions()
    if positions:
        for p in positions:
            print(f'    {p["symbol"]}  pos={p["position"]}  avg_cost={p["avg_cost"]}')
    else:
        print('    No positions yet (order may be pending fill or market closed)')

    conn.close()

    print('\n' + '='*60)
    print('M11 VERIFICATION COMPLETE')
    print('='*60)
    print(f'  Broker order ID : {result.broker_order_id}')
    print(f'  Check IB Gateway paper blotter to see the bracket order.')
    print(f'  execution_intents row id={intent_id}  status={result.status}')
    print('\nNOTE: test used signal_id=888888, 1 share only.')
    print('      Cancel the test order in IB Gateway if needed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Connection test only — no order submitted')
    args = parser.parse_args()
    run(dry_run=args.dry_run)

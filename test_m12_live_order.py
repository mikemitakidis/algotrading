#!/usr/bin/env python3
"""
test_m12_live_order.py — Final M12 controlled live execution proof.

Submits exactly 1 share of a specified symbol to the live IBKR account.
Uses fixed quantity — bypasses portfolio risk sizing entirely.
All other live safety controls remain active:
  - kill switch checked
  - safety gate checked
  - account verification checked
  - reconciliation checked

This script is for explicit human-approved controlled testing ONLY.
Do not run this script without reading the preconditions below.

Preconditions:
  1. BROKER=ibkr_live in .env
  2. IBKR_LIVE_ACCOUNT=<live_account_id> in .env
  3. IBKR_LIVE_PORT=4001 in .env
  4. IBKR_LIVE_CONFIRMED=yes in .env
  5. RISK_MAX_POSITION_PCT=2.0 (default is fine)
  6. RISK_MAX_OPEN_POSITIONS=2 in .env (allows for existing dust positions)
  7. Kill switch is inactive
  8. Live Gateway running on port 4001
  9. US market hours (Mon-Fri 14:30-21:00 UTC) for immediate fill
  10. You have explicitly approved this run

Usage:
  python3 test_m12_live_order.py --symbol F --qty 1 --dry-run
  python3 test_m12_live_order.py --symbol F --qty 1
"""
import argparse
import json
import os
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


def run(symbol: str, qty: int, dry_run: bool):
    print('\n' + '='*60)
    print('M12 CONTROLLED LIVE ORDER TEST')
    print('='*60)
    print(f'Symbol   : {symbol}')
    print(f'Quantity : {qty} share(s) — FIXED, not risk-sized')
    print(f'Mode     : {"DRY RUN — no order submitted" if dry_run else "LIVE — real order will be placed"}')

    from bot.brokers.ibkr_broker import IBKRBroker, _get_connection_params
    from bot.brokers.base import OrderIntent
    from bot.kill_switch import get_kill_switch_state
    from bot.flywheel import init_flywheel_tables, log_intent

    # ── Pre-flight checks ──────────────────────────────────────────────────
    print('\n[PRE-FLIGHT]')

    broker_name = os.getenv('BROKER', 'paper').lower()
    if broker_name != 'ibkr_live':
        print(f'  FAIL: BROKER={broker_name} — must be ibkr_live')
        sys.exit(1)
    print(f'  BROKER={broker_name} ✓')

    ks = get_kill_switch_state()
    if ks.get('active'):
        print(f'  FAIL: Kill switch is ACTIVE — deactivate before running')
        sys.exit(1)
    print(f'  Kill switch: inactive ✓')

    _, _, account, _ = _get_connection_params()
    if not account:
        print('  FAIL: IBKR_LIVE_ACCOUNT not set in .env')
        sys.exit(1)
    print(f'  Account: {account} ✓')

    if dry_run:
        print('\nDRY RUN — stopping before order submission.')
        print('Pre-flight passed. Run without --dry-run to place real order.')
        return

    # ── Connection + account verification ─────────────────────────────────
    print('\n[1] Connecting to live Gateway...')
    broker = IBKRBroker()
    status = broker.connection_status()
    print(f'  connected      : {status["connected"]}')
    print(f'  account        : {status.get("account")}')
    print(f'  account_verified: {status.get("account_verified")}')
    print(f'  server_version : {status.get("server_version")}')

    if not status['connected']:
        print(f'\n  FAIL: {status.get("error")}')
        sys.exit(1)
    if not status.get('account_verified'):
        print(f'\n  FAIL: {status.get("account_msg")}')
        sys.exit(1)
    print('  PASS: connected and account verified')

    # ── Reconciliation ─────────────────────────────────────────────────────
    print('\n[2] Reconciling live broker state...')
    recon = broker.reconcile()
    print(f'  open positions : {recon["positions"]}')
    print(f'  open orders    : {len(recon["open_orders"])}')
    if recon['warnings']:
        print(f'  warnings       : {recon["warnings"]}')

    # Block if symbol already has open position or order
    conflict = any(
        p.get('symbol') == symbol
        for p in recon['positions']
        if abs(p.get('position', 0)) > 0
    ) or any(
        o.get('symbol') == symbol
        for o in recon['open_orders']
    )
    if conflict:
        print(f'  FAIL: {symbol} already has open position or order — cannot submit duplicate')
        sys.exit(1)
    print('  PASS: no conflict for', symbol)

    # ── Get current price for bracket levels ──────────────────────────────
    print(f'\n[3] Fetching current price for {symbol}...')
    try:
        from ib_insync import IB, Stock
        ib = IB()
        host, port, _, client_id = _get_connection_params()
        ib.connect(host, port, clientId=client_id, timeout=10, readonly=False)
        contract = Stock(symbol, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, '', False, False)
        ib.sleep(2)
        price = ticker.last or ticker.close or ticker.bid
        ib.cancelMktData(contract)
        ib.disconnect()
        if not price or price <= 0:
            raise ValueError(f'Could not get price for {symbol}')
        price = round(float(price), 2)
        stop  = round(price * 0.98, 2)   # 2% stop
        target= round(price * 1.02, 2)   # 2% target
        print(f'  price={price}  stop={stop}  target={target}')
    except Exception as e:
        print(f'  FAIL: could not get price: {e}')
        sys.exit(1)

    # ── Build intent with FIXED quantity ──────────────────────────────────
    print(f'\n[4] Building fixed-quantity intent ({qty} share)...')
    intent = OrderIntent(
        signal_id      = 777001,
        symbol         = symbol,
        direction      = 'long',
        route          = 'IBKR',
        entry_price    = price,
        stop_loss      = stop,
        target_price   = target,
        valid_count    = 3,
        strategy_version = 1,
        position_size  = qty,      # FIXED — bypasses risk sizing
        risk_usd       = round(qty * (price - stop), 2),
    )
    print(f'  symbol={intent.symbol}  qty={intent.position_size}  entry={intent.entry_price}')
    print(f'  stop={intent.stop_loss}  target={intent.target_price}  risk_usd=${intent.risk_usd}')
    print(f'  NOTE: position_size is FIXED at {qty}, not portfolio-sized')

    # ── Submit ──────────────────────────────────────────────────────────────
    print(f'\n[5] Submitting to live IBKR account {account}...')
    result = broker.submit(intent)
    print(f'  status         : {result.status}')
    print(f'  broker_order_id: {result.broker_order_id}')
    print(f'  reason         : {result.reason}')
    print(f'  submitted_at   : {result.submitted_at}')

    # ── Log to flywheel ────────────────────────────────────────────────────
    print('\n[6] Logging to execution_intents...')
    db_path = BASE_DIR / 'data' / 'signals.db'
    conn = sqlite3.connect(str(db_path))
    init_flywheel_tables(conn)
    intent_id = log_intent(
        conn, intent.signal_id,
        symbol, 'long', 'IBKR',
        price, stop, target,
        qty, intent.risk_usd,
        3, 1,
        'ibkr_live', result.status,
        broker_order_id=result.broker_order_id,
        rejection_reason=result.reason if result.status != 'accepted' else None,
        risk_checks={'fixed_qty': True, 'bypass_risk_sizing': True},
    )
    print(f'  execution_intents row id={intent_id}  status={result.status}')
    conn.close()

    # ── Result ──────────────────────────────────────────────────────────────
    print('\n' + '='*60)
    if result.status == 'accepted':
        perm = result.broker_order_id
        print(f'PASS: Live order accepted')
        print(f'  broker_order_id : {perm}')
        if perm and 'PERM' in str(perm):
            print(f'  CONFIRMED: broker-assigned permId present')
        print()
        print('NEXT STEPS (manual):')
        print(f'  1. Check live Gateway blotter for {symbol} bracket order')
        print(f'  2. If filled: place SELL {qty} {symbol} MKT to flatten position')
        print(f'  3. If unfilled: cancel parent order in Gateway')
        print(f'  4. Confirm position = 0 before closing Gateway')
    else:
        print(f'FAIL: {result.status}')
        print(f'  reason: {result.reason}')
    print('='*60)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--symbol',  default='F',
                   help='Symbol to trade (default: F — Ford, ~$10)')
    p.add_argument('--qty',     type=int, default=1,
                   help='Fixed share quantity (default: 1)')
    p.add_argument('--dry-run', action='store_true',
                   help='Pre-flight checks only, no order submitted')
    args = p.parse_args()
    run(args.symbol, args.qty, args.dry_run)

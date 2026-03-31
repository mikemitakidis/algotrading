#!/usr/bin/env python3
"""
test_m12.py — M12 live safety envelope verification.

Tests all safety gates WITHOUT placing any live order.
Proves the system correctly blocks live trading when safety config is incomplete.

Usage:
  python test_m12.py              # full safety-gate test (no live order)
  python test_m12.py --status     # connection + safety status only
"""
import argparse, json, os, sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_env = BASE_DIR / '.env'
if _env.exists():
    try:
        from dotenv import load_dotenv; load_dotenv(_env)
    except ImportError:
        pass

sys.path.insert(0, str(BASE_DIR))

def run(status_only=False):
    print('\n' + '='*60)
    print('M12 LIVE SAFETY ENVELOPE VERIFICATION')
    print('='*60)

    from bot.brokers.ibkr_broker import _check_live_safety_config, IBKRBroker
    from bot.brokers.base import OrderIntent

    # ── 1. Safety gate with missing config ────────────────────────────────
    print('\n[1] Testing safety gate BLOCKS when config incomplete...')
    # Temporarily clear live env vars
    saved = {}
    for k in ('IBKR_LIVE_ACCOUNT','IBKR_LIVE_CONFIRMED','IBKR_LIVE_PORT'):
        saved[k] = os.environ.pop(k, None)
    os.environ['BROKER'] = 'ibkr_live'

    safe, reason = _check_live_safety_config()
    assert not safe, f"FAIL: safety gate should block but returned safe=True"
    print(f'    PASS: safety gate BLOCKED — reasons: {reason}')

    # ── 2. Safety gate blocks if CONFIRMED != yes ─────────────────────────
    print('\n[2] Testing safety gate BLOCKS if IBKR_LIVE_CONFIRMED != yes...')
    os.environ['IBKR_LIVE_ACCOUNT']   = 'TEST_ACCOUNT'
    os.environ['IBKR_LIVE_PORT']      = '4001'
    os.environ['IBKR_LIVE_CONFIRMED'] = 'no'

    safe, reason = _check_live_safety_config()
    assert not safe, "FAIL: CONFIRMED=no should block"
    assert 'IBKR_LIVE_CONFIRMED' in reason
    print(f'    PASS: CONFIRMED=no correctly blocked — {reason}')

    # ── 3. Safety gate PASSES when all config present ─────────────────────
    print('\n[3] Testing safety gate PASSES when fully configured...')
    os.environ['IBKR_LIVE_CONFIRMED'] = 'yes'
    os.environ['RISK_MAX_POSITION_PCT'] = '1.0'

    safe, reason = _check_live_safety_config()
    assert safe, f"FAIL: full config should pass but got: {reason}"
    print(f'    PASS: safety gate PASSED with full config — {reason}')

    # ── 4. Submit blocked when BROKER=ibkr_live but safety fails ─────────
    print('\n[4] Testing submit() is BLOCKED when safety gate fails...')
    os.environ['IBKR_LIVE_CONFIRMED'] = 'no'  # re-break it
    intent = OrderIntent(
        signal_id=777777, symbol='AAPL', direction='long', route='IBKR',
        entry_price=213.50, stop_loss=207.50, target_price=224.00,
        valid_count=3, strategy_version=1, position_size=1,
    )
    broker = IBKRBroker()
    result = broker.submit(intent)
    assert result.status == 'live_safety_blocked', \
        f"FAIL: expected live_safety_blocked, got {result.status}"
    print(f'    PASS: submit() returned status={result.status}')
    print(f'    reason: {result.reason}')

    # ── Restore env ───────────────────────────────────────────────────────
    os.environ['BROKER'] = 'ibkr_paper'
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v

    if status_only:
        print('\n[Status only — skipping connection test]')
    else:
        # ── 5. Paper connection still works ───────────────────────────────
        print('\n[5] Confirming paper connection still intact...')
        broker_paper = IBKRBroker()
        status = broker_paper.connection_status()
        print(f'    connected={status["connected"]}  mode={status.get("mode","?")}')
        print(f'    account={status.get("account","?")}')
        if status['connected']:
            print('    PASS: paper connection OK')
        else:
            print(f'    WARNING: paper connection failed: {status.get("error")}')
            print('    (IB Gateway may not be running — this is not a code failure)')

    print('\n' + '='*60)
    print('M12 SAFETY GATE VERIFICATION COMPLETE')
    print('='*60)
    print('Summary:')
    print('  [1] Incomplete config → BLOCKED           PASS')
    print('  [2] CONFIRMED=no → BLOCKED                PASS')
    print('  [3] Full config → PASSES                  PASS')
    print('  [4] submit() blocked by safety gate       PASS')
    print()
    print('To enable live trading, add ALL of these to .env:')
    print('  BROKER=ibkr_live')
    print('  IBKR_LIVE_ACCOUNT=<your_live_account_id>')
    print('  IBKR_LIVE_PORT=4001')
    print('  IBKR_LIVE_CONFIRMED=yes')
    print('  RISK_MAX_POSITION_PCT=1.0  (or any value <= 2.0)')
    print()
    print('What is still unproven (requires live account + IB Gateway on port 4001):')
    print('  - Account verification against live account ID')
    print('  - Live reconciliation (open orders/positions from live account)')
    print('  - Actual live bracket order submission')
    print('  - Live fill confirmation and outcome tracking')

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--status', action='store_true')
    run(status_only=p.parse_args().status)

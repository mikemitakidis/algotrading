#!/usr/bin/env python3
"""
test_m12.py — M12 live safety envelope verification.

Tests:
  1. Startup hard-stop proof (simulates main.py startup refusal)
  2. Safety gate: blocked with incomplete config
  3. Safety gate: blocked if CONFIRMED != yes
  4. Safety gate: passes with full config
  5. submit() returns live_safety_blocked when gate fails
  6. Risk manager hard-cap check (>2% blocked in live mode)
  7. [Requires live Gateway on port 4001] Live connection + account verification
  8. [Requires live Gateway on port 4001] Reconciliation: broker positions + orders
  9. [Requires live Gateway on port 4001] Duplicate conflict blocks live submission

Usage:
  python test_m12.py              # safety gate tests only (no Gateway needed)
  python test_m12.py --live       # adds live connection + reconciliation tests
                                  # requires IB Gateway on port 4001 + live account
"""
import argparse, json, os, sys, sqlite3, tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_env = BASE_DIR / '.env'
if _env.exists():
    try:
        from dotenv import load_dotenv; load_dotenv(_env)
    except ImportError:
        pass
sys.path.insert(0, str(BASE_DIR))


def section(title):
    print(f'\n{"─"*60}')
    print(f'  {title}')
    print(f'{"─"*60}')


def run(live_tests=False):
    print('\n' + '='*60)
    print('M12 LIVE SAFETY ENVELOPE — FULL VERIFICATION')
    print('='*60)

    from bot.brokers.ibkr_broker import _check_live_safety_config, IBKRBroker
    from bot.brokers.base import OrderIntent

    passed = []
    failed = []

    def check(name, cond, detail=''):
        if cond:
            passed.append(name)
            print(f'    ✓ PASS: {name}  {detail}')
        else:
            failed.append(name)
            print(f'    ✗ FAIL: {name}  {detail}')

    # Save original env
    saved = {k: os.environ.pop(k, None) for k in
             ('BROKER','IBKR_LIVE_ACCOUNT','IBKR_LIVE_CONFIRMED',
              'IBKR_LIVE_PORT','RISK_MAX_POSITION_PCT')}

    # ── Test 1: startup hard-stop simulation ──────────────────────────────
    section('1. Startup hard-stop simulation (main.py logic)')
    os.environ['BROKER'] = 'ibkr_live'
    # No live config set
    safe, reason = _check_live_safety_config()
    check('startup_refuses_with_no_live_config', not safe, f'reason={reason[:60]}')

    # ── Test 2: each missing field blocks independently ───────────────────
    section('2. Each missing safety field blocks independently')
    cases = [
        ('no_account',    {},                                          'IBKR_LIVE_ACCOUNT'),
        ('no_confirmed',  {'IBKR_LIVE_ACCOUNT':'X','IBKR_LIVE_PORT':'4001'}, 'IBKR_LIVE_CONFIRMED'),
        ('no_port',       {'IBKR_LIVE_ACCOUNT':'X','IBKR_LIVE_CONFIRMED':'yes'}, 'IBKR_LIVE_PORT'),
        ('confirmed_no',  {'IBKR_LIVE_ACCOUNT':'X','IBKR_LIVE_PORT':'4001',
                           'IBKR_LIVE_CONFIRMED':'no'},               'IBKR_LIVE_CONFIRMED'),
    ]
    for name, env_extra, expected_in_reason in cases:
        for k in ('IBKR_LIVE_ACCOUNT','IBKR_LIVE_CONFIRMED','IBKR_LIVE_PORT'):
            os.environ.pop(k, None)
        os.environ.update(env_extra)
        safe, reason = _check_live_safety_config()
        check(f'blocks_on_{name}', not safe and expected_in_reason in reason,
              f'{expected_in_reason} in reason={reason[:60]}')

    # ── Test 3: full config passes ────────────────────────────────────────
    section('3. Full config passes safety gate')
    os.environ.update({
        'IBKR_LIVE_ACCOUNT':   'TEST_LIVE_ACCT',
        'IBKR_LIVE_PORT':      '4001',
        'IBKR_LIVE_CONFIRMED': 'yes',
        'RISK_MAX_POSITION_PCT': '1.5',
    })
    safe, reason = _check_live_safety_config()
    check('full_config_passes', safe, reason)

    # ── Test 4: hard cap blocks >2% ───────────────────────────────────────
    section('4. Position size hard cap (>2% blocked in live mode)')
    os.environ['RISK_MAX_POSITION_PCT'] = '3.0'
    safe, reason = _check_live_safety_config()
    check('hard_cap_blocks_above_2pct', not safe, f'reason={reason[:60]}')

    # ── Test 5: submit() blocked by safety gate ───────────────────────────
    section('5. submit() returns live_safety_blocked when gate fails')
    os.environ['IBKR_LIVE_CONFIRMED'] = 'no'
    intent = OrderIntent(
        signal_id=777777, symbol='AAPL', direction='long', route='IBKR',
        entry_price=213.50, stop_loss=207.50, target_price=224.00,
        valid_count=3, strategy_version=1, position_size=1,
    )
    broker = IBKRBroker()
    result = broker.submit(intent)
    check('submit_blocked_by_safety_gate',
          result.status == 'live_safety_blocked',
          f'status={result.status}  reason={result.reason[:60]}')

    # ── Restore env ───────────────────────────────────────────────────────
    os.environ['BROKER'] = 'ibkr_paper'
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)

    # ── Live Gateway tests (optional) ─────────────────────────────────────
    if live_tests:
        section('6. Live Gateway connection + account verification')
        os.environ['BROKER'] = 'ibkr_live'
        live_acct = os.getenv('IBKR_LIVE_ACCOUNT', '').strip()
        if not live_acct:
            print('    SKIP: IBKR_LIVE_ACCOUNT not set — set in .env for live tests')
        else:
            broker_live = IBKRBroker()
            status = broker_live.connection_status()
            check('live_gateway_connected', status.get('connected', False),
                  f'host={status.get("host")}:{status.get("port")}')
            check('live_account_verified', status.get('account_verified', False),
                  status.get('account_msg',''))
            check('live_mode_flag', status.get('is_live', False))
            check('live_safety_status', status.get('live_safety_ok', False),
                  status.get('live_safety_msg',''))

            section('7. Broker reconciliation: positions + orders')
            recon = broker_live.reconcile()
            print(f'    open positions : {recon["positions"]}')
            print(f'    open orders    : {len(recon["open_orders"])}')
            print(f'    warnings       : {recon["warnings"]}')
            check('reconcile_returned', 'positions' in recon)

            section('8. Duplicate conflict blocks live submission')
            # If a position exists, submitting same symbol should be blocked by risk
            if recon['positions']:
                sym = recon['positions'][0]['symbol']
                print(f'    Testing duplicate block for {sym}...')
                from bot.risk import RiskManager
                rm = RiskManager()
                test_intent = OrderIntent(
                    signal_id=666666, symbol=sym, direction='long', route='IBKR',
                    entry_price=100.0, stop_loss=95.0, target_price=110.0,
                    valid_count=3, strategy_version=1,
                )
                passed_risk, checks, reason = rm.evaluate(test_intent)
                check('broker_duplicate_blocked',
                      not passed_risk and 'broker_position_exists' in (reason or ''),
                      f'reason={reason}')
            else:
                print('    No open positions to test duplicate block — skipped')

        os.environ['BROKER'] = 'ibkr_paper'

    # ── Summary ───────────────────────────────────────────────────────────
    print('\n' + '='*60)
    print('SUMMARY')
    print('='*60)
    print(f'  PASSED: {len(passed)}/{len(passed)+len(failed)}')
    if failed:
        print(f'  FAILED: {failed}')
    print()
    print('What is still unproven without a live order:')
    print('  - Live bracket order fill and confirmation')
    print('  - Outcome linkage: fill → execution_intent → signal_outcome')
    print('  - GTC order cancellation on live account')
    print('  - Partial fill handling')
    print()
    if not live_tests:
        print('Run with --live for Gateway connection + reconciliation tests.')

    return len(failed) == 0


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true',
                   help='Run live Gateway tests (requires port 4001 + live account)')
    ok = run(live_tests=p.parse_args().live)
    sys.exit(0 if ok else 1)

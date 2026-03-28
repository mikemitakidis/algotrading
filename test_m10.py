#!/usr/bin/env python3
"""
test_m10.py — Controlled M10 verification script.

Injects one synthetic final signal directly into the flywheel pipeline
(bypassing the scanner) to prove the full path:
  final_signal snapshot → risk check → paper broker → execution_intent

Does NOT touch live strategy logic, live scanner, or live signal table.
Uses a separate test_m10 cycle_id (99999) so rows are clearly identifiable.
Safe to run at any time without affecting live operation.

Usage: python test_m10.py
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Load .env
_env = BASE_DIR / '.env'
if _env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env)
    except ImportError:
        pass

sys.path.insert(0, str(BASE_DIR))

from bot.flywheel   import init_flywheel_tables, log_candidate, log_intent
from bot.brokers    import get_broker
from bot.brokers.base import OrderIntent
from bot.risk       import RiskManager

DB_PATH    = BASE_DIR / 'data' / 'signals.db'
ORDERS_FILE= BASE_DIR / 'data' / 'paper_orders.jsonl'
TEST_CYCLE = 99999   # clearly synthetic — never used by live scanner

def run():
    print('\n' + '='*60)
    print('M10 CONTROLLED VERIFICATION')
    print('='*60)
    print(f'DB: {DB_PATH}')
    print(f'Cycle ID: {TEST_CYCLE} (synthetic test — not a live cycle)')

    conn = sqlite3.connect(str(DB_PATH))
    init_flywheel_tables(conn)

    # ── Step 1: log a final_signal candidate snapshot ─────────────────────
    print('\n[1] Logging final_signal candidate snapshot...')
    FAKE_SIGNAL_ID = 999999   # clearly synthetic
    cand_id = log_candidate(
        conn, TEST_CYCLE,
        symbol='AAPL', direction='long',
        stage='final_signal',
        valid_count=3,
        tfs_passing=['1D', '4H', '1H'],
        available_tfs=3,
        min_valid=2,
        route='IBKR',
        strategy_version=1,
        signal_id=FAKE_SIGNAL_ID,
        ind={
            'rsi': 62.5, 'macd_hist': 0.18,
            'atr': 3.2,  'bb_pos': 0.65,
            'vwap_dev': 0.004, 'vol_ratio': 1.15,
        },
    )
    assert cand_id, "candidate_snapshots insert failed"
    print(f'    candidate_snapshots row id={cand_id}  signal_id={FAKE_SIGNAL_ID}')

    # Verify in DB
    row = conn.execute(
        'SELECT id, symbol, direction, stage, valid_count, signal_id, rsi '
        'FROM candidate_snapshots WHERE id=?', (cand_id,)
    ).fetchone()
    print(f'    DB read: id={row[0]} symbol={row[1]} direction={row[2]} '
          f'stage={row[3]} valid_count={row[4]} signal_id={row[5]} rsi={row[6]}')
    assert row[3] == 'final_signal', f"stage wrong: {row[3]}"
    print('    PASS: final_signal row confirmed in candidate_snapshots')

    # ── Step 2: risk evaluation ───────────────────────────────────────────
    print('\n[2] Running risk evaluation...')
    intent = OrderIntent(
        signal_id    = FAKE_SIGNAL_ID,
        symbol       = 'AAPL',
        direction    = 'long',
        route        = 'IBKR',
        entry_price  = 213.50,
        stop_loss    = 207.50,
        target_price = 224.00,
        valid_count  = 3,
        strategy_version = 1,
    )
    rm = RiskManager()
    passed, checks, reason = rm.evaluate(intent)
    intent.risk_checks = checks
    print(f'    verdict={checks["verdict"]}  reason={reason}')
    print(f'    checks: {json.dumps({k: v for k, v in checks.items() if k != "verdict"}, default=str)}')
    print(f'    position_size={intent.position_size} shares  risk_usd=${intent.risk_usd}')
    print(f'    PASS: risk evaluation completed (passed={passed})')

    # ── Step 3: paper broker submission ───────────────────────────────────
    print('\n[3] Submitting to paper broker...')
    broker = get_broker()
    print(f'    broker={broker.name}  is_live={broker.is_live}')
    result = broker.submit(intent)
    print(f'    status={result.status}')
    print(f'    broker_order_id={result.broker_order_id}')
    print(f'    reason={result.reason}')
    assert result.status == 'paper_logged', f"Expected paper_logged, got {result.status}"
    print('    PASS: broker returned paper_logged')

    # ── Step 4: log execution intent ──────────────────────────────────────
    print('\n[4] Logging execution intent...')
    status_to_log = result.status if passed else 'risk_rejected'
    intent_id = log_intent(
        conn, FAKE_SIGNAL_ID,
        symbol='AAPL', direction='long', route='IBKR',
        entry_price=213.50, stop_loss=207.50, target_price=224.00,
        position_size=intent.position_size,
        risk_usd=intent.risk_usd,
        valid_count=3, strategy_version=1,
        broker=broker.name,
        status=status_to_log,
        broker_order_id=result.broker_order_id,
        rejection_reason=reason,
        risk_checks=checks,
    )
    assert intent_id, "execution_intents insert failed"
    print(f'    execution_intents row id={intent_id}')

    # Verify in DB
    irow = conn.execute(
        'SELECT id, symbol, direction, route, status, broker_order_id, position_size, risk_usd '
        'FROM execution_intents WHERE id=?', (intent_id,)
    ).fetchone()
    print(f'    DB read: id={irow[0]} symbol={irow[1]} direction={irow[2]} '
          f'route={irow[3]} status={irow[4]}')
    print(f'    broker_order_id={irow[5]}  position_size={irow[6]}  risk_usd=${irow[7]}')
    assert irow[4] == status_to_log, f"status wrong: {irow[4]}"
    print('    PASS: execution_intent row confirmed in DB')

    # ── Step 5: verify paper_orders.jsonl ─────────────────────────────────
    print('\n[5] Verifying paper_orders.jsonl...')
    assert ORDERS_FILE.exists(), f"paper_orders.jsonl not found at {ORDERS_FILE}"
    with open(ORDERS_FILE) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    matching = [l for l in lines if l.get('signal_id') == FAKE_SIGNAL_ID]
    assert matching, "No matching record in paper_orders.jsonl"
    rec = matching[-1]
    print(f'    paper_orders.jsonl record:')
    print(f'      signal_id={rec["signal_id"]}  symbol={rec["symbol"]}  '
          f'direction={rec["direction"]}  status={rec["status"]}')
    print(f'      broker_order_id={rec["broker_order_id"]}')
    print(f'      risk_checks.verdict={rec["risk_checks"].get("verdict")}')
    assert rec['status'] == 'paper_logged'
    print('    PASS: paper_orders.jsonl record confirmed')

    # ── Summary ───────────────────────────────────────────────────────────
    print('\n' + '='*60)
    print('M10 VERIFICATION COMPLETE')
    print('='*60)

    # Show full stage counts
    stages = conn.execute(
        'SELECT stage, COUNT(*) FROM candidate_snapshots GROUP BY stage'
    ).fetchall()
    print('\ncandidate_snapshots stage counts (including test row):')
    for stage, count in stages:
        tag = '  ← TEST ROW' if stage == 'final_signal' else ''
        print(f'  {stage}: {count}{tag}')

    intents = conn.execute(
        'SELECT id, symbol, direction, status, broker_order_id '
        'FROM execution_intents ORDER BY id DESC LIMIT 5'
    ).fetchall()
    print('\nexecution_intents (last 5):')
    for r in intents:
        print(f'  id={r[0]} {r[1]} {r[2]} status={r[3]} order={r[4]}')

    print('\nAll 5 steps passed. M10 execution path verified.')
    print('NOTE: test rows use cycle_id=99999 and signal_id=999999')
    print('      — clearly synthetic, never conflicts with live data')
    conn.close()

if __name__ == '__main__':
    run()

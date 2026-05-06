#!/usr/bin/env python3
"""
test_m14_risk.py — M14 portfolio risk offline test suite.
All tests run without a broker connection.
"""
import os, sys, json, sqlite3, tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Use temp DB so tests never touch production data
_TMP_DB = tempfile.mktemp(suffix='.db')
os.environ['SIGNALS_DB_PATH'] = _TMP_DB  # flywheel will use this if supported

from bot.flywheel import init_flywheel_tables, get_daily_state, get_persistent_state
from bot.risk import (PortfolioRiskPolicy, PortfolioRiskContext,
                      SectorMap, RiskManager)
from bot.brokers.base import OrderIntent

_conn = sqlite3.connect(_TMP_DB)
init_flywheel_tables(_conn)

PASS = 0
FAIL = 0

def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        print(f'  PASS: {name}')
        PASS += 1
    else:
        print(f'  FAIL: {name}  ({detail})')
        FAIL += 1

def section(title):
    print(f'\n[{title}]')

def make_intent(symbol='AAPL', qty=10, entry=200.0, stop=195.0, target=210.0):
    i = OrderIntent(
        signal_id=99001, symbol=symbol, direction='long', route='IBKR',
        entry_price=entry, stop_loss=stop, target_price=target,
        valid_count=2, strategy_version=1,
    )
    i.position_size = qty
    i.risk_usd = qty * (entry - stop)
    return i

def make_ctx(mode='paper', positions=None, open_orders=None, local_intents=None,
             daily_override=None, persistent_override=None, portfolio_value=100_000):
    from bot.risk import SectorMap
    sm = SectorMap()
    daily = daily_override or get_daily_state(_conn)
    persistent = persistent_override or get_persistent_state(_conn)
    return PortfolioRiskContext(
        broker='ibkr_paper' if mode == 'paper' else 'ibkr_live',
        mode=mode,
        portfolio_value=portfolio_value,
        portfolio_value_source='config',
        positions=positions or [],
        open_orders=open_orders or [],
        local_open_intents=local_intents or [],
        daily_state=daily,
        persistent_state=persistent,
        sector_map=sm,
    )

def fresh_policy(**env_overrides):
    for k, v in env_overrides.items():
        os.environ[k] = str(v)
    p = PortfolioRiskPolicy()
    return p

print('='*60)
print('M14 PORTFOLIO RISK TEST SUITE')
print('='*60)

# ── Test 1: Daily P&L unavailable — paper mode warns, allows ─────────────────
section('1. Daily P&L unavailable: paper mode')
os.environ['RISK_REQUIRE_DAILY_PNL_FOR_LIVE'] = 'true'
policy = fresh_policy()
ctx = make_ctx(mode='paper')
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('paper_mode_allows_with_unavailable_pnl', passed,
      f'reason={reason}')
check('daily_pnl_available_is_false', checks.get('daily_pnl_available') == False)
check('warning_added', 'daily_pnl_unavailable' in ctx.warnings)

# ── Test 2: Daily P&L unavailable — live mode blocks ─────────────────────────
section('2. Daily P&L unavailable: live mode blocks')
policy = fresh_policy(RISK_REQUIRE_DAILY_PNL_FOR_LIVE='true')
ctx = make_ctx(mode='live')
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('live_blocks_with_unavailable_pnl', not passed, f'passed={passed}')
check('reason_is_daily_pnl_unavailable', reason == 'daily_pnl_unavailable_live_block',
      f'reason={reason}')

# ── Test 3: Daily loss limit exceeded ─────────────────────────────────────────
section('3. Daily loss limit exceeded')
policy = fresh_policy(RISK_MAX_DAILY_LOSS_PCT='2.0',
                      RISK_ALLOW_DAILY_LOSS_OVERRIDE='false')
daily_breached = {'date': '2099-01-01', 'realised_pnl_usd': -3000,
                  'realised_pnl_pct': -3.0, 'daily_pnl_source': 'signal_outcomes',
                  'daily_pnl_available': 1, 'daily_loss_block_active': 1,
                  'daily_loss_alert_sent': 1}
ctx = make_ctx(mode='paper', daily_override=daily_breached)
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('daily_loss_blocks_trade', not passed)
check('reason_is_daily_loss', reason == 'daily_loss_limit_exceeded', f'reason={reason}')

# ── Test 4: Daily loss override allows trading ────────────────────────────────
section('4. Daily loss override: allows when configured')
policy = fresh_policy(RISK_MAX_DAILY_LOSS_PCT='2.0',
                      RISK_ALLOW_DAILY_LOSS_OVERRIDE='true',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
ctx = make_ctx(mode='live', daily_override=daily_breached)
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('override_allows_trading', passed, f'reason={reason}')

# ── Test 5: Max open trades ───────────────────────────────────────────────────
section('5. Max open trades exceeded')
policy = fresh_policy(RISK_MAX_OPEN_POSITIONS='2',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
# 2 bracket orders (each standalone)
orders = [
    {'order_id': 1, 'parent_id': 0, 'oca_group': '', 'symbol': 'MSFT', 'qty': 10},
    {'order_id': 2, 'parent_id': 0, 'oca_group': '', 'symbol': 'GOOG', 'qty': 10},
]
ctx = make_ctx(mode='paper', open_orders=orders)
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('max_open_trades_blocks', not passed, f'reason={reason}')
check('reason_is_max_open_trades', reason == 'max_open_trades_exceeded', f'{reason}')

# ── Test 6: Bracket legs count as ONE trade ───────────────────────────────────
section('6. Bracket legs count as one trade, not three')
policy = fresh_policy(RISK_MAX_OPEN_POSITIONS='2',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
# 3 orders: parent + TP + SL for same bracket
bracket_orders = [
    {'order_id': 10, 'parent_id': 0,  'oca_group': '', 'symbol': 'AAPL', 'qty': 5},
    {'order_id': 11, 'parent_id': 10, 'oca_group': '', 'symbol': 'AAPL', 'qty': 5},
    {'order_id': 12, 'parent_id': 10, 'oca_group': '', 'symbol': 'AAPL', 'qty': 5},
]
ctx = make_ctx(mode='paper', open_orders=bracket_orders)
trade_count, _ = policy._count_open_trades(ctx)
check('bracket_3_legs_count_as_1', trade_count == 1,
      f'trade_count={trade_count} (expected 1)')
# With limit=2, one bracket + new trade should pass
passed, checks, reason = policy.evaluate(make_intent('MSFT'), ctx)
check('new_trade_allowed_with_one_bracket_open', passed, f'reason={reason}')

# ── Test 7: Symbol exposure exceeded ─────────────────────────────────────────
section('7. Symbol exposure exceeded')
policy = fresh_policy(RISK_MAX_SYMBOL_EXPOSURE_PCT='10.0',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
# Existing 9% AAPL exposure via local intent
local = [{'symbol': 'AAPL', 'position_size': 45, 'entry_price': 200.0}]
ctx = make_ctx(mode='paper', local_intents=local)
# New AAPL trade: 10 shares × 200 = 2000 = 2% of 100k
# Total projected: 9% + 2% = 11% > 10% → reject
intent = make_intent('AAPL', qty=10, entry=200.0, stop=196.0, target=208.0)
passed, checks, reason = policy.evaluate(intent, ctx)
check('symbol_exposure_exceeds_limit_blocks', not passed, f'reason={reason}')
check('reason_is_symbol_exposure', reason == 'symbol_exposure_exceeded', f'{reason}')
check('estimated_flag_present', 'symbol_exposure_estimated' in checks)

# ── Test 8: Sector exposure exceeded ─────────────────────────────────────────
section('8. Sector exposure exceeded')
policy = fresh_policy(RISK_MAX_SECTOR_EXPOSURE_PCT='25.0',
                      RISK_REQUIRE_SECTOR_FOR_LIVE='true',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
# Large tech positions
local_tech = [
    {'symbol': 'MSFT', 'position_size': 60, 'entry_price': 400.0},   # 24%
]
ctx = make_ctx(mode='paper', local_intents=local_tech)
# New AAPL (tech) trade: 3 shares × 200 = 600 = 0.6%
# Total projected: 24% + 0.6% = 24.6% < 25% — should pass
intent = make_intent('AAPL', qty=3, entry=200.0, stop=196.0, target=208.0)
passed, checks, reason = policy.evaluate(intent, ctx)
check('sector_under_limit_allows', passed, f'reason={reason}')

# Now push over limit
local_tech2 = [
    {'symbol': 'MSFT', 'position_size': 120, 'entry_price': 400.0},  # 48%
]
ctx2 = make_ctx(mode='paper', local_intents=local_tech2)
passed2, checks2, reason2 = policy.evaluate(intent, ctx2)
check('sector_over_limit_blocks', not passed2, f'reason={reason2}')
check('reason_is_sector_exposure', reason2 == 'sector_exposure_exceeded', f'{reason2}')

# ── Test 9: Unknown sector — paper warns ─────────────────────────────────────
section('9. Unknown sector: paper warns but allows')
policy = fresh_policy(RISK_REQUIRE_SECTOR_FOR_LIVE='true',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
ctx = make_ctx(mode='paper')
# Use a symbol not in symbol_metadata.csv
intent_unknown = make_intent('ZZUNKNOWN', qty=1, entry=10.0, stop=9.0, target=11.0)
passed, checks, reason = policy.evaluate(intent_unknown, ctx)
check('unknown_sector_paper_allows', passed, f'reason={reason}')
check('sector_warning_added', any('sector_unknown' in w for w in ctx.warnings))
check('sector_check_unavailable', checks.get('sector_exposure_ok') == 'unknown')

# ── Test 10: Unknown sector — live blocks ────────────────────────────────────
section('10. Unknown sector: live blocks')
policy = fresh_policy(RISK_REQUIRE_SECTOR_FOR_LIVE='true',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
ctx = make_ctx(mode='live')
passed, checks, reason = policy.evaluate(intent_unknown, ctx)
check('unknown_sector_live_blocks', not passed, f'reason={reason}')
check('reason_is_sector_unknown', reason == 'sector_unknown_live_block', f'{reason}')

# ── Test 11: Loss streak — outcomes unavailable ───────────────────────────────
section('11. Loss streak: outcomes unavailable paper')
policy = fresh_policy(RISK_REQUIRE_OUTCOMES_FOR_LIVE='false',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
ctx = make_ctx(mode='paper')
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('outcomes_unavailable_paper_allows', passed, f'reason={reason}')
check('streak_check_unavailable', checks.get('loss_streak', {}).get('outcomes_available') == False)

# ── Test 12: risk_rejected row has correct semantics ─────────────────────────
section('12. risk_rejected status semantics controlled')
from bot.flywheel import log_intent
policy = fresh_policy(RISK_MAX_OPEN_POSITIONS='0',  # 0 = always reject max_open_trades
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
ctx = make_ctx(mode='paper')
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('policy_rejects', not passed)
check('reason_code_controlled', reason in PortfolioRiskPolicy.REASONS,
      f'reason={reason}')

# Simulate writing to DB as main.py would
intent = make_intent()
intent.risk_checks = json.dumps(checks)
row_id = log_intent(_conn, 99001, 'AAPL', 'long', 'IBKR',
                    200.0, 195.0, 210.0, 10, 50.0, 2, 1,
                    'ibkr_paper', 'risk_rejected',
                    rejection_reason=reason,
                    risk_checks=checks)
row = _conn.execute(
    'SELECT status, rejection_reason, risk_checks FROM execution_intents WHERE id=?',
    (row_id,)).fetchone()
check('status_is_risk_rejected', row[0] == 'risk_rejected', f'status={row[0]}')
check('rejection_reason_stored', row[1] == reason, f'stored={row[1]}')
check('risk_checks_json_stored', row[2] is not None)

# ── Test 13: portfolio_risk_snapshots written every cycle ─────────────────────
section('13. portfolio_risk_snapshots written every cycle')
from bot.flywheel import write_portfolio_snapshot
ctx_snap = make_ctx(mode='paper')
write_portfolio_snapshot(_conn, cycle_id=999, broker='ibkr_paper', ctx=ctx_snap)
snap = _conn.execute(
    'SELECT cycle_id, broker, risk_status FROM portfolio_risk_snapshots ORDER BY id DESC LIMIT 1'
).fetchone()
check('snapshot_written', snap is not None)
check('snapshot_cycle_id', snap[0] == 999, f'cycle_id={snap[0]}')
check('snapshot_broker', snap[1] == 'ibkr_paper', f'broker={snap[1]}')
check('snapshot_risk_status', snap[2] in ('ok', 'warning', 'blocked'), f'status={snap[2]}')

# ── Test 14: daily_loss_block independent of kill switch ─────────────────────
section('14. Daily loss block independent of kill switch')
# Kill switch deactivated but daily loss still active
from bot.kill_switch import deactivate_kill_switch
deactivate_kill_switch('test cleanup')
policy = fresh_policy(RISK_MAX_DAILY_LOSS_PCT='2.0',
                      RISK_ALLOW_DAILY_LOSS_OVERRIDE='false',
                      RISK_REQUIRE_DAILY_PNL_FOR_LIVE='false')
ctx = make_ctx(mode='paper', daily_override=daily_breached)
passed, checks, reason = policy.evaluate(make_intent(), ctx)
check('daily_loss_blocks_even_with_ks_off', not passed, f'reason={reason}')
from bot.kill_switch import is_kill_switch_active
check('kill_switch_still_inactive', not is_kill_switch_active())

# ── Test 15: M12 unchanged — RiskManager interface untouched ─────────────────
section('15. RiskManager interface unchanged (M12 compatibility)')
rm = RiskManager()
passed_rm, checks_rm, reason_rm = rm.evaluate(make_intent())
check('riskmanager_returns_3tuple', isinstance((passed_rm, checks_rm, reason_rm), tuple))
check('riskmanager_checks_is_dict', isinstance(checks_rm, dict))
check('riskmanager_no_portfolio_keys',
      'daily_pnl_available' not in checks_rm,
      'portfolio keys leaked into base risk')

# ── Summary ───────────────────────────────────────────────────────────────────
import os as _os
_os.unlink(_TMP_DB)

print('\n' + '='*60)
print(f'RESULT: {PASS}/{PASS+FAIL} tests passed')
print('='*60)
sys.exit(0 if FAIL == 0 else 1)

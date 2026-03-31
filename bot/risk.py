"""
bot/risk.py
Risk manager — Milestone 10.

Evaluates an OrderIntent against configured limits before submission.
All checks are logged. Every rejection has an explicit reason code.

Configuration (all in .env with safe defaults):
  RISK_MAX_POSITION_PCT   max % of portfolio per position (default: 2.0)
  RISK_MAX_OPEN_POSITIONS max concurrent open positions (default: 10)
  RISK_PORTFOLIO_SIZE     simulated portfolio size USD (default: 100000)
  RISK_ALLOW_DUPLICATES   allow same symbol+direction twice (default: false)

M10 scope: no real portfolio tracking. Checks against paper_orders.jsonl
for open position counting and duplicate detection.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.brokers.base import OrderIntent

log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).resolve().parent.parent
# paper_orders.jsonl kept for audit — risk checks use execution_intents DB
ORDERS_FILE = BASE_DIR / 'data' / 'paper_orders.jsonl'


# Synthetic signal IDs used in test scripts — never count as real open positions
_TEST_SIGNAL_IDS = {888888, 999999}


def _load_open_intents() -> list:
    """
    Load real open intents from execution_intents SQLite table.
    Excludes: synthetic test rows, risk_rejected, error, not_implemented.
    Only counts: accepted, paper_logged (real submitted orders).
    """
    db = BASE_DIR / 'data' / 'signals.db'
    if not db.exists():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            """SELECT symbol, direction, signal_id, status
               FROM execution_intents
               WHERE status IN ('accepted', 'paper_logged')
               AND signal_id NOT IN ({})\n""".format(
                ','.join(str(i) for i in _TEST_SIGNAL_IDS)
            )
        ).fetchall()
        conn.close()
        return [{'symbol': r[0], 'direction': r[1],
                 'signal_id': r[2], 'status': r[3]} for r in rows]
    except Exception as e:
        log.warning('[RISK] _load_open_intents failed: %s', e)
        return []


class RiskManager:
    """
    Evaluates order intents against risk limits.
    Returns (passed: bool, checks: dict, reason: str).

    checks dict contains every individual check result for full auditability.
    """

    def __init__(self):
        self.max_position_pct  = float(os.getenv('RISK_MAX_POSITION_PCT',   '2.0'))
        self.max_open           = int(os.getenv('RISK_MAX_OPEN_POSITIONS', '10'))
        self.portfolio_size     = float(os.getenv('RISK_PORTFOLIO_SIZE', '100000'))
        self.allow_duplicates   = os.getenv('RISK_ALLOW_DUPLICATES', 'false').lower() == 'true'

    def evaluate(self, intent: OrderIntent) -> tuple[bool, dict, Optional[str]]:
        """
        Run all risk checks on an intent.

        Returns:
            (passed: bool, checks: dict, rejection_reason: str | None)

        checks dict keys:
            market_hours      : bool
            position_size_ok  : bool
            max_positions_ok  : bool
            duplicate_ok      : bool
            verdict           : 'pass' | 'reject'
            position_size_usd : float
            open_positions    : int
        """
        checks = {}
        reasons = []

        # 0. Live mode hard limits + full reconciliation
        import os as _os
        broker = _os.getenv('BROKER', 'paper').lower().strip()
        if broker == 'ibkr_live':
            # Hard cap: position size cannot exceed 2% in live mode
            if self.max_position_pct > 2.0:
                reasons.append('live_position_pct_exceeds_2pct_hard_cap')
                checks['live_hard_cap_ok'] = False
            else:
                checks['live_hard_cap_ok'] = True

            # Full reconciliation: broker positions + open orders + local intents
            try:
                from bot.brokers.ibkr_broker import IBKRBroker
                _broker = IBKRBroker()
                recon = _broker.reconcile()
                broker_positions = recon.get('positions', [])
                broker_orders    = recon.get('open_orders', [])
                recon_warnings   = recon.get('warnings', [])

                checks['broker_positions']  = len(broker_positions)
                checks['broker_open_orders']= len(broker_orders)
                checks['recon_warnings']    = recon_warnings

                # ── Block policy (broker is source of truth for live) ──────
                # 1. Existing broker POSITION in this symbol → block
                pos_conflict = any(
                    p.get('symbol') == intent.symbol
                    for p in broker_positions
                    if abs(p.get('position', 0)) > 0
                )
                # 2. Existing broker OPEN ORDER for this symbol → block
                #    (covers bracket legs not yet filled: prevents double-entry)
                order_conflict = any(
                    o.get('symbol') == intent.symbol
                    for o in broker_orders
                )
                broker_conflict = pos_conflict or order_conflict

                if broker_conflict:
                    block_reason = (
                        f'broker_position_exists_{intent.symbol}' if pos_conflict
                        else f'broker_open_order_exists_{intent.symbol}'
                    )
                    reasons.append(block_reason)
                    checks['broker_conflict_ok'] = False
                    checks['broker_conflict_reason'] = block_reason
                else:
                    checks['broker_conflict_ok'] = True
                    checks['broker_conflict_reason'] = None

                # Max-open count: broker positions + local accepted intents
                local_intents    = _load_open_intents()
                combined_open    = len(broker_positions) + len(local_intents)
                checks['combined_open_positions'] = combined_open
                if combined_open >= self.max_open:
                    reasons.append(f'combined_max_open_{self.max_open}')
                    checks['combined_max_ok'] = False
                else:
                    checks['combined_max_ok'] = True

                log.info('[RISK] Live recon: positions=%d open_orders=%d '
                         'local_intents=%d combined=%d conflict=%s(%s)',
                         len(broker_positions), len(broker_orders),
                         len(local_intents), combined_open,
                         broker_conflict, checks.get('broker_conflict_reason','none'))
                if recon_warnings:
                    log.warning('[RISK] Recon warnings: %s', recon_warnings)

            except Exception as _e:
                # Fail-safe for live: if reconcile fails, block the order
                reasons.append('live_reconcile_failed')
                checks['broker_positions']   = -1
                checks['broker_duplicate_ok']= False
                log.error('[RISK] Live reconcile FAILED — blocking order: %s', _e)

        # 1. Market hours (basic — US equities Mon-Fri)
        now = datetime.now(timezone.utc)
        is_weekday = now.weekday() < 5
        checks['market_hours'] = is_weekday
        if not is_weekday:
            reasons.append('market_closed_weekend')

        # 2. Position size
        risk_per_trade = abs(intent.entry_price - intent.stop_loss)
        if risk_per_trade > 0:
            # Size so that 1× stop = max_position_pct% of portfolio
            max_risk_usd     = self.portfolio_size * (self.max_position_pct / 100)
            position_size    = max_risk_usd / risk_per_trade
            position_size_usd = position_size * intent.entry_price
        else:
            position_size     = 0
            position_size_usd = 0

        intent.position_size = round(position_size, 2)
        intent.risk_usd      = round(position_size * risk_per_trade, 2)
        checks['position_size_usd'] = round(position_size_usd, 2)
        checks['position_size_ok']  = position_size_usd > 0

        if not checks['position_size_ok']:
            reasons.append('zero_position_size')

        # 3. Max open positions
        open_intents = _load_open_intents()
        open_count   = len(open_intents)
        checks['open_positions']  = open_count
        checks['max_positions_ok'] = open_count < self.max_open
        if not checks['max_positions_ok']:
            reasons.append(f'max_open_positions_{self.max_open}')

        # 4. Duplicate check
        if not self.allow_duplicates:
            existing = any(
                r.get('symbol') == intent.symbol and
                r.get('direction') == intent.direction
                for r in open_intents
            )
            checks['duplicate_ok'] = not existing
            if existing:
                reasons.append(f'duplicate_{intent.symbol}_{intent.direction}')
        else:
            checks['duplicate_ok'] = True

        passed = len(reasons) == 0
        checks['verdict'] = 'pass' if passed else 'reject'
        rejection_reason = ', '.join(reasons) if reasons else None

        if passed:
            log.info('[RISK] %s %s: PASS (size=%.0f shares, risk=$%.0f)',
                     intent.symbol, intent.direction.upper(),
                     intent.position_size or 0, intent.risk_usd or 0)
        else:
            log.info('[RISK] %s %s: REJECT — %s',
                     intent.symbol, intent.direction.upper(), rejection_reason)

        return passed, checks, rejection_reason

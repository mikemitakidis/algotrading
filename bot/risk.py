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
ORDERS_FILE = BASE_DIR / 'data' / 'paper_orders.jsonl'


def _load_open_intents() -> list:
    """Read paper_orders.jsonl and return all logged intents."""
    if not ORDERS_FILE.exists():
        return []
    try:
        records = []
        with open(ORDERS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
    except Exception:
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

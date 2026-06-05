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
                # P0-4 (audit, 2026-06-05): stash the full reconcile
                # result so bot.portfolio_ctx.gather() can reuse it
                # when building PortfolioRiskContext later in the
                # same scan tick — without paying for a second IBKR
                # round-trip per signal (audit Correction B).
                checks['_recon']             = recon

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

# ── M14 Portfolio Risk Layer ──────────────────────────────────────────────────

import csv
import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PortfolioRiskContext:
    """
    Single context object built once per signal evaluation.
    Reused across PortfolioRiskPolicy, snapshot write, and dashboard.
    Eliminates duplicate broker reconciliation calls.
    """
    broker: str = 'paper'
    mode: str = 'paper'                   # 'live' | 'paper'
    portfolio_value: float = 100_000.0
    portfolio_value_source: str = 'config'
    positions: list = field(default_factory=list)         # from reconcile()
    open_orders: list = field(default_factory=list)       # from reconcile() enriched
    local_open_intents: list = field(default_factory=list)# execution_intents accepted
    daily_state: dict = field(default_factory=dict)
    persistent_state: dict = field(default_factory=dict)  # portfolio_risk_state kv
    sector_map: dict = field(default_factory=dict)        # symbol → sector
    kill_switch_active: bool = False
    warnings: list = field(default_factory=list)


class SectorMap:
    """Loads data/symbol_metadata.csv. Operator-editable, no code change needed."""

    def __init__(self):
        self._map: dict[str, str] = {}
        self._load()

    def _load(self):
        csv_path = BASE_DIR / 'data' / 'symbol_metadata.csv'
        if not csv_path.exists():
            log.warning('[M14] data/symbol_metadata.csv not found — sector checks unavailable')
            return
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row.get('symbol', '').strip().upper()
                    sector = row.get('sector', '').strip()
                    if sym and sector:
                        self._map[sym] = sector
            log.info('[M14] SectorMap loaded: %d symbols', len(self._map))
        except Exception as e:
            log.error('[M14] SectorMap load error: %s', e)

    def get(self, symbol: str) -> Optional[str]:
        return self._map.get(symbol.upper())

    def reload(self):
        self._map.clear()
        self._load()


class PortfolioRiskPolicy:
    """
    M14 portfolio-level risk gate.
    Called AFTER RiskManager.evaluate() passes.
    Does NOT modify the existing RiskManager interface.

    Returns (passed: bool, checks: dict, rejection_reason: str | None)
    Same return signature as RiskManager for consistency.
    """

    # Controlled rejection reason codes — never dynamic
    REASONS = {
        'daily_pnl_unavailable_live_block',
        'daily_loss_limit_exceeded',
        'max_open_trades_exceeded',
        'symbol_exposure_exceeded',
        'sector_exposure_exceeded',
        'sector_unknown_live_block',
        'loss_streak_cooldown_active',
        'outcomes_unavailable_live_block',
    }

    def __init__(self):
        self.max_daily_loss_pct   = float(os.getenv('RISK_MAX_DAILY_LOSS_PCT', '3.0'))
        self.max_daily_loss_usd   = os.getenv('RISK_MAX_DAILY_LOSS_USD', '').strip()
        self.require_pnl_for_live = os.getenv('RISK_REQUIRE_DAILY_PNL_FOR_LIVE', 'true').lower() == 'true'
        self.allow_loss_override  = os.getenv('RISK_ALLOW_DAILY_LOSS_OVERRIDE', 'false').lower() == 'true'
        self.max_symbol_pct       = float(os.getenv('RISK_MAX_SYMBOL_EXPOSURE_PCT', '10.0'))
        self.max_sector_pct       = float(os.getenv('RISK_MAX_SECTOR_EXPOSURE_PCT', '30.0'))
        self.require_sector_live  = os.getenv('RISK_REQUIRE_SECTOR_FOR_LIVE', 'true').lower() == 'true'
        self.loss_streak_limit    = int(os.getenv('RISK_LOSS_STREAK_LIMIT', '3'))
        self.cooldown_mins        = int(os.getenv('RISK_LOSS_STREAK_COOLDOWN_MINS', '60'))
        self.require_outcomes_live= os.getenv('RISK_REQUIRE_OUTCOMES_FOR_LIVE', 'false').lower() == 'true'
        self.max_open_trades      = int(os.getenv('RISK_MAX_OPEN_POSITIONS', '10'))
        self.portfolio_size       = float(os.getenv('RISK_PORTFOLIO_SIZE', '100000'))
        self.sector_map           = SectorMap()

    def _reload_config(self):
        """Re-read env vars — called each evaluate() so dashboard edits take effect."""
        self.__init__()

    def _count_open_trades(self, ctx: PortfolioRiskContext) -> tuple[int, dict]:
        """
        Count open trades, deduplicating bracket legs.
        A parent + TP + STP bracket counts as ONE trade.
        Fallback chain: parentId → ocaGroup → symbol+time window.
        """
        trade_ids = set()
        detail = {'broker_orders': 0, 'broker_positions': 0, 'local_intents': 0}

        # Broker open orders — group by bracket
        for o in ctx.open_orders:
            parent_id = o.get('parent_id') or o.get('parentId') or 0
            oca_group = o.get('oca_group') or o.get('ocaGroup') or ''
            order_id  = o.get('order_id') or o.get('orderId') or 0

            if parent_id and parent_id != 0:
                # Child leg — key under parent's order_id
                trade_ids.add(f'bracket_{parent_id}')
            elif oca_group:
                trade_ids.add(f'oca_{oca_group}')
            else:
                # Parent or standalone — use bracket_{order_id}
                # so that child legs (bracket_{this_id}) resolve to same key
                trade_ids.add(f'bracket_{order_id}')
        detail['broker_orders'] = len(trade_ids)

        # Broker positions — deduplicate against order set by symbol
        broker_symbols = {o.get('symbol') for o in ctx.open_orders}
        for p in ctx.positions:
            sym = p.get('symbol', '')
            if abs(p.get('position', 0)) > 0 and sym not in broker_symbols:
                trade_ids.add(f'pos_{sym}')
        detail['broker_positions'] = len(trade_ids) - detail['broker_orders']

        # Local intents — deduplicate against broker state
        for intent in ctx.local_open_intents:
            sym = intent.get('symbol', '')
            if sym not in broker_symbols:
                trade_ids.add(f'local_{sym}')
        detail['local_intents'] = len(trade_ids) - detail['broker_orders'] - detail['broker_positions']

        return len(trade_ids), detail

    def _calc_symbol_exposure(self, symbol: str, new_notional: float,
                               ctx: PortfolioRiskContext) -> tuple[float, bool]:
        """
        Returns (projected_pct, estimated).
        estimated=True if broker market values unavailable, falling back to notional.
        """
        existing = 0.0
        estimated = False

        for p in ctx.positions:
            if p.get('symbol') == symbol and abs(p.get('position', 0)) > 0:
                mkt_val = p.get('market_value')
                if mkt_val is not None:
                    existing += abs(float(mkt_val))
                else:
                    existing += abs(p['position']) * p.get('avg_cost', 0)
                    estimated = True

        for o in ctx.open_orders:
            if o.get('symbol') == symbol:
                qty = o.get('qty') or o.get('totalQuantity') or 0
                price = o.get('lmt_price') or o.get('lmtPrice') or o.get('aux_price') or 0
                existing += abs(float(qty)) * float(price)
                estimated = True

        for intent in ctx.local_open_intents:
            if intent.get('symbol') == symbol:
                existing += abs(intent.get('position_size', 0) * intent.get('entry_price', 0))
                estimated = True

        projected = existing + new_notional
        return (projected / ctx.portfolio_value) * 100, estimated

    def _calc_sector_exposure(self, sector: str, new_notional: float,
                               ctx: PortfolioRiskContext) -> tuple[float, bool]:
        """Sector exposure including positions + orders + local intents + candidate."""
        existing = 0.0
        estimated = False

        all_positions = {p['symbol']: p for p in ctx.positions}
        all_orders = ctx.open_orders
        all_intents = ctx.local_open_intents

        for sym, p in all_positions.items():
            if ctx.sector_map.get(sym) == sector and abs(p.get('position', 0)) > 0:
                mkt = p.get('market_value')
                if mkt is not None:
                    existing += abs(float(mkt))
                else:
                    existing += abs(p['position']) * p.get('avg_cost', 0)
                    estimated = True

        for o in all_orders:
            sym = o.get('symbol', '')
            if ctx.sector_map.get(sym) == sector:
                qty = o.get('qty') or 0
                price = o.get('lmt_price') or o.get('lmtPrice') or 0
                existing += abs(float(qty)) * float(price)
                estimated = True

        for intent in all_intents:
            sym = intent.get('symbol', '')
            if ctx.sector_map.get(sym) == sector:
                existing += abs(intent.get('position_size', 0) * intent.get('entry_price', 0))
                estimated = True

        projected = existing + new_notional
        return (projected / ctx.portfolio_value) * 100, estimated

    def _check_loss_streak(self, ctx: PortfolioRiskContext) -> tuple[bool, dict]:
        """
        Returns (blocked, detail).
        Uses signal_outcomes.outcome ordered by resolved_at DESC.
        signal_outcomes has no qty column — P&L not faked.
        """
        detail = {'outcomes_available': False, 'streak': 0, 'cooldown_until': None}

        # Check active cooldown from persistent state
        cooldown_str = ctx.persistent_state.get('cooldown_until')
        if cooldown_str:
            try:
                from datetime import datetime, timezone
                cooldown_dt = datetime.fromisoformat(cooldown_str)
                now = datetime.now(timezone.utc)
                if cooldown_dt > now:
                    detail['cooldown_until'] = cooldown_str
                    detail['outcomes_available'] = True
                    return True, detail
            except Exception:
                pass

        # Read consecutive losses from signal_outcomes
        db = BASE_DIR / 'data' / 'signals.db'
        if not db.exists():
            return False, detail
        try:
            import sqlite3
            conn = sqlite3.connect(str(db))
            rows = conn.execute(
                """SELECT outcome FROM signal_outcomes
                   WHERE outcome IN ('WIN','LOSS')
                   AND resolved_at IS NOT NULL
                   ORDER BY resolved_at DESC LIMIT ?""",
                (self.loss_streak_limit + 5,)
            ).fetchall()
            conn.close()

            if not rows:
                detail['outcomes_available'] = False
                return False, detail

            detail['outcomes_available'] = True
            streak = 0
            for (outcome,) in rows:
                if outcome == 'LOSS':
                    streak += 1
                else:
                    break
            detail['streak'] = streak

            if streak >= self.loss_streak_limit:
                return True, detail
        except Exception as e:
            log.warning('[M14] Loss streak check failed: %s', e)

        return False, detail

    def evaluate(self, intent: OrderIntent,
                 ctx: PortfolioRiskContext) -> tuple[bool, dict, Optional[str]]:
        """
        Portfolio risk gate. Called after RiskManager.evaluate() passes.
        Returns (passed, checks, rejection_reason) — same signature as RiskManager.
        """
        # Re-read config on each call so dashboard edits take effect next signal
        self._reload_config()

        checks: dict[str, Any] = {}
        reasons: list[str] = []
        is_live = (ctx.mode == 'live')

        # ── 1. Daily loss ──────────────────────────────────────────────────────
        daily = ctx.daily_state
        pnl_available = bool(daily.get('daily_pnl_available', False))
        checks['daily_pnl_available'] = pnl_available
        checks['daily_pnl_source']    = daily.get('daily_pnl_source', 'unavailable')

        if pnl_available:
            pnl_pct = float(daily.get('realised_pnl_pct', 0))
            checks['daily_pnl_pct'] = pnl_pct
            loss_pct = -pnl_pct  # positive = loss
            checks['daily_loss_pct'] = loss_pct

            if self.allow_loss_override:
                # Override active — skip all daily loss enforcement
                checks['daily_loss_ok'] = 'overridden'
            elif daily.get('daily_loss_block_active'):
                reasons.append('daily_loss_limit_exceeded')
                checks['daily_loss_ok'] = False
            elif loss_pct >= self.max_daily_loss_pct:
                reasons.append('daily_loss_limit_exceeded')
                checks['daily_loss_ok'] = False
                checks['daily_loss_limit'] = self.max_daily_loss_pct
            else:
                checks['daily_loss_ok'] = True
        else:
            checks['daily_loss_ok'] = 'unavailable'
            if is_live and self.require_pnl_for_live:
                reasons.append('daily_pnl_unavailable_live_block')
            else:
                ctx.warnings.append('daily_pnl_unavailable')

        # ── 2. Max open trades (bracket-aware) ────────────────────────────────
        trade_count, trade_detail = self._count_open_trades(ctx)
        checks['open_trade_count'] = trade_count
        checks['open_trade_detail'] = trade_detail
        checks['max_open_trades_limit'] = self.max_open_trades

        if trade_count >= self.max_open_trades:
            reasons.append('max_open_trades_exceeded')
            checks['max_open_trades_ok'] = False
        else:
            checks['max_open_trades_ok'] = True

        # ── 3. Symbol exposure ────────────────────────────────────────────────
        new_notional = abs((intent.position_size or 0) * (intent.entry_price or 0))
        sym_pct, sym_estimated = self._calc_symbol_exposure(
            intent.symbol, new_notional, ctx)
        checks['symbol_exposure_pct']       = round(sym_pct, 2)
        checks['symbol_exposure_limit']     = self.max_symbol_pct
        checks['symbol_exposure_estimated'] = sym_estimated

        if sym_pct > self.max_symbol_pct:
            reasons.append('symbol_exposure_exceeded')
            checks['symbol_exposure_ok'] = False
        else:
            checks['symbol_exposure_ok'] = True

        # ── 4. Sector exposure ────────────────────────────────────────────────
        sector = ctx.sector_map.get(intent.symbol)
        checks['sector'] = sector

        if sector:
            sec_pct, sec_estimated = self._calc_sector_exposure(
                sector, new_notional, ctx)
            checks['sector_exposure_pct']       = round(sec_pct, 2)
            checks['sector_exposure_limit']     = self.max_sector_pct
            checks['sector_exposure_estimated'] = sec_estimated

            if sec_pct > self.max_sector_pct:
                reasons.append('sector_exposure_exceeded')
                checks['sector_exposure_ok'] = False
            else:
                checks['sector_exposure_ok'] = True
        else:
            checks['sector_exposure_ok'] = 'unknown'
            if is_live and self.require_sector_live:
                reasons.append('sector_unknown_live_block')
            else:
                ctx.warnings.append(f'sector_unknown_{intent.symbol}')

        # ── 5. Loss streak cooldown ───────────────────────────────────────────
        streak_blocked, streak_detail = self._check_loss_streak(ctx)
        checks['loss_streak'] = streak_detail

        if not streak_detail['outcomes_available']:
            checks['streak_ok'] = 'unavailable'
            if is_live and self.require_outcomes_live:
                reasons.append('outcomes_unavailable_live_block')
            else:
                ctx.warnings.append('outcomes_unavailable')
        elif streak_blocked:
            reasons.append('loss_streak_cooldown_active')
            checks['streak_ok'] = False
        else:
            checks['streak_ok'] = True

        # ── Final verdict ─────────────────────────────────────────────────────
        passed = len(reasons) == 0
        checks['verdict']  = 'pass' if passed else 'reject'
        checks['warnings'] = ctx.warnings

        rejection_reason = reasons[0] if reasons else None  # first/primary reason
        checks['all_reasons'] = reasons

        if passed:
            log.info('[PORTFOLIO_RISK] %s %s: PASS (warnings=%s)',
                     intent.symbol, intent.direction.upper(), ctx.warnings or 'none')
        else:
            log.info('[PORTFOLIO_RISK] %s %s: REJECT — %s',
                     intent.symbol, intent.direction.upper(), rejection_reason)

        return passed, checks, rejection_reason

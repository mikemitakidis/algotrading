#!/usr/bin/env python3
"""main.py - Algo Trader v1"""
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
LOG_PATH   = BASE_DIR / 'logs' / 'bot.log'
STATE_PATH = BASE_DIR / 'data' / 'bot_state.json'

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

root = logging.getLogger()
if not root.handlers:
    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh  = logging.FileHandler(str(LOG_PATH))
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

log = logging.getLogger(__name__)

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

from bot.config   import load
from bot.focus    import FOCUS_SYMBOLS
from bot.database import init_db, insert_signal, init_features_table, insert_signal_features
from bot.flywheel  import init_flywheel_tables, log_candidate, log_intent, recent_intents, update_intent_status
from bot.brokers   import get_broker, get_broker_name
from bot.brokers.base import OrderIntent
from bot.risk       import RiskManager
from bot.scanner  import scan_cycle
from bot.notifier import (alert_startup, alert_stopped,
                          alert_crash, alert_cycle_summary, alert_signal)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def _read_existing_state() -> dict:
    """Read existing state file so we can preserve last-cycle fields on restart."""
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def write_state(state: dict) -> None:
    """Atomically write bot state JSON so dashboard never reads a partial file."""
    tmp = STATE_PATH.with_suffix('.tmp')
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, default=str))
        tmp.replace(STATE_PATH)
    except Exception as exc:
        log.debug('[STATE] write failed: %s', exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info('=' * 55)
    log.info('ALGO TRADER v1 -- SHADOW MODE -- STARTING')
    log.info('=' * 55)

    prev = _read_existing_state()

    try:
        config = load()
        log.info('[STARTUP] Config loaded.')
        log.info('[STARTUP] Mode: %s | Scan: %ds | Focus: %d',
                 config['bot_mode'], config['scan_interval_secs'], config['focus_size'])
    except Exception as e:
        log.error('[STARTUP] Config error: %s', e)
        write_state({'phase': 'crashed', 'error': str(e)})
        sys.exit(1)

    (BASE_DIR / 'data').mkdir(parents=True, exist_ok=True)

    # ── Live trading startup hard-stop ───────────────────────────────────
    # If BROKER=ibkr_live, ALL safety config must be present before
    # the bot is allowed to start. This is a hard sys.exit(1), not a warning.
    _broker_name = config.get('broker', 'paper').lower().strip()
    if _broker_name == 'ibkr_live':
        from bot.brokers.ibkr_broker import _check_live_safety_config
        _safe, _reason = _check_live_safety_config()
        if not _safe:
            log.error('=' * 60)
            log.error('[STARTUP] LIVE TRADING STARTUP REFUSED')
            log.error('[STARTUP] Safety config incomplete: %s', _reason)
            log.error('[STARTUP] Fix .env and restart. Bot will NOT start.')
            log.error('=' * 60)
            write_state({'phase': 'refused', 'reason': f'live_safety: {_reason}'})
            sys.exit(1)
        log.info('[STARTUP] *** LIVE TRADING MODE — REAL MONEY ***')
        log.info('[STARTUP] Live safety config: OK')

    conn = init_db(config['db_path'])
    init_features_table(conn)
    init_flywheel_tables(conn)
    risk_mgr = RiskManager()
    broker   = get_broker()
    log.info('[STARTUP] Broker: %s | Risk: max_pos=%s max_open=%d portfolio=$%.0f',
             broker.name, risk_mgr.max_position_pct, risk_mgr.max_open, risk_mgr.portfolio_size)

    focus = FOCUS_SYMBOLS[:config['focus_size']]
    log.info('[STARTUP] Focus: %d curated large-cap symbols (no Tier A ranking in V1)', len(focus))
    log.info('[STARTUP] Data: Yahoo Finance | 1 symbol/request | 8-12s delay | browser session | disk cache')
    log.info('[STARTUP] First cycle uses disk cache where available. Fresh fetches: 8-12s each.')

    if config['telegram_enabled']:
        log.info('[STARTUP] Telegram: ENABLED')
    else:
        log.info('[STARTUP] Telegram: disabled')

    alert_startup(config)

    uptime_started = datetime.now(timezone.utc).isoformat()
    scan_interval  = config['scan_interval_secs']

    write_state({
        'phase':               'starting',
        'mode':                config['bot_mode'],
        'cycle':               0,
        'focus_count':         len(focus),
        'scan_interval_secs':  scan_interval,
        'uptime_started':      uptime_started,
        'last_cycle_at':       prev.get('last_cycle_at'),
        'last_cycle_signals':  prev.get('last_cycle_signals'),
        'last_cycle_tfs':      prev.get('last_cycle_tfs'),
        'last_cycle_tfs_list': prev.get('last_cycle_tfs_list', []),
        'last_cycle_symbols':  prev.get('last_cycle_symbols'),
        'last_cycle_duration_s': prev.get('last_cycle_duration_s'),
        'next_cycle_at':       None,
    })

    cycle = 0

    try:
        while True:
            cycle += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            log.info('[MAIN] === Cycle %d starting | %d symbols ===', cycle, len(focus))

            write_state({
                'phase':               'scanning',
                'mode':                config['bot_mode'],
                'cycle':               cycle,
                'focus_count':         len(focus),
                'scan_interval_secs':  scan_interval,
                'uptime_started':      uptime_started,
                'last_cycle_at':       prev.get('last_cycle_at'),
                'last_cycle_signals':  prev.get('last_cycle_signals'),
                'last_cycle_tfs':      prev.get('last_cycle_tfs'),
                'last_cycle_tfs_list': prev.get('last_cycle_tfs_list', []),
                'last_cycle_symbols':  prev.get('last_cycle_symbols'),
                'last_cycle_duration_s': prev.get('last_cycle_duration_s'),
                'next_cycle_at':       None,
                'scan_started_at':     now_iso,
            })

            cycle_start = time.monotonic()
            signals, meta = scan_cycle(focus, config, conn=conn, cycle_id=cycle)
            cycle_duration = round(time.monotonic() - cycle_start)

            inserted = 0
            for signal in signals:
                ml_feats = signal.pop('_ml_features', {})
                signal.pop('_candidate_stage', None)  # strip scanner tag
                row_id = insert_signal(conn, signal)
                if row_id:
                    inserted += 1
                    insert_signal_features(conn, row_id, signal, ml_feats)
                    # Log final_signal to flywheel
                    log_candidate(
                        conn, cycle, signal.get('symbol',''), signal.get('direction',''),
                        stage='final_signal',
                        valid_count=signal.get('valid_count', 0),
                        tfs_passing=[k for k in ('1D','4H','1H','15m')
                                     if signal.get(f'tf_{k.lower().replace("h","h")}', 0)],
                        available_tfs=meta.get('tfs_available', 0),
                        min_valid=0,
                        route=signal.get('route',''),
                        strategy_version=signal.get('strategy_version', 1),
                        signal_id=row_id,
                        ind={k: signal.get(k) for k in ('rsi','macd_hist','atr','bb_pos','vwap_dev','vol_ratio')},
                    )
                    # Risk check + execution intent
                    intent = OrderIntent(
                        signal_id=row_id,
                        symbol=signal.get('symbol',''),
                        direction=signal.get('direction',''),
                        route=signal.get('route',''),
                        entry_price=signal.get('entry_price', 0.0),
                        stop_loss=signal.get('stop_loss', 0.0),
                        target_price=signal.get('target_price', 0.0),
                        valid_count=signal.get('valid_count', 0),
                        strategy_version=signal.get('strategy_version', 1),
                    )
                    risk_passed, risk_checks, risk_reason = risk_mgr.evaluate(intent)
                    intent.risk_checks = risk_checks
                    if risk_passed:
                        result = broker.submit(intent)
                        intent_id = log_intent(conn, row_id,
                            signal.get('symbol',''), signal.get('direction',''),
                            signal.get('route',''),
                            signal.get('entry_price',0), signal.get('stop_loss',0),
                            signal.get('target_price',0),
                            intent.position_size, intent.risk_usd,
                            signal.get('valid_count',0), signal.get('strategy_version',1),
                            broker.name, result.status,
                            broker_order_id=result.broker_order_id,
                            risk_checks=risk_checks)
                        # Wire lifecycle: log initial status transition
                        if intent_id:
                            update_intent_status(
                                conn, intent_id, result.status,
                                event=f'broker_response:{result.status}'
                            )
                            if result.status not in ('accepted','paper_logged'):
                                update_intent_status(
                                    conn, intent_id, 'error',
                                    event=f'broker_error:{result.reason[:80] if result.reason else "unknown"}'
                                )
                    else:
                        intent_id = log_intent(conn, row_id,
                            signal.get('symbol',''), signal.get('direction',''),
                            signal.get('route',''),
                            signal.get('entry_price',0), signal.get('stop_loss',0),
                            signal.get('target_price',0),
                            intent.position_size, intent.risk_usd,
                            signal.get('valid_count',0), signal.get('strategy_version',1),
                            broker.name, 'risk_rejected',
                            rejection_reason=risk_reason,
                            risk_checks=risk_checks)
                        if intent_id:
                            update_intent_status(
                                conn, intent_id, 'risk_rejected',
                                event=f'risk_rejected:{risk_reason}'
                            )
                    alert_signal(config, signal)

            if inserted:
                log.info('[MAIN] DB: %d signals inserted', inserted)

            alert_cycle_summary(config, cycle, len(signals), len(focus))

            last_cycle_at = datetime.now(timezone.utc).isoformat()
            next_cycle_at = datetime.fromtimestamp(
                time.time() + scan_interval, tz=timezone.utc
            ).isoformat()

            log.info('[MAIN] === Cycle %d complete | Signals: %d | Next in %ds ===',
                     cycle, len(signals), scan_interval)

            state = {
                'phase':               'cooldown',
                'mode':                config['bot_mode'],
                'cycle':               cycle,
                'focus_count':         len(focus),
                'scan_interval_secs':  scan_interval,
                'uptime_started':      uptime_started,
                'last_cycle_at':       last_cycle_at,
                'last_cycle_signals':  len(signals),
                'last_cycle_tfs':      meta.get('tfs_available', 0),
                'last_cycle_tfs_list': meta.get('tfs_list', []),
                'last_cycle_symbols':  meta.get('symbols_scanned', len(focus)),
                'last_cycle_duration_s': cycle_duration,
                'next_cycle_at':       next_cycle_at,
            }
            write_state(state)
            prev = state

            time.sleep(scan_interval)

    except KeyboardInterrupt:
        log.info('[MAIN] Stopped by user.')
        alert_stopped(config, 'Clean shutdown')
        write_state({**prev, 'phase': 'stopped'})

    except Exception as e:
        log.error('[MAIN] Unhandled crash: %s', e, exc_info=True)
        alert_crash(config, str(e))
        write_state({**prev, 'phase': 'crashed', 'error': str(e)})
        raise


if __name__ == '__main__':
    main()

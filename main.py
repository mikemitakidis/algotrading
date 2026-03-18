#!/usr/bin/env python3
"""main.py — Algo Trader v1"""
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / 'logs' / 'bot.log'
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

from bot.config   import load
from bot.assets   import ASSET_UNIVERSE
from bot.database import init_db, insert_signal
from bot.scanner  import rank_symbols, scan_cycle
from bot.data     import load_focus_cache
from bot.notifier import (alert_startup, alert_stopped,
                          alert_crash, alert_cycle_summary, alert_signal)


def get_symbols() -> list:
    symbols = [s for s in ASSET_UNIVERSE if '.' not in s and '-' not in s and len(s) <= 5]
    log.info('[STARTUP] Asset universe: %d US symbols', len(symbols))
    return symbols


def main():
    log.info('=' * 55)
    log.info('ALGO TRADER v1 -- SHADOW MODE -- STARTING')
    log.info('=' * 55)

    try:
        config = load()
        log.info('[STARTUP] Config loaded.')
        log.info('[STARTUP] Mode: %s | Scan: %ds | Rank every: %ds | Focus: %d',
                 config['bot_mode'], config['scan_interval_secs'],
                 config['rank_interval_secs'], config['focus_size'])
    except Exception as e:
        log.error('[STARTUP] Config error: %s', e)
        sys.exit(1)

    (BASE_DIR / 'data').mkdir(parents=True, exist_ok=True)
    conn    = init_db(config['db_path'])
    symbols = get_symbols()

    if not symbols:
        log.error('[STARTUP] No symbols loaded. Exiting.')
        sys.exit(1)

    # No aggressive connectivity test — yfinance rate limits are per-IP
    # and startup tests make it worse. The scan loop handles all retries.
    log.info('[STARTUP] Skipping startup connectivity test (avoids triggering rate limits)')
    log.info('[STARTUP] Data provider: Yahoo Finance (yfinance) | Batch size: 20 | Paced with jitter')

    if config['telegram_enabled']:
        log.info('[STARTUP] Telegram: ENABLED')
    else:
        log.info('[STARTUP] Telegram: disabled')

    alert_startup(config)

    # Try loading cached focus set — avoids immediate full re-rank on restart
    focus = load_focus_cache(max_age_secs=config['rank_interval_secs'])
    if focus:
        last_rank = datetime.now(timezone.utc)
        log.info('[STARTUP] Loaded %d symbols from focus cache — skipping initial rank', len(focus))
    else:
        focus     = []
        last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
        log.info('[STARTUP] No valid focus cache — will rank on first cycle')

    cycle = 0

    try:
        while True:
            cycle += 1
            now     = datetime.now(timezone.utc)
            elapsed = (now - last_rank).total_seconds()

            # Re-rank if cache is stale
            if elapsed >= config['rank_interval_secs']:
                log.info('[MAIN] Re-ranking (%.1fh since last rank)...', elapsed / 3600)
                new_focus = rank_symbols(symbols, config['focus_size'])
                if new_focus:
                    focus     = new_focus
                    last_rank = now
                    log.info('[MAIN] Focus set updated: %d symbols', len(focus))
                else:
                    # Rate limited during ranking — use existing focus if available
                    if focus:
                        log.warning('[MAIN] Ranking failed — keeping existing focus (%d symbols)', len(focus))
                        last_rank = now  # reset timer to avoid hammering
                    else:
                        log.warning('[MAIN] Ranking failed and no existing focus. '
                                    'Waiting 10 min before retry...')
                        time.sleep(600)
                        continue

            if not focus:
                log.warning('[MAIN] Focus empty. Waiting 10 min...')
                time.sleep(600)
                continue

            log.info('[MAIN] === Cycle %d starting | Focus: %d symbols ===', cycle, len(focus))
            signals = scan_cycle(focus, config)

            inserted = 0
            for signal in signals:
                row_id = insert_signal(conn, signal)
                if row_id:
                    inserted += 1
                    alert_signal(config, signal)

            if inserted > 0:
                log.info('[MAIN] DB: %d signals inserted', inserted)

            alert_cycle_summary(config, cycle, len(signals), len(focus))

            log.info('[MAIN] === Cycle %d complete | Signals: %d | Next in %ds ===',
                     cycle, len(signals), config['scan_interval_secs'])
            time.sleep(config['scan_interval_secs'])

    except KeyboardInterrupt:
        log.info('[MAIN] Stopped by user.')
        alert_stopped(config, 'Clean shutdown')

    except Exception as e:
        log.error('[MAIN] Unhandled crash: %s', e, exc_info=True)
        alert_crash(config, str(e))
        raise


if __name__ == '__main__':
    main()

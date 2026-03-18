#!/usr/bin/env python3
"""main.py — Algo Trader v1 Entry Point"""
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
from bot.notifier import (alert_startup, alert_stopped,
                          alert_crash, alert_cycle_summary, alert_signal)
from bot.data     import fetch_bars


def get_symbols() -> list:
    symbols = [s for s in ASSET_UNIVERSE if '.' not in s and '-' not in s and len(s) <= 5]
    log.info('[STARTUP] Asset universe: %d US symbols', len(symbols))
    return symbols


def connectivity_test() -> bool:
    """
    Test yfinance with a single symbol.
    On rate limit: warn and return True so the bot continues.
    The main scan loop has its own retry logic.
    Only return False on a genuine network failure.
    """
    log.info('[STARTUP] Connectivity test (single symbol, 1d)...')
    for attempt in range(3):
        try:
            bars = fetch_bars(['AAPL'], '2d', '1d')
            if bars:
                log.info('[STARTUP] yfinance OK: AAPL %d bars', len(bars.get('AAPL', [])))
                return True
            # Empty result — could be rate limit or no data
            wait = [10, 30, 60][attempt]
            log.warning('[STARTUP] Connectivity attempt %d: no data. Waiting %ds...', attempt+1, wait)
            time.sleep(wait)
        except Exception as e:
            err = str(e)
            if 'Rate' in err or 'Too Many' in err:
                # Rate limit is not a network failure — bot can still run
                log.warning('[STARTUP] yfinance rate limit on connectivity test. '
                            'Continuing anyway — main scan loop will retry.')
                return True
            wait = [10, 30, 60][attempt]
            log.warning('[STARTUP] Connectivity attempt %d error: %s. Waiting %ds...', attempt+1, err[:80], wait)
            time.sleep(wait)

    log.warning('[STARTUP] Connectivity test inconclusive after 3 attempts. '
                'Continuing — scan loop will handle retries.')
    return True   # Don't exit — let the scan loop try


def main():
    log.info('=' * 55)
    log.info('ALGO TRADER v1 -- SHADOW MODE -- STARTING')
    log.info('=' * 55)

    try:
        config = load()
        log.info('[STARTUP] Config loaded.')
        log.info('[STARTUP] Mode: %s | Scan: %ds | Focus: %d',
                 config['bot_mode'], config['scan_interval_secs'], config['focus_size'])
    except Exception as e:
        log.error('[STARTUP] Config error: %s', e)
        sys.exit(1)

    (BASE_DIR / 'data').mkdir(parents=True, exist_ok=True)
    conn    = init_db(config['db_path'])
    symbols = get_symbols()

    if not symbols:
        log.error('[STARTUP] No symbols loaded. Exiting.')
        sys.exit(1)

    connectivity_test()   # warns but never exits

    if config['telegram_enabled']:
        log.info('[STARTUP] Telegram: ENABLED')
    else:
        log.info('[STARTUP] Telegram: disabled (set TELEGRAM_ENABLED=true in .env to enable)')

    alert_startup(config)

    focus     = []
    last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cycle     = 0

    try:
        while True:
            cycle += 1
            now     = datetime.now(timezone.utc)
            elapsed = (now - last_rank).total_seconds()

            if elapsed >= config['rank_interval_secs']:
                log.info('[TIER-A] Re-ranking (%.1fh since last rank)...', elapsed/3600)
                focus     = rank_symbols(symbols, config['focus_size'])
                last_rank = now

            if not focus:
                log.warning('[MAIN] Focus empty after ranking. Waiting 10 min then retrying.')
                time.sleep(600)
                last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
                continue

            log.info('[MAIN] === Cycle %d starting ===', cycle)
            signals = scan_cycle(focus, config)

            for signal in signals:
                row_id = insert_signal(conn, signal)
                if row_id:
                    alert_signal(config, signal)

            alert_cycle_summary(config, cycle, len(signals), len(focus))

            log.info('[MAIN] === Cycle %d complete. Signals: %d. Next in %ds ===',
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

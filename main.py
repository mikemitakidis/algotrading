#!/usr/bin/env python3
"""main.py - Algo Trader v1"""
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

import warnings
warnings.filterwarnings('ignore', category=FutureWarning)

from bot.config   import load
from bot.focus    import FOCUS_SYMBOLS
from bot.database import init_db, insert_signal
from bot.scanner  import scan_cycle
from bot.notifier import (alert_startup, alert_stopped,
                          alert_crash, alert_cycle_summary, alert_signal)


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
    conn = init_db(config['db_path'])

    # V1: use curated focus list directly — no Tier A ranking
    # Tier A dynamic ranking re-enabled in V2 once bar cache is warm
    focus = FOCUS_SYMBOLS[:config['focus_size']]
    log.info('[STARTUP] Focus: %d curated large-cap symbols (no Tier A ranking in V1)', len(focus))
    log.info('[STARTUP] Data: Yahoo Finance | 1 symbol/request | 8-12s delay | browser session | disk cache')
    log.info('[STARTUP] First cycle uses disk cache where available. Fresh fetches: 8-12s each.')

    if config['telegram_enabled']:
        log.info('[STARTUP] Telegram: ENABLED')
    else:
        log.info('[STARTUP] Telegram: disabled')

    alert_startup(config)

    cycle = 0

    try:
        while True:
            cycle += 1
            log.info('[MAIN] === Cycle %d starting | %d symbols ===', cycle, len(focus))

            signals = scan_cycle(focus, config)

            inserted = 0
            for signal in signals:
                row_id = insert_signal(conn, signal)
                if row_id:
                    inserted += 1
                    alert_signal(config, signal)

            if inserted:
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

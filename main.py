#!/usr/bin/env python3
"""
main.py — Algo Trader v1 Entry Point
"""
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / 'logs' / 'bot.log'
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Logging: FileHandler only — stdout is handled by the calling process ──────
# Using both FileHandler AND StreamHandler causes duplicate lines when the
# calling process (deploy.sh / sync.sh) also redirects stdout to the log file.
root = logging.getLogger()
if not root.handlers:
    root.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    fh = logging.FileHandler(str(LOG_PATH))
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # Only add stdout handler when running interactively (not redirected)
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

log = logging.getLogger(__name__)

from bot.config   import load
from bot.assets   import ASSET_UNIVERSE
from bot.database import init_db, insert_signal
from bot.scanner  import rank_symbols, scan_cycle
from bot.notifier import send_alert
from bot.data     import fetch_bars


def get_symbols() -> list:
    """Filter asset universe to clean US tickers only."""
    symbols = [s for s in ASSET_UNIVERSE if '.' not in s and '-' not in s and len(s) <= 5]
    log.info(f'[STARTUP] Asset universe: {len(symbols)} US symbols loaded from bot.assets')
    return symbols


def connectivity_test() -> bool:
    log.info('[STARTUP] Running connectivity test...')
    bars = fetch_bars(['AAPL', 'MSFT', 'NVDA', 'SPY', 'QQQ'], '5d', '1d')
    if bars:
        summary = ' | '.join(f'{k}:{len(v)}bars' for k, v in bars.items())
        log.info(f'[STARTUP] yfinance OK: {summary}')
        return True
    log.error('[STARTUP] yfinance FAILED — no data returned')
    return False


def main():
    log.info('=' * 55)
    log.info('ALGO TRADER v1 — SHADOW MODE — STARTING')
    log.info('=' * 55)

    # Config
    try:
        config = load()
        log.info('[STARTUP] Config loaded. All required keys present.')
        log.info(f'[STARTUP] Mode: {config["bot_mode"]} | '
                 f'Scan: {config["scan_interval_secs"]}s | '
                 f'Focus: {config["focus_size"]}')
    except Exception as e:
        log.error(f'[STARTUP] Config error: {e}')
        sys.exit(1)

    # DB
    (BASE_DIR / 'data').mkdir(parents=True, exist_ok=True)
    conn = init_db(config['db_path'])

    # Symbols
    symbols = get_symbols()
    if not symbols:
        log.error('[STARTUP] No symbols loaded. Exiting.')
        sys.exit(1)

    # Connectivity
    if not connectivity_test():
        log.error('[STARTUP] Cannot reach yfinance. Check server internet access.')
        sys.exit(1)

    # Telegram
    if config['telegram_token'] and config['telegram_chat_id']:
        log.info('[STARTUP] Telegram: CONFIGURED — alerts will be sent')
    else:
        log.info('[STARTUP] Telegram: not configured — alerts skipped (set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env to enable)')

    # Main loop
    focus: list = []
    last_rank   = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cycle       = 0

    while True:
        try:
            cycle += 1
            now     = datetime.now(timezone.utc)
            elapsed = (now - last_rank).total_seconds()

            if elapsed >= config['rank_interval_secs']:
                log.info(f'[TIER-A] Re-ranking (last rank {elapsed/3600:.1f}h ago)...')
                focus     = rank_symbols(symbols, config['focus_size'])
                last_rank = now

            if not focus:
                log.warning('[MAIN] Focus set empty after ranking. Retrying in 5 min.')
                time.sleep(300)
                last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
                continue

            log.info(f'[MAIN] === Cycle {cycle} starting ===')
            signals = scan_cycle(focus, config)

            for signal in signals:
                row_id = insert_signal(conn, signal)
                if row_id:
                    send_alert(config, signal)

            log.info(f'[MAIN] === Cycle {cycle} complete. Signals: {len(signals)}. '
                     f'Next in {config["scan_interval_secs"]}s ===')
            time.sleep(config['scan_interval_secs'])

        except KeyboardInterrupt:
            log.info('[MAIN] Stopped.')
            break
        except Exception as e:
            log.error(f'[MAIN] Unhandled error cycle {cycle}: {e}', exc_info=True)
            time.sleep(60)


if __name__ == '__main__':
    main()

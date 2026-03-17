#!/usr/bin/env python3
"""
main.py — Algo Trader v1 Entry Point
Loads config, initialises DB, runs scan loop.
Imports only from bot/ package.
"""
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Logging setup (must happen before any bot imports) ────────────────────────
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / 'logs' / 'bot.log'
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(str(LOG_PATH)),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Bot imports ───────────────────────────────────────────────────────────────
from bot.config   import load
from bot.database import init_db, insert_signal
from bot.scanner  import rank_symbols, scan_cycle
from bot.notifier import send_alert
from bot.data     import fetch_bars


def connectivity_test() -> bool:
    """Fetch 5 well-known symbols on daily bars. Return True if any data received."""
    log.info('[STARTUP] Running connectivity test...')
    test_symbols = ['AAPL', 'MSFT', 'NVDA', 'SPY', 'QQQ']
    bars = fetch_bars(test_symbols, '5d', '1d')
    if bars:
        summary = ' | '.join(f"{k}:{len(v)}bars" for k, v in bars.items())
        log.info(f'[STARTUP] yfinance OK: {summary}')
        return True
    else:
        log.error('[STARTUP] yfinance connectivity FAILED — no data returned for test symbols')
        return False


def get_assets() -> list:
    """Load US equity symbol list from config/assets.py."""
    assets_path = BASE_DIR / 'config' / 'assets.py'
    if not assets_path.exists():
        log.error(f'[STARTUP] assets.py not found at {assets_path}')
        return []
    sys.path.insert(0, str(BASE_DIR / 'config'))
    from assets import ASSET_UNIVERSE
    # Filter to plain US tickers only (no dots, no dashes, max 5 chars)
    symbols = [s for s in ASSET_UNIVERSE if '.' not in s and '-' not in s and len(s) <= 5]
    log.info(f'[STARTUP] Asset universe loaded: {len(symbols)} US symbols')
    return symbols


def main():
    log.info('=' * 55)
    log.info('ALGO TRADER v1 — SHADOW MODE — STARTING')
    log.info('=' * 55)

    # ── Load config ───────────────────────────────────────────────────────────
    try:
        config = load()
        log.info('[STARTUP] Config loaded. All required keys present.')
        log.info(f'[STARTUP] Mode: {config["bot_mode"]} | '
                 f'Scan interval: {config["scan_interval_secs"]}s | '
                 f'Focus size: {config["focus_size"]}')
    except (FileNotFoundError, ValueError) as e:
        log.error(f'[STARTUP] Config error: {e}')
        sys.exit(1)

    # ── Initialise database ───────────────────────────────────────────────────
    (BASE_DIR / 'data').mkdir(parents=True, exist_ok=True)
    conn = init_db(config['db_path'])

    # ── Load assets ───────────────────────────────────────────────────────────
    symbols = get_assets()
    if not symbols:
        log.error('[STARTUP] No symbols loaded. Exiting.')
        sys.exit(1)

    # ── Connectivity test ─────────────────────────────────────────────────────
    if not connectivity_test():
        log.error('[STARTUP] Cannot fetch market data. Check server internet access.')
        sys.exit(1)

    # ── Telegram config summary ───────────────────────────────────────────────
    if config['telegram_token'] and config['telegram_chat_id']:
        log.info('[STARTUP] Telegram: configured — alerts will be sent')
    else:
        log.info('[STARTUP] Telegram: not configured — alerts will be skipped')

    # ── Main loop ─────────────────────────────────────────────────────────────
    focus: list = []
    last_rank   = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cycle       = 0

    while True:
        try:
            cycle += 1
            now    = datetime.now(timezone.utc)
            elapsed_since_rank = (now - last_rank).total_seconds()

            # Re-rank every rank_interval_secs (default 6 hours)
            if elapsed_since_rank >= config['rank_interval_secs']:
                log.info(f'[TIER-A] Re-ranking (last rank {elapsed_since_rank/3600:.1f}h ago)...')
                focus     = rank_symbols(symbols, config['focus_size'])
                last_rank = now

            if not focus:
                log.warning('[MAIN] Focus set is empty after ranking. Retrying in 5 min.')
                time.sleep(300)
                last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
                continue

            # Full 4-TF scan cycle
            log.info(f'[MAIN] === Cycle {cycle} starting ===')
            signals = scan_cycle(focus, config)

            # Process each signal
            for signal in signals:
                # Insert to DB
                row_id = insert_signal(conn, signal)

                # Send Telegram alert
                if row_id:
                    send_alert(config, signal)

            log.info(
                f'[MAIN] === Cycle {cycle} complete. '
                f'Signals: {len(signals)}. '
                f'Next in {config["scan_interval_secs"]}s ==='
            )
            time.sleep(config['scan_interval_secs'])

        except KeyboardInterrupt:
            log.info('[MAIN] Stopped by user.')
            break
        except Exception as e:
            log.error(f'[MAIN] Unhandled error in cycle {cycle}: {e}', exc_info=True)
            log.info('[MAIN] Sleeping 60s before retry...')
            time.sleep(60)


if __name__ == '__main__':
    main()

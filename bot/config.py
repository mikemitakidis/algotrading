"""
bot/config.py
Loads .env and returns config dict.
All secrets from environment only. No defaults for secrets.
"""
import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def load() -> dict:
    env_path = BASE_DIR / '.env'
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)
        log.info('[CONFIG] Loaded .env from %s', env_path)
    else:
        log.warning('[CONFIG] No .env file at %s -- using environment variables', env_path)

    # Dashboard password: warn if using default
    dashboard_password = os.getenv('DASHBOARD_PASSWORD', '').strip()
    if not dashboard_password:
        dashboard_password = 'changeme'
        log.warning('[CONFIG] DASHBOARD_PASSWORD not set -- using default "changeme"')

    # Telegram: fully optional
    tg_enabled = os.getenv('TELEGRAM_ENABLED', 'false').strip().lower() in ('true', '1', 'yes')
    tg_token   = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    tg_chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()

    if tg_enabled and tg_token and tg_chat_id:
        log.info('[CONFIG] Telegram: ENABLED')
    elif tg_enabled:
        log.warning('[CONFIG] Telegram: TELEGRAM_ENABLED=true but token or chat_id is missing -- disabling')
        tg_enabled = False
    else:
        log.info('[CONFIG] Telegram: disabled (set TELEGRAM_ENABLED=true in .env to enable)')

    return {
        # Telegram
        'telegram_enabled':       tg_enabled,
        'telegram_token':         tg_token,
        'telegram_chat_id':       tg_chat_id,
        'telegram_cooldown_secs': int(os.getenv('TELEGRAM_COOLDOWN_SECS', '14400')),  # 4 hours

        # Dashboard
        'dashboard_password': dashboard_password,
        'dashboard_port':     int(os.getenv('DASHBOARD_PORT', '8080')),

        # Bot behaviour
        'bot_mode':              os.getenv('BOT_MODE', 'shadow'),
        'scan_interval_secs':    int(os.getenv('SCAN_INTERVAL_SECS', '900')),
        'rank_interval_secs':    int(os.getenv('RANK_INTERVAL_SECS', '21600')),
        'focus_size':            int(os.getenv('FOCUS_SIZE', '150')),

        # Paths
        'db_path':   str(BASE_DIR / 'data' / 'signals.db'),
        'log_path':  str(BASE_DIR / 'logs' / 'bot.log'),
        'base_dir':  str(BASE_DIR),
    }

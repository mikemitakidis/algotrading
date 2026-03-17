"""
bot/config.py
Loads .env and returns config dict.
- DASHBOARD_PASSWORD: defaults to 'changeme' with a loud warning if not set
- TELEGRAM_TOKEN / TELEGRAM_CHAT_ID: fully optional, alerts silently skipped if absent
- All other settings have safe defaults
- Raises FileNotFoundError only if .env is missing AND no env vars are set at all
"""
import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def load() -> dict:
    # Try loading .env — tolerate missing file (env vars may be set directly)
    env_path = BASE_DIR / '.env'
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)
        log.info('[CONFIG] Loaded .env from %s', env_path)
    else:
        log.warning('[CONFIG] No .env file found at %s — relying on environment variables', env_path)

    # DASHBOARD_PASSWORD: warn loudly if using default
    dashboard_password = os.getenv('DASHBOARD_PASSWORD', '').strip()
    if not dashboard_password:
        dashboard_password = 'changeme'
        log.warning('[CONFIG] DASHBOARD_PASSWORD not set — using default "changeme". Set it in .env immediately.')

    # Telegram: fully optional
    telegram_token   = os.getenv('TELEGRAM_TOKEN',   '').strip()
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not telegram_token or not telegram_chat_id:
        log.info('[CONFIG] Telegram not configured — alerts will be skipped')

    return {
        'telegram_token':     telegram_token,
        'telegram_chat_id':   telegram_chat_id,
        'dashboard_password': dashboard_password,
        'dashboard_port':     int(os.getenv('DASHBOARD_PORT', '8080')),
        'bot_mode':           os.getenv('BOT_MODE', 'shadow'),
        'scan_interval_secs': int(os.getenv('SCAN_INTERVAL_SECS', '900')),   # 15 min
        'rank_interval_secs': int(os.getenv('RANK_INTERVAL_SECS', '21600')), # 6 hours
        'focus_size':         int(os.getenv('FOCUS_SIZE', '150')),
        'db_path':            str(BASE_DIR / 'data' / 'signals.db'),
        'log_path':           str(BASE_DIR / 'logs' / 'bot.log'),
        'base_dir':           str(BASE_DIR),
    }

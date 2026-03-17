"""
bot/config.py
Loads .env and validates required keys.
Raises clear errors if anything is missing.
No defaults for secrets.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

def load() -> dict:
    """Load .env from BASE_DIR. Validate required keys. Return config dict."""
    env_path = BASE_DIR / '.env'
    if not env_path.exists():
        raise FileNotFoundError(
            f".env file not found at {env_path}. "
            f"Copy .env.example to .env and fill in your values."
        )

    load_dotenv(env_path)

    # Required keys — bot will not start without these
    required = ['DASHBOARD_PASSWORD']

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ValueError(
            f"Missing required .env keys: {', '.join(missing)}. "
            f"See .env.example for reference."
        )

    return {
        # Telegram — optional in V1 (alerts silently skipped if not set)
        'telegram_token':    os.getenv('TELEGRAM_TOKEN', ''),
        'telegram_chat_id':  os.getenv('TELEGRAM_CHAT_ID', ''),

        # Dashboard
        'dashboard_password': os.getenv('DASHBOARD_PASSWORD'),
        'dashboard_port':     int(os.getenv('DASHBOARD_PORT', '8080')),

        # Bot behaviour
        'bot_mode':              os.getenv('BOT_MODE', 'shadow'),
        'scan_interval_secs':    int(os.getenv('SCAN_INTERVAL_SECS', '900')),  # 15 min
        'rank_interval_secs':    int(os.getenv('RANK_INTERVAL_SECS', '21600')),  # 6 hours
        'focus_size':            int(os.getenv('FOCUS_SIZE', '150')),

        # Paths
        'db_path':   str(BASE_DIR / 'data' / 'signals.db'),
        'log_path':  str(BASE_DIR / 'logs' / 'bot.log'),
        'base_dir':  str(BASE_DIR),
    }

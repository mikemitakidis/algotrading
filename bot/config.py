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

        # Sentiment (M8) — all optional, default off
        # Broker / execution (M10)
        'broker':              os.getenv('BROKER', 'paper'),
        'ibkr_host':           os.getenv('IBKR_HOST',    '127.0.0.1'),
        'ibkr_port':           int(os.getenv('IBKR_PORT', '4002')),
        'ibkr_account':        os.getenv('IBKR_ACCOUNT', 'DUP623346'),
        # M12 live trading safety config
        'ibkr_live_account':   os.getenv('IBKR_LIVE_ACCOUNT', '').strip(),
        'ibkr_live_port':      int(os.getenv('IBKR_LIVE_PORT', '4001')),
        'ibkr_live_confirmed': os.getenv('IBKR_LIVE_CONFIRMED', '').strip(),
        'risk_max_pos_pct':    float(os.getenv('RISK_MAX_POSITION_PCT', '2.0')),
        'risk_max_open':       int(os.getenv('RISK_MAX_OPEN_POSITIONS', '10')),
        'risk_portfolio_size': float(os.getenv('RISK_PORTFOLIO_SIZE', '100000')),
        'alpaca_key':          os.getenv('ALPACA_KEY', '').strip(),
        'alpaca_secret':       os.getenv('ALPACA_SECRET', '').strip(),
        'alphavantage_key':    os.getenv('ALPHAVANTAGE_KEY', '').strip(),
        'sentiment_mode':      os.getenv('SENTIMENT_MODE',     'off'),
        'sentiment_provider':  os.getenv('SENTIMENT_PROVIDER', 'disabled'),
        'sentiment_threshold': float(os.getenv('SENTIMENT_THRESHOLD', '0.1')),

        # Paths
        'db_path':   str(BASE_DIR / 'data' / 'signals.db'),
        'log_path':  str(BASE_DIR / 'logs' / 'bot.log'),
        'base_dir':  str(BASE_DIR),
    }

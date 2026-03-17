"""
bot/notifier.py
Sends Telegram alerts for generated signals.
Silently skips if token or chat_id not configured.
No DB access. No Flask.
"""
import logging
import requests

log = logging.getLogger(__name__)

TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'


def _format_alert(signal: dict) -> str:
    """Format a signal dict into a readable Telegram message."""
    sym       = signal['symbol']
    direction = signal['direction'].upper()
    route     = signal['route']
    count     = signal['valid_count']
    rsi       = signal.get('rsi', 0)
    price     = signal.get('price', 0)
    atr       = signal.get('atr', 0)

    # ATR-based stop loss and take profit
    if direction == 'LONG':
        sl = price - 2 * atr
        tp = price + 3 * atr
        arrow = '🟢'
    else:
        sl = price + 2 * atr
        tp = price - 3 * atr
        arrow = '🔴'

    tfs_valid = []
    if signal.get('tf_1d'):  tfs_valid.append('Daily')
    if signal.get('tf_4h'):  tfs_valid.append('4H')
    if signal.get('tf_1h'):  tfs_valid.append('1H')
    if signal.get('tf_15m'): tfs_valid.append('15m')

    label = '⭐ ETORO' if route == 'ETORO' else '📊 IBKR'

    return (
        f"{arrow} *{sym}* — {direction}\n"
        f"{label} | {count}/4 Timeframes\n"
        f"─────────────────\n"
        f"💰 Entry:  ${price:.2f}\n"
        f"🛑 SL:     ${sl:.2f}  (2×ATR)\n"
        f"🎯 TP:     ${tp:.2f}  (3×ATR)\n"
        f"─────────────────\n"
        f"RSI: {rsi:.1f} | ATR: {atr:.2f}\n"
        f"Valid TFs: {', '.join(tfs_valid)}\n"
        f"Mode: SHADOW (no real trade)"
    )


def send_alert(config: dict, signal: dict) -> bool:
    """
    Send a Telegram alert for a signal.
    Returns True if sent, False if skipped or failed.
    """
    token   = config.get('telegram_token', '').strip()
    chat_id = config.get('telegram_chat_id', '').strip()

    if not token or not chat_id:
        log.debug(f"[TELEGRAM] Skipped (token or chat_id not configured)")
        return False

    message = _format_alert(signal)
    url     = TELEGRAM_API.format(token=token)

    try:
        resp = requests.post(
            url,
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'Markdown'},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info(f"[TELEGRAM] Alert sent: {signal['symbol']} {signal['direction'].upper()}")
            return True
        else:
            log.warning(f"[TELEGRAM] Failed: HTTP {resp.status_code} — {resp.text[:100]}")
            return False
    except Exception as e:
        log.warning(f"[TELEGRAM] Error: {e}")
        return False

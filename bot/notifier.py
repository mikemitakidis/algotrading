"""
bot/notifier.py
Telegram alert module.

Alert types:
  startup, stopped, crash, signal, cycle_summary

Spam control:
  per (symbol, direction) cooldown tracked in memory

Failure handling:
  never raises — logs and returns False on any error
"""
import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_last_sent: dict = {}
TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'


def _is_enabled(config: dict) -> bool:
    return (
        config.get('telegram_enabled', False) and
        bool(config.get('telegram_token', '').strip()) and
        bool(config.get('telegram_chat_id', '').strip())
    )


def _send(config: dict, text: str) -> bool:
    token   = config['telegram_token'].strip()
    chat_id = config['telegram_chat_id'].strip()
    url     = TELEGRAM_API.format(token=token)
    try:
        resp = requests.post(
            url,
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info('[TELEGRAM] Sent: %s...', text[:60].strip().replace('\n', ' '))
            return True
        log.warning('[TELEGRAM] HTTP %s: %s', resp.status_code, resp.text[:120])
        return False
    except requests.exceptions.Timeout:
        log.warning('[TELEGRAM] Request timed out')
        return False
    except Exception as e:
        log.warning('[TELEGRAM] Error: %s', e)
        return False


def _in_cooldown(config: dict, symbol: str, direction: str) -> bool:
    cooldown = config.get('telegram_cooldown_secs', 14400)
    last     = _last_sent.get((symbol, direction), 0)
    return (time.time() - last) < cooldown


def _mark_sent(symbol: str, direction: str):
    _last_sent[(symbol, direction)] = time.time()


def alert_startup(config: dict) -> bool:
    if not _is_enabled(config):
        return False
    ts   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    text = (
        '&#x1F7E2; <b>Algo Trader v1 &#x2014; Started</b>\n'
        'Mode: %s\n'
        'Focus size: %d symbols\n'
        'Scan interval: %d min\n'
        'Time: %s'
    ) % (
        config.get('bot_mode', 'shadow').upper(),
        config.get('focus_size', 150),
        config.get('scan_interval_secs', 900) // 60,
        ts,
    )
    return _send(config, text)


def alert_stopped(config: dict, reason: str = 'Clean shutdown') -> bool:
    if not _is_enabled(config):
        return False
    ts   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    text = '&#x1F534; <b>Algo Trader v1 &#x2014; Stopped</b>\nReason: %s\nTime: %s' % (reason, ts)
    return _send(config, text)


def alert_crash(config: dict, error: str) -> bool:
    if not _is_enabled(config):
        return False
    ts   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    text = '&#x1F4A5; <b>Algo Trader v1 &#x2014; CRASH</b>\nError: %s\nTime: %s' % (error[:200], ts)
    log.warning('[TELEGRAM] Sending crash alert: %s', error[:80])
    return _send(config, text)


def alert_signal(config: dict, signal: dict) -> bool:
    if not _is_enabled(config):
        return False

    sym       = signal.get('symbol', '?')
    direction = signal.get('direction', '?')

    if _in_cooldown(config, sym, direction):
        log.debug('[TELEGRAM] Skipped %s %s -- in cooldown', sym, direction)
        return False

    route  = signal.get('route', 'SHADOW')
    count  = signal.get('valid_count', 0)
    price  = signal.get('price', 0.0)
    rsi    = signal.get('rsi', 0.0)
    atr    = signal.get('atr', 0.0)
    macd   = signal.get('macd_hist', 0.0)
    ema20  = signal.get('ema20', 0.0)
    ema50  = signal.get('ema50', 0.0)
    ts     = signal.get('timestamp', '')[:19].replace('T', ' ')

    tfs = []
    if signal.get('tf_1d'):  tfs.append('Daily')
    if signal.get('tf_4h'):  tfs.append('4H')
    if signal.get('tf_1h'):  tfs.append('1H')
    if signal.get('tf_15m'): tfs.append('15m')
    tf_str    = ' + '.join(tfs) if tfs else 'N/A'
    ema_trend = 'Uptrend' if ema20 > ema50 else 'Downtrend'

    if direction == 'long':
        sl, tp, arrow = price - 2*atr, price + 3*atr, '&#x1F7E2;'
    else:
        sl, tp, arrow = price + 2*atr, price - 3*atr, '&#x1F534;'

    route_label = {
        'ETORO': '&#x2B50; ETORO (4/4)',
        'IBKR':  '&#x1F4CA; IBKR (3/4)',
    }.get(route, route)

    text = (
        '%s <b>%s &#x2014; %s</b>\n'
        'Route: %s\n'
        '&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;\n'
        '&#x1F4B0; Entry: <b>$%.2f</b>\n'
        '&#x1F6D1; SL:    $%.2f  (2xATR)\n'
        '&#x1F3AF; TP:    $%.2f  (3xATR)\n'
        '&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;\n'
        'RSI:        %.1f\n'
        'MACD Hist:  %.4f\n'
        'EMA Trend:  %s\n'
        'ATR:        %.2f\n'
        '&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;&#x2500;\n'
        'TFs: %s (%d/4)\n'
        'Mode: SHADOW\n'
        'Time: %s UTC'
    ) % (
        arrow, sym, direction.upper(),
        route_label,
        price, sl, tp,
        rsi, macd, ema_trend, atr,
        tf_str, count, ts,
    )

    result = _send(config, text)
    if result:
        _mark_sent(sym, direction)
    return result


def alert_cycle_summary(config: dict, cycle: int,
                        signal_count: int, symbols_scanned: int) -> bool:
    if not _is_enabled(config):
        return False
    if signal_count == 0:
        return False
    ts   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    text = (
        '&#x1F4CB; <b>Cycle %d Summary</b>\n'
        'Symbols scanned: %d\n'
        'Signals found:   %d\n'
        'Time: %s'
    ) % (cycle, symbols_scanned, signal_count, ts)
    return _send(config, text)


def send_test(config: dict) -> bool:
    """Send a test message. Used by dashboard test button."""
    if not _is_enabled(config):
        log.warning('[TELEGRAM] Test requested but Telegram is not enabled/configured')
        return False
    ts   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    text = (
        '&#x2705; <b>Algo Trader &#x2014; Telegram Test</b>\n'
        'Configuration is working correctly.\n'
        'Time: %s'
    ) % ts
    result = _send(config, text)
    if result:
        log.info('[TELEGRAM] Test message sent successfully')
    else:
        log.warning('[TELEGRAM] Test message FAILED -- check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env')
    return result

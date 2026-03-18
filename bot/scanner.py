"""
bot/scanner.py
Tier A ranking and Tier B scan cycle.
Uses cached focus set from data.py to survive rate limits and restarts.
"""
import logging
from datetime import datetime, timezone

from bot.data       import fetch_bars, resample_to_4h, save_focus_cache
from bot.indicators import compute

log = logging.getLogger(__name__)

TIMEFRAMES = [
    ('1D',  '3mo',  '1d',  False),
    ('4H',  '1mo',  '1h',  True),
    ('1H',  '15d',  '1h',  False),
    ('15m', '5d',   '15m', False),
]


def score_timeframe(ind: dict, direction: str) -> int:
    if not ind:
        return 0
    if direction == 'long':
        m   = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0)          else 0
        t   = 1 if (ind['ema20'] > ind['ema50'] * 0.995)                     else 0
        vol = 1 if (ind['vwap_dev'] > -0.015 and ind['vol_ratio'] > 0.6)    else 0
    elif direction == 'short':
        m   = 1 if (ind['rsi'] > 50 and ind['macd_hist'] < 0)               else 0
        t   = 1 if (ind['ema20'] < ind['ema50'] * 1.005)                     else 0
        vol = 1 if (ind['vwap_dev'] < 0.015 and ind['vol_ratio'] > 0.6)     else 0
    else:
        return 0
    return 1 if (m + t + vol == 3) else 0


def rank_symbols(symbols: list, focus_size: int = 150) -> list:
    """
    Tier A: rank all symbols on daily bars, return top N by momentum score.
    Saves result to disk cache after successful ranking.
    Returns empty list if no data received (caller handles degraded mode).
    """
    log.info('[TIER-A] Ranking %d symbols (batch size=20, paced)...', len(symbols))
    bars = fetch_bars(symbols, '3mo', '1d')

    if not bars:
        log.warning('[TIER-A] No data received from yfinance. '
                    'Rate limited or network issue. Will retry next cycle.')
        return []

    scored = {}
    for sym, df in bars.items():
        ind = compute(df)
        if ind:
            scored[sym] = ind['pchg'] * 100 + (ind['vol_ratio'] - 1) * 5

    if not scored:
        log.warning('[TIER-A] Indicators computed for 0 symbols.')
        return []

    ranked = sorted(scored, key=scored.get, reverse=True)
    focus  = ranked[:focus_size]

    log.info('[TIER-A] Done. Bars: %d | Scored: %d | Focus: %d | Top 5: %s',
             len(bars), len(scored), len(focus), focus[:5])

    # Persist to disk — used by main loop on restart
    save_focus_cache(focus, {k: scored[k] for k in focus})
    return focus


def scan_cycle(focus: list, config: dict) -> list:
    """
    Tier B: run 4-timeframe scan on focus symbols.
    Returns list of signal dicts. Never crashes — returns [] on total failure.
    """
    log.info('[CYCLE] Scanning %d symbols across 4 timeframes (paced)...', len(focus))

    cached_inds:   dict = {}
    cached_scores: dict = {}
    tfs_completed = []

    for tf_label, period, interval, do_resample in TIMEFRAMES:
        log.info('[CYCLE] Fetching %s (%s, %s)...', tf_label, interval, period)
        bars = fetch_bars(focus, period, interval)

        if not bars:
            log.warning('[CYCLE] %s: no data — skipping this timeframe', tf_label)
            continue

        tfs_completed.append(tf_label)

        for sym, df in bars.items():
            if do_resample:
                df = resample_to_4h(df)
            ind = compute(df)
            if ind is None:
                continue

            cached_inds.setdefault(sym, {})[tf_label] = ind

            for direction in ('long', 'short'):
                if score_timeframe(ind, direction):
                    cached_scores.setdefault(sym, {'long': {}, 'short': {}})
                    cached_scores[sym][direction][tf_label] = 1

    log.info('[CYCLE] Timeframes completed: %s | Symbols with hits: %d',
             tfs_completed, len(cached_scores))

    if not tfs_completed:
        log.warning('[CYCLE] No timeframes returned data this cycle. '
                    'Possible sustained rate limit — will retry next cycle.')
        return []

    signals = []
    now_utc = datetime.now(timezone.utc).isoformat()

    for sym, dirs in cached_scores.items():
        for direction, tfs in dirs.items():
            count = sum(tfs.values())
            if count < 3:
                continue

            route = 'ETORO' if count == 4 else 'IBKR'
            best_ind = next(
                (cached_inds[sym][t] for t in ('1D', '4H', '1H', '15m')
                 if sym in cached_inds and t in cached_inds[sym]),
                None
            )
            if not best_ind:
                continue

            signal = {
                'timestamp':   now_utc,
                'symbol':      sym,
                'direction':   direction,
                'route':       route,
                'tf_15m':      tfs.get('15m', 0),
                'tf_1h':       tfs.get('1H',  0),
                'tf_4h':       tfs.get('4H',  0),
                'tf_1d':       tfs.get('1D',  0),
                'valid_count': count,
                **best_ind,
            }
            signals.append(signal)
            log.info('[SIGNAL] %s %s %s %d/4 TF | RSI:%.1f Price:$%.2f',
                     route, sym, direction.upper(), count,
                     best_ind['rsi'], best_ind['price'])

    log.info('[CYCLE] Complete. Signals: %d', len(signals))
    return signals

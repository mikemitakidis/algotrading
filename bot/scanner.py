"""
bot/scanner.py
4-timeframe scan cycle with partial-data support.

Key design: run signal scoring on whatever timeframes returned data.
Do not require all 4 TFs — partial data is better than no cycle.
Signal routing adjusts to available TF count:
  - 4/4 TFs valid -> ETORO
  - 3/4 TFs valid -> IBKR
  - 2/4 TFs valid -> IBKR (lower confidence, logged)
  - 1/4 TFs valid -> logged only, not inserted
"""
import logging
from datetime import datetime, timezone

from bot.data       import fetch_bars, resample_to_4h
from bot.indicators import compute

log = logging.getLogger(__name__)

TIMEFRAMES = [
    ('1D',  '3mo', '1d',  False),
    ('4H',  '1mo', '1h',  True),
    ('1H',  '15d', '1h',  False),
    ('15m', '5d',  '15m', False),
]


def score_timeframe(ind, direction):
    if not ind:
        return 0
    if direction == 'long':
        m   = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0)       else 0
        t   = 1 if (ind['ema20'] > ind['ema50'] * 0.995)                  else 0
        vol = 1 if (ind['vwap_dev'] > -0.015 and ind['vol_ratio'] > 0.6) else 0
    else:
        m   = 1 if (ind['rsi'] > 50 and ind['macd_hist'] < 0)            else 0
        t   = 1 if (ind['ema20'] < ind['ema50'] * 1.005)                  else 0
        vol = 1 if (ind['vwap_dev'] < 0.015 and ind['vol_ratio'] > 0.6)  else 0
    return 1 if (m + t + vol == 3) else 0


def scan_cycle(focus, config):
    """
    Run 4-TF scan on focus symbols.
    Works with partial data — uses whatever TFs returned data.
    Writes signals to DB for any symbol with >= 2 valid TFs.
    Returns list of signal dicts.
    """
    log.info('[CYCLE] Scanning %d symbols across 4 timeframes...', len(focus))

    cached_inds   = {}   # sym -> {tf -> ind}
    cached_scores = {}   # sym -> {direction -> {tf -> 1}}
    tfs_with_data = []

    for tf_label, period, interval, do_resample in TIMEFRAMES:
        log.info('[CYCLE] Fetching %s (%s)...', tf_label, interval)
        bars = fetch_bars(focus, period, interval)

        if not bars:
            log.warning('[CYCLE] %s: no data available (fresh or cached) — skipping', tf_label)
            continue

        tfs_with_data.append(tf_label)
        log.info('[CYCLE] %s: %d/%d symbols available', tf_label, len(bars), len(focus))

        for sym, df in bars.items():
            if do_resample:
                df = resample_to_4h(df)
            ind = compute(df)
            if ind is None:
                continue

            cached_inds.setdefault(sym, {})[tf_label] = ind

            for direction in ('long', 'short'):
                s = score_timeframe(ind, direction)
                if s:
                    cached_scores.setdefault(sym, {'long': {}, 'short': {}})
                    cached_scores[sym][direction][tf_label] = 1
                    log.debug('[SCORE] %s %s %s: PASS (RSI=%.1f MACD=%.4f EMA20/50=%.2f/%.2f vol=%.2f)',
                              sym, tf_label, direction, ind['rsi'], ind['macd_hist'],
                              ind['ema20'], ind['ema50'], ind['vol_ratio'])

    available_tfs = len(tfs_with_data)
    log.info('[CYCLE] Timeframes with data: %d/4 %s', available_tfs, tfs_with_data)
    log.info('[CYCLE] Symbols with >= 1 valid TF score: %d', len(cached_scores))

    if available_tfs == 0:
        log.warning('[CYCLE] No timeframes returned data. '
                    'Cache may be empty. Bot will retry next cycle.')
        return []

    # Minimum valid TFs required for a signal (scales with available data)
    # With 4 TFs available: need 3 (normal)
    # With 3 TFs available: need 2
    # With 2 TFs available: need 2
    # With 1 TF available:  need 1 (cache-only mode, logged separately)
    if available_tfs >= 3:
        min_valid = 3
    elif available_tfs == 2:
        min_valid = 2
    else:
        min_valid = 1

    log.info('[CYCLE] Signal threshold: %d/%d valid TFs required', min_valid, available_tfs)

    signals   = []
    now_utc   = datetime.now(timezone.utc).isoformat()

    for sym, dirs in cached_scores.items():
        for direction, tfs in dirs.items():
            count = sum(tfs.values())
            if count < min_valid:
                continue

            # Route based on count relative to AVAILABLE tfs
            if count >= 4:
                route = 'ETORO'
            elif count >= 3:
                route = 'IBKR'
            elif count >= 2:
                route = 'IBKR'
            else:
                route = 'WATCH'   # 1-TF only — logged but threshold may exclude

            if min_valid == 1 and count == 1:
                route = 'WATCH'

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
            log.info('[SIGNAL] %s %s %s %d/%d TF | RSI:%.1f Price:$%.2f | TFs:%s',
                     route, sym, direction.upper(), count, available_tfs,
                     best_ind['rsi'], best_ind['price'], list(tfs.keys()))

    log.info('[CYCLE] Complete. %d signals generated from %d/%d TFs.',
             len(signals), available_tfs, len(TIMEFRAMES))
    return signals

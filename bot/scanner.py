"""
bot/scanner.py
4-timeframe scan cycle with partial-data support.

All scoring thresholds come from bot/strategy.py (loaded from data/strategy.json).
This means dashboard edits to strategy parameters take effect after bot restart.

Signal routing (ETORO / IBKR / WATCH) is label-only in shadow mode.
Entry/stop/target are computed and logged for every signal (ML/backtesting use).
"""
import logging
from datetime import datetime, timezone

from bot.data       import fetch_bars, resample_to_4h
from bot.indicators import compute
from bot.strategy   import load as load_strategy

log = logging.getLogger(__name__)


def score_timeframe(ind: dict, direction: str, strategy: dict) -> int:
    """
    Score one timeframe for one direction.
    Returns 1 if ALL three conditions pass, 0 otherwise.

    Three conditions:
      momentum — RSI + MACD histogram
      trend    — EMA20 vs EMA50
      volume   — VWAP deviation + volume ratio
    """
    if not ind:
        return 0

    if direction == 'long':
        cfg      = strategy.get('long', {})
        rsi_min  = float(cfg.get('rsi_min',       30))
        rsi_max  = float(cfg.get('rsi_max',        75))
        macd_gt  = float(cfg.get('macd_hist_gt',  0.0))
        ema_tol  = float(cfg.get('ema_tolerance', 0.005))
        vwap_min = float(cfg.get('vwap_dev_min', -0.015))
        vol_min  = float(cfg.get('vol_ratio_min', 0.6))

        momentum = 1 if (rsi_min < ind['rsi'] < rsi_max and ind['macd_hist'] > macd_gt) else 0
        trend    = 1 if (ind['ema20'] > ind['ema50'] * (1.0 - ema_tol))                 else 0
        volume   = 1 if (ind['vwap_dev'] > vwap_min and ind['vol_ratio'] > vol_min)     else 0

    else:  # short
        cfg      = strategy.get('short', {})
        rsi_min  = float(cfg.get('rsi_min',        50))
        macd_lt  = float(cfg.get('macd_hist_lt',  0.0))
        ema_tol  = float(cfg.get('ema_tolerance', 0.005))
        vwap_max = float(cfg.get('vwap_dev_max',  0.015))
        vol_min  = float(cfg.get('vol_ratio_min',  0.6))

        momentum = 1 if (ind['rsi'] > rsi_min and ind['macd_hist'] < macd_lt)         else 0
        trend    = 1 if (ind['ema20'] < ind['ema50'] * (1.0 + ema_tol))               else 0
        volume   = 1 if (ind['vwap_dev'] < vwap_max and ind['vol_ratio'] > vol_min)   else 0

    return 1 if (momentum + trend + volume == 3) else 0


def _build_timeframes(strategy: dict) -> list:
    """
    Build the ordered list of timeframes to scan based on strategy config.
    Returns list of (label, period, interval, do_resample) tuples.
    Only includes enabled timeframes.
    """
    order = [
        ('tf_1d',  '1D'),
        ('tf_4h',  '4H'),
        ('tf_1h',  '1H'),
        ('tf_15m', '15m'),
    ]
    tf_cfg = strategy.get('timeframes', {})
    result = []
    for tf_key, tf_label in order:
        cfg = tf_cfg.get(tf_key, {})
        if cfg.get('enabled', True):
            result.append((
                tf_label,
                cfg.get('period',   '3mo'),
                cfg.get('interval', '1d'),
                cfg.get('resample', False),
            ))
    return result


def scan_cycle(focus: list, config: dict):
    """
    Run scan on focus symbols across all enabled timeframes.
    Strategy thresholds are loaded fresh from data/strategy.json on every call.

    Returns (signals, meta) tuple.
    signals: list of signal dicts ready for DB insert
    meta:    dict with cycle statistics for the state file
    """
    strategy   = load_strategy()
    timeframes = _build_timeframes(strategy)
    confluence = strategy.get('confluence', {})
    risk_cfg   = strategy.get('risk', {})
    routing    = strategy.get('routing', {})

    atr_stop   = float(risk_cfg.get('atr_stop_mult',   2.0))
    atr_target = float(risk_cfg.get('atr_target_mult', 3.0))
    etoro_min  = int(routing.get('etoro_min_tfs',  4))
    ibkr_min   = int(routing.get('ibkr_min_tfs',   2))
    strat_ver  = int(strategy.get('version', 1))

    log.info('[CYCLE] Scanning %d symbols | strategy v%d | %d TFs enabled',
             len(focus), strat_ver, len(timeframes))

    cached_inds   = {}   # sym -> {tf_label -> ind}
    cached_scores = {}   # sym -> {direction -> {tf_label -> 1}}
    tfs_with_data = []

    for tf_label, period, interval, do_resample in timeframes:
        log.info('[CYCLE] Fetching %s (%s)...', tf_label, interval)
        bars = fetch_bars(focus, period, interval)

        if not bars:
            log.warning('[CYCLE] %s: no data — skipping', tf_label)
            continue

        tfs_with_data.append(tf_label)
        log.info('[CYCLE] %s: %d/%d symbols', tf_label, len(bars), len(focus))

        for sym, df in bars.items():
            if do_resample:
                df = resample_to_4h(df)
            ind = compute(df)
            if ind is None:
                continue

            cached_inds.setdefault(sym, {})[tf_label] = ind

            for direction in ('long', 'short'):
                if score_timeframe(ind, direction, strategy):
                    cached_scores.setdefault(sym, {'long': {}, 'short': {}})
                    cached_scores[sym][direction][tf_label] = 1

    available_tfs = len(tfs_with_data)
    log.info('[CYCLE] TFs with data: %d/%d %s', available_tfs, len(timeframes), tfs_with_data)
    log.info('[CYCLE] Symbols with >=1 valid TF score: %d', len(cached_scores))

    if available_tfs == 0:
        log.warning('[CYCLE] No TF data. Cache may be empty — retrying next cycle.')
        return [], {'tfs_available': 0, 'tfs_list': [], 'symbols_scanned': len(focus)}

    # Minimum valid TFs scales with available data, but respects confluence setting
    cfg_min = int(confluence.get('min_valid_tfs', 3))
    if available_tfs >= len(timeframes):
        min_valid = cfg_min
    elif available_tfs >= 2:
        min_valid = max(2, cfg_min - 1)
    else:
        min_valid = 1

    log.info('[CYCLE] Signal threshold: %d/%d valid TFs (config=%d)',
             min_valid, available_tfs, cfg_min)

    signals  = []
    now_utc  = datetime.now(timezone.utc).isoformat()

    for sym, dirs in cached_scores.items():
        for direction, tfs in dirs.items():
            count = sum(tfs.values())
            if count < min_valid:
                continue

            # Route label (shadow mode — no execution)
            if count >= etoro_min:
                route = 'ETORO'
            elif count >= ibkr_min:
                route = 'IBKR'
            else:
                route = 'WATCH'

            # Use best available indicator set (prefer higher TFs)
            best_ind = next(
                (cached_inds[sym][t] for t in ('1D', '4H', '1H', '15m')
                 if sym in cached_inds and t in cached_inds[sym]),
                None
            )
            if not best_ind:
                continue

            # Compute risk levels from ATR
            entry  = best_ind['price']
            atr    = best_ind['atr']
            if direction == 'long':
                stop_loss    = round(entry - atr_stop   * atr, 4)
                target_price = round(entry + atr_target * atr, 4)
            else:
                stop_loss    = round(entry + atr_stop   * atr, 4)
                target_price = round(entry - atr_target * atr, 4)

            signal = {
                'timestamp':        now_utc,
                'symbol':           sym,
                'direction':        direction,
                'route':            route,
                'tf_15m':           tfs.get('15m', 0),
                'tf_1h':            tfs.get('1H',  0),
                'tf_4h':            tfs.get('4H',  0),
                'tf_1d':            tfs.get('1D',  0),
                'valid_count':      count,
                'entry_price':      round(entry, 4),
                'stop_loss':        stop_loss,
                'target_price':     target_price,
                'strategy_version': strat_ver,
                **best_ind,
            }
            signals.append(signal)
            log.info('[SIGNAL] %s %s %s %d/%d TF | RSI:%.1f Price:$%.2f SL:$%.2f TP:$%.2f | TFs:%s',
                     route, sym, direction.upper(), count, available_tfs,
                     best_ind['rsi'], entry, stop_loss, target_price,
                     list(tfs.keys()))

    log.info('[CYCLE] Complete. %d signals from %d/%d TFs.',
             len(signals), available_tfs, len(timeframes))

    meta = {
        'tfs_available':  available_tfs,
        'tfs_list':       tfs_with_data,
        'symbols_scanned': len(focus),
    }
    return signals, meta

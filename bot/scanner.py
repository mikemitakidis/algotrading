"""
bot/scanner.py
Categorical scoring, Tier A ranking, Tier B scan cycle.
Imports from data.py and indicators.py only.
No DB writes. No alerts. No Flask.
"""
import logging
from datetime import datetime, timezone

from bot.data import fetch_bars, resample_to_4h
from bot.indicators import compute

log = logging.getLogger(__name__)

# Timeframe definitions: (label, period, interval, resample_to_4h)
TIMEFRAMES = [
    ('1D',  '3mo',  '1d',   False),
    ('4H',  '1mo',  '1h',   True),   # fetch 1H, resample to 4H
    ('1H',  '15d',  '1h',   False),
    ('15m', '5d',   '15m',  False),
]


def score_timeframe(ind: dict, direction: str) -> int:
    """
    Score a single timeframe for a given direction.
    Returns 1 only if ALL 3 categories pass.
    Returns 0 if any category fails or ind is None.

    Categories:
    - Momentum: RSI range + MACD histogram direction
    - Trend:    EMA20 vs EMA50 alignment
    - Volume:   VWAP deviation + volume ratio
    """
    if not ind:
        return 0

    if direction == 'long':
        momentum = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0) else 0
        trend    = 1 if (ind['ema20'] > ind['ema50'] * 0.995)            else 0
        volume   = 1 if (ind['vwap_dev'] > -0.015 and ind['vol_ratio'] > 0.6) else 0
    elif direction == 'short':
        momentum = 1 if (ind['rsi'] > 50 and ind['macd_hist'] < 0)      else 0
        trend    = 1 if (ind['ema20'] < ind['ema50'] * 1.005)            else 0
        volume   = 1 if (ind['vwap_dev'] < 0.015 and ind['vol_ratio'] > 0.6) else 0
    else:
        return 0

    return 1 if (momentum + trend + volume == 3) else 0


def rank_symbols(symbols: list, focus_size: int = 150) -> list:
    """
    Tier A: fetch daily bars for all symbols, score by momentum, return top N.
    """
    log.info(f"[TIER-A] Ranking {len(symbols)} symbols on daily bars...")
    bars = fetch_bars(symbols, '3mo', '1d')
    log.info(f"[TIER-A] Bars received for {len(bars)} symbols")

    scored = {}
    for sym, df in bars.items():
        ind = compute(df)
        if ind:
            scored[sym] = ind['pchg'] * 100 + (ind['vol_ratio'] - 1) * 5

    ranked = sorted(scored, key=scored.get, reverse=True)
    focus  = ranked[:focus_size]

    log.info(f"[TIER-A] Scored: {len(scored)}. Focus set: {len(focus)}. Top 5: {focus[:5]}")
    return focus


def scan_cycle(focus: list, config: dict) -> list:
    """
    Tier B: full 4-timeframe scan on focus symbols.

    Returns list of signal dicts ready for DB insert and Telegram alert.
    Each signal dict contains all indicator values + routing info.
    """
    log.info(f"[CYCLE] Scanning {len(focus)} symbols across 4 timeframes...")

    # Cache: sym -> {tf_label -> indicator dict}
    cached_inds: dict   = {}
    # Cache: sym -> {direction -> {tf_label -> 1}}
    cached_scores: dict = {}

    for tf_label, period, interval, do_resample in TIMEFRAMES:
        log.info(f"[CYCLE] Fetching {tf_label} ({interval}, {period})...")
        bars = fetch_bars(focus, period, interval)
        log.info(f"[CYCLE] {tf_label}: got {len(bars)}/{len(focus)} symbols")

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

    log.info(f"[CYCLE] Symbols with ≥1 valid TF score: {len(cached_scores)}")

    # Evaluate confluences
    signals = []
    now_utc = datetime.now(timezone.utc).isoformat()

    for sym, dirs in cached_scores.items():
        for direction, tfs in dirs.items():
            count = sum(tfs.values())
            if count < 3:
                continue

            route = 'ETORO' if count == 4 else 'IBKR'

            # Use best available indicator set (prefer longer timeframe)
            best_ind = next(
                (cached_inds[sym][t] for t in ('1D', '4H', '1H', '15m')
                 if sym in cached_inds and t in cached_inds[sym]),
                None
            )
            if not best_ind:
                continue

            signal = {
                'timestamp': now_utc,
                'symbol':    sym,
                'direction': direction,
                'route':     route,
                'tf_15m':    tfs.get('15m', 0),
                'tf_1h':     tfs.get('1H',  0),
                'tf_4h':     tfs.get('4H',  0),
                'tf_1d':     tfs.get('1D',  0),
                'valid_count': count,
                **best_ind,
            }
            signals.append(signal)
            log.info(
                f"[SIGNAL] {route} {sym} {direction.upper()} {count}/4 TF | "
                f"RSI:{best_ind['rsi']:.1f} Price:${best_ind['price']:.2f} | "
                f"TFs:{list(tfs.keys())}"
            )

    log.info(f"[CYCLE] Done. Signals generated: {len(signals)}")
    return signals

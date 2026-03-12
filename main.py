#!/usr/bin/env python3
"""
Algo Trader v2 - Main Engine
Multi-timeframe confluence strategy
Markets: US Stocks (1,701 assets)
Mode: SHADOW (no real trades)
"""

import time
import logging
import sqlite3
import yaml
import os
import sys
from datetime import datetime, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
import pandas as pd
import numpy as np

# ── Logging ──────────────────────────────────────────────────────────────────
os.makedirs('/opt/algo-trader/logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/opt/algo-trader/logs/bot.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    with open('/opt/algo-trader/config/settings.yaml') as f:
        return yaml.safe_load(f)

# ── Asset Universe ─────────────────────────────────────────────────────────────
def get_assets():
    sys.path.insert(0, '/opt/algo-trader/config')
    from assets import ASSET_UNIVERSE
    return ASSET_UNIVERSE

# ── Data Client ───────────────────────────────────────────────────────────────
def get_client(cfg):
    return StockHistoricalDataClient(
        api_key=cfg['alpaca']['api_key'],
        secret_key=cfg['alpaca']['secret_key']
    )

# ── Fetch Bars ─────────────────────────────────────────────────────────────────
def fetch_bars(client, symbols, timeframe, limit=100):
    """Fetch bars in batches of 100 to respect rate limits."""
    all_bars = {}
    tf_map = {
        '15m': TimeFrame(15, TimeFrameUnit.Minute),
        '1H':  TimeFrame(1,  TimeFrameUnit.Hour),
        '4H':  TimeFrame(4,  TimeFrameUnit.Hour),
        '1D':  TimeFrame(1,  TimeFrameUnit.Day),
    }
    tf = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))
    end = datetime.utcnow()
    start = end - timedelta(days=60)

    for i in range(0, len(symbols), 100):
        batch = symbols[i:i+100]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=tf,
                start=start,
                end=end,
                feed='iex',
                limit=limit
            )
            bars = client.get_stock_bars(req).df
            if not bars.empty:
                for sym in batch:
                    if sym in bars.index.get_level_values(0):
                        all_bars[sym] = bars.loc[sym].copy()
        except Exception as e:
            log.warning(f"Batch {i//100+1} error: {e}")
        time.sleep(0.35)  # Rate limit
    return all_bars

# ── Indicators ────────────────────────────────────────────────────────────────
def compute_indicators(df):
    """Compute all technical indicators."""
    if len(df) < 20:
        return None
    c = df['close'].astype(float)
    v = df['volume'].astype(float)

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))

    # MACD
    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9).mean()

    # EMAs
    ema20 = c.ewm(span=20).mean()
    ema50 = c.ewm(span=50).mean() if len(c) >= 50 else ema20

    # Bollinger Bands
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_width = (bb_upper - bb_lower) / (sma20 + 1e-9)

    # VWAP deviation
    vwap = (c * v).cumsum() / (v.cumsum() + 1e-9)
    vwap_dev = (c - vwap) / (vwap + 1e-9)

    # OBV
    obv = (np.sign(c.diff()) * v).cumsum()

    # ATR
    h = df['high'].astype(float)
    l = df['low'].astype(float)
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    # Volume ratio
    vol_ratio = v / (v.rolling(20).mean() + 1e-9)

    return {
        'rsi': rsi.iloc[-1],
        'macd_hist': macd_hist.iloc[-1],
        'ema20': ema20.iloc[-1],
        'ema50': ema50.iloc[-1],
        'bb_width': bb_width.iloc[-1],
        'bb_pos': (c.iloc[-1] - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1] + 1e-9),
        'vwap_dev': vwap_dev.iloc[-1],
        'obv_slope': (obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9) if len(obv) > 5 else 0,
        'atr': atr.iloc[-1],
        'vol_ratio': vol_ratio.iloc[-1],
        'price': c.iloc[-1],
        'price_change_20': (c.iloc[-1] - c.iloc[-20]) / (c.iloc[-20] + 1e-9) if len(c) >= 20 else 0,
    }

# ── Categorical Scoring ────────────────────────────────────────────────────────
def score_timeframe(ind, direction='long'):
    """Score a timeframe: returns 1 only if ALL 3 categories are bullish/bearish."""
    if ind is None:
        return 0, {}

    if direction == 'long':
        momentum = 1 if (40 < ind['rsi'] < 70 and ind['macd_hist'] > 0) else 0
        trend = 1 if (ind['ema20'] > ind['ema50'] and ind['bb_pos'] > 0.5) else 0
        volume = 1 if (ind['vwap_dev'] > 0 and ind['obv_slope'] > 0 and ind['vol_ratio'] > 1.0) else 0
    else:  # short
        momentum = 1 if (ind['rsi'] > 60 and ind['macd_hist'] < 0) else 0
        trend = 1 if (ind['ema20'] < ind['ema50'] and ind['bb_pos'] < 0.5) else 0
        volume = 1 if (ind['vwap_dev'] < 0 and ind['obv_slope'] < 0 and ind['vol_ratio'] > 1.0) else 0

    valid = 1 if (momentum + trend + volume == 3) else 0
    return valid, {'momentum': momentum, 'trend': trend, 'volume': volume}

# ── ML Logging ────────────────────────────────────────────────────────────────
def init_db():
    db = sqlite3.connect('/opt/algo-trader/data/signals.db')
    db.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, direction TEXT,
        tf_15m INTEGER, tf_1h INTEGER, tf_4h INTEGER, tf_1d INTEGER,
        valid_count INTEGER, route TEXT,
        rsi_15m REAL, macd_15m REAL, rsi_1h REAL, macd_1h REAL,
        rsi_4h REAL, macd_4h REAL, rsi_1d REAL, macd_1d REAL,
        vwap_dev REAL, obv_slope REAL, vol_ratio REAL,
        atr REAL, price REAL, bb_width REAL
    )''')
    db.commit()
    return db

def log_signal(db, sym, direction, scores, inds, route):
    ind = inds.get('1D') or inds.get('1H') or {}
    if not ind:
        return
    db.execute('''INSERT INTO signals VALUES (
        NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
    )''', (
        datetime.utcnow().isoformat(), sym, direction,
        scores.get('15m', 0), scores.get('1H', 0),
        scores.get('4H', 0), scores.get('1D', 0),
        sum(scores.values()), route,
        inds.get('15m', {}).get('rsi'), inds.get('15m', {}).get('macd_hist'),
        inds.get('1H', {}).get('rsi'), inds.get('1H', {}).get('macd_hist'),
        inds.get('4H', {}).get('rsi'), inds.get('4H', {}).get('macd_hist'),
        inds.get('1D', {}).get('rsi'), inds.get('1D', {}).get('macd_hist'),
        ind.get('vwap_dev'), ind.get('obv_slope'), ind.get('vol_ratio'),
        ind.get('atr'), ind.get('price'), ind.get('bb_width')
    ))
    db.commit()

# ── Tier A: Rank All US Symbols ────────────────────────────────────────────────
def rank_symbols(client, symbols):
    """Fetch daily bars for all symbols, rank by momentum, return top 150."""
    log.info(f"Tier A: Ranking {len(symbols)} symbols...")
    bars = fetch_bars(client, symbols, '1D', limit=30)
    scores = {}
    for sym, df in bars.items():
        try:
            ind = compute_indicators(df)
            if ind:
                # Momentum score: price change + RSI proximity to 55
                score = ind['price_change_20'] * 100 + abs(ind['rsi'] - 55) * -0.1
                scores[sym] = score
        except Exception:
            pass
    ranked = sorted(scores, key=scores.get, reverse=True)
    top = ranked[:150]
    log.info(f"Tier A complete. Focus set: {len(top)} symbols")
    return top

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("ALGO TRADER v2 STARTING - SHADOW MODE")
    log.info("=" * 60)

    cfg = load_config()
    client = get_client(cfg)
    assets = get_assets()
    db = init_db()

    # US stocks only for Tier A (Alpaca supports US equities)
    us_symbols = [a for a in assets if '.' not in a and '-' not in a]
    log.info(f"Total US symbols: {len(us_symbols)}")

    focus_symbols = []
    last_rank_time = datetime.utcnow() - timedelta(hours=7)  # Force rank on start
    cycle = 0

    while True:
        try:
            cycle += 1
            now = datetime.utcnow()

            # Tier A: Re-rank every 6 hours
            if (now - last_rank_time).total_seconds() > 21600:
                focus_symbols = rank_symbols(client, us_symbols)
                last_rank_time = now

            if not focus_symbols:
                log.warning("No focus symbols yet, waiting...")
                time.sleep(60)
                continue

            log.info(f"Cycle {cycle}: Analyzing {len(focus_symbols)} focus symbols across 4 timeframes...")

            # Tier B: Full multi-timeframe analysis
            signal_count = 0
            for tf_name in ['15m', '1H', '4H', '1D']:
                bars = fetch_bars(client, focus_symbols, tf_name, limit=100)

                for sym in focus_symbols:
                    if sym not in bars:
                        continue
                    try:
                        ind = compute_indicators(bars[sym])
                        if ind is None:
                            continue

                        for direction in ['long', 'short']:
                            valid, detail = score_timeframe(ind, direction)
                            if valid:
                                if sym not in _tf_scores:
                                    _tf_scores[sym] = {'long': {}, 'short': {}}
                                _tf_scores[sym][direction][tf_name] = 1
                    except Exception as e:
                        log.debug(f"{sym}/{tf_name}: {e}")

            # Evaluate signals
            for sym, dirs in _tf_scores.items():
                for direction, tfs in dirs.items():
                    count = sum(tfs.values())
                    if count >= 3:
                        route = 'ETORO' if count == 4 else 'IBKR'
                        log.info(f"SIGNAL [{route}] {sym} {direction.upper()} — {count}/4 TF confluence")
                        log_signal(db, sym, direction, tfs, {}, route)
                        signal_count += 1

            _tf_scores.clear()
            log.info(f"Cycle {cycle} complete. Signals found: {signal_count}. Sleeping 15 min...")
            time.sleep(900)  # 15 minutes between cycles

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}", exc_info=True)
            time.sleep(60)

_tf_scores = {}

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Algo Trader v2 - Main Engine
Multi-timeframe confluence strategy
"""
import time, logging, sqlite3, yaml, os, sys
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

os.makedirs('/opt/algo-trader/logs', exist_ok=True)
os.makedirs('/opt/algo-trader/data', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/opt/algo-trader/logs/bot.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def load_config():
    with open('/opt/algo-trader/config/settings.yaml') as f:
        return yaml.safe_load(f)

def get_assets():
    sys.path.insert(0, '/opt/algo-trader/config')
    from assets import ASSET_UNIVERSE
    return ASSET_UNIVERSE

def get_client(cfg):
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        api_key=cfg['alpaca']['api_key'],
        secret_key=cfg['alpaca']['secret_key']
    )

def fetch_bars(client, symbols, timeframe, limit=60):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    all_bars = {}
    tf_map = {
        '15m': TimeFrame(15, TimeFrameUnit.Minute),
        '1H':  TimeFrame(1,  TimeFrameUnit.Hour),
        '4H':  TimeFrame(4,  TimeFrameUnit.Hour),
        '1D':  TimeFrame(1,  TimeFrameUnit.Day),
    }
    tf = tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))
    end = datetime.utcnow() - timedelta(minutes=15)  # 15 min delay for free tier
    start = end - timedelta(days=30)

    for i in range(0, len(symbols), 100):
        batch = symbols[i:i+100]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=tf,
                start=start,
                end=end,
                limit=limit
                # No feed param - let Alpaca use account default
            )
            bars = client.get_stock_bars(req).df
            if not bars.empty:
                if hasattr(bars.index, 'levels'):
                    for sym in batch:
                        try:
                            sym_data = bars.loc[sym]
                            if len(sym_data) >= 10:
                                all_bars[sym] = sym_data.copy()
                        except KeyError:
                            pass
                else:
                    pass
        except Exception as e:
            log.warning(f"Batch {i//100+1} error: {str(e)[:80]}")
        time.sleep(0.4)
    return all_bars

def compute_indicators(df):
    if len(df) < 15:
        return None
    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)

        # RSI
        delta = c.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / (loss + 1e-9)))

        # MACD
        ema12 = c.ewm(span=12).mean()
        ema26 = c.ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        macd_hist = macd_line - signal_line

        # EMAs
        ema20 = c.ewm(span=20).mean()
        ema50 = c.ewm(span=min(50, len(c)-1)).mean()

        # Bollinger Bands
        sma20 = c.rolling(min(20, len(c))).mean()
        std20 = c.rolling(min(20, len(c))).std()
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20

        # VWAP deviation
        vwap = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = (c - vwap) / (vwap + 1e-9)

        # OBV slope
        obv = (np.sign(c.diff()) * v).cumsum()
        obv_slope = (obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9) if len(obv) > 5 else 0

        # ATR
        h = df['high'].astype(float)
        l = df['low'].astype(float)
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()

        vol_ratio = v / (v.rolling(min(20, len(v))).mean() + 1e-9)

        bb_range = (bb_upper.iloc[-1] - bb_lower.iloc[-1])
        bb_pos = (c.iloc[-1] - bb_lower.iloc[-1]) / (bb_range + 1e-9) if bb_range > 0 else 0.5

        return {
            'rsi': float(rsi.iloc[-1]),
            'macd_hist': float(macd_hist.iloc[-1]),
            'ema20': float(ema20.iloc[-1]),
            'ema50': float(ema50.iloc[-1]),
            'bb_pos': float(bb_pos),
            'bb_width': float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / (sma20.iloc[-1] + 1e-9)),
            'vwap_dev': float(vwap_dev.iloc[-1]),
            'obv_slope': float(obv_slope),
            'atr': float(atr.iloc[-1]),
            'vol_ratio': float(vol_ratio.iloc[-1]),
            'price': float(c.iloc[-1]),
            'price_change_20': float((c.iloc[-1] - c.iloc[-min(20,len(c))]) / (c.iloc[-min(20,len(c))] + 1e-9)),
        }
    except Exception as e:
        log.debug(f"Indicator error: {e}")
        return None

def score_timeframe(ind, direction='long'):
    if ind is None:
        return 0
    try:
        if direction == 'long':
            momentum = 1 if (35 < ind['rsi'] < 72 and ind['macd_hist'] > 0) else 0
            trend    = 1 if (ind['ema20'] > ind['ema50'] and ind['bb_pos'] > 0.45) else 0
            volume   = 1 if (ind['vwap_dev'] > -0.01 and ind['obv_slope'] > 0 and ind['vol_ratio'] > 0.8) else 0
        else:
            momentum = 1 if (ind['rsi'] > 55 and ind['macd_hist'] < 0) else 0
            trend    = 1 if (ind['ema20'] < ind['ema50'] and ind['bb_pos'] < 0.55) else 0
            volume   = 1 if (ind['vwap_dev'] < 0.01 and ind['obv_slope'] < 0 and ind['vol_ratio'] > 0.8) else 0
        return 1 if (momentum + trend + volume == 3) else 0
    except:
        return 0

def init_db():
    db = sqlite3.connect('/opt/algo-trader/data/signals.db')
    db.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, direction TEXT,
        tf_15m INTEGER, tf_1h INTEGER, tf_4h INTEGER, tf_1d INTEGER,
        valid_count INTEGER, route TEXT,
        rsi REAL, macd REAL, vwap_dev REAL, obv_slope REAL,
        vol_ratio REAL, atr REAL, price REAL, bb_width REAL
    )''')
    db.commit()
    return db

def log_signal(db, sym, direction, scores, ind, route):
    try:
        db.execute('INSERT INTO signals VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            datetime.utcnow().isoformat(), sym, direction,
            scores.get('15m',0), scores.get('1H',0),
            scores.get('4H',0), scores.get('1D',0),
            sum(scores.values()), route,
            ind.get('rsi'), ind.get('macd_hist'), ind.get('vwap_dev'),
            ind.get('obv_slope'), ind.get('vol_ratio'), ind.get('atr'),
            ind.get('price'), ind.get('bb_width')
        ))
        db.commit()
    except Exception as e:
        log.warning(f"DB log error: {e}")

def rank_symbols(client, symbols):
    log.info(f"Tier A: Ranking {len(symbols)} symbols on daily bars...")
    bars = fetch_bars(client, symbols, '1D', limit=25)
    scores = {}
    for sym, df in bars.items():
        try:
            ind = compute_indicators(df)
            if ind and not any(map(lambda x: x != x, ind.values())):  # no NaN
                score = ind['price_change_20'] * 100 + (ind['vol_ratio'] - 1) * 10
                scores[sym] = score
        except:
            pass
    ranked = sorted(scores, key=scores.get, reverse=True)
    top = ranked[:150]
    log.info(f"Tier A complete. Scored: {len(scores)} symbols. Focus set: {len(top)}")
    return top

_tf_scores = {}

def main():
    log.info("=" * 60)
    log.info("ALGO TRADER v2 STARTING - SHADOW MODE")
    log.info("=" * 60)

    cfg = load_config()
    client = get_client(cfg)
    assets = get_assets()
    db = init_db()

    # US stocks only (no dots = US equity)
    us_symbols = [a for a in assets if '.' not in a and len(a) <= 5]
    log.info(f"Total US symbols loaded: {len(us_symbols)}")

    focus_symbols = []
    last_rank_time = datetime.utcnow() - timedelta(hours=7)
    cycle = 0

    while True:
        try:
            cycle += 1
            now = datetime.utcnow()

            # Re-rank every 6 hours
            if (now - last_rank_time).total_seconds() > 21600:
                focus_symbols = rank_symbols(client, us_symbols)
                last_rank_time = now

            if not focus_symbols:
                log.warning("Focus set empty — retrying rank in 5 min...")
                time.sleep(300)
                last_rank_time = datetime.utcnow() - timedelta(hours=7)
                continue

            log.info(f"Cycle {cycle}: Analyzing {len(focus_symbols)} symbols across 4 timeframes...")
            signal_count = 0
            _tf_scores.clear()

            for tf_name in ['1D', '4H', '1H', '15m']:
                log.info(f"  Fetching {tf_name} bars...")
                bars = fetch_bars(client, focus_symbols, tf_name)
                log.info(f"  Got data for {len(bars)} symbols on {tf_name}")

                for sym, df in bars.items():
                    ind = compute_indicators(df)
                    if ind is None:
                        continue
                    for direction in ['long', 'short']:
                        valid = score_timeframe(ind, direction)
                        if valid:
                            if sym not in _tf_scores:
                                _tf_scores[sym] = {'long': {}, 'short': {}, 'ind': ind}
                            _tf_scores[sym][direction][tf_name] = 1

            # Evaluate confluences
            for sym, data in _tf_scores.items():
                for direction in ['long', 'short']:
                    tfs = data[direction]
                    count = sum(tfs.values())
                    if count >= 3:
                        route = 'ETORO' if count == 4 else 'IBKR'
                        ind = data.get('ind', {})
                        log.info(f"  *** SIGNAL [{route}] {sym} {direction.upper()} | {count}/4 TF | RSI:{ind.get('rsi',0):.1f} Price:{ind.get('price',0):.2f}")
                        log_signal(db, sym, direction, tfs, ind, route)
                        signal_count += 1

            log.info(f"Cycle {cycle} done. Signals: {signal_count}. Next cycle in 15 min.")
            time.sleep(900)

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            time.sleep(120)

if __name__ == '__main__':
    main()

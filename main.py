#!/usr/bin/env python3
"""
Algo Trader v2 — Fully Fixed & Diagnosed
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

LOOKBACK = {'15m': 5, '1H': 15, '4H': 30, '1D': 90}

def load_config():
    with open('/opt/algo-trader/config/settings.yaml') as f:
        return yaml.safe_load(f)

def get_assets():
    sys.path.insert(0, '/opt/algo-trader/config')
    from assets import ASSET_UNIVERSE
    return ASSET_UNIVERSE

def get_client(cfg):
    from alpaca.data.historical import StockHistoricalDataClient
    api_key    = cfg['alpaca']['api_key']
    secret_key = cfg['alpaca']['secret_key']
    log.info(f"Alpaca API key loaded: {api_key[:8]}... secret: {'SET' if secret_key else 'MISSING'}")
    if not secret_key:
        raise ValueError("Alpaca secret_key is missing from settings.yaml!")
    return StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

def fetch_bars(client, symbols, timeframe):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf_map = {
        '15m': TimeFrame(15, TimeFrameUnit.Minute),
        '1H':  TimeFrame(1,  TimeFrameUnit.Hour),
        '4H':  TimeFrame(4,  TimeFrameUnit.Hour),
        '1D':  TimeFrame(1,  TimeFrameUnit.Day),
    }
    tf    = tf_map[timeframe]
    days  = LOOKBACK[timeframe]
    end   = datetime.utcnow() - timedelta(minutes=20)
    start = end - timedelta(days=days)

    all_bars = {}
    batch_size = 50
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        batch_num = i // batch_size + 1
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=tf,
                start=start,
                end=end,
                feed='iex'   # IEX = free tier. Never use 'sip' (paid only).
            )
            raw = client.get_stock_bars(req)
            df  = raw.df

            if df is None or df.empty:
                log.info(f"  Batch {batch_num}/{total_batches} ({timeframe}): empty response")
                time.sleep(0.4)
                continue

            log.info(f"  Batch {batch_num}/{total_batches} ({timeframe}): got {len(df)} rows, index type: {type(df.index).__name__}")

            if isinstance(df.index, pd.MultiIndex):
                for sym in batch:
                    try:
                        s = df.loc[sym].copy()
                        if len(s) >= 10:
                            all_bars[sym] = s
                    except KeyError:
                        pass
            else:
                # Single-symbol response — index is just timestamps
                if len(df) >= 10 and len(batch) == 1:
                    all_bars[batch[0]] = df.copy()

        except Exception as e:
            log.warning(f"  Batch {batch_num}/{total_batches} ({timeframe}) ERROR: {str(e)[:150]}")
        time.sleep(0.4)

    return all_bars

def compute_indicators(df):
    if len(df) < 14:
        return None
    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)
        h = df['high'].astype(float)
        l = df['low'].astype(float)

        delta = c.diff()
        up    = delta.clip(lower=0).rolling(14).mean()
        dn    = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - (100 / (1 + up / (dn + 1e-9)))

        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()

        n     = min(20, len(c))
        sma   = c.rolling(n).mean()
        std   = c.rolling(n).std()
        bb_up = sma + 2 * std
        bb_lo = sma - 2 * std
        bb_rng = float(bb_up.iloc[-1] - bb_lo.iloc[-1])
        bb_pos = float((c.iloc[-1] - bb_lo.iloc[-1]) / (bb_rng + 1e-9)) if bb_rng > 0 else 0.5

        vwap     = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = float((c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9))

        obv       = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)) if len(obv) > 5 else 0.0

        tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        vol_ma    = v.rolling(min(20, len(v))).mean()
        vol_ratio = float(v.iloc[-1] / (vol_ma.iloc[-1] + 1e-9))

        lb        = min(20, len(c) - 1)
        price_chg = float((c.iloc[-1] - c.iloc[-lb]) / (c.iloc[-lb] + 1e-9))

        ind = {
            'rsi': float(rsi.iloc[-1]), 'macd_hist': float(macd_hist.iloc[-1]),
            'ema20': float(ema20.iloc[-1]), 'ema50': float(ema50.iloc[-1]),
            'bb_pos': bb_pos, 'bb_width': float(bb_rng / (sma.iloc[-1] + 1e-9)),
            'vwap_dev': vwap_dev, 'obv_slope': obv_slope,
            'atr': atr, 'vol_ratio': vol_ratio,
            'price': float(c.iloc[-1]), 'price_chg': price_chg,
        }
        if any(not np.isfinite(x) for x in ind.values()):
            return None
        return ind
    except Exception as e:
        log.debug(f"Indicator error: {e}")
        return None

def score_timeframe(ind, direction='long'):
    if ind is None:
        return 0
    try:
        if direction == 'long':
            momentum = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0)     else 0
            trend    = 1 if (ind['ema20'] > ind['ema50'] * 0.995)                else 0
            volume   = 1 if (ind['vwap_dev'] > -0.01 and ind['vol_ratio'] > 0.7) else 0
        else:
            momentum = 1 if (ind['rsi'] > 52 and ind['macd_hist'] < 0)          else 0
            trend    = 1 if (ind['ema20'] < ind['ema50'] * 1.005)                else 0
            volume   = 1 if (ind['vwap_dev'] < 0.01 and ind['vol_ratio'] > 0.7)  else 0
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
        rsi REAL, macd_hist REAL, ema20 REAL, ema50 REAL,
        bb_pos REAL, bb_width REAL, vwap_dev REAL, obv_slope REAL,
        vol_ratio REAL, atr REAL, price REAL, price_chg REAL
    )''')
    db.commit()
    return db

def log_signal(db, sym, direction, scores, ind, route):
    if not ind:
        return
    try:
        db.execute('''INSERT INTO signals VALUES
            (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            datetime.utcnow().isoformat(), sym, direction,
            scores.get('15m',0), scores.get('1H',0),
            scores.get('4H',0),  scores.get('1D',0),
            sum(scores.values()), route,
            ind['rsi'], ind['macd_hist'], ind['ema20'], ind['ema50'],
            ind['bb_pos'], ind['bb_width'], ind['vwap_dev'], ind['obv_slope'],
            ind['vol_ratio'], ind['atr'], ind['price'], ind['price_chg'],
        ))
        db.commit()
    except Exception as e:
        log.warning(f"DB insert {sym}: {e}")

def rank_symbols(client, symbols):
    log.info(f"Tier A: Ranking {len(symbols)} symbols on daily bars...")
    bars   = fetch_bars(client, symbols, '1D')
    log.info(f"Tier A: Received daily bars for {len(bars)} symbols")
    scored = {}
    for sym, df in bars.items():
        try:
            ind = compute_indicators(df)
            if ind:
                scored[sym] = ind['price_chg'] * 100 + (ind['vol_ratio'] - 1) * 5
        except:
            pass
    ranked = sorted(scored, key=scored.get, reverse=True)
    top    = ranked[:150]
    log.info(f"Tier A complete. Scored: {len(scored)}. Focus set: {len(top)}")
    if top:
        log.info(f"Top 10 symbols: {top[:10]}")
    else:
        log.warning("Focus set is EMPTY — check Alpaca credentials and API access")
    return top

def main():
    log.info("=" * 60)
    log.info("ALGO TRADER v2 STARTING — SHADOW MODE")
    log.info("=" * 60)

    cfg    = load_config()
    client = get_client(cfg)
    assets = get_assets()
    db     = init_db()

    us_syms = [a for a in assets if '.' not in a and '-' not in a and len(a) <= 5]
    log.info(f"Total US symbols: {len(us_syms)}")

    # Quick connectivity test before full run
    log.info("Running connectivity test with 5 symbols...")
    test_bars = fetch_bars(client, ['AAPL','MSFT','NVDA','SPY','QQQ'], '1D')
    log.info(f"Connectivity test: got data for {len(test_bars)}/5 symbols")
    if not test_bars:
        log.error("CONNECTIVITY FAILED — cannot get data from Alpaca. Check secret_key in settings.yaml")
        log.error("Go to http://138.199.196.95:8080 → Settings → check secret_key is set")
    else:
        log.info(f"Connectivity OK. Sample symbol data points: {[f'{k}:{len(v)}bars' for k,v in list(test_bars.items())[:3]]}")

    focus     = []
    last_rank = datetime.utcnow() - timedelta(hours=7)
    cycle     = 0

    while True:
        try:
            cycle += 1
            now = datetime.utcnow()

            if (now - last_rank).total_seconds() > 21600:
                focus     = rank_symbols(client, us_syms)
                last_rank = now

            if not focus:
                log.warning("Focus empty — retry in 5 min")
                time.sleep(300)
                last_rank = datetime.utcnow() - timedelta(hours=7)
                continue

            log.info(f"Cycle {cycle}: Scanning {len(focus)} symbols across 4 timeframes...")

            cached_inds   = {}
            cached_scores = {}

            for tf in ['1D', '4H', '1H', '15m']:
                log.info(f"  Fetching {tf}...")
                bars = fetch_bars(client, focus, tf)
                log.info(f"  {tf}: got data for {len(bars)}/{len(focus)} symbols")

                for sym, df in bars.items():
                    ind = compute_indicators(df)
                    if ind is None:
                        continue
                    if sym not in cached_inds:
                        cached_inds[sym] = {}
                    cached_inds[sym][tf] = ind

                    for direction in ['long', 'short']:
                        if score_timeframe(ind, direction):
                            if sym not in cached_scores:
                                cached_scores[sym] = {'long': {}, 'short': {}}
                            cached_scores[sym][direction][tf] = 1

            log.info(f"  Symbols with at least 1 valid TF: {len(cached_scores)}")

            signal_count = 0
            for sym, dirs in cached_scores.items():
                for direction, tfs in dirs.items():
                    count = sum(tfs.values())
                    if count >= 3:
                        route    = 'ETORO' if count == 4 else 'IBKR'
                        best_ind = None
                        for pref in ['1D', '4H', '1H', '15m']:
                            if sym in cached_inds and pref in cached_inds[sym]:
                                best_ind = cached_inds[sym][pref]
                                break
                        if not best_ind:
                            continue
                        log.info(
                            f"*** SIGNAL [{route}] {sym} {direction.upper()} "
                            f"{count}/4 TF | RSI:{best_ind['rsi']:.1f} "
                            f"Price:${best_ind['price']:.2f} TFs:{list(tfs.keys())}"
                        )
                        log_signal(db, sym, direction, tfs, best_ind, route)
                        signal_count += 1

            log.info(f"Cycle {cycle} done. Signals: {signal_count}. Sleeping 15 min...")
            time.sleep(900)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == '__main__':
    main()

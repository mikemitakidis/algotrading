#!/usr/bin/env python3
"""Algo Trader v2 - yfinance only, clean rebuild"""
import time, logging, sqlite3, os, sys, yaml
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import yfinance as yf

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
    return [a for a in ASSET_UNIVERSE if '.' not in a and '-' not in a and len(a) <= 5]

def fetch(symbols, period, interval):
    all_bars = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i+100]
        try:
            raw = yf.download(batch, period=period, interval=interval,
                              group_by='ticker', auto_adjust=True,
                              progress=False, threads=True)
            if raw.empty:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                for sym in batch:
                    try:
                        df = raw[sym].dropna()
                        df.columns = [c.lower() for c in df.columns]
                        if len(df) >= 20:
                            all_bars[sym] = df
                    except: pass
            else:
                if len(batch) == 1 and not raw.empty:
                    raw.columns = [c.lower() for c in raw.columns]
                    if len(raw) >= 20:
                        all_bars[batch[0]] = raw
        except Exception as e:
            log.warning(f"Fetch error ({interval}): {e}")
        time.sleep(0.1)
    return all_bars

def indicators(df):
    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)
        h = df['high'].astype(float)
        l = df['low'].astype(float)
        if len(c) < 20: return None

        d = c.diff()
        rsi = 100 - 100/(1 + d.clip(lower=0).rolling(14).mean() / (-d.clip(upper=0).rolling(14).mean() + 1e-9))
        e12 = c.ewm(span=12,adjust=False).mean()
        e26 = c.ewm(span=26,adjust=False).mean()
        macd = (e12-e26) - (e12-e26).ewm(span=9,adjust=False).mean()
        e20 = c.ewm(span=20,adjust=False).mean()
        e50 = c.ewm(span=50,adjust=False).mean()
        sma = c.rolling(20).mean(); std = c.rolling(20).std()
        bb_pos = (c - (sma-2*std)) / ((4*std) + 1e-9)
        vwap = (c*v).cumsum()/(v.cumsum()+1e-9)
        vwap_dev = (c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1]+1e-9)
        obv = (np.sign(c.diff())*v).cumsum()
        obv_slope = (obv.iloc[-1]-obv.iloc[-5])/(abs(obv.iloc[-5])+1e-9) if len(obv)>5 else 0
        tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        vol_ratio = v.iloc[-1]/(v.rolling(20).mean().iloc[-1]+1e-9)
        lb = min(20,len(c)-1)
        pchg = (c.iloc[-1]-c.iloc[-lb])/(c.iloc[-lb]+1e-9)

        ind = dict(rsi=float(rsi.iloc[-1]), macd=float(macd.iloc[-1]),
                   e20=float(e20.iloc[-1]), e50=float(e50.iloc[-1]),
                   bb_pos=float(bb_pos.iloc[-1]), vwap_dev=float(vwap_dev),
                   obv_slope=float(obv_slope), atr=float(atr),
                   vol_ratio=float(vol_ratio), price=float(c.iloc[-1]),
                   bb_width=float(4*std.iloc[-1]/(sma.iloc[-1]+1e-9)), pchg=float(pchg))
        return ind if all(np.isfinite(x) for x in ind.values()) else None
    except: return None

def score(ind, direction):
    if not ind: return 0
    if direction == 'long':
        m = 1 if 30 < ind['rsi'] < 75 and ind['macd'] > 0 else 0
        t = 1 if ind['e20'] > ind['e50'] * 0.995 else 0
        vol = 1 if ind['vwap_dev'] > -0.015 and ind['vol_ratio'] > 0.6 else 0
    else:
        m = 1 if ind['rsi'] > 50 and ind['macd'] < 0 else 0
        t = 1 if ind['e20'] < ind['e50'] * 1.005 else 0
        vol = 1 if ind['vwap_dev'] < 0.015 and ind['vol_ratio'] > 0.6 else 0
    return 1 if m+t+vol == 3 else 0

def init_db():
    db = sqlite3.connect('/opt/algo-trader/data/signals.db')
    db.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, direction TEXT,
        tf_15m INT, tf_1h INT, tf_4h INT, tf_1d INT,
        valid_count INT, route TEXT,
        rsi REAL, macd REAL, e20 REAL, e50 REAL,
        bb_pos REAL, bb_width REAL, vwap_dev REAL,
        obv_slope REAL, vol_ratio REAL, atr REAL,
        price REAL, pchg REAL)''')
    db.commit()
    return db

def save_signal(db, sym, direction, scores, ind, route):
    if not ind: return
    try:
        db.execute('INSERT INTO signals VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            datetime.now(timezone.utc).isoformat(), sym, direction,
            scores.get('15m',0), scores.get('1H',0), scores.get('4H',0), scores.get('1D',0),
            sum(scores.values()), route,
            ind['rsi'], ind['macd'], ind['e20'], ind['e50'],
            ind['bb_pos'], ind['bb_width'], ind['vwap_dev'],
            ind['obv_slope'], ind['vol_ratio'], ind['atr'],
            ind['price'], ind['pchg']))
        db.commit()
    except Exception as e:
        log.warning(f"DB error: {e}")

def rank(symbols):
    log.info(f"Ranking {len(symbols)} symbols...")
    bars = fetch(symbols, '3mo', '1d')
    scored = {}
    for sym, df in bars.items():
        ind = indicators(df)
        if ind: scored[sym] = ind['pchg']*100 + (ind['vol_ratio']-1)*5
    top = sorted(scored, key=scored.get, reverse=True)[:150]
    log.info(f"Ranked {len(bars)} symbols. Focus set: {len(top)}. Top: {top[:5]}")
    return top

def main():
    log.info("="*50)
    log.info("ALGO TRADER v2 — CLEAN REBUILD — YFINANCE")
    log.info("="*50)
    db = init_db()
    symbols = get_assets()
    log.info(f"Loaded {len(symbols)} US symbols")

    # Connectivity test
    log.info("Testing yfinance connectivity...")
    test = fetch(['AAPL','MSFT','NVDA'], '5d', '1d')
    if test:
        log.info(f"OK: {[f'{k}:{len(v)}bars' for k,v in test.items()]}")
    else:
        log.error("yfinance connectivity FAILED")

    focus, last_rank, cycle = [], datetime(2000,1,1,tzinfo=timezone.utc), 0

    TFS = [('1D','3mo','1d'), ('4H','1mo','1h'), ('1H','15d','1h'), ('15m','5d','15m')]

    while True:
        try:
            cycle += 1
            now = datetime.now(timezone.utc)

            if (now-last_rank).total_seconds() > 21600:
                focus = rank(symbols)
                last_rank = now

            if not focus:
                log.warning("Focus empty, retrying in 5 min")
                time.sleep(300)
                last_rank = datetime(2000,1,1,tzinfo=timezone.utc)
                continue

            log.info(f"Cycle {cycle}: scanning {len(focus)} symbols...")
            inds, scores_cache = {}, {}

            for tf_label, period, interval in TFS:
                bars = fetch(focus, period, interval)
                log.info(f"  {tf_label}: {len(bars)} symbols")
                for sym, df in bars.items():
                    # Resample 1h -> 4H if needed
                    if tf_label == '4H':
                        df = df.resample('4h').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
                    ind = indicators(df)
                    if not ind: continue
                    inds.setdefault(sym, {})[tf_label] = ind
                    for d in ['long','short']:
                        if score(ind, d):
                            scores_cache.setdefault(sym, {'long':{}, 'short':{}})[d][tf_label] = 1

            log.info(f"  Symbols with signals: {len(scores_cache)}")
            n = 0
            for sym, dirs in scores_cache.items():
                for d, tfs in dirs.items():
                    cnt = sum(tfs.values())
                    if cnt >= 3:
                        route = 'ETORO' if cnt == 4 else 'IBKR'
                        best = next((inds[sym][t] for t in ['1D','4H','1H','15m'] if sym in inds and t in inds[sym]), None)
                        if not best: continue
                        log.info(f"*** [{route}] {sym} {d.upper()} {cnt}/4 TF | RSI:{best['rsi']:.1f} Price:${best['price']:.2f}")
                        save_signal(db, sym, d, tfs, best, route)
                        n += 1

            log.info(f"Cycle {cycle} done. Signals: {n}. Next in 15min.")
            time.sleep(900)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == '__main__':
    main()

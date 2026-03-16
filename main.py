#!/usr/bin/env python3
"""
Algo Trader v2 - Advanced Multi-Source Bot
Data: yfinance (market data) + Alpaca (future execution)
Indicators: RSI, MACD, EMA, Bollinger, VWAP, OBV, ATR, Volume
"""
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

def fetch_yf(symbols, period, interval):
    """Fetch bars using yfinance - free, reliable, no subscription needed."""
    all_bars = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i+100]
        try:
            raw = yf.download(
                batch, period=period, interval=interval,
                group_by='ticker', auto_adjust=True,
                progress=False, threads=True
            )
            if raw is None or raw.empty:
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                for sym in batch:
                    try:
                        df = raw[sym].dropna()
                        df.columns = [c.lower() for c in df.columns]
                        if len(df) >= 20:
                            all_bars[sym] = df
                    except Exception:
                        pass
            else:
                if len(batch) == 1 and len(raw) >= 20:
                    raw.columns = [c.lower() for c in raw.columns]
                    all_bars[batch[0]] = raw
        except Exception as e:
            log.warning(f"yfinance {interval} batch error: {str(e)[:80]}")
        time.sleep(0.1)
    return all_bars

def compute_indicators(df):
    """Compute all technical indicators."""
    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)
        h = df['high'].astype(float)
        l = df['low'].astype(float)
        if len(c) < 26:
            return None

        # RSI 14
        delta = c.diff()
        up = delta.clip(lower=0).rolling(14).mean()
        dn = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100 / (1 + up / (dn + 1e-9))

        # MACD 12/26/9
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_sig = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_sig

        # EMA 20 / 50
        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()

        # Bollinger Bands 20,2
        sma20 = c.rolling(20).mean()
        std20 = c.rolling(20).std()
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20
        bb_width = (bb_upper - bb_lower) / (sma20 + 1e-9)
        bb_pos = (c - bb_lower) / (bb_upper - bb_lower + 1e-9)

        # VWAP deviation
        vwap = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = (c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9)

        # OBV slope
        obv = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)) if len(obv) > 5 else 0.0

        # ATR 14
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()

        # Volume ratio
        vol_ratio = v.iloc[-1] / (v.rolling(20).mean().iloc[-1] + 1e-9)

        # Price change 20 bars
        lb = min(20, len(c)-1)
        pchg = (c.iloc[-1] - c.iloc[-lb]) / (c.iloc[-lb] + 1e-9)

        ind = {
            'rsi': float(rsi.iloc[-1]),
            'macd_hist': float(macd_hist.iloc[-1]),
            'macd_line': float(macd_line.iloc[-1]),
            'ema20': float(ema20.iloc[-1]),
            'ema50': float(ema50.iloc[-1]),
            'bb_pos': float(bb_pos.iloc[-1]),
            'bb_width': float(bb_width.iloc[-1]),
            'vwap_dev': float(vwap_dev),
            'obv_slope': obv_slope,
            'atr': float(atr.iloc[-1]),
            'vol_ratio': float(vol_ratio),
            'price': float(c.iloc[-1]),
            'pchg': float(pchg),
        }
        return ind if all(np.isfinite(x) for x in ind.values()) else None
    except Exception as e:
        log.debug(f"Indicator error: {e}")
        return None

def score_timeframe(ind, direction):
    """Categorical scoring: ALL 3 categories must pass."""
    if not ind:
        return 0
    try:
        if direction == 'long':
            momentum = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0) else 0
            trend    = 1 if (ind['ema20'] > ind['ema50'] * 0.995) else 0
            volume   = 1 if (ind['vwap_dev'] > -0.015 and ind['vol_ratio'] > 0.6) else 0
        else:
            momentum = 1 if (ind['rsi'] > 50 and ind['macd_hist'] < 0) else 0
            trend    = 1 if (ind['ema20'] < ind['ema50'] * 1.005) else 0
            volume   = 1 if (ind['vwap_dev'] < 0.015 and ind['vol_ratio'] > 0.6) else 0
        return 1 if (momentum + trend + volume == 3) else 0
    except:
        return 0

def init_db():
    db = sqlite3.connect('/opt/algo-trader/data/signals.db')
    db.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, direction TEXT,
        tf_15m INT, tf_1h INT, tf_4h INT, tf_1d INT,
        valid_count INT, route TEXT,
        rsi REAL, macd_hist REAL, ema20 REAL, ema50 REAL,
        bb_pos REAL, bb_width REAL, vwap_dev REAL,
        obv_slope REAL, vol_ratio REAL, atr REAL,
        price REAL, pchg REAL)''')
    db.commit()
    return db

def save_signal(db, sym, direction, scores, ind, route):
    if not ind:
        return
    try:
        db.execute(
            'INSERT INTO signals VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (datetime.now(timezone.utc).isoformat(), sym, direction,
             scores.get('15m',0), scores.get('1H',0),
             scores.get('4H',0), scores.get('1D',0),
             sum(scores.values()), route,
             ind['rsi'], ind['macd_hist'], ind['ema20'], ind['ema50'],
             ind['bb_pos'], ind['bb_width'], ind['vwap_dev'],
             ind['obv_slope'], ind['vol_ratio'], ind['atr'],
             ind['price'], ind['pchg'])
        )
        db.commit()
    except Exception as e:
        log.warning(f"DB error: {e}")

def rank_symbols(symbols):
    """Tier A: rank all symbols by momentum, pick top 150."""
    log.info(f"Tier A: Ranking {len(symbols)} symbols...")
    bars = fetch_yf(symbols, '3mo', '1d')
    scored = {}
    for sym, df in bars.items():
        ind = compute_indicators(df)
        if ind:
            scored[sym] = ind['pchg'] * 100 + (ind['vol_ratio'] - 1) * 5
    top = sorted(scored, key=scored.get, reverse=True)[:150]
    log.info(f"Bars fetched: {len(bars)}. Scored: {len(scored)}. Focus set: {len(top)}")
    if top:
        log.info(f"Top 5: {top[:5]}")
    return top

def main():
    log.info("=" * 55)
    log.info("ALGO TRADER v2 — yfinance — SHADOW MODE")
    log.info("=" * 55)

    cfg     = load_config()
    db      = init_db()
    symbols = get_assets()
    log.info(f"US symbols loaded: {len(symbols)}")

    # Quick connectivity test
    log.info("Connectivity test...")
    test = fetch_yf(['AAPL', 'MSFT', 'NVDA', 'SPY', 'QQQ'], '5d', '1d')
    if test:
        log.info(f"yfinance OK: {[f'{k}:{len(v)}bars' for k,v in test.items()]}")
    else:
        log.error("yfinance FAILED — check internet access on server")

    focus     = []
    last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cycle     = 0

    TFS = [
        ('1D',  '3mo',  '1d'),
        ('4H',  '1mo',  '1h'),   # will be resampled to 4H
        ('1H',  '15d',  '1h'),
        ('15m', '5d',   '15m'),
    ]

    while True:
        try:
            cycle += 1
            now = datetime.now(timezone.utc)

            # Re-rank every 6 hours
            if (now - last_rank).total_seconds() > 21600:
                focus     = rank_symbols(symbols)
                last_rank = now

            if not focus:
                log.warning("Focus empty — retrying rank in 5 min")
                time.sleep(300)
                last_rank = datetime(2000, 1, 1, tzinfo=timezone.utc)
                continue

            log.info(f"Cycle {cycle}: scanning {len(focus)} symbols across 4 TFs...")

            cached_inds   = {}   # sym -> {tf -> indicators}
            cached_scores = {}   # sym -> {direction -> {tf -> 1}}

            for tf_label, period, interval in TFS:
                log.info(f"  Fetching {tf_label}...")
                bars = fetch_yf(focus, period, interval)
                log.info(f"  {tf_label}: got {len(bars)} symbols")

                for sym, df in bars.items():
                    if tf_label == '4H':
                        df = df.resample('4h').agg({
                            'open':'first','high':'max',
                            'low':'min','close':'last','volume':'sum'
                        }).dropna()

                    ind = compute_indicators(df)
                    if not ind:
                        continue

                    cached_inds.setdefault(sym, {})[tf_label] = ind

                    for direction in ['long', 'short']:
                        if score_timeframe(ind, direction):
                            cached_scores.setdefault(sym, {'long':{}, 'short':{}})
                            cached_scores[sym][direction][tf_label] = 1

            log.info(f"  Symbols with ≥1 valid TF: {len(cached_scores)}")

            n = 0
            for sym, dirs in cached_scores.items():
                for direction, tfs in dirs.items():
                    count = sum(tfs.values())
                    if count >= 3:
                        route    = 'ETORO' if count == 4 else 'IBKR'
                        best_ind = next(
                            (cached_inds[sym][t] for t in ['1D','4H','1H','15m']
                             if sym in cached_inds and t in cached_inds[sym]), None
                        )
                        if not best_ind:
                            continue
                        log.info(
                            f"*** SIGNAL [{route}] {sym} {direction.upper()} "
                            f"{count}/4 TF | RSI:{best_ind['rsi']:.1f} "
                            f"Price:${best_ind['price']:.2f} | TFs:{list(tfs.keys())}"
                        )
                        save_signal(db, sym, direction, tfs, best_ind, route)
                        n += 1

            log.info(f"Cycle {cycle} done. Signals: {n}. Sleeping 15 min...")
            time.sleep(900)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == '__main__':
    main()

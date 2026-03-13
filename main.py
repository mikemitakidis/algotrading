#!/usr/bin/env python3
"""
Algo Trader v2 — Production Ready
Data: yfinance (free, no subscription, works immediately)
Alpaca: reserved for order execution only (future phase)
"""
import time, logging, sqlite3, yaml, os, sys
from datetime import datetime, timedelta
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

# Timeframe map for yfinance
YF_INTERVALS = {
    '15m': ('15m',  '5d'),    # 15-min bars, 5 days
    '1H':  ('1h',  '15d'),    # 1-hour bars, 15 days
    '4H':  ('1h',  '30d'),    # 1-hour bars resampled to 4H, 30 days
    '1D':  ('1d',  '120d'),   # Daily bars, 120 days
}

def load_config():
    with open('/opt/algo-trader/config/settings.yaml') as f:
        return yaml.safe_load(f)

def get_assets():
    sys.path.insert(0, '/opt/algo-trader/config')
    from assets import ASSET_UNIVERSE
    return ASSET_UNIVERSE

def fetch_bars_yf(symbols, timeframe):
    """Fetch OHLCV bars using yfinance — free, no API key, no subscription."""
    interval, period = YF_INTERVALS[timeframe]
    all_bars = {}
    batch_size = 100

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        try:
            raw = yf.download(
                tickers=batch,
                period=period,
                interval=interval,
                group_by='ticker',
                auto_adjust=True,
                progress=False,
                threads=True
            )
            if raw.empty:
                continue

            # Multi-ticker response
            if isinstance(raw.columns, pd.MultiIndex):
                for sym in batch:
                    try:
                        df = raw[sym].dropna()
                        df.columns = [c.lower() for c in df.columns]
                        if len(df) >= 14:
                            # Resample 1H → 4H if needed
                            if timeframe == '4H':
                                df = df.resample('4h').agg({
                                    'open': 'first', 'high': 'max',
                                    'low': 'min', 'close': 'last',
                                    'volume': 'sum'
                                }).dropna()
                            if len(df) >= 14:
                                all_bars[sym] = df
                    except Exception:
                        pass
            else:
                # Single ticker
                df = raw.copy()
                df.columns = [c.lower() for c in df.columns]
                if len(df) >= 14:
                    if timeframe == '4H':
                        df = df.resample('4h').agg({
                            'open': 'first', 'high': 'max',
                            'low': 'min', 'close': 'last',
                            'volume': 'sum'
                        }).dropna()
                    if len(df) >= 14 and len(batch) == 1:
                        all_bars[batch[0]] = df

        except Exception as e:
            log.warning(f"yfinance batch {i//batch_size+1} ({timeframe}): {str(e)[:100]}")
        time.sleep(0.2)

    return all_bars

def compute_indicators(df):
    if len(df) < 14:
        return None
    try:
        c = df['close'].astype(float)
        v = df['volume'].astype(float)
        h = df['high'].astype(float)
        l = df['low'].astype(float)

        # RSI 14
        delta = c.diff()
        up    = delta.clip(lower=0).rolling(14).mean()
        dn    = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - (100 / (1 + up / (dn + 1e-9)))

        # MACD 12/26/9
        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

        # EMA 20/50
        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()

        # Bollinger Bands
        n     = min(20, len(c))
        sma   = c.rolling(n).mean()
        std   = c.rolling(n).std()
        bb_up = sma + 2*std
        bb_lo = sma - 2*std
        bb_rng = float(bb_up.iloc[-1] - bb_lo.iloc[-1])
        bb_pos = float((c.iloc[-1] - bb_lo.iloc[-1]) / (bb_rng + 1e-9)) if bb_rng > 0 else 0.5

        # VWAP deviation
        vwap     = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = float((c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9))

        # OBV slope
        obv       = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)) if len(obv) > 5 else 0.0

        # ATR
        tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # Volume ratio
        vol_ma    = v.rolling(min(20, len(v))).mean()
        vol_ratio = float(v.iloc[-1] / (vol_ma.iloc[-1] + 1e-9))

        # Price momentum
        lb        = min(20, len(c)-1)
        price_chg = float((c.iloc[-1] - c.iloc[-lb]) / (c.iloc[-lb] + 1e-9))

        ind = {
            'rsi': float(rsi.iloc[-1]),
            'macd_hist': float(macd_hist.iloc[-1]),
            'ema20': float(ema20.iloc[-1]),
            'ema50': float(ema50.iloc[-1]),
            'bb_pos': bb_pos,
            'bb_width': float(bb_rng / (sma.iloc[-1] + 1e-9)),
            'vwap_dev': vwap_dev,
            'obv_slope': obv_slope,
            'atr': atr,
            'vol_ratio': vol_ratio,
            'price': float(c.iloc[-1]),
            'price_chg': price_chg,
        }
        if any(not np.isfinite(x) for x in ind.values()):
            return None
        return ind
    except Exception as e:
        log.debug(f"Indicator error: {e}")
        return None

def score_timeframe(ind, direction='long'):
    """All 3 categories must pass: Momentum + Trend + Volume."""
    if ind is None:
        return 0
    try:
        if direction == 'long':
            momentum = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0)          else 0
            trend    = 1 if (ind['ema20'] > ind['ema50'] * 0.995)                     else 0
            volume   = 1 if (ind['vwap_dev'] > -0.015 and ind['vol_ratio'] > 0.6)    else 0
        else:
            momentum = 1 if (ind['rsi'] > 50 and ind['macd_hist'] < 0)               else 0
            trend    = 1 if (ind['ema20'] < ind['ema50'] * 1.005)                     else 0
            volume   = 1 if (ind['vwap_dev'] < 0.015 and ind['vol_ratio'] > 0.6)     else 0
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

def rank_symbols(symbols):
    """Tier A: rank all US symbols by momentum, return top 150."""
    log.info(f"Tier A: Ranking {len(symbols)} symbols...")
    bars   = fetch_bars_yf(symbols, '1D')
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
    log.info(f"Tier A done. Data received: {len(bars)} symbols. Scored: {len(scored)}. Focus: {len(top)}")
    if top:
        log.info(f"Top 10: {top[:10]}")
    return top

def main():
    log.info("=" * 60)
    log.info("ALGO TRADER v2 — SHADOW MODE")
    log.info("Data source: yfinance (free, no API limits)")
    log.info("=" * 60)

    cfg    = load_config()
    db     = init_db()
    assets = get_assets()

    us_syms = [a for a in assets if '.' not in a and '-' not in a and len(a) <= 5]
    log.info(f"US symbols loaded: {len(us_syms)}")

    # Quick connectivity test
    log.info("Running connectivity test...")
    test = fetch_bars_yf(['AAPL','MSFT','NVDA','SPY','QQQ'], '1D')
    if test:
        log.info(f"Connectivity OK: {[f'{k}:{len(v)}bars' for k,v in test.items()]}")
    else:
        log.error("Connectivity FAILED — yfinance returned no data")

    focus     = []
    last_rank = datetime.utcnow() - timedelta(hours=7)
    cycle     = 0

    while True:
        try:
            cycle += 1
            now = datetime.utcnow()

            # Re-rank every 6 hours
            if (now - last_rank).total_seconds() > 21600:
                focus     = rank_symbols(us_syms)
                last_rank = now

            if not focus:
                log.warning("Focus set empty — retrying rank in 5 min")
                time.sleep(300)
                last_rank = datetime.utcnow() - timedelta(hours=7)
                continue

            log.info(f"Cycle {cycle}: Scanning {len(focus)} symbols across 4 timeframes...")

            cached_inds   = {}
            cached_scores = {}

            for tf in ['1D', '4H', '1H', '15m']:
                log.info(f"  Fetching {tf}...")
                bars = fetch_bars_yf(focus, tf)
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

            log.info(f"  Symbols with valid TF scores: {len(cached_scores)}")

            signal_count = 0
            for sym, dirs in cached_scores.items():
                for direction, tfs in dirs.items():
                    count = sum(tfs.values())
                    if count >= 3:
                        route    = 'ETORO' if count == 4 else 'IBKR'
                        best_ind = None
                        for pref in ['1D','4H','1H','15m']:
                            if sym in cached_inds and pref in cached_inds[sym]:
                                best_ind = cached_inds[sym][pref]
                                break
                        if not best_ind:
                            continue
                        log.info(
                            f"*** SIGNAL [{route}] {sym} {direction.upper()} "
                            f"{count}/4 TF | RSI:{best_ind['rsi']:.1f} "
                            f"Price:${best_ind['price']:.2f} | TFs:{list(tfs.keys())}"
                        )
                        log_signal(db, sym, direction, tfs, best_ind, route)
                        signal_count += 1

            log.info(f"Cycle {cycle} done. Signals: {signal_count}. Sleeping 15 min...")
            time.sleep(900)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Cycle {cycle} error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Algo Trader v2 — Fixed & Complete
Fixes:
  1. feed='iex' explicitly set (free Alpaca tier — SIP requires paid plan)
  2. Dynamic lookback per timeframe (prevents API truncation)
  3. Indicator dict cached and passed correctly to log_signal (ML bug fixed)
  4. Looser scoring thresholds to generate real signals
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

# ── Dynamic lookback — prevents Alpaca 10k bar truncation ────────────────────
LOOKBACK = {
    '15m': 5,    # ~160 bars per symbol
    '1H':  15,   # ~90 bars per symbol
    '4H':  30,   # ~90 bars per symbol
    '1D':  90,   # 90 daily bars
}

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
    batch_size = 50  # smaller batches = more reliable
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=tf,
                start=start,
                end=end,
                feed='iex',          # FIX: explicitly IEX — free tier. SIP = paid only.
                adjustment='raw'
            )
            raw = client.get_stock_bars(req)
            df  = raw.df
            if df is None or df.empty:
                continue

            if isinstance(df.index, pd.MultiIndex):
                for sym in batch:
                    try:
                        s = df.loc[sym].copy()
                        if len(s) >= 10:
                            all_bars[sym] = s
                    except KeyError:
                        pass
            elif len(df) >= 10 and len(batch) == 1:
                all_bars[batch[0]] = df.copy()

        except Exception as e:
            err = str(e)[:120]
            log.warning(f"Batch {i//batch_size+1} ({timeframe}): {err}")
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

        # RSI 14
        delta = c.diff()
        up   = delta.clip(lower=0).rolling(14).mean()
        down = (-delta.clip(upper=0)).rolling(14).mean()
        rsi  = 100 - (100 / (1 + up / (down + 1e-9)))

        # MACD 12/26/9
        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        sig_line  = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - sig_line

        # EMA 20 / 50
        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()

        # Bollinger Bands 20,2
        n     = min(20, len(c))
        sma   = c.rolling(n).mean()
        std   = c.rolling(n).std()
        bb_up = sma + 2 * std
        bb_lo = sma - 2 * std
        bb_rng = float(bb_up.iloc[-1] - bb_lo.iloc[-1])
        bb_pos = float((c.iloc[-1] - bb_lo.iloc[-1]) / (bb_rng + 1e-9)) if bb_rng > 0 else 0.5
        bb_w   = float(bb_rng / (sma.iloc[-1] + 1e-9))

        # VWAP deviation
        vwap     = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = float((c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9))

        # OBV slope (5 bars)
        obv = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)) if len(obv) > 5 else 0.0

        # ATR 14
        tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # Volume ratio vs 20-bar avg
        vol_ma    = v.rolling(min(20, len(v))).mean()
        vol_ratio = float(v.iloc[-1] / (vol_ma.iloc[-1] + 1e-9))

        # 20-bar price momentum
        lb = min(20, len(c) - 1)
        price_chg = float((c.iloc[-1] - c.iloc[-lb]) / (c.iloc[-lb] + 1e-9))

        ind = {
            'rsi':        float(rsi.iloc[-1]),
            'macd_hist':  float(macd_hist.iloc[-1]),
            'ema20':      float(ema20.iloc[-1]),
            'ema50':      float(ema50.iloc[-1]),
            'bb_pos':     bb_pos,
            'bb_width':   bb_w,
            'vwap_dev':   vwap_dev,
            'obv_slope':  obv_slope,
            'atr':        atr,
            'vol_ratio':  vol_ratio,
            'price':      float(c.iloc[-1]),
            'price_chg':  price_chg,
        }
        # Reject NaN / Inf
        if any(not np.isfinite(x) for x in ind.values()):
            return None
        return ind

    except Exception as e:
        log.debug(f"Indicator error: {e}")
        return None

def score_timeframe(ind, direction='long'):
    """All 3 categories must = 1. Thresholds loosened for real signal generation."""
    if ind is None:
        return 0
    try:
        if direction == 'long':
            momentum = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0)                             else 0
            trend    = 1 if (ind['ema20'] > ind['ema50'] * 0.995)                                        else 0
            volume   = 1 if (ind['vwap_dev'] > -0.01 and ind['vol_ratio'] > 0.7)                        else 0
        else:
            momentum = 1 if (ind['rsi'] > 52 and ind['macd_hist'] < 0)                                  else 0
            trend    = 1 if (ind['ema20'] < ind['ema50'] * 1.005)                                        else 0
            volume   = 1 if (ind['vwap_dev'] < 0.01 and ind['vol_ratio'] > 0.7)                         else 0
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
    """FIX: ind is always a populated dict — never called with {} anymore."""
    if not ind:
        log.warning(f"Empty ind for {sym} — skipping DB write")
        return
    try:
        db.execute('''INSERT INTO signals VALUES
            (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            datetime.utcnow().isoformat(), sym, direction,
            scores.get('15m', 0), scores.get('1H', 0),
            scores.get('4H',  0), scores.get('1D', 0),
            sum(scores.values()), route,
            ind['rsi'], ind['macd_hist'], ind['ema20'], ind['ema50'],
            ind['bb_pos'], ind['bb_width'], ind['vwap_dev'], ind['obv_slope'],
            ind['vol_ratio'], ind['atr'], ind['price'], ind['price_chg'],
        ))
        db.commit()
    except Exception as e:
        log.warning(f"DB insert error {sym}: {e}")

def rank_symbols(client, symbols):
    log.info(f"Tier A: Ranking {len(symbols)} symbols...")
    bars   = fetch_bars(client, symbols, '1D')
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
    log.info(f"Tier A done. Bars received: {len(bars)}. Scored: {len(scored)}. Focus: {len(top)}")
    if top:
        log.info(f"Top symbols: {top[:10]}")
    return top

def main():
    log.info("=" * 60)
    log.info("ALGO TRADER v2 — ALL BUGS FIXED")
    log.info("  Fix 1: feed='iex' (free tier IEX, not SIP)")
    log.info("  Fix 2: Dynamic lookback per timeframe")
    log.info("  Fix 3: Indicator dict cached & passed to ML DB")
    log.info("=" * 60)

    cfg    = load_config()
    client = get_client(cfg)
    assets = get_assets()
    db     = init_db()

    # US stocks only
    us_syms = [a for a in assets if '.' not in a and '-' not in a and len(a) <= 5]
    log.info(f"US symbols: {len(us_syms)}")

    focus         = []
    last_rank     = datetime.utcnow() - timedelta(hours=7)
    cycle         = 0

    while True:
        try:
            cycle += 1
            now = datetime.utcnow()

            # Re-rank every 6 hours (forced on startup)
            if (now - last_rank).total_seconds() > 21600:
                focus     = rank_symbols(client, us_syms)
                last_rank = now

            if not focus:
                log.warning("Focus set empty — retry rank in 5 min")
                time.sleep(300)
                last_rank = datetime.utcnow() - timedelta(hours=7)
                continue

            log.info(f"Cycle {cycle}: Scanning {len(focus)} symbols across 4 TFs...")

            # FIX: Cache indicators per symbol per timeframe
            # Previously indicators were computed inside loop but never stored
            # → log_signal was called with empty {} → nothing written to DB
            cached_inds   = {}   # sym → {tf → ind_dict}
            cached_scores = {}   # sym → {direction → {tf → 1}}

            for tf in ['1D', '4H', '1H', '15m']:
                log.info(f"  Fetching {tf}...")
                bars = fetch_bars(client, focus, tf)
                log.info(f"  {tf}: got data for {len(bars)} symbols")

                for sym, df in bars.items():
                    ind = compute_indicators(df)
                    if ind is None:
                        continue

                    # Cache the indicators
                    if sym not in cached_inds:
                        cached_inds[sym] = {}
                    cached_inds[sym][tf] = ind

                    # Score both directions
                    for direction in ['long', 'short']:
                        if score_timeframe(ind, direction):
                            if sym not in cached_scores:
                                cached_scores[sym] = {'long': {}, 'short': {}}
                            cached_scores[sym][direction][tf] = 1

            # Evaluate confluences
            signal_count = 0
            for sym, dirs in cached_scores.items():
                for direction, tfs in dirs.items():
                    count = sum(tfs.values())
                    if count >= 3:
                        route = 'ETORO' if count == 4 else 'IBKR'

                        # Use best available indicator set
                        best_ind = None
                        for pref in ['1D', '4H', '1H', '15m']:
                            if sym in cached_inds and pref in cached_inds[sym]:
                                best_ind = cached_inds[sym][pref]
                                break

                        if best_ind is None:
                            continue

                        log.info(
                            f"*** SIGNAL [{route}] {sym} {direction.upper()} "
                            f"{count}/4 TF | RSI:{best_ind['rsi']:.1f} "
                            f"Price:${best_ind['price']:.2f} TFs:{list(tfs.keys())}"
                        )
                        log_signal(db, sym, direction, tfs, best_ind, route)
                        signal_count += 1

            log.info(f"Cycle {cycle} done. Signals: {signal_count}. Next in 15min.")
            time.sleep(900)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == '__main__':
    main()

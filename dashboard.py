#!/usr/bin/env python3
"""
Algo Trader v2 — Dashboard
Real session-based authentication. All write endpoints protected.
"""
import subprocess, sqlite3, os, yaml, signal, hashlib, secrets
from datetime import datetime
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string


# ── Self-heal: write correct main.py on every dashboard start ────────────────
import os as _os
_MAIN_PATH = '/opt/algo-trader/main.py'
_CORRECT_MAIN = '''#!/usr/bin/env python3
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
    db.execute(\'\'\'CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, direction TEXT,
        tf_15m INTEGER, tf_1h INTEGER, tf_4h INTEGER, tf_1d INTEGER,
        valid_count INTEGER, route TEXT,
        rsi REAL, macd_hist REAL, ema20 REAL, ema50 REAL,
        bb_pos REAL, bb_width REAL, vwap_dev REAL, obv_slope REAL,
        vol_ratio REAL, atr REAL, price REAL, price_chg REAL
    )\'\'\')
    db.commit()
    return db

def log_signal(db, sym, direction, scores, ind, route):
    if not ind:
        return
    try:
        db.execute(\'\'\'INSERT INTO signals VALUES
            (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)\'\'\', (
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
'''

try:
    with open(_MAIN_PATH, 'r') as _f:
        _existing = _f.read()
    if 'yfinance' not in _existing or 'yf.download' not in _existing:
        with open(_MAIN_PATH, 'w') as _f:
            _f.write(_CORRECT_MAIN)
        print("Self-heal: main.py updated to yfinance version")
except Exception as _e:
    print(f"Self-heal error: {_e}")
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # Random secret each restart

CONFIG_PATH  = '/opt/algo-trader/config/settings.yaml'
LOG_PATH     = '/opt/algo-trader/logs/bot.log'
DB_PATH      = '/opt/algo-trader/data/signals.db'
START_SCRIPT = '/opt/algo-trader/start.sh'

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def get_password():
    try:
        cfg = load_config()
        return cfg.get('dashboard', {}).get('password', 'AlgoTrader2024!')
    except:
        return 'AlgoTrader2024!'

def is_logged_in():
    # Accept session cookie (browser) OR internal header (Vercel proxy)
    if session.get('authenticated') is True:
        return True
    pw = get_password()
    if request.headers.get('X-Internal-Auth') == pw:
        return True
    return False

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Algo Trader v2.0</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0d1117; color:#e6edf3; font-family:'Segoe UI',Arial,sans-serif; min-height:100vh; }
.navbar { background:#161b22; border-bottom:1px solid #30363d; padding:14px 28px; display:flex; align-items:center; justify-content:space-between; }
.brand { font-size:20px; font-weight:700; color:#58a6ff; display:flex; align-items:center; gap:10px; }
.brand span { background:#1f6feb; color:#fff; font-size:11px; padding:2px 8px; border-radius:12px; font-weight:600; }
.nav-links { display:flex; gap:6px; }
.nav-links a { color:#8b949e; text-decoration:none; padding:7px 14px; border-radius:6px; font-size:14px; cursor:pointer; transition:all .2s; }
.nav-links a:hover, .nav-links a.active { background:#21262d; color:#e6edf3; }
.nav-links a.logout { color:#f85149; }
.page { display:none; padding:28px; max-width:1400px; margin:0 auto; }
.page.active { display:block; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px; }
.card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:24px; }
.card-title { font-size:11px; font-weight:600; color:#8b949e; letter-spacing:1px; text-transform:uppercase; margin-bottom:16px; }
.status-row { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.dot { width:12px; height:12px; border-radius:50%; flex-shrink:0; }
.dot.green { background:#3fb950; box-shadow:0 0 8px #3fb950; animation:pulse 2s infinite; }
.dot.red { background:#f85149; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
.status-text { font-size:22px; font-weight:600; }
.btn-row { display:flex; gap:10px; flex-wrap:wrap; }
.btn { padding:9px 18px; border:none; border-radius:7px; font-size:14px; font-weight:600; cursor:pointer; display:flex; align-items:center; gap:7px; transition:all .2s; }
.btn-start { background:#238636; color:#fff; } .btn-start:hover { background:#2ea043; }
.btn-stop  { background:#da3633; color:#fff; } .btn-stop:hover  { background:#f85149; }
.btn-restart { background:#1f6feb; color:#fff; } .btn-restart:hover { background:#388bfd; }
.metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:#30363d; border-radius:8px; overflow:hidden; }
.metric { background:#161b22; padding:20px; text-align:center; }
.metric-val { font-size:32px; font-weight:700; }
.metric-val.blue { color:#58a6ff; } .metric-val.green { color:#3fb950; }
.metric-lbl { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:1px; margin-top:4px; }
.logbox { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:16px; font-family:'Courier New',monospace; font-size:12px; height:400px; overflow-y:auto; line-height:1.6; white-space:pre-wrap; }
.logbox .warn { color:#d29922; } .logbox .err { color:#f85149; } .logbox .info { color:#8b949e; } .logbox .signal { color:#3fb950; font-weight:700; }
.signal-table { width:100%; border-collapse:collapse; font-size:13px; }
.signal-table th { background:#21262d; color:#8b949e; padding:10px 12px; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.8px; }
.signal-table td { padding:10px 12px; border-top:1px solid #21262d; }
.badge { padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; }
.badge-etoro { background:#0d4a1a; color:#3fb950; border:1px solid #238636; }
.badge-ibkr { background:#0d2d5a; color:#58a6ff; border:1px solid #1f6feb; }
.badge-long { background:#0d4a1a; color:#3fb950; } .badge-short { background:#4a0d0d; color:#f85149; }
.refresh-btn { background:none; border:1px solid #30363d; color:#8b949e; padding:6px 12px; border-radius:6px; cursor:pointer; font-size:12px; float:right; }
.refresh-btn:hover { border-color:#58a6ff; color:#58a6ff; }
textarea.settings-area { width:100%; background:#0d1117; color:#e6edf3; border:1px solid #30363d; border-radius:8px; padding:16px; font-family:'Courier New',monospace; font-size:13px; height:480px; resize:vertical; }
.btn-save { background:#238636; color:#fff; padding:10px 24px; border:none; border-radius:7px; font-size:14px; font-weight:600; cursor:pointer; margin-top:12px; }
.btn-save:hover { background:#2ea043; }
.alert { padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.alert-success { background:#0d4a1a; border:1px solid #238636; color:#3fb950; }
.alert-error   { background:#4a0d0d; border:1px solid #da3633; color:#f85149; }
.login-wrap { display:flex; align-items:center; justify-content:center; min-height:100vh; background:#0d1117; }
.login-box { background:#161b22; border:1px solid #30363d; border-radius:14px; padding:40px; width:360px; }
.login-title { font-size:22px; font-weight:700; color:#58a6ff; text-align:center; margin-bottom:8px; }
.login-sub { color:#8b949e; text-align:center; font-size:14px; margin-bottom:28px; }
.login-box input { width:100%; background:#0d1117; border:1px solid #30363d; color:#e6edf3; padding:11px 14px; border-radius:8px; font-size:15px; margin-bottom:14px; outline:none; }
.login-box input:focus { border-color:#58a6ff; }
.login-box button { width:100%; background:#238636; color:#fff; border:none; padding:12px; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; }
.login-box button:hover { background:#2ea043; }
.login-err { color:#f85149; font-size:13px; text-align:center; margin-top:10px; }
</style>
</head>
<body>
<div id="loginPage" class="login-wrap" style="display:none">
  <div class="login-box">
    <div class="login-title">🤖 Algo Trader</div>
    <div class="login-sub">v2.0 — Shadow Mode</div>
    <input type="password" id="pwInput" placeholder="Password" onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Login</button>
    <div class="login-err" id="loginErr"></div>
  </div>
</div>
<div id="mainApp" style="display:none">
<nav class="navbar">
  <div class="brand">🤖 Algo Trader <span>v2.0</span></div>
  <div class="nav-links">
    <a onclick="showPage('overview')" id="nav-overview" class="active">Overview</a>
    <a onclick="showPage('signals')"  id="nav-signals">Signals</a>
    <a onclick="showPage('params')"   id="nav-params">Parameters</a>
    <a onclick="showPage('logs')"     id="nav-logs">Logs</a>
    <a onclick="showPage('settings')" id="nav-settings">Settings</a>
    <a onclick="doLogout()" class="logout">Logout</a>
  </div>
</nav>

<div id="overview" class="page active">
  <div class="grid2">
    <div class="card">
      <div class="card-title">Bot Status</div>
      <div class="status-row">
        <div class="dot" id="statusDot"></div>
        <div class="status-text" id="statusText">Loading...</div>
      </div>
      <div class="btn-row">
        <button class="btn btn-start"   onclick="botAction('start')">▶ Start</button>
        <button class="btn btn-stop"    onclick="botAction('stop')">⏹ Stop</button>
        <button class="btn btn-restart" onclick="botAction('restart')">↺ Restart</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Performance</div>
      <div class="metrics">
        <div class="metric"><div class="metric-val blue" id="sigCount">0</div><div class="metric-lbl">Signals</div></div>
        <div class="metric"><div class="metric-val green" id="winCount">0</div><div class="metric-lbl">Wins</div></div>
        <div class="metric"><div class="metric-val blue" id="winRate">N/A</div><div class="metric-lbl">Win Rate</div></div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Recent Signals <button class="refresh-btn" onclick="loadSignals()">↻ Refresh</button></div>
    <div id="signalTableWrap"><div style="color:#8b949e;text-align:center;padding:30px">No signals yet — bot is scanning in shadow mode...</div></div>
  </div>
  <div class="card" style="margin-top:20px">
    <div class="card-title">Live Log Feed <button class="refresh-btn" onclick="loadLogs()">↻ Refresh</button></div>
    <div class="logbox" id="logbox">Loading...</div>
  </div>
</div>

<div id="signals" class="page">
  <div class="card">
    <div class="card-title">All Signals <button class="refresh-btn" onclick="loadAllSignals()">↻ Refresh</button></div>
    <div id="allSignalsWrap"><div style="color:#8b949e;padding:20px">Loading...</div></div>
  </div>
</div>

<div id="params" class="page">
  <div class="card">
    <div class="card-title">Strategy Parameters</div>
    <table class="signal-table">
      <tr><th>Parameter</th><th>Value</th><th>Description</th></tr>
      <tr><td>RSI Long Range</td><td>30 – 75</td><td>Momentum building, not overbought</td></tr>
      <tr><td>RSI Short Min</td><td>> 52</td><td>Overbought territory</td></tr>
      <tr><td>MACD Signal</td><td>Histogram > 0 (long) / < 0 (short)</td><td>Trend direction confirmation</td></tr>
      <tr><td>EMA Crossover</td><td>EMA20 vs EMA50 (±0.5% tolerance)</td><td>Trend alignment</td></tr>
      <tr><td>Bollinger Position</td><td>bb_pos > 0.45 (long) / < 0.55 (short)</td><td>Price position within bands</td></tr>
      <tr><td>VWAP Deviation</td><td>Within ±1%</td><td>Institutional price level</td></tr>
      <tr><td>Volume Ratio</td><td>> 0.7× 20-bar average</td><td>Confirms participation</td></tr>
      <tr><td>OBV Slope</td><td>Positive (long) / Negative (short)</td><td>Volume pressure direction</td></tr>
      <tr><td>ATR Period</td><td>14 bars</td><td>Used for position sizing & stops</td></tr>
      <tr><td>Focus Set Size</td><td>Top 150</td><td>Re-ranked every 6 hours</td></tr>
      <tr><td>Scan Cycle</td><td>Every 15 minutes</td><td>Full 4-TF analysis</td></tr>
      <tr><td>eToro Min TF</td><td>4 / 4 timeframes</td><td>Manual execution via Telegram</td></tr>
      <tr><td>IBKR Min TF</td><td>3 / 4 timeframes</td><td>Automated (future)</td></tr>
    </table>
  </div>
</div>

<div id="logs" class="page">
  <div class="card">
    <div class="card-title">Bot Logs (last 300 lines) <button class="refresh-btn" onclick="loadFullLogs()">↻ Refresh</button></div>
    <div class="logbox" id="fullLogbox" style="height:600px">Loading...</div>
  </div>
</div>

<div id="settings" class="page">
  <div class="card">
    <div class="card-title">Edit settings.yaml</div>
    <div id="settingsAlert"></div>
    <textarea class="settings-area" id="settingsArea">Loading...</textarea>
    <br><button class="btn-save" onclick="saveSettings()">Save & Restart Bot</button>
  </div>
</div>
</div>

<script>
let authed = false;

async function doLogin() {
  const pw = document.getElementById('pwInput').value;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw})});
  const d = await r.json();
  if (d.ok) {
    authed = true;
    document.getElementById('loginPage').style.display = 'none';
    document.getElementById('mainApp').style.display = 'block';
    loadAll();
    setInterval(loadAll, 30000);
  } else {
    document.getElementById('loginErr').textContent = 'Incorrect password';
  }
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  location.reload();
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-links a').forEach(a => a.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  if (name === 'logs')    loadFullLogs();
  if (name === 'settings') loadSettings();
  if (name === 'signals') loadAllSignals();
}

function loadAll() { loadStatus(); loadSignals(); loadLogs(); }

async function loadStatus() {
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    const dot = document.getElementById('statusDot');
    document.getElementById('statusText').textContent = d.running ? 'Running — SHADOW mode' : 'Stopped';
    dot.className = 'dot ' + (d.running ? 'green' : 'red');
    document.getElementById('sigCount').textContent = d.signal_count || 0;
    document.getElementById('winCount').textContent = d.win_count || 0;
    document.getElementById('winRate').textContent = d.win_rate || 'N/A';
  } catch(e) {}
}

async function loadSignals() {
  try {
    const r = await fetch('/api/signals?limit=10'); const d = await r.json();
    const wrap = document.getElementById('signalTableWrap');
    if (!d.signals || !d.signals.length) {
      wrap.innerHTML = '<div style="color:#8b949e;text-align:center;padding:30px">No signals yet — bot is scanning in shadow mode...</div>';
      return;
    }
    let h = '<table class="signal-table"><tr><th>Time</th><th>Symbol</th><th>Direction</th><th>Route</th><th>TFs</th><th>RSI</th><th>Price</th></tr>';
    d.signals.forEach(s => {
      h += `<tr><td>${s.timestamp?.slice(0,19)||''}</td><td><b>${s.symbol}</b></td>
        <td><span class="badge badge-${s.direction}">${s.direction?.toUpperCase()}</span></td>
        <td><span class="badge badge-${s.route?.toLowerCase()}">${s.route}</span></td>
        <td>${s.valid_count}/4</td><td>${(s.rsi||0).toFixed(1)}</td><td>$${(s.price||0).toFixed(2)}</td></tr>`;
    });
    wrap.innerHTML = h + '</table>';
  } catch(e) {}
}

async function loadAllSignals() {
  try {
    const r = await fetch('/api/signals?limit=200'); const d = await r.json();
    const wrap = document.getElementById('allSignalsWrap');
    if (!d.signals || !d.signals.length) {
      wrap.innerHTML = '<div style="color:#8b949e;padding:20px">No signals yet.</div>'; return;
    }
    let h = '<table class="signal-table"><tr><th>Time</th><th>Symbol</th><th>Direction</th><th>Route</th><th>TFs</th><th>RSI</th><th>MACD</th><th>Price</th><th>ATR</th></tr>';
    d.signals.forEach(s => {
      h += `<tr><td>${s.timestamp?.slice(0,19)||''}</td><td><b>${s.symbol}</b></td>
        <td><span class="badge badge-${s.direction}">${s.direction?.toUpperCase()}</span></td>
        <td><span class="badge badge-${s.route?.toLowerCase()}">${s.route}</span></td>
        <td>${s.valid_count}/4</td><td>${(s.rsi||0).toFixed(1)}</td>
        <td>${(s.macd_hist||0).toFixed(3)}</td><td>$${(s.price||0).toFixed(2)}</td><td>${(s.atr||0).toFixed(2)}</td></tr>`;
    });
    wrap.innerHTML = h + '</table>';
  } catch(e) {}
}

function colorLog(line) {
  if (line.includes('SIGNAL') || line.includes('*** ')) return `<span class="signal">${line}</span>`;
  if (line.includes('ERROR') || line.includes('error')) return `<span class="err">${line}</span>`;
  if (line.includes('WARNING')) return `<span class="warn">${line}</span>`;
  return `<span class="info">${line}</span>`;
}

async function loadLogs() {
  try {
    const r = await fetch('/api/logs?lines=80'); const d = await r.json();
    const el = document.getElementById('logbox');
    el.innerHTML = d.lines.map(colorLog).join('\n');
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

async function loadFullLogs() {
  try {
    const r = await fetch('/api/logs?lines=300'); const d = await r.json();
    const el = document.getElementById('fullLogbox');
    el.innerHTML = d.lines.map(colorLog).join('\n');
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

async function loadSettings() {
  try {
    const r = await fetch('/api/settings'); const d = await r.json();
    document.getElementById('settingsArea').value = d.content || '';
  } catch(e) {}
}

async function saveSettings() {
  const content = document.getElementById('settingsArea').value;
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({content})});
  const d = await r.json();
  const alert = document.getElementById('settingsAlert');
  alert.innerHTML = d.ok
    ? '<div class="alert alert-success">✅ Settings saved. Bot restarting...</div>'
    : '<div class="alert alert-error">❌ Error: ' + (d.error||'unknown') + '</div>';
  setTimeout(() => alert.innerHTML = '', 4000);
}

async function botAction(action) {
  await fetch('/api/' + action, {method:'POST'});
  setTimeout(loadStatus, 2000);
}

// Check if already logged in
fetch('/api/status').then(r => {
  if (r.ok) {
    authed = true;
    document.getElementById('loginPage').style.display = 'none';
    document.getElementById('mainApp').style.display = 'block';
    loadAll();
    setInterval(loadAll, 30000);
  } else {
    document.getElementById('loginPage').style.display = 'flex';
  }
}).catch(() => {
  document.getElementById('loginPage').style.display = 'flex';
});
</script>
</body>
</html>'''

# Flask routes
@app.route('/')
def index():
    if not is_logged_in():
        return render_template_string(HTML)
    return render_template_string(HTML)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    if data.get('password') == get_password():
        session['authenticated'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/status')
@require_auth
def status():
    import subprocess
    running = bool(subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True).stdout.strip())
    sig_count = ibkr_count = etoro_count = 0
    try:
        db = sqlite3.connect(DB_PATH)
        sig_count   = db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        ibkr_count  = db.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        etoro_count = db.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        db.close()
    except: pass
    return jsonify({'running': running, 'signal_count': sig_count,
                    'ibkr_count': ibkr_count, 'etoro_count': etoro_count,
                    'win_count': 0, 'win_rate': 'N/A'})

@app.route('/api/signals')
@require_auth
def signals():
    limit = min(int(request.args.get('limit', 10)), 500)
    try:
        db = sqlite3.connect(DB_PATH)
        rows = db.execute(
            'SELECT * FROM signals ORDER BY id DESC LIMIT ?', (limit,)
        ).fetchall()
        cols = [d[0] for d in db.execute('SELECT * FROM signals LIMIT 1').description] if rows else []
        db.close()
        return jsonify({'signals': [dict(zip(cols, r)) for r in rows]})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})

@app.route('/api/logs')
@require_auth
def logs():
    lines = min(int(request.args.get('lines', 100)), 500)
    try:
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
        return jsonify({'lines': [l.rstrip() for l in all_lines[-lines:]]})
    except:
        return jsonify({'lines': ['Log file not found']})

@app.route('/api/settings', methods=['GET', 'POST'])
@require_auth
def settings():
    if request.method == 'GET':
        try:
            with open(CONFIG_PATH) as f:
                return jsonify({'content': f.read()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        data = request.get_json(silent=True) or {}
        content = data.get('content', '')
        try:
            yaml.safe_load(content)  # validate YAML before saving
            with open(CONFIG_PATH, 'w') as f:
                f.write(content)
            subprocess.Popen(['bash', START_SCRIPT])
            return jsonify({'ok': True})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/api/start', methods=['POST'])
@require_auth
def start():
    subprocess.Popen(['bash', START_SCRIPT])
    return jsonify({'ok': True})

@app.route('/api/stop', methods=['POST'])
@require_auth
def stop():
    subprocess.run(['pkill', '-f', 'main.py'])
    return jsonify({'ok': True})

@app.route('/api/restart', methods=['POST'])
@require_auth
def restart():
    # Download latest code directly from GitHub (bypasses git pull issues)
    def _update_and_restart():
        import time, urllib.request
        time.sleep(1)
        base = 'https://raw.githubusercontent.com/mikemitakidis/algotrading/main/'
        files = {
            '/opt/algo-trader/main.py':      base + 'main.py',
            '/opt/algo-trader/dashboard.py': base + 'dashboard.py',
            '/opt/algo-trader/start.sh':     base + 'start.sh',
        }
        for dest, url in files.items():
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                print(f"Download failed {dest}: {e}")
        time.sleep(1)
        subprocess.Popen(['bash', START_SCRIPT],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    import threading
    threading.Thread(target=_update_and_restart, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/api/start', methods=['POST'])
@require_auth  
def start_bot():
    subprocess.Popen(['bash', '-c', f'sleep 1 && bash {START_SCRIPT}'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)

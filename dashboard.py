#!/usr/bin/env python3
"""
Algo Trader v2 — Dashboard
Real session-based authentication. All write endpoints protected.
"""
import subprocess, sqlite3, os, yaml, signal, hashlib, secrets
from datetime import datetime
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string

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
    return session.get('authenticated') is True

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
    sig_count = win_count = 0
    try:
        db = sqlite3.connect(DB_PATH)
        sig_count = db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        db.close()
    except: pass
    return jsonify({'running': running, 'signal_count': sig_count,
                    'win_count': win_count, 'win_rate': 'N/A'})

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
    subprocess.run(['pkill', '-f', 'main.py'])
    import time; time.sleep(1)
    subprocess.Popen(['bash', START_SCRIPT])
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)

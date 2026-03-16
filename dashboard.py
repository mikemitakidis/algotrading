#!/usr/bin/env python3
"""Algo Trader v2 — Dashboard. Clean rebuild."""
import subprocess, sqlite3, os, yaml, secrets
from flask import Flask, request, jsonify, session, render_template_string

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

BASE        = '/opt/algo-trader'
CONFIG_PATH = f'{BASE}/config/settings.yaml'
LOG_PATH    = f'{BASE}/logs/bot.log'
DB_PATH     = f'{BASE}/data/signals.db'

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def get_password():
    try:
        return load_config().get('dashboard', {}).get('password', 'AlgoTrader2024!')
    except:
        return 'AlgoTrader2024!'

def auth_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Algo Trader v2.0</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',Arial,sans-serif;min-height:100vh}
.nav{background:#161b22;border-bottom:1px solid #30363d;padding:0 28px;display:flex;align-items:center;justify-content:space-between;height:52px}
.brand{font-size:18px;font-weight:700;color:#58a6ff}
.brand span{background:#1f6feb;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px}
.nav a{color:#8b949e;text-decoration:none;padding:7px 14px;border-radius:6px;font-size:14px;cursor:pointer}
.nav a:hover,.nav a.active{background:#21262d;color:#e6edf3}
.nav a.logout{color:#f85149}
.page{display:none;padding:24px;max-width:1400px;margin:0 auto}
.page.active{display:block}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px}
.ct{font-size:11px;font-weight:600;color:#8b949e;letter-spacing:1px;text-transform:uppercase;margin-bottom:16px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:#30363d;border-radius:8px;overflow:hidden}
.metric{background:#161b22;padding:18px;text-align:center}
.mv{font-size:32px;font-weight:700;color:#58a6ff}
.mv.g{color:#3fb950}.mv.y{color:#d29922}
.ml{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-top:4px}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.btn{padding:9px 18px;border:none;border-radius:7px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
.btn-start{background:#238636;color:#fff}.btn-stop{background:#da3633;color:#fff}.btn-restart{background:#1f6feb;color:#fff}
.dot{width:13px;height:13px;border-radius:50%;display:inline-block;margin-right:10px}
.dot.g{background:#3fb950;box-shadow:0 0 8px #3fb950}.dot.r{background:#f85149}
.status-row{display:flex;align-items:center;margin-bottom:16px}
.status-text{font-size:20px;font-weight:600}
.logbox{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:14px;font-family:'Courier New',monospace;font-size:12px;height:400px;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#21262d;color:#8b949e;padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.8px}
td{padding:10px 12px;border-top:1px solid #21262d}
.tag{padding:2px 9px;border-radius:10px;font-size:11px;font-weight:600}
.tag-long{background:#0d4a1a;color:#3fb950}.tag-short{background:#4a0d0d;color:#f85149}
.tag-etoro{background:#2d1f00;color:#d29922}.tag-ibkr{background:#0d2d5a;color:#58a6ff}
.tag-ibkr,.tag-etoro,.tag-long,.tag-short{white-space:nowrap}
textarea{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:14px;border-radius:8px;font-family:'Courier New',monospace;font-size:13px;height:480px;resize:vertical}
.inp{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:14px;width:100%}
.alert-ok{background:#0d4a1a;border:1px solid #238636;color:#3fb950;padding:12px 16px;border-radius:8px;margin-bottom:16px}
.alert-err{background:#4a0d0d;border:1px solid #da3633;color:#f85149;padding:12px 16px;border-radius:8px;margin-bottom:16px}
.rfbtn{float:right;background:none;border:1px solid #30363d;color:#8b949e;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:40px;width:360px}
.login-box input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:11px 14px;border-radius:8px;font-size:15px;margin-bottom:12px;outline:none;font-family:inherit}
.login-box button{width:100%;background:#238636;color:#fff;border:none;padding:12px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
</style>
</head>
<body>
<div id="loginWrap" class="login-wrap" style="display:none">
  <div class="login-box">
    <div style="font-size:22px;font-weight:700;color:#58a6ff;text-align:center;margin-bottom:8px">🤖 Algo Trader</div>
    <div style="color:#8b949e;text-align:center;margin-bottom:28px">v2.0 — Shadow Mode</div>
    <input type="password" id="pw" placeholder="Password" onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()">Login</button>
    <div id="loginErr" style="color:#f85149;text-align:center;margin-top:10px;font-size:13px"></div>
  </div>
</div>
<div id="appWrap" style="display:none">
<nav class="nav">
  <div class="brand">🤖 Algo Trader <span>v2.0</span></div>
  <div>
    <a onclick="showPage('overview')" id="n-overview" class="active">Overview</a>
    <a onclick="showPage('signals')"  id="n-signals">Signals</a>
    <a onclick="showPage('params')"   id="n-params">Parameters</a>
    <a onclick="showPage('logs')"     id="n-logs">Logs</a>
    <a onclick="showPage('settings')" id="n-settings">Settings</a>
    <a onclick="logout()" class="logout">Logout</a>
  </div>
</nav>

<!-- OVERVIEW -->
<div id="overview" class="page active">
  <div class="g2">
    <div class="card">
      <div class="ct">Bot Status</div>
      <div class="status-row"><div class="dot r" id="dot"></div><div class="status-text" id="statusText">Loading...</div></div>
      <div class="btn-row">
        <button class="btn btn-start"   onclick="act('start')">▶ Start</button>
        <button class="btn btn-stop"    onclick="act('stop')">⏹ Stop</button>
        <button class="btn btn-restart" onclick="act('restart')">↺ Restart</button>
      </div>
    </div>
    <div class="card">
      <div class="ct">Performance</div>
      <div class="metrics">
        <div class="metric"><div class="mv" id="mTotal">0</div><div class="ml">Signals</div></div>
        <div class="metric"><div class="mv g" id="mIbkr">0</div><div class="ml">IBKR</div></div>
        <div class="metric"><div class="mv y" id="mEtoro">0</div><div class="ml">eToro</div></div>
      </div>
    </div>
  </div>
  <div class="card" style="margin-bottom:20px">
    <div class="ct">Recent Signals <button class="rfbtn" onclick="loadSig()">↻</button></div>
    <div id="sigWrap"><div style="color:#8b949e;text-align:center;padding:30px">No signals yet — scanning in shadow mode...</div></div>
  </div>
  <div class="card">
    <div class="ct">Live Log Feed <button class="rfbtn" onclick="loadLog()">↻</button></div>
    <div class="logbox" id="logbox">Loading...</div>
  </div>
</div>

<!-- SIGNALS -->
<div id="signals" class="page">
  <div class="card" style="margin-bottom:16px">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input class="inp" style="width:180px" placeholder="Search symbol..." id="sigSearch" oninput="renderAllSig()">
      <select class="inp" style="width:140px" id="sigRoute" onchange="renderAllSig()">
        <option value="">All Routes</option><option>ETORO</option><option>IBKR</option>
      </select>
      <select class="inp" style="width:140px" id="sigDir" onchange="renderAllSig()">
        <option value="">All Directions</option><option>long</option><option>short</option>
      </select>
      <button class="btn btn-restart" style="padding:7px 14px;font-size:13px" onclick="loadAllSig()">↻ Refresh</button>
      <span id="sigCount" style="color:#8b949e;font-size:13px;margin-left:auto"></span>
    </div>
  </div>
  <div class="card" style="overflow-x:auto"><div id="allSigWrap"></div></div>
</div>

<!-- PARAMETERS -->
<div id="params" class="page">
  <div class="card">
    <div class="ct">Strategy Parameters</div>
    <table>
      <tr><th>Parameter</th><th>Value</th><th>Description</th></tr>
      <tr><td>RSI Long Range</td><td>30 – 75</td><td>Momentum zone for long entries</td></tr>
      <tr><td>RSI Short Min</td><td>&gt; 50</td><td>Overbought for short entries</td></tr>
      <tr><td>MACD Histogram</td><td>&gt; 0 long / &lt; 0 short</td><td>Trend direction confirmation</td></tr>
      <tr><td>EMA 20/50 Trend</td><td>EMA20 &gt; EMA50 × 0.995</td><td>Uptrend alignment (0.5% tolerance)</td></tr>
      <tr><td>VWAP Deviation</td><td>Within ±1.5%</td><td>Price vs institutional benchmark</td></tr>
      <tr><td>Volume Ratio</td><td>&gt; 0.6× 20-bar avg</td><td>Participation confirmation</td></tr>
      <tr><td>OBV Slope</td><td>Positive/Negative</td><td>Volume-weighted price pressure</td></tr>
      <tr><td>ATR Period</td><td>14 bars</td><td>Volatility measure for stops</td></tr>
      <tr><td>Bollinger Bands</td><td>20 period, 2 std</td><td>Volatility envelope</td></tr>
      <tr><td>Focus Set</td><td>Top 150 symbols</td><td>Re-ranked every 6 hours</td></tr>
      <tr><td>Scan Cycle</td><td>Every 15 minutes</td><td>Full 4-TF scan</td></tr>
      <tr><td>eToro Signal</td><td>4/4 timeframes</td><td>Telegram alert only (manual trade)</td></tr>
      <tr><td>IBKR Signal</td><td>3/4 timeframes</td><td>Future automated execution</td></tr>
    </table>
  </div>
</div>

<!-- LOGS -->
<div id="logs" class="page">
  <div class="card" style="margin-bottom:16px">
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn" style="background:#21262d;font-size:13px" onclick="setLF('all')">All</button>
      <button class="btn" style="background:#21262d;font-size:13px" onclick="setLF('signals')">Signals Only</button>
      <button class="btn" style="background:#21262d;font-size:13px" onclick="setLF('errors')">Errors/Warnings</button>
      <button class="btn btn-restart" style="font-size:13px" onclick="loadFullLog()">↻ Refresh</button>
    </div>
  </div>
  <div class="card"><div class="logbox" id="fullLog" style="height:600px">Loading...</div></div>
</div>

<!-- SETTINGS -->
<div id="settings" class="page">
  <div class="card">
    <div class="ct">Settings.yaml — Edit & Save</div>
    <div id="settingsAlert"></div>
    <textarea id="yamlEditor" spellcheck="false">Loading...</textarea>
    <div style="display:flex;gap:12px;margin-top:12px">
      <button class="btn btn-start" style="padding:10px 28px;font-size:14px" onclick="saveSettings()">💾 Save & Restart Bot</button>
      <button class="btn" style="background:#21262d;padding:10px 28px;font-size:14px" onclick="loadSettings()">↺ Reset</button>
    </div>
  </div>
</div>
</div><!-- end appWrap -->

<script>
let _allSigs = [], _logFilter = 'all';

async function api(path, method='GET', body=null) {
  try {
    const r = await fetch('/api/'+path, {
      method, headers: body ? {'Content-Type':'application/json'} : {},
      body: body ? JSON.stringify(body) : null
    });
    return await r.json();
  } catch(e) { return {error: e.message}; }
}

async function login() {
  const pw = document.getElementById('pw').value;
  const r = await api('login', 'POST', {password: pw});
  if (r.ok) {
    document.getElementById('loginWrap').style.display = 'none';
    document.getElementById('appWrap').style.display = 'block';
    startApp();
  } else {
    document.getElementById('loginErr').textContent = 'Incorrect password';
  }
}

async function logout() {
  await api('logout', 'POST');
  location.reload();
}

function startApp() {
  loadAll();
  setInterval(loadAll, 25000);
}

function showPage(p) {
  document.querySelectorAll('.page').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.nav a').forEach(x => x.classList.remove('active'));
  document.getElementById(p).classList.add('active');
  document.getElementById('n-'+p).classList.add('active');
  if (p==='logs') loadFullLog();
  if (p==='settings') loadSettings();
  if (p==='signals') loadAllSig();
}

function loadAll() { loadStatus(); loadSig(); loadLog(); }

async function loadStatus() {
  const d = await api('status');
  const run = d.running;
  document.getElementById('dot').className = 'dot '+(run?'g':'r');
  document.getElementById('statusText').textContent = run ? 'Running — SHADOW mode' : 'Stopped';
  document.getElementById('mTotal').textContent = d.signal_count || 0;
  document.getElementById('mIbkr').textContent  = d.ibkr_count  || 0;
  document.getElementById('mEtoro').textContent = d.etoro_count || 0;
}

function sigRow(s) {
  return `<tr>
    <td>${(s.timestamp||'').slice(0,19)}</td>
    <td><b>${s.symbol}</b></td>
    <td><span class="tag tag-${s.direction}">${(s.direction||'').toUpperCase()}</span></td>
    <td><span class="tag tag-${(s.route||'').toLowerCase()}">${s.route}</span></td>
    <td>${s.valid_count}/4</td>
    <td style="color:${s.rsi>70?'#f85149':s.rsi<30?'#3fb950':'inherit'}">${(s.rsi||0).toFixed(1)}</td>
    <td style="color:${s.macd_hist>0?'#3fb950':'#f85149'}">${(s.macd_hist||0).toFixed(4)}</td>
    <td>${(s.ema20||0).toFixed(2)}</td>
    <td>${(s.ema50||0).toFixed(2)}</td>
    <td>${((s.vwap_dev||0)*100).toFixed(2)}%</td>
    <td>${(s.vol_ratio||0).toFixed(2)}x</td>
    <td>${(s.atr||0).toFixed(2)}</td>
    <td><b>$${(s.price||0).toFixed(2)}</b></td>
  </tr>`;
}

const sigHdr = `<table><tr>
  <th>Time</th><th>Symbol</th><th>Dir</th><th>Route</th><th>TFs</th>
  <th>RSI</th><th>MACD</th><th>EMA20</th><th>EMA50</th>
  <th>VWAP Dev</th><th>Vol Ratio</th><th>ATR</th><th>Price</th>
</tr>`;

async function loadSig() {
  const d = await api('signals?limit=10');
  const w = document.getElementById('sigWrap');
  if (!d.signals || !d.signals.length) {
    w.innerHTML = '<div style="color:#8b949e;text-align:center;padding:30px">No signals yet — bot is scanning in shadow mode...</div>';
    return;
  }
  w.innerHTML = sigHdr + d.signals.map(sigRow).join('') + '</table>';
}

async function loadAllSig() {
  const d = await api('signals?limit=500');
  _allSigs = d.signals || [];
  renderAllSig();
}

function renderAllSig() {
  const search = (document.getElementById('sigSearch').value||'').toLowerCase();
  const route  = document.getElementById('sigRoute').value;
  const dir    = document.getElementById('sigDir').value;
  const filtered = _allSigs.filter(s =>
    (!search || (s.symbol||'').toLowerCase().includes(search)) &&
    (!route  || s.route === route) &&
    (!dir    || s.direction === dir)
  );
  document.getElementById('sigCount').textContent = filtered.length + ' signals';
  const w = document.getElementById('allSigWrap');
  if (!filtered.length) { w.innerHTML = '<div style="color:#8b949e;padding:20px">No signals match filter.</div>'; return; }
  w.innerHTML = sigHdr + filtered.map(sigRow).join('') + '</table>';
}

function colorLine(l) {
  if (l.includes('SIGNAL')||l.includes('***')) return `<span style="color:#3fb950;font-weight:600">${l}</span>`;
  if (l.includes('ERROR')) return `<span style="color:#f85149">${l}</span>`;
  if (l.includes('WARNING')) return `<span style="color:#d29922">${l}</span>`;
  if (l.includes('OK')||l.includes('Focus set')||l.includes('Tier A done')||l.includes('Connectivity')) return `<span style="color:#58a6ff">${l}</span>`;
  return `<span style="color:#8b949e">${l}</span>`;
}

function setLF(f) { _logFilter = f; loadFullLog(); }

async function loadLog() {
  const d = await api('logs?lines=80');
  const el = document.getElementById('logbox');
  el.innerHTML = (d.lines||[]).map(colorLine).join('\n');
  el.scrollTop = el.scrollHeight;
}

async function loadFullLog() {
  const d = await api('logs?lines=500');
  let lines = d.lines||[];
  if (_logFilter==='signals') lines = lines.filter(l=>l.includes('SIGNAL')||l.includes('***'));
  if (_logFilter==='errors')  lines = lines.filter(l=>l.includes('ERROR')||l.includes('WARNING'));
  const el = document.getElementById('fullLog');
  el.innerHTML = lines.map(colorLine).join('\n');
  el.scrollTop = el.scrollHeight;
}

async function loadSettings() {
  const d = await api('settings');
  document.getElementById('yamlEditor').value = d.content || '';
}

async function saveSettings() {
  const content = document.getElementById('yamlEditor').value;
  const r = await api('settings', 'POST', {content});
  const el = document.getElementById('settingsAlert');
  el.innerHTML = r.ok
    ? '<div class="alert-ok">✅ Saved. Bot restarting...</div>'
    : `<div class="alert-err">❌ ${r.error||'Failed'}</div>`;
  setTimeout(()=>el.innerHTML='', 4000);
}

async function act(action) {
  await api(action, 'POST');
  setTimeout(loadStatus, 2500);
}

// Auto-detect login state
api('status').then(d => {
  if (!d.error && d.running !== undefined) {
    document.getElementById('loginWrap').style.display = 'none';
    document.getElementById('appWrap').style.display = 'block';
    startApp();
  } else {
    document.getElementById('loginWrap').style.display = 'flex';
  }
}).catch(()=> document.getElementById('loginWrap').style.display = 'flex');
</script>
</body>
</html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    if data.get('password') == get_password():
        session['authed'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/status')
@auth_required
def status():
    running = bool(subprocess.run(['pgrep','-f','main.py'], capture_output=True).stdout.strip())
    total = ibkr = etoro = 0
    try:
        db = sqlite3.connect(DB_PATH)
        total = db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        ibkr  = db.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        etoro = db.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        db.close()
    except: pass
    return jsonify({'running': running, 'signal_count': total,
                    'ibkr_count': ibkr, 'etoro_count': etoro})

@app.route('/api/signals')
@auth_required
def signals():
    limit = min(int(request.args.get('limit', 20)), 500)
    try:
        db   = sqlite3.connect(DB_PATH)
        rows = db.execute('SELECT * FROM signals ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        cols = [d[0] for d in db.execute('PRAGMA table_info(signals)').fetchall()]
        db.close()
        return jsonify({'signals': [dict(zip(cols, r)) for r in rows]})
    except Exception as e:
        return jsonify({'signals': [], 'error': str(e)})

@app.route('/api/logs')
@auth_required
def logs():
    lines = min(int(request.args.get('lines', 100)), 500)
    try:
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
        return jsonify({'lines': [l.rstrip() for l in all_lines[-lines:]]})
    except:
        return jsonify({'lines': ['Log file not found']})

@app.route('/api/settings', methods=['GET'])
@auth_required
def get_settings():
    try:
        with open(CONFIG_PATH) as f:
            return jsonify({'content': f.read()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings', methods=['POST'])
@auth_required
def save_settings():
    data = request.get_json(silent=True) or {}
    content = data.get('content', '')
    try:
        import yaml as _yaml
        _yaml.safe_load(content)  # validate
        with open(CONFIG_PATH, 'w') as f:
            f.write(content)
        # restart bot only
        subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
        import time; time.sleep(1)
        venv = f'{BASE}/venv/bin/python3'
        subprocess.Popen([venv, f'{BASE}/main.py'],
                         stdout=open(f'{BASE}/logs/bot.log','a'),
                         stderr=subprocess.STDOUT,
                         start_new_session=True)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/api/start', methods=['POST'])
@auth_required
def start():
    subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
    import time; time.sleep(1)
    venv = f'{BASE}/venv/bin/python3'
    subprocess.Popen([venv, f'{BASE}/main.py'],
                     stdout=open(f'{BASE}/logs/bot.log','a'),
                     stderr=subprocess.STDOUT,
                     start_new_session=True)
    return jsonify({'ok': True})

@app.route('/api/stop', methods=['POST'])
@auth_required
def stop():
    subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
    return jsonify({'ok': True})

@app.route('/api/restart', methods=['POST'])
@auth_required
def restart():
    def _do():
        import time
        time.sleep(1)
        subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
        time.sleep(1)
        venv = f'{BASE}/venv/bin/python3'
        subprocess.Popen([venv, f'{BASE}/main.py'],
                         stdout=open(f'{BASE}/logs/bot.log','a'),
                         stderr=subprocess.STDOUT,
                         start_new_session=True)
    import threading
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)

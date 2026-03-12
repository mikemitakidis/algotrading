#!/usr/bin/env python3
"""
Algo Trader Dashboard - Password Protected
Run: python3 dashboard.py
Access: http://138.199.196.95:8080
Password: AlgoTrader2024!
"""
import os, json, subprocess, sqlite3, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

LOG_FILE = "/opt/algo-trader/logs/bot.log"
SETTINGS_FILE = "/opt/algo-trader/config/settings.yaml"
DB_FILE = "/opt/algo-trader/data/signals.db"
BOT_SCRIPT = "/opt/algo-trader/main.py"
VENV_PYTHON = "/opt/algo-trader/venv/bin/python3"

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Algo Trader Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
#login{display:flex;align-items:center;justify-content:center;min-height:100vh;background:linear-gradient(135deg,#0d1117 0%,#161b22 100%)}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:16px;padding:48px 40px;width:360px;text-align:center;box-shadow:0 24px 48px rgba(0,0,0,.5)}
.logo{font-size:48px;margin-bottom:12px}
.login-box h1{color:#58a6ff;margin-bottom:6px;font-size:24px;font-weight:600}
.login-box p{color:#8b949e;margin-bottom:28px;font-size:14px}
input[type=password]{width:100%;padding:13px 16px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:15px;margin-bottom:12px;outline:none;transition:border .2s}
input[type=password]:focus{border-color:#58a6ff}
.login-btn{width:100%;padding:13px;background:linear-gradient(135deg,#238636,#2ea043);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:15px;font-weight:600;letter-spacing:.3px;transition:opacity .2s}
.login-btn:hover{opacity:.9}
.err{color:#f85149;font-size:13px;margin-top:10px;min-height:20px}
#app{display:none}
.nav{background:#161b22;border-bottom:1px solid #30363d;padding:0 24px;display:flex;align-items:center;gap:4px;height:56px;position:sticky;top:0;z-index:10}
.nav-title{color:#58a6ff;font-size:17px;font-weight:600;margin-right:auto;display:flex;align-items:center;gap:8px}
.nav-btn{background:none;border:none;color:#8b949e;cursor:pointer;padding:7px 13px;border-radius:6px;font-size:13px;font-weight:500;transition:all .15s}
.nav-btn:hover{background:#21262d;color:#e6edf3}
.nav-btn.active{background:#21262d;color:#e6edf3}
.nav-logout{color:#f85149!important}
main{padding:24px;max-width:1200px;margin:0 auto}
.page{display:none}.page.active{display:block}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:720px){.grid2{grid-template-columns:1fr}}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px;margin-bottom:16px}
.card-title{font-size:11px;color:#8b949e;margin-bottom:14px;text-transform:uppercase;letter-spacing:.7px;font-weight:600}
.status-row{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.dot{width:12px;height:12px;border-radius:50%;flex-shrink:0;transition:background .3s}
.dot.on{background:#3fb950;box-shadow:0 0 8px rgba(63,185,80,.6)}
.dot.off{background:#da3633}
.dot.loading{background:#d29922;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.status-txt{font-size:17px;font-weight:500}
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.ab{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;transition:all .15s;letter-spacing:.2px}
.ab:hover{filter:brightness(1.1)}
.ag{background:#238636;color:#fff}.ar{background:#da3633;color:#fff}
.ab2{background:#1f6feb;color:#fff}.agr{background:#21262d;color:#e6edf3;border:1px solid #30363d}
pre{background:#010409;padding:14px;border-radius:8px;font-size:11.5px;overflow:auto;max-height:420px;white-space:pre-wrap;line-height:1.6;font-family:'Consolas','Monaco',monospace}
.metrics{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0}
.metric{text-align:center;padding:14px 8px}
.metric .val{font-size:30px;font-weight:700;color:#58a6ff;line-height:1}
.metric .lbl{font-size:11px;color:#8b949e;margin-top:5px;text-transform:uppercase;letter-spacing:.4px}
.val.green{color:#3fb950}.val.red{color:#f85149}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 8px;color:#8b949e;border-bottom:1px solid #30363d;font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.3px}
td{padding:10px 8px;border-bottom:1px solid #21262d}
tr:last-child td{border-bottom:none}
tr:hover td{background:#21262d}
.badge{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600}
.buy{background:#0f2a1a;color:#3fb950}.sell{background:#2a0f0f;color:#f85149}
.etoro{background:#0a1a2e;color:#58a6ff}.ibkr{background:#1a1a0a;color:#d29922}
textarea{width:100%;background:#010409;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:14px;font-family:'Consolas','Monaco',monospace;font-size:12.5px;line-height:1.6;resize:vertical;outline:none;transition:border .2s}
textarea:focus{border-color:#58a6ff}
.save-btn{padding:10px 22px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;margin-top:10px;transition:all .15s}
.save-btn:hover{background:#2ea043}
.toast{position:fixed;bottom:24px;right:24px;padding:12px 20px;border-radius:8px;font-size:14px;display:none;z-index:999;color:#fff;font-weight:500;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.empty{color:#8b949e;text-align:center;padding:32px;font-size:14px}
.offline-banner{background:#21262d;border:1px solid #30363d;border-radius:8px;padding:16px;text-align:center;color:#8b949e;font-size:14px}
.section-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.section-row h2{font-size:18px;font-weight:600}
.param-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:720px){.param-grid{grid-template-columns:1fr}}
.param-item{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:14px}
.param-label{font-size:11px;color:#8b949e;margin-bottom:6px;text-transform:uppercase;letter-spacing:.4px}
.param-val{font-size:15px;font-weight:500;font-family:monospace}
.param-val.num{color:#58a6ff}.param-val.str{color:#e6edf3}.param-val.bool-t{color:#3fb950}.param-val.bool-f{color:#f85149}
</style>
</head>
<body>
<div id="login">
  <div class="login-box">
    <div class="logo">🤖</div>
    <h1>Algo Trader</h1>
    <p>Private dashboard — access restricted</p>
    <input type="password" id="pw" placeholder="Enter password" onkeydown="if(event.key==='Enter')login()">
    <button class="login-btn" onclick="login()">Login →</button>
    <div class="err" id="err"></div>
  </div>
</div>

<div id="app">
  <nav class="nav">
    <div class="nav-title">🤖 Algo Trader <span style="font-size:11px;color:#8b949e;font-weight:400">v2.0</span></div>
    <button class="nav-btn active" id="nav-overview" onclick="showPage('overview',this)">Overview</button>
    <button class="nav-btn" id="nav-signals" onclick="showPage('signals',this)">Signals</button>
    <button class="nav-btn" id="nav-params" onclick="showPage('params',this)">Parameters</button>
    <button class="nav-btn" id="nav-logs" onclick="showPage('logs',this)">Logs</button>
    <button class="nav-btn" id="nav-settings" onclick="showPage('settings',this)">Settings</button>
    <button class="nav-btn nav-logout" onclick="logout()">Logout</button>
  </nav>
  <main>

    <!-- OVERVIEW -->
    <div class="page active" id="page-overview">
      <div class="grid2">
        <div class="card">
          <div class="card-title">Bot Status</div>
          <div class="status-row">
            <div class="dot loading" id="sdot"></div>
            <span class="status-txt" id="stxt">Connecting...</span>
          </div>
          <div class="btn-row">
            <button class="ab ag" onclick="botAction('start')">▶ Start</button>
            <button class="ab ar" onclick="botAction('stop')">⏹ Stop</button>
            <button class="ab ab2" onclick="botAction('restart')">↺ Restart</button>
          </div>
        </div>
        <div class="card">
          <div class="card-title">Performance</div>
          <div class="metrics">
            <div class="metric"><div class="val" id="s-total">—</div><div class="lbl">Signals</div></div>
            <div class="metric"><div class="val green" id="s-wins">—</div><div class="lbl">Wins</div></div>
            <div class="metric"><div class="val" id="s-wr">—</div><div class="lbl">Win Rate</div></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="section-row"><div class="card-title" style="margin:0">Recent Signals</div><button class="ab agr" style="font-size:11px;padding:5px 10px" onclick="loadOverviewSigs()">↺</button></div>
        <div id="ov-sigs"><div class="empty">Loading...</div></div>
      </div>
      <div class="card">
        <div class="section-row"><div class="card-title" style="margin:0">Live Log Feed</div><button class="ab agr" style="font-size:11px;padding:5px 10px" onclick="loadOvLogs()">↺</button></div>
        <pre id="ov-logs">Connecting to server...</pre>
      </div>
    </div>

    <!-- SIGNALS -->
    <div class="page" id="page-signals">
      <div class="card">
        <div class="section-row">
          <div class="card-title" style="margin:0">All Signals</div>
          <button class="ab agr" style="font-size:11px;padding:5px 10px" onclick="loadAllSigs()">↺ Refresh</button>
        </div>
        <div id="all-sigs"><div class="empty">Loading...</div></div>
      </div>
    </div>

    <!-- PARAMETERS -->
    <div class="page" id="page-params">
      <div class="card">
        <div class="card-title">Bot Parameters (from settings.yaml)</div>
        <div id="params-grid"><div class="empty">Loading...</div></div>
      </div>
    </div>

    <!-- LOGS -->
    <div class="page" id="page-logs">
      <div class="card">
        <div class="section-row">
          <div class="card-title" style="margin:0">Bot Logs</div>
          <button class="ab agr" onclick="loadLogs()">↺ Refresh</button>
        </div>
        <pre id="full-logs">Loading...</pre>
      </div>
    </div>

    <!-- SETTINGS -->
    <div class="page" id="page-settings">
      <div class="card">
        <div class="card-title">settings.yaml — Edit and save to apply changes</div>
        <textarea id="settings-txt" rows="28" placeholder="Loading..."></textarea>
        <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
          <button class="save-btn" onclick="saveSettings()">💾 Save & Restart Bot</button>
          <span style="font-size:12px;color:#8b949e">Changes apply immediately</span>
        </div>
      </div>
    </div>

  </main>
</div>

<div class="toast" id="toast"></div>

<script>
const SERVER = 'http://138.199.196.95:8080';
const PASS = 'AlgoTrader2024!';

function login() {
  const v = document.getElementById('pw').value;
  if (v === PASS) {
    sessionStorage.setItem('auth','1');
    document.getElementById('login').style.display = 'none';
    document.getElementById('app').style.display = 'block';
    init();
  } else {
    document.getElementById('err').textContent = 'Incorrect password. Try again.';
    document.getElementById('pw').value = '';
    document.getElementById('pw').focus();
  }
}
function logout() { sessionStorage.removeItem('auth'); location.reload(); }

if (sessionStorage.getItem('auth')) {
  document.getElementById('login').style.display = 'none';
  document.getElementById('app').style.display = 'block';
}

function showPage(n, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + n).classList.add('active');
  btn.classList.add('active');
  if (n === 'logs') loadLogs();
  if (n === 'signals') loadAllSigs();
  if (n === 'settings') loadSettings();
  if (n === 'params') loadParams();
}

function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = ok ? '#238636' : '#da3633';
  t.style.display = 'block';
  setTimeout(() => t.style.display='none', 3000);
}

async function api(path, method='GET', body=null) {
  try {
    const o = { method, headers:{'Content-Type':'application/json'} };
    if (body) o.body = JSON.stringify(body);
    const r = await fetch(SERVER + path, o);
    if (!r.ok) return null;
    return await r.json();
  } catch(e) { return null; }
}

async function loadStatus() {
  const d = await api('/api/status');
  const dot = document.getElementById('sdot');
  const txt = document.getElementById('stxt');
  if (d && d.running) {
    dot.className = 'dot on'; txt.textContent = 'Running — SHADOW mode';
  } else if (d) {
    dot.className = 'dot off'; txt.textContent = 'Stopped';
  } else {
    dot.className = 'dot off'; txt.textContent = '⚠️ Server offline — run: bash start.sh';
  }
}

async function botAction(a) {
  toast('Sending ' + a + ' command...');
  const d = await api('/api/' + a, 'POST');
  toast(d ? '✅ Bot ' + a + ' successful!' : '❌ Server not reachable', !!d);
  setTimeout(loadStatus, 2500);
}

function sigsTable(sigs) {
  if (!sigs || !sigs.length) return '<div class="empty">No signals yet — bot is scanning in shadow mode...</div>';
  return `<table>
    <tr><th>Time</th><th>Symbol</th><th>Direction</th><th>Broker</th><th>Entry</th><th>Stop Loss</th><th>Target</th><th>Result</th></tr>
    ${sigs.map(s => `<tr>
      <td style="color:#8b949e">${(s.ts||'').substring(0,16).replace('T',' ')}</td>
      <td><strong>${s.symbol}</strong></td>
      <td><span class="badge ${(s.direction||'').toLowerCase()}">${s.direction}</span></td>
      <td><span class="badge ${(s.broker||'').toLowerCase()}">${s.broker}</span></td>
      <td>$${(+s.entry||0).toFixed(3)}</td>
      <td style="color:#f85149">$${(+s.sl||0).toFixed(3)}</td>
      <td style="color:#3fb950">$${(+s.tp||0).toFixed(3)}</td>
      <td>${s.outcome===1?'✅ WIN':s.outcome===0?'❌ LOSS':'⏳ Open'}</td>
    </tr>`).join('')}
  </table>`;
}

async function loadOverviewSigs() {
  const d = await api('/api/signals');
  if (d && d.signals) {
    const s = d.signals, t = s.length;
    const w = s.filter(x=>x.outcome===1).length;
    const lab = s.filter(x=>x.outcome!==-1).length;
    document.getElementById('s-total').textContent = t;
    document.getElementById('s-wins').textContent = w;
    const wr = lab ? Math.round(w/lab*100)+'%' : 'N/A';
    document.getElementById('s-wr').textContent = wr;
    document.getElementById('ov-sigs').innerHTML = sigsTable(s.slice(0,8));
  } else {
    document.getElementById('ov-sigs').innerHTML = '<div class="offline-banner">⚠️ Cannot reach server API. Start the bot first.</div>';
  }
}

async function loadAllSigs() {
  const d = await api('/api/signals?limit=500');
  document.getElementById('all-sigs').innerHTML = d && d.signals ? sigsTable(d.signals) : '<div class="offline-banner">⚠️ Server not reachable</div>';
}

async function loadOvLogs() {
  const d = await api('/api/logs?lines=30');
  const el = document.getElementById('ov-logs');
  el.textContent = d ? d.logs : '⚠️ Server not reachable.\\n\\nThe bot server needs to be running.\\nIn VS Code terminal, type:\\n\\n  bash start.sh';
  el.scrollTop = el.scrollHeight;
}

async function loadLogs() {
  const d = await api('/api/logs?lines=200');
  const el = document.getElementById('full-logs');
  el.textContent = d ? d.logs : '⚠️ Server not reachable';
  el.scrollTop = el.scrollHeight;
}

async function loadParams() {
  const d = await api('/api/settings');
  const el = document.getElementById('params-grid');
  if (!d) { el.innerHTML = '<div class="offline-banner">⚠️ Server not reachable</div>'; return; }
  // Parse YAML-like params to display
  const params = [];
  const lines = d.content.split('\\n');
  lines.forEach(line => {
    const m = line.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\\s*:\\s*(.+)$/);
    if (m && !line.startsWith('#') && !line.startsWith(' ')) {
      params.push({ key: m[1], val: m[2].trim() });
    }
  });
  if (!params.length) { el.innerHTML = '<div class="empty">No parameters found</div>'; return; }
  el.innerHTML = '<div class="param-grid">' + params.map(p => {
    const v = p.val.replace(/['"]/g,'');
    let cls = 'str';
    if (!isNaN(v)) cls = 'num';
    else if (v === 'true' || v === 'yes') cls = 'bool-t';
    else if (v === 'false' || v === 'no') cls = 'bool-f';
    return `<div class="param-item"><div class="param-label">${p.key}</div><div class="param-val ${cls}">${v}</div></div>`;
  }).join('') + '</div>';
}

async function loadSettings() {
  const d = await api('/api/settings');
  document.getElementById('settings-txt').value = d ? d.content : '# Server not reachable';
}

async function saveSettings() {
  const c = document.getElementById('settings-txt').value;
  const d = await api('/api/settings','POST',{content:c});
  if (d && d.ok) {
    toast('✅ Saved! Restarting bot...');
    setTimeout(() => botAction('restart'), 1200);
  } else {
    toast('❌ Failed — server not reachable', false);
  }
}

function init() {
  loadStatus(); loadOverviewSigs(); loadOvLogs();
  setInterval(loadStatus, 6000);
  setInterval(loadOvLogs, 20000);
  setInterval(loadOverviewSigs, 45000);
}
if (sessionStorage.getItem('auth')) init();
</script>
</body>
</html>
"""

def is_bot_running():
    try:
        r = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True)
        return bool(r.stdout.strip())
    except: return False

def get_signals(limit=100):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))]
        conn.close()
        return rows
    except Exception as e:
        return []

def get_logs(lines=100):
    try:
        r = subprocess.run(["tail", f"-{lines}", LOG_FILE], capture_output=True, text=True)
        return r.stdout
    except: return "Log file not found"

def get_settings():
    try: return open(SETTINGS_FILE).read()
    except: return "# Could not read settings.yaml"

def save_settings(content):
    try: open(SETTINGS_FILE, "w").write(content); return True
    except: return False

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self.send_json({"running": is_bot_running()})
        elif path == "/api/signals":
            limit = int(params.get("limit", [100])[0])
            self.send_json({"signals": get_signals(limit)})
        elif path == "/api/logs":
            lines = int(params.get("lines", [100])[0])
            self.send_json({"logs": get_logs(lines)})
        elif path == "/api/settings":
            self.send_json({"content": get_settings()})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/start":
            if not is_bot_running():
                log_f = open(LOG_FILE, "a")
                subprocess.Popen([VENV_PYTHON, BOT_SCRIPT], stdout=log_f, stderr=log_f, cwd="/opt/algo-trader")
            self.send_json({"ok": True})
        elif path == "/api/stop":
            subprocess.run(["pkill", "-f", "main.py"])
            self.send_json({"ok": True})
        elif path == "/api/restart":
            subprocess.run(["pkill", "-f", "main.py"])
            time.sleep(1)
            log_f = open(LOG_FILE, "a")
            subprocess.Popen([VENV_PYTHON, BOT_SCRIPT], stdout=log_f, stderr=log_f, cwd="/opt/algo-trader")
            self.send_json({"ok": True})
        elif path == "/api/settings":
            ok = save_settings(body.get("content", ""))
            self.send_json({"ok": ok})
        else:
            self.send_json({"error": "not found"}, 404)

if __name__ == "__main__":
    print("🤖 Algo Trader Dashboard")
    print("   URL:      http://138.199.196.95:8080")
    print("   Password: AlgoTrader2024!")
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")

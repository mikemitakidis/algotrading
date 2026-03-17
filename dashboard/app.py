"""
dashboard/app.py
Flask dashboard. Reads bot.log and signals.db only.
No backtick JS template literals — uses string concatenation for safety.
"""
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, session, render_template_string
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

app = Flask(__name__)
_pw = os.getenv('DASHBOARD_PASSWORD', 'changeme')
app.secret_key = _pw + '_algo_session'

LOG_PATH = BASE_DIR / 'logs' / 'bot.log'
DB_PATH  = BASE_DIR / 'data' / 'signals.db'


def get_password():
    return os.getenv('DASHBOARD_PASSWORD', 'changeme')


def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


HTML = (
    '<!DOCTYPE html>'
    '<html lang="en">'
    '<head>'
    '<meta charset="UTF-8">'
    '<title>Algo Trader v1</title>'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<style>'
    '*{box-sizing:border-box;margin:0;padding:0}'
    'body{background:#0d1117;color:#e6edf3;font-family:Segoe UI,Arial,sans-serif;min-height:100vh}'
    'nav{background:#161b22;border-bottom:1px solid #30363d;padding:0 24px;display:flex;align-items:center;justify-content:space-between;height:52px}'
    '.brand{font-size:17px;font-weight:700;color:#58a6ff}'
    '.brand em{background:#1f6feb;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px;font-style:normal}'
    'nav a{color:#8b949e;text-decoration:none;padding:7px 13px;border-radius:6px;font-size:14px;cursor:pointer}'
    'nav a:hover,nav a.active{background:#21262d;color:#e6edf3}'
    'nav a.out{color:#f85149}'
    '.page{display:none;padding:24px;max-width:1400px;margin:0 auto}'
    '.page.on{display:block}'
    '.g2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}'
    '.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:22px}'
    '.ct{font-size:11px;font-weight:600;color:#8b949e;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px}'
    '.sr{display:flex;align-items:center;gap:10px;margin-bottom:14px}'
    '.dot{width:12px;height:12px;border-radius:50%}'
    '.dot.g{background:#3fb950;box-shadow:0 0 7px #3fb950}'
    '.dot.r{background:#f85149}'
    '.st{font-size:19px;font-weight:600}'
    '.br{display:flex;gap:10px;flex-wrap:wrap}'
    '.btn{padding:8px 16px;border:none;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}'
    '.gs{background:#238636;color:#fff}.rs{background:#da3633;color:#fff}.bl{background:#1f6feb;color:#fff}.gy{background:#21262d;color:#e6edf3}'
    '.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:#30363d;border-radius:8px;overflow:hidden}'
    '.metric{background:#161b22;padding:16px;text-align:center}'
    '.mv{font-size:30px;font-weight:700;color:#58a6ff}'
    '.mv.g{color:#3fb950}.mv.y{color:#d29922}'
    '.ml{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-top:3px}'
    '.logbox{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:14px;font-family:Courier New,monospace;font-size:12px;height:380px;overflow-y:auto;line-height:1.6;white-space:pre-wrap}'
    'table{width:100%;border-collapse:collapse;font-size:13px}'
    'th{background:#21262d;color:#8b949e;padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.7px}'
    'td{padding:9px 12px;border-top:1px solid #21262d}'
    '.tag{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}'
    '.tl{background:#0d4a1a;color:#3fb950}.ts{background:#4a0d0d;color:#f85149}'
    '.te{background:#2d1f00;color:#d29922}.ti{background:#0d2d5a;color:#58a6ff}'
    '.rfbtn{float:right;background:none;border:1px solid #30363d;color:#8b949e;padding:4px 11px;border-radius:6px;cursor:pointer;font-size:12px}'
    '.login{display:flex;align-items:center;justify-content:center;min-height:100vh}'
    '.lbox{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:38px;width:340px}'
    '.lbox input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:10px 13px;border-radius:7px;font-size:15px;margin-bottom:12px;outline:none;font-family:inherit}'
    '.lbox input:focus{border-color:#58a6ff}'
    '.lbox button{width:100%;background:#238636;color:#fff;border:none;padding:11px;border-radius:7px;font-size:15px;font-weight:600;cursor:pointer}'
    '</style>'
    '</head>'
    '<body>'
    
    '<div id="loginWrap" class="login">'
    '<div class="lbox">'
    '<div style="font-size:21px;font-weight:700;color:#58a6ff;text-align:center;margin-bottom:6px">&#x1F916; Algo Trader</div>'
    '<div style="color:#8b949e;text-align:center;margin-bottom:26px">v1.0 &mdash; Shadow Mode</div>'
    '<input type="password" id="pw" placeholder="Password" onkeydown="if(event.key===\'Enter\')doLogin()">'
    '<button onclick="doLogin()">Login</button>'
    '<div id="lerr" style="color:#f85149;text-align:center;margin-top:9px;font-size:13px"></div>'
    '</div>'
    '</div>'
    
    '<div id="appWrap" style="display:none">'
    '<nav>'
    '<div class="brand">&#x1F916; Algo Trader <em>v1.0</em></div>'
    '<div>'
    '<a onclick="go(\'overview\')" id="n-overview" class="active">Overview</a>'
    '<a onclick="go(\'signals\')"  id="n-signals">Signals</a>'
    '<a onclick="go(\'logs\')"     id="n-logs">Logs</a>'
    '<a onclick="doLogout()" class="out">Logout</a>'
    '</div>'
    '</nav>'
    
    '<div id="overview" class="page on">'
    '<div class="g2">'
    '<div class="card">'
    '<div class="ct">Bot Status</div>'
    '<div class="sr"><div class="dot r" id="dot"></div><div class="st" id="stText">Loading...</div></div>'
    '<div class="br">'
    '<button class="btn gs" onclick="act(\'start\')">&#x25B6; Start</button>'
    '<button class="btn rs" onclick="act(\'stop\')">&#x23F9; Stop</button>'
    '<button class="btn bl" onclick="act(\'restart\')">&#x21BA; Restart</button>'
    '</div>'
    '</div>'
    '<div class="card">'
    '<div class="ct">Signals</div>'
    '<div class="metrics">'
    '<div class="metric"><div class="mv" id="mT">0</div><div class="ml">Total</div></div>'
    '<div class="metric"><div class="mv g" id="mI">0</div><div class="ml">IBKR</div></div>'
    '<div class="metric"><div class="mv y" id="mE">0</div><div class="ml">eToro</div></div>'
    '</div>'
    '</div>'
    '</div>'
    
    '<div class="card" style="margin-bottom:20px">'
    '<div class="ct">Recent Signals <button class="rfbtn" onclick="loadSig()">&#x21BB;</button></div>'
    '<div id="sigWrap"><div style="color:#8b949e;text-align:center;padding:28px">No signals yet &mdash; bot is scanning...</div></div>'
    '</div>'
    
    '<div class="card">'
    '<div class="ct">Live Log <button class="rfbtn" onclick="loadLog()">&#x21BB;</button></div>'
    '<div class="logbox" id="logbox">Loading...</div>'
    '</div>'
    '</div>'
    
    '<div id="signals" class="page">'
    '<div class="card" style="margin-bottom:16px">'
    '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
    '<input style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 12px;border-radius:7px;font-size:13px;width:160px;font-family:inherit" placeholder="Symbol..." id="sfilt" oninput="renderSig()">'
    '<select style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 12px;border-radius:7px;font-size:13px;font-family:inherit" id="rfilt" onchange="renderSig()">'
    '<option value="">All Routes</option><option>ETORO</option><option>IBKR</option>'
    '</select>'
    '<select style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 12px;border-radius:7px;font-size:13px;font-family:inherit" id="dfilt" onchange="renderSig()">'
    '<option value="">All Directions</option><option>long</option><option>short</option>'
    '</select>'
    '<button class="btn bl" style="font-size:12px;padding:7px 13px" onclick="loadAllSig()">&#x21BB; Refresh</button>'
    '<span id="scount" style="color:#8b949e;font-size:13px;margin-left:auto"></span>'
    '</div>'
    '</div>'
    '<div class="card" style="overflow-x:auto"><div id="allSig"></div></div>'
    '</div>'
    
    '<div id="logs" class="page">'
    '<div class="card" style="margin-bottom:16px">'
    '<div style="display:flex;gap:10px;flex-wrap:wrap">'
    '<button class="btn gy" onclick="setLF(\'all\')">All</button>'
    '<button class="btn gy" onclick="setLF(\'sig\')">Signals</button>'
    '<button class="btn gy" onclick="setLF(\'err\')">Errors/Warnings</button>'
    '<button class="btn bl" onclick="loadFullLog()">&#x21BB; Refresh</button>'
    '</div>'
    '</div>'
    '<div class="card"><div class="logbox" id="fullLog" style="height:580px">Loading...</div></div>'
    '</div>'
    '</div>'
    
    '<script>'
    'var _sigs=[], _lf="all";'
    
    'function doLogin(){'
    '  var pw=document.getElementById("pw").value;'
    '  if(!pw){document.getElementById("lerr").textContent="Enter a password";return;}'
    '  document.getElementById("lerr").textContent="Checking...";'
    '  fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:pw})})'
    '  .then(function(r){return r.json();})'
    '  .then(function(d){'
    '    if(d.ok===true){'
    '      document.getElementById("loginWrap").style.display="none";'
    '      document.getElementById("appWrap").style.display="block";'
    '      boot();'
    '    }else{'
    '      document.getElementById("lerr").textContent="Incorrect password";'
    '    }'
    '  })'
    '  .catch(function(e){document.getElementById("lerr").textContent="Error: "+e.message;});'
    '}'
    
    'function doLogout(){'
    '  fetch("/api/logout",{method:"POST"}).then(function(){location.reload();});'
    '}'
    
    'function boot(){loadAll();setInterval(loadAll,25000);}'
    
    'function go(p){'
    '  document.querySelectorAll(".page").forEach(function(x){x.classList.remove("on");});'
    '  document.querySelectorAll("nav a").forEach(function(x){x.classList.remove("active");});'
    '  document.getElementById(p).classList.add("on");'
    '  var n=document.getElementById("n-"+p);'
    '  if(n)n.classList.add("active");'
    '  if(p==="logs")loadFullLog();'
    '  if(p==="signals")loadAllSig();'
    '}'
    
    'function loadAll(){loadStatus();loadSig();loadLog();}'
    
    'function loadStatus(){'
    '  fetch("/api/status").then(function(r){return r.json();}).then(function(d){'
    '    document.getElementById("dot").className="dot "+(d.running?"g":"r");'
    '    document.getElementById("stText").textContent=d.running?"Running - SHADOW mode":"Stopped";'
    '    document.getElementById("mT").textContent=(d.counts&&d.counts.total)||0;'
    '    document.getElementById("mI").textContent=(d.counts&&d.counts.ibkr)||0;'
    '    document.getElementById("mE").textContent=(d.counts&&d.counts.etoro)||0;'
    '  }).catch(function(){});'
    '}'
    
    'function makeRow(s){'
    '  var tc="tag t"+(s.direction==="long"?"l":"s");'
    '  var rc="tag t"+(s.route==="ETORO"?"e":"i");'
    '  var ts=(s.timestamp||"").slice(0,19);'
    '  var rsi=(s.rsi||0).toFixed(1);'
    '  var macd=(s.macd_hist||0).toFixed(4);'
    '  var atr=(s.atr||0).toFixed(2);'
    '  var price="$"+(s.price||0).toFixed(2);'
    '  return "<tr><td>"+ts+"</td><td><b>"+s.symbol+"</b></td>"'
    '    +"<td><span class=\'"+tc+"\'>"+s.direction.toUpperCase()+"</span></td>"'
    '    +"<td><span class=\'"+rc+"\'>"+s.route+"</span></td>"'
    '    +"<td>"+s.valid_count+"/4</td>"'
    '    +"<td>"+rsi+"</td><td>"+macd+"</td><td>"+atr+"</td><td><b>"+price+"</b></td></tr>";'
    '}'
    
    'var HDR="<table><tr><th>Time</th><th>Symbol</th><th>Dir</th><th>Route</th><th>TFs</th><th>RSI</th><th>MACD</th><th>ATR</th><th>Price</th></tr>";'
    
    'function loadSig(){'
    '  fetch("/api/signals?limit=10").then(function(r){return r.json();}).then(function(d){'
    '    var w=document.getElementById("sigWrap");'
    '    var sigs=d.signals||[];'
    '    if(!sigs.length){w.innerHTML="<div style=\'color:#8b949e;text-align:center;padding:28px\'>No signals yet</div>";return;}'
    '    w.innerHTML=HDR+sigs.map(makeRow).join("")+"</table>";'
    '  }).catch(function(){});'
    '}'
    
    'function loadAllSig(){'
    '  fetch("/api/signals?limit=500").then(function(r){return r.json();}).then(function(d){'
    '    _sigs=d.signals||[];renderSig();'
    '  }).catch(function(){});'
    '}'
    
    'function renderSig(){'
    '  var s=(document.getElementById("sfilt").value||"").toLowerCase();'
    '  var r=document.getElementById("rfilt").value;'
    '  var d=document.getElementById("dfilt").value;'
    '  var f=_sigs.filter(function(x){'
    '    return(!s||(x.symbol||"").toLowerCase().indexOf(s)>=0)&&(!r||x.route===r)&&(!d||x.direction===d);'
    '  });'
    '  document.getElementById("scount").textContent=f.length+" signals";'
    '  var w=document.getElementById("allSig");'
    '  if(!f.length){w.innerHTML="<div style=\'color:#8b949e;padding:18px\'>No signals match filter.</div>";return;}'
    '  w.innerHTML=HDR+f.map(makeRow).join("")+"</table>";'
    '}'
    
    'function colorLine(l){'
    '  if(l.indexOf("[SIGNAL]")>=0||l.indexOf("ETORO")>=0||l.indexOf("IBKR")>=0)'
    '    return "<span style=\'color:#3fb950;font-weight:600\'>"+l+"</span>";'
    '  if(l.indexOf("ERROR")>=0)'
    '    return "<span style=\'color:#f85149\'>"+l+"</span>";'
    '  if(l.indexOf("WARNING")>=0)'
    '    return "<span style=\'color:#d29922\'>"+l+"</span>";'
    '  if(l.indexOf("[TIER-A]")>=0||l.indexOf("[CYCLE]")>=0||l.indexOf("yfinance OK")>=0)'
    '    return "<span style=\'color:#58a6ff\'>"+l+"</span>";'
    '  return "<span style=\'color:#8b949e\'>"+l+"</span>";'
    '}'
    
    'function setLF(f){_lf=f;loadFullLog();}'
    
    'function loadLog(){'
    '  fetch("/api/logs?lines=80").then(function(r){return r.json();}).then(function(d){'
    '    var el=document.getElementById("logbox");'
    '    el.innerHTML=(d.lines||[]).map(colorLine).join("\\n");'
    '    el.scrollTop=el.scrollHeight;'
    '  }).catch(function(){});'
    '}'
    
    'function loadFullLog(){'
    '  fetch("/api/logs?lines=500").then(function(r){return r.json();}).then(function(d){'
    '    var lines=d.lines||[];'
    '    if(_lf==="sig")lines=lines.filter(function(l){return l.indexOf("[SIGNAL]")>=0||l.indexOf("ETORO")>=0||l.indexOf("IBKR")>=0;});'
    '    if(_lf==="err")lines=lines.filter(function(l){return l.indexOf("ERROR")>=0||l.indexOf("WARNING")>=0;});'
    '    var el=document.getElementById("fullLog");'
    '    el.innerHTML=lines.map(colorLine).join("\\n");'
    '    el.scrollTop=el.scrollHeight;'
    '  }).catch(function(){});'
    '}'
    
    'function act(a){'
    '  fetch("/api/"+a,{method:"POST"}).then(function(){setTimeout(loadStatus,2500);}).catch(function(){});'
    '}'
    
    '</script>'
    '</body>'
    '</html>'
)


@app.route('/')
def index():
    return HTML


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
@require_auth
def status():
    running = bool(
        subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True).stdout.strip()
    )
    counts = {'total': 0, 'ibkr': 0, 'etoro': 0}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        counts['total'] = conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        counts['ibkr']  = conn.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        counts['etoro'] = conn.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return jsonify({'running': running, 'counts': counts})


@app.route('/api/signals')
@require_auth
def signals():
    limit = min(int(request.args.get('limit', 20)), 500)
    try:
        conn   = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute('SELECT * FROM signals ORDER BY id DESC LIMIT ?', (limit,))
        cols   = [d[0] for d in cursor.description]
        rows   = [dict(zip(cols, r)) for r in cursor.fetchall()]
        conn.close()
        return jsonify({'signals': rows})
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
    except Exception:
        return jsonify({'lines': ['Log file not found']})


@app.route('/api/start', methods=['POST'])
@require_auth
def start():
    _run_bot()
    return jsonify({'ok': True})


@app.route('/api/stop', methods=['POST'])
@require_auth
def stop():
    subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/restart', methods=['POST'])
@require_auth
def restart():
    def _do():
        time.sleep(1)
        subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
        time.sleep(1)
        _run_bot()
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({'ok': True})


def _run_bot():
    venv_python = BASE_DIR / 'venv' / 'bin' / 'python3'
    main_py     = BASE_DIR / 'main.py'
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(venv_python), str(main_py)],
        stdout=open(LOG_PATH, 'a'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', '8080'))
    app.run(host='0.0.0.0', port=port, debug=False)

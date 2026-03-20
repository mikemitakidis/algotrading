"""
dashboard/app.py
Flask dashboard for Algo Trader v1.
Reads bot_state.json, bot.log, and signals.db only — does not trade.
No JS backtick template literals.
"""
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, request, jsonify, session
from dotenv import load_dotenv

BASE_DIR   = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

app = Flask(__name__)
_pw = os.getenv('DASHBOARD_PASSWORD', 'changeme')
app.secret_key = _pw + '_algo_session'

LOG_PATH   = BASE_DIR / 'logs' / 'bot.log'
DB_PATH    = BASE_DIR / 'data' / 'signals.db'
STATE_PATH = BASE_DIR / 'data' / 'bot_state.json'


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


# ─────────────────────────────────────────────────────────────────────────────
# HTML Template
# ─────────────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Algo Trader v1</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:Segoe UI,Arial,sans-serif;min-height:100vh}
nav{background:#161b22;border-bottom:1px solid #30363d;padding:0 24px;display:flex;align-items:center;justify-content:space-between;height:52px;position:sticky;top:0;z-index:100}
.brand{font-size:17px;font-weight:700;color:#58a6ff}
.brand em{background:#1f6feb;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px;font-style:normal}
nav a{color:#8b949e;text-decoration:none;padding:7px 13px;border-radius:6px;font-size:14px;cursor:pointer;user-select:none}
nav a:hover,nav a.active{background:#21262d;color:#e6edf3}
nav a.out{color:#f85149}
.page{display:none;padding:24px;max-width:1400px;margin:0 auto}
.page.on{display:block}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-bottom:18px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:22px}
.ct{font-size:11px;font-weight:600;color:#8b949e;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.sr{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}
.dot.g{background:#3fb950;box-shadow:0 0 8px #3fb950}
.dot.r{background:#f85149}
.dot.y{background:#d29922;box-shadow:0 0 8px #d29922}
.dot.b{background:#58a6ff;box-shadow:0 0 8px #58a6ff}
.dot.gy{background:#6e7681}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.dot.pulse{animation:pulse 1.4s ease-in-out infinite}
.st{font-size:19px;font-weight:700}
.sub{font-size:12px;color:#8b949e;margin-top:2px}
.br{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.btn{padding:8px 16px;border:none;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;transition:opacity .15s}
.btn:disabled{opacity:.5;cursor:not-allowed}
.gs{background:#238636;color:#fff}.rs{background:#da3633;color:#fff}.bl{background:#1f6feb;color:#fff}.gy-btn{background:#21262d;color:#e6edf3}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.5px;text-transform:uppercase}
.badge-scan{background:#2d2000;color:#d29922;border:1px solid #d29922}
.badge-cool{background:#0d4a1a;color:#3fb950;border:1px solid #3fb950}
.badge-start{background:#0d2d5a;color:#58a6ff;border:1px solid #58a6ff}
.badge-stop{background:#21262d;color:#6e7681;border:1px solid #6e7681}
.badge-crash{background:#4a0d0d;color:#f85149;border:1px solid #f85149}
.metric-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:#30363d;border-radius:8px;overflow:hidden;margin-top:4px}
.metric{background:#161b22;padding:14px;text-align:center}
.mv{font-size:26px;font-weight:700;color:#58a6ff}
.mv.g{color:#3fb950}.mv.y{color:#d29922}.mv.r{color:#f85149}
.ml{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-top:2px}
.logbox{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:14px;font-family:'Courier New',monospace;font-size:12px;height:340px;overflow-y:auto;line-height:1.65;white-space:pre-wrap;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#21262d;color:#8b949e;padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.7px}
td{padding:9px 12px;border-top:1px solid #21262d;vertical-align:middle}
tr:hover td{background:#1c2128}
.tag{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}
.tl{background:#0d4a1a;color:#3fb950}.ts{background:#4a0d0d;color:#f85149}
.te{background:#2d1f00;color:#d29922}.ti{background:#0d2d5a;color:#58a6ff}
.tw{background:#21262d;color:#8b949e}
.rfbtn{background:none;border:1px solid #30363d;color:#8b949e;padding:4px 11px;border-radius:6px;cursor:pointer;font-size:12px;margin-left:auto}
.rfbtn:hover{background:#21262d;color:#e6edf3}
.login{display:flex;align-items:center;justify-content:center;min-height:100vh}
.lbox{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:38px;width:340px}
.lbox input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:10px 13px;border-radius:7px;font-size:15px;margin-bottom:12px;outline:none;font-family:inherit}
.lbox input:focus{border-color:#58a6ff}
.lbox button{width:100%;background:#238636;color:#fff;border:none;padding:11px;border-radius:7px;font-size:15px;font-weight:600;cursor:pointer}
.stat-item{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #21262d}
.stat-item:last-child{border-bottom:none}
.stat-label{font-size:12px;color:#8b949e;min-width:130px}
.stat-value{font-size:13px;font-weight:600;color:#e6edf3}
.empty-state{color:#8b949e;text-align:center;padding:32px;font-size:13px}
.countdown{font-size:28px;font-weight:700;color:#58a6ff;font-variant-numeric:tabular-nums}
.countdown.done{color:#3fb950}
.filter-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.filter-bar input,.filter-bar select{background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 12px;border-radius:7px;font-size:13px;font-family:inherit;outline:none}
.filter-bar input:focus,.filter-bar select:focus{border-color:#58a6ff}
.tfsbar{display:flex;gap:4px;margin-top:6px}
.tfpip{padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;background:#21262d;color:#6e7681}
.tfpip.on{background:#0d2d5a;color:#58a6ff}
.btn-feedback{font-size:12px;color:#8b949e;margin-left:4px;min-height:20px}
.section-title{font-size:13px;font-weight:600;color:#e6edf3;margin-bottom:12px}
input[type=password],input[type=text],input[type=number],select{outline:none}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
</head>
<body>

<!-- ── Login ── -->
<div id="loginWrap" class="login">
<div class="lbox">
  <div style="font-size:21px;font-weight:700;color:#58a6ff;text-align:center;margin-bottom:6px">&#x1F916; Algo Trader</div>
  <div style="color:#8b949e;text-align:center;margin-bottom:26px">v1.0 &mdash; Shadow Mode</div>
  <input type="password" id="pw" placeholder="Password" onkeydown="if(event.key==='Enter')doLogin()">
  <button onclick="doLogin()">Login</button>
  <div id="lerr" style="color:#f85149;text-align:center;margin-top:9px;font-size:13px"></div>
</div>
</div>

<!-- ── App ── -->
<div id="appWrap" style="display:none">
<nav>
  <div class="brand">&#x1F916; Algo Trader <em>v1.0</em></div>
  <div>
    <a onclick="go('overview')"  id="n-overview"  class="active">Overview</a>
    <a onclick="go('signals')"   id="n-signals">Signals</a>
    <a onclick="go('logs')"      id="n-logs">Logs</a>
    <a onclick="go('backtest')"  id="n-backtest">Backtest</a>
    <a onclick="go('strategy')"  id="n-strategy">Strategy</a>
    <a onclick="go('settings')"  id="n-settings">Settings</a>
    <a onclick="doLogout()" class="out">Logout</a>
  </div>
</nav>

<!-- ════════════════ OVERVIEW ════════════════ -->
<div id="overview" class="page on">

  <!-- Row 1: Bot Status + Phase -->
  <div class="g2">

    <div class="card">
      <div class="ct">Bot Status</div>
      <div class="sr">
        <div class="dot gy" id="dot"></div>
        <div>
          <div class="st" id="stText">Loading...</div>
          <div class="sub" id="stSub">&nbsp;</div>
        </div>
      </div>
      <div class="br">
        <button class="btn gs" id="btnStart"   onclick="act('start')">&#x25B6; Start</button>
        <button class="btn rs" id="btnStop"    onclick="act('stop')">&#x23F9; Stop</button>
        <button class="btn bl" id="btnRestart" onclick="act('restart')">&#x21BA; Restart</button>
        <span class="btn-feedback" id="actFeedback"></span>
      </div>
    </div>

    <div class="card">
      <div class="ct">Current Phase</div>
      <div style="margin-bottom:12px">
        <span class="badge badge-stop" id="phaseBadge">unknown</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Cycle #</span>
        <span class="stat-value" id="cycleNum">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Next scan in</span>
        <span class="countdown" id="countdown">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Last scan at</span>
        <span class="stat-value" id="lastCycleAt">&mdash;</span>
      </div>
    </div>

  </div>

  <!-- Row 2: Last Cycle + System -->
  <div class="g2">

    <div class="card">
      <div class="ct">Last Cycle Summary</div>
      <div class="stat-item">
        <span class="stat-label">Signals generated</span>
        <span class="stat-value" id="lcSignals">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Timeframes OK</span>
        <span class="stat-value" id="lcTfs">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Symbols scanned</span>
        <span class="stat-value" id="lcSymbols">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Duration</span>
        <span class="stat-value" id="lcDuration">&mdash;</span>
      </div>
      <div class="tfsbar" id="tfsbar"></div>
    </div>

    <div class="card">
      <div class="ct">System</div>
      <div class="stat-item">
        <span class="stat-label">Mode</span>
        <span class="stat-value" id="sysMode">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Focus symbols</span>
        <span class="stat-value" id="sysFocus">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">DB total signals</span>
        <span class="stat-value" id="sysDbTotal">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">DB by route</span>
        <span class="stat-value" id="sysDbRoutes">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Telegram</span>
        <span class="stat-value" id="sysTg">&mdash;</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Scan interval</span>
        <span class="stat-value" id="sysInterval">&mdash;</span>
      </div>
    </div>

  </div>

  <!-- Recent Signals -->
  <div class="card" style="margin-bottom:18px">
    <div class="ct">
      Recent Signals
      <button class="rfbtn" onclick="loadSig()">&#x21BB; Refresh</button>
    </div>
    <div id="sigWrap"><div class="empty-state">No signals yet &mdash; bot is scanning in shadow mode</div></div>
  </div>

  <!-- Live Log -->
  <div class="card">
    <div class="ct">
      Live Log
      <button class="rfbtn" onclick="loadLog()">&#x21BB; Refresh</button>
    </div>
    <div class="logbox" id="logbox">Loading...</div>
  </div>

</div><!-- /overview -->

<!-- ════════════════ SIGNALS ════════════════ -->
<div id="signals" class="page">
  <div class="card" style="margin-bottom:16px">
    <div class="filter-bar">
      <input style="width:150px" placeholder="Symbol..." id="sfilt" oninput="renderSig()">
      <select id="rfilt" onchange="renderSig()">
        <option value="">All Routes</option>
        <option>ETORO</option><option>IBKR</option><option>WATCH</option>
      </select>
      <select id="dfilt" onchange="renderSig()">
        <option value="">All Directions</option>
        <option>long</option><option>short</option>
      </select>
      <button class="btn bl" style="font-size:12px;padding:7px 13px" onclick="loadAllSig()">&#x21BB; Refresh</button>
      <span id="scount" style="color:#8b949e;font-size:13px;margin-left:auto"></span>
    </div>
  </div>
  <div class="card" style="overflow-x:auto"><div id="allSig"></div></div>
</div>

<!-- ════════════════ LOGS ════════════════ -->
<div id="logs" class="page">
  <div class="card" style="margin-bottom:16px">
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <button class="btn gy-btn" id="lf-all" onclick="setLF('all')">All</button>
      <button class="btn gy-btn" id="lf-sig" onclick="setLF('sig')">Signals only</button>
      <button class="btn gy-btn" id="lf-err" onclick="setLF('err')">Errors/Warnings</button>
      <button class="btn gy-btn" id="lf-cyc" onclick="setLF('cyc')">Cycle events</button>
      <button class="btn bl"     onclick="loadFullLog()">&#x21BB; Refresh</button>
    </div>
  </div>
  <div class="card"><div class="logbox" id="fullLog" style="height:600px">Loading...</div></div>
</div>



<!-- ════════════════ BACKTEST ════════════════ -->
<div id="backtest" class="page">

  <div class="card" style="margin-bottom:18px">
    <div class="ct">Backtest Configuration</div>
    <div style="font-size:12px;color:#8b949e;margin-bottom:16px">
      Uses the <b>exact same</b> indicators, thresholds, and confluence rules as the live bot.
      Same strategy version currently active: <span id="btStratVer" style="color:#58a6ff">loading...</span>
    </div>
    <div class="g2" style="margin-bottom:16px">
      <div>
        <div class="section-title" style="margin-bottom:6px">Symbols</div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:6px">Comma-separated. Max 10. Valid US tickers only &mdash; e.g. <b style="color:#58a6ff">AAPL</b> (not APPL), <b style="color:#58a6ff">MSFT</b>, <b style="color:#58a6ff">NVDA</b>.</div>
        <textarea id="btSymbols" rows="3"
          style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:13px;font-family:inherit;resize:vertical"
          placeholder="AAPL, MSFT, NVDA"></textarea>
        <div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">
          <span style="font-size:10px;color:#6e7681;margin-right:4px">Presets:</span>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btValidPreset('aapl1y')">AAPL 1yr</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btValidPreset('mega1y')">Mega-cap 5 1yr</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btValidPreset('mixed1y')">Mixed 10 1yr</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btValidPreset('90d15m')">90d (15m avail)</button>
          <span style="font-size:10px;color:#6e7681;margin-left:8px;margin-right:4px">Custom:</span>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btPreset('mega')">Mega-cap (5)</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btPreset('tech')">Tech (8)</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btPreset('mixed')">Mixed (10)</button>
        </div>
      </div>
      <div>
        <div class="section-title" style="margin-bottom:6px">Date Range</div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:6px">
          Daily data: up to 2 years &nbsp;|&nbsp; 1H data: up to 730 days &nbsp;|&nbsp; 15m data: last 60 days only
        </div>
        <div class="stat-item" style="border:none;padding:4px 0">
          <span class="stat-label">Start date</span>
          <input type="date" id="btStart"
            style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 10px;border-radius:6px;font-size:13px;font-family:inherit">
        </div>
        <div class="stat-item" style="border:none;padding:4px 0">
          <span class="stat-label">End date</span>
          <input type="date" id="btEnd"
            style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 10px;border-radius:6px;font-size:13px;font-family:inherit">
        </div>
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btDatePreset(30)">30d</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btDatePreset(90)">90d</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btDatePreset(180)">6mo</button>
          <button class="btn gy-btn" style="font-size:11px;padding:5px 10px" onclick="btDatePreset(365)">1yr</button>
        </div>
      </div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn gs" id="btRunBtn" style="font-size:14px;padding:10px 28px" onclick="runBacktest()">&#x25B6; Run Backtest</button>
      <button class="btn rs" id="btCancelBtn" style="font-size:13px;padding:8px 16px;display:none" onclick="cancelBacktest()">&#x23F9; Cancel</button>
      <span id="btRunMsg" style="font-size:13px;color:#8b949e"></span>
    </div>
    <div id="btDataStatus" style="font-size:12px;margin-top:10px;min-height:18px"></div>
  </div>

  <!-- Progress -->
  <div id="btProgress" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">Running...</div>
    <div style="background:#21262d;border-radius:6px;height:10px;overflow:hidden;margin-bottom:8px">
      <div id="btProgressBar" style="height:100%;background:#1f6feb;border-radius:6px;width:0%;transition:width .4s"></div>
    </div>
    <div id="btProgressMsg" style="font-size:12px;color:#8b949e">Initialising...</div>
  </div>

  <!-- Summary Stats -->
  <div id="btSummarySection" style="display:none">
    <div class="g4" style="margin-bottom:18px">
      <div class="card" style="text-align:center">
        <div class="mv" id="bs_total">-</div><div class="ml">Total Trades</div>
      </div>
      <div class="card" style="text-align:center">
        <div class="mv g" id="bs_wr">-</div><div class="ml">Win Rate</div>
      </div>
      <div class="card" style="text-align:center">
        <div class="mv" id="bs_pf">-</div><div class="ml">Profit Factor</div>
      </div>
      <div class="card" style="text-align:center">
        <div class="mv r" id="bs_dd">-</div><div class="ml">Max Drawdown</div>
      </div>
    </div>

    <div class="g2" style="margin-bottom:18px">
      <div class="card">
        <div class="ct">Returns</div>
        <div class="stat-item"><span class="stat-label">Avg return</span><span class="stat-value" id="bs_avg_ret">-</span></div>
        <div class="stat-item"><span class="stat-label">Avg win</span><span class="stat-value" id="bs_avg_win" style="color:#3fb950">-</span></div>
        <div class="stat-item"><span class="stat-label">Avg loss</span><span class="stat-value" id="bs_avg_los" style="color:#f85149">-</span></div>
        <div class="stat-item"><span class="stat-label">Final equity (100 start)</span><span class="stat-value" id="bs_eq">-</span></div>
        <div class="stat-item"><span class="stat-label">Wins / Losses / Timeouts</span><span class="stat-value" id="bs_wlt">-</span></div>
        <div class="stat-item"><span class="stat-label">Annualised return</span><span class="stat-value" id="bs_ann_ret">-</span></div>
        <div class="stat-item"><span class="stat-label">Avg hold (days)</span><span class="stat-value" id="bs_hold">-</span></div>
        <div class="stat-item"><span class="stat-label">Max consec. wins / losses</span><span class="stat-value" id="bs_streak_w">-</span> / <span class="stat-value" id="bs_streak_l">-</span></div>
      </div>
      <div class="card">
        <div class="ct">By Confluence</div>
        <div id="bs_by_conf"></div>
        <div class="ct" style="margin-top:16px">By Direction</div>
        <div id="bs_by_dir"></div>
        <div class="ct" style="margin-top:16px">By Route</div>
        <div id="bs_by_route"></div>
      </div>
    </div>
  </div>

  <!-- TF Availability Panel -->
  <div id="btTFPanel" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">Timeframe Availability <span id="btTFNote" style="font-size:10px;color:#d29922;font-weight:400"></span></div>
    <div id="btTFContent"></div>
  </div>

  <!-- Equity Curve -->
  <div id="btEquitySection" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">Equity Curve <span style="font-size:10px;color:#6e7681;font-weight:400">starting equity = 100</span></div>
    <canvas id="btEquityChart" style="width:100%;max-height:200px"></canvas>
  </div>

  <!-- Monthly Breakdown -->
  <div id="btMonthlySection" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">Monthly Breakdown</div>
    <div id="btMonthlyContent" style="overflow-x:auto"></div>
  </div>

  <!-- Per-Symbol Stats -->
  <div id="btSymSection" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">Per-Symbol Performance</div>
    <div id="btSymContent"></div>
  </div>

  <!-- Run Metadata Banner -->
  <div id="btMetaBanner" style="font-size:11px;color:#6e7681;margin-bottom:8px;padding:6px 0;border-top:1px solid #21262d"></div>

  <!-- Trade List -->
  <div id="btTradesSection" style="display:none" class="card">
    <div class="ct">
      Trade List
      <span id="btTradeCount" style="color:#8b949e;font-size:11px;margin-left:4px"></span>
      <button class="rfbtn" style="color:#58a6ff;margin-right:8px" onclick="exportSummaryJson()">&#x2B07; Summary JSON</button>
      <a id="btCsvLink" href="/api/backtest/csv" target="_blank"
        style="margin-left:auto;background:none;border:1px solid #30363d;color:#58a6ff;padding:4px 11px;border-radius:6px;cursor:pointer;font-size:12px;text-decoration:none">&#x2B07; CSV</a>
    </div>
    <div style="overflow-x:auto"><div id="btTradeTable"></div></div>
  </div>

  <!-- Diagnostics -->
  <div id="btDiagSection" style="display:none;margin-top:18px" class="card">
    <div class="ct">Diagnostics <span style="font-size:10px;color:#8b949e;font-weight:400">Why these trades? Why 0 trades?</span></div>
    <div style="font-size:11px;color:#8b949e;margin-bottom:12px">
      Shows data coverage per timeframe, candidate signals before filtering, and rejection reasons.
    </div>
    <div id="btDiagContent"></div>
  </div>

  <!-- Run History -->
  <div class="card" style="margin-top:18px">
    <div class="ct">
      Run History
      <span style="font-size:10px;color:#6e7681;font-weight:400">last 20 runs</span>
      <button class="rfbtn" onclick="loadHistory()">&#x21BB;</button>
    </div>
    <div id="btHistoryContent"><div class="empty-state">No runs recorded yet.</div></div>
  </div>

</div><!-- /backtest -->

<!-- ════════════════ STRATEGY ════════════════ -->
<div id="strategy" class="page">

  <div class="card" style="margin-bottom:18px">
    <div class="ct">
      Strategy Engine
      <span id="stratBadge" style="font-size:10px;padding:2px 9px;border-radius:8px;background:#0d2d5a;color:#58a6ff">v1</span>
      <span id="stratUpdated" style="font-size:11px;color:#6e7681;margin-left:4px"></span>
      <button class="rfbtn" onclick="loadStrategy()">&#x21BB; Refresh</button>
    </div>
    <div style="font-size:12px;color:#8b949e;margin-bottom:16px">
      All thresholds below are the exact values the live bot uses to score signals.
      Changes take effect after the bot restarts (saved automatically).
      <span style="color:#3fb950">&#x2714; Active in live strategy</span>
      &nbsp;&nbsp;
      <span style="color:#58a6ff">&#x1F4BE; Collected for ML/backtesting</span>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <button class="btn gs" style="font-size:13px" onclick="saveStrategy()">Save &amp; Restart Bot</button>
      <button class="btn gy-btn" style="font-size:13px" onclick="resetStrategy()">Reset to Defaults</button>
      <span id="stratSaveMsg" style="font-size:13px;color:#8b949e;margin-left:4px"></span>
    </div>
  </div>

  <div class="g2">

    <!-- Timeframes -->
    <div class="card">
      <div class="ct">Timeframes <span style="color:#3fb950;font-size:10px">&#x2714; Active</span></div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:12px">Enable/disable timeframes used in confluence scoring.</div>
      <div id="tfToggles"></div>
    </div>

    <!-- Confluence -->
    <div class="card">
      <div class="ct">Confluence <span style="color:#3fb950;font-size:10px">&#x2714; Active</span></div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:12px">How many timeframes must independently agree for a signal to fire.</div>
      <div class="stat-item">
        <span class="stat-label">Min valid TFs</span>
        <div style="display:flex;align-items:center;gap:8px">
          <input type="number" id="s_min_valid_tfs" min="1" max="4" step="1"
            style="width:70px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:6px 10px;border-radius:6px;font-size:14px;font-family:inherit">
          <span style="font-size:11px;color:#6e7681">/ 4 TFs &nbsp; default: 3</span>
        </div>
      </div>
      <div style="margin-top:12px;font-size:11px;color:#8b949e">
        3 = strict (recommended) &nbsp;|&nbsp; 2 = more signals, lower confidence
      </div>
    </div>

  </div>

  <!-- Long + Short Rules -->
  <div class="g2">

    <div class="card">
      <div class="ct">Long Signal Rules <span style="color:#3fb950;font-size:10px">&#x2714; Active</span></div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:14px">All 3 conditions must pass on a timeframe for it to count as a long agreement.</div>

      <div class="section-title" style="margin-bottom:8px">Momentum (RSI + MACD)</div>
      <div class="stat-item">
        <span class="stat-label">RSI minimum</span>
        <input type="number" id="s_long_rsi_min" min="1" max="99" step="1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 30</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">RSI maximum</span>
        <input type="number" id="s_long_rsi_max" min="2" max="100" step="1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 75</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">MACD hist &gt;</span>
        <input type="number" id="s_long_macd_gt" step="0.001"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0</span>
      </div>

      <div class="section-title" style="margin-top:14px;margin-bottom:8px">Trend (EMA)</div>
      <div class="stat-item">
        <span class="stat-label">EMA20/50 tolerance</span>
        <input type="number" id="s_long_ema_tol" min="0" max="0.1" step="0.001"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0.005</span>
      </div>

      <div class="section-title" style="margin-top:14px;margin-bottom:8px">Volume</div>
      <div class="stat-item">
        <span class="stat-label">VWAP dev min</span>
        <input type="number" id="s_long_vwap_min" step="0.001"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: -0.015</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Volume ratio min</span>
        <input type="number" id="s_long_vol_min" min="0" max="5" step="0.1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0.6</span>
      </div>
    </div>

    <div class="card">
      <div class="ct">Short Signal Rules <span style="color:#3fb950;font-size:10px">&#x2714; Active</span></div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:14px">All 3 conditions must pass on a timeframe for it to count as a short agreement.</div>

      <div class="section-title" style="margin-bottom:8px">Momentum (RSI + MACD)</div>
      <div class="stat-item">
        <span class="stat-label">RSI minimum</span>
        <input type="number" id="s_short_rsi_min" min="1" max="99" step="1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 50</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">MACD hist &lt;</span>
        <input type="number" id="s_short_macd_lt" step="0.001"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0</span>
      </div>

      <div class="section-title" style="margin-top:14px;margin-bottom:8px">Trend (EMA)</div>
      <div class="stat-item">
        <span class="stat-label">EMA20/50 tolerance</span>
        <input type="number" id="s_short_ema_tol" min="0" max="0.1" step="0.001"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0.005</span>
      </div>

      <div class="section-title" style="margin-top:14px;margin-bottom:8px">Volume</div>
      <div class="stat-item">
        <span class="stat-label">VWAP dev max</span>
        <input type="number" id="s_short_vwap_max" step="0.001"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0.015</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">Volume ratio min</span>
        <input type="number" id="s_short_vol_min" min="0" max="5" step="0.1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 0.6</span>
      </div>
    </div>

  </div>

  <!-- Risk + Routing -->
  <div class="g2">

    <div class="card">
      <div class="ct">Risk / ATR Parameters <span style="color:#58a6ff;font-size:10px">&#x1F4BE; ML use</span></div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:14px">
        Stop and target are computed from ATR(14) at signal time and logged for every signal.<br>
        <b>Shadow mode only</b> — no broker execution in V1.
      </div>
      <div class="stat-item">
        <span class="stat-label">ATR stop multiplier</span>
        <input type="number" id="s_atr_stop" min="0.1" max="20" step="0.1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 2.0</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">ATR target multiplier</span>
        <input type="number" id="s_atr_target" min="0.1" max="50" step="0.1"
          style="width:80px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 3.0</span>
      </div>
      <div style="margin-top:10px;font-size:11px;color:#6e7681">
        Long: Stop = Entry &minus; (ATR &times; stop_mult) &nbsp;|&nbsp; Target = Entry + (ATR &times; target_mult)<br>
        Short: Stop = Entry + (ATR &times; stop_mult) &nbsp;|&nbsp; Target = Entry &minus; (ATR &times; target_mult)
      </div>
    </div>

    <div class="card">
      <div class="ct">Route Labels <span style="color:#3fb950;font-size:10px">&#x2714; Active</span></div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:14px">
        Route is a label only &mdash; no real execution in shadow mode.<br>
        ETORO and IBKR are placeholders for future broker integration.
      </div>
      <div class="stat-item">
        <span class="stat-label">eToro min TFs</span>
        <input type="number" id="s_etoro_min" min="1" max="4" step="1"
          style="width:70px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 4</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">IBKR min TFs</span>
        <input type="number" id="s_ibkr_min" min="1" max="4" step="1"
          style="width:70px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:5px 8px;border-radius:6px;font-size:13px;font-family:inherit">
        <span style="font-size:11px;color:#6e7681">def: 2</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">WATCH</span>
        <span style="font-size:12px;color:#6e7681">Below IBKR min &mdash; logged only, not stored</span>
      </div>
    </div>

  </div>

  <!-- Audit Trail -->
  <div class="card">
    <div class="ct">
      Change History
      <button class="rfbtn" onclick="loadStrategy()">&#x21BB;</button>
    </div>
    <div id="auditWrap"><div class="empty-state">No changes recorded yet.</div></div>
  </div>

</div><!-- /strategy -->

<!-- ════════════════ SETTINGS ════════════════ -->
<div id="settings" class="page">

  <div class="card" style="margin-bottom:20px">
    <div class="ct">
      Telegram Alerts
      <span id="tgBig" style="font-size:10px;padding:2px 8px;border-radius:8px;background:#21262d;color:#8b949e">loading...</span>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">

      <div>
        <div class="section-title">Current status</div>
        <span id="tgStatusBig" style="font-size:13px;color:#8b949e">Loading...</span>
      </div>
      <div style="display:flex;align-items:flex-end">
        <button class="btn gs" id="tgTestBig" onclick="sendTgTest()" style="font-size:13px">&#x1F4E4; Send Test</button>
      </div>

      <div>
        <div class="section-title">Enable Telegram</div>
        <select id="tgEnabled" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:14px;width:100%;font-family:inherit">
          <option value="false">Disabled</option>
          <option value="true">Enabled</option>
        </select>
      </div>

      <div>
        <div class="section-title">Cooldown (seconds)</div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Suppress duplicate alerts</div>
        <input type="number" id="tgCooldown" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:14px;width:100%;font-family:inherit" value="14400">
      </div>

      <div>
        <div class="section-title">Bot Token</div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:4px">From @BotFather on Telegram</div>
        <input type="password" id="tgToken" placeholder="1234567890:ABCdef..." style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:14px;width:100%;font-family:inherit">
      </div>

      <div>
        <div class="section-title">Chat ID</div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Your numeric Telegram chat ID</div>
        <div style="display:flex;gap:8px;align-items:center">
          <input type="text" id="tgChatId" placeholder="123456789" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:14px;flex:1;font-family:inherit">
          <button class="btn bl" style="font-size:12px;padding:8px 12px;white-space:nowrap" onclick="findChatId()">Find My ID</button>
        </div>
        <div id="chatIdResult" style="font-size:12px;color:#8b949e;margin-top:6px"></div>
      </div>

    </div>
    <div style="display:flex;gap:12px;align-items:center">
      <button class="btn gs" style="padding:10px 28px;font-size:14px" onclick="saveTgSettings()">Save &amp; Restart Bot</button>
      <span id="tgSaveMsg" style="font-size:13px;color:#8b949e"></span>
    </div>
  </div>

  <div class="card">
    <div class="ct">Dashboard Password</div>
    <div style="max-width:400px">
      <input type="password" id="newPw" placeholder="New password" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px 12px;border-radius:7px;font-size:14px;width:100%;font-family:inherit;margin-bottom:12px">
      <button class="btn gs" style="font-size:13px" onclick="savePw()">Save Password</button>
      <span id="pwSaveMsg" style="font-size:13px;color:#8b949e;margin-left:12px"></span>
    </div>
  </div>

</div><!-- /settings -->
</div><!-- /appWrap -->

<script>
// ─── globals ───
var _sigs = [];
var _lf   = 'all';
var _nextCycleAt = null;
var _cdownTimer  = null;
var _lastStatus  = {};

// ─── auth ───
function doLogin(){
  var pw = document.getElementById('pw').value;
  if(!pw){ document.getElementById('lerr').textContent='Enter a password'; return; }
  document.getElementById('lerr').textContent = 'Checking...';
  fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(d.ok){
      document.getElementById('loginWrap').style.display = 'none';
      document.getElementById('appWrap').style.display   = 'block';
      boot();
    } else {
      document.getElementById('lerr').textContent = 'Incorrect password';
    }
  }).catch(function(e){ document.getElementById('lerr').textContent = 'Error: ' + e.message; });
}

function doLogout(){
  fetch('/api/logout', {method:'POST'}).then(function(){ location.reload(); });
}

// ─── navigation ───
function go(p){
  document.querySelectorAll('.page').forEach(function(x){ x.classList.remove('on'); });
  document.querySelectorAll('nav a').forEach(function(x){ x.classList.remove('active'); });
  var pg = document.getElementById(p);
  if(pg) pg.classList.add('on');
  var nav = document.getElementById('n-' + p);
  if(nav) nav.classList.add('active');
  if(p === 'logs')     loadFullLog();
  if(p === 'signals')  loadAllSig();
  if(p === 'settings') loadTgSettings();
  if(p === 'strategy')  loadStrategy();
  if(p === 'backtest')  initBacktest();
}

// ─── boot ───
function boot(){
  loadAll();
  loadTg();
  setInterval(loadAll,  20000);
  setInterval(loadTg,   60000);
  startCountdown();
}

function loadAll(){ loadStatus(); loadSig(); loadLog(); }

// ─── status ───
function loadStatus(){
  fetch('/api/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    _lastStatus = d;
    applyStatus(d);
  }).catch(function(){});
}

function applyStatus(d){
  // running dot + text
  var dot  = document.getElementById('dot');
  var stTx = document.getElementById('stText');
  var stSb = document.getElementById('stSub');
  var phase = (d.phase || 'unknown').toLowerCase();

  if(d.running){
    if(phase === 'scanning'){
      dot.className = 'dot y pulse';
      stTx.textContent = 'Running \u2014 SCANNING';
    } else {
      dot.className = 'dot g';
      stTx.textContent = 'Running \u2014 ' + (d.mode || 'shadow').toUpperCase() + ' MODE';
    }
    stSb.textContent = d.uptime_started ? ('Since ' + fmtTime(d.uptime_started)) : '';
  } else {
    dot.className = 'dot ' + (phase === 'crashed' ? 'r' : 'gy');
    stTx.textContent = phase === 'crashed' ? 'CRASHED' : 'Stopped';
    stSb.textContent = '';
  }

  // phase badge
  var badge = document.getElementById('phaseBadge');
  if(badge){
    badge.className = 'badge ' + phaseBadgeClass(phase);
    badge.textContent = phase;
  }

  // cycle info
  setText('cycleNum', d.cycle > 0 ? ('#' + d.cycle) : '\u2014');
  setText('lastCycleAt', d.last_cycle_at ? fmtTime(d.last_cycle_at) : '\u2014');

  // next cycle countdown
  _nextCycleAt = d.next_cycle_at || null;
  if(phase === 'scanning') _nextCycleAt = null;

  // last cycle summary
  setText('lcSignals',  d.last_cycle_signals != null ? (d.last_cycle_signals + (d.last_cycle_signals === 1 ? ' signal' : ' signals')) : '\u2014');
  setText('lcTfs',      d.last_cycle_tfs     != null ? (d.last_cycle_tfs + ' / 4') : '\u2014');
  setText('lcSymbols',  d.last_cycle_symbols  != null ? d.last_cycle_symbols : '\u2014');
  setText('lcDuration', d.last_cycle_duration_s != null ? (d.last_cycle_duration_s + 's') : '\u2014');

  // TF pips
  var tfsbar = document.getElementById('tfsbar');
  if(tfsbar){
    var all = ['1D','4H','1H','15m'];
    var got = d.last_cycle_tfs_list || [];
    tfsbar.innerHTML = all.map(function(tf){
      var on = got.indexOf(tf) >= 0 ? ' on' : '';
      return '<span class="tfpip' + on + '">' + tf + '</span>';
    }).join('');
  }

  // system
  setText('sysMode',     (d.mode || '\u2014').toUpperCase());
  setText('sysFocus',    d.focus_count != null ? (d.focus_count + ' symbols') : '\u2014');
  setText('sysInterval', d.scan_interval_secs ? fmtSecs(d.scan_interval_secs) : '\u2014');

  // DB counts
  var counts = d.counts || {};
  setText('sysDbTotal',  counts.total != null ? counts.total : '\u2014');
  if(counts.total != null){
    setText('sysDbRoutes', 'eToro: ' + (counts.etoro||0) + ' \u00b7 IBKR: ' + (counts.ibkr||0));
  } else {
    setText('sysDbRoutes', '\u2014');
  }

  // Telegram
  var tg  = d.telegram || {};
  var tgEl = document.getElementById('sysTg');
  if(tgEl){
    if(tg.ready){
      tgEl.innerHTML = '<span style="color:#3fb950">&#x2714; Enabled &amp; ready</span>';
    } else if(tg.enabled){
      tgEl.innerHTML = '<span style="color:#d29922">&#x26A0; Enabled but misconfigured</span>';
    } else {
      tgEl.innerHTML = '<span style="color:#6e7681">Disabled</span>';
    }
  }
}

function phaseBadgeClass(phase){
  if(phase === 'scanning') return 'badge-scan';
  if(phase === 'cooldown') return 'badge-cool';
  if(phase === 'starting') return 'badge-start';
  if(phase === 'crashed')  return 'badge-crash';
  return 'badge-stop';
}

// ─── countdown ───
function startCountdown(){
  if(_cdownTimer) clearInterval(_cdownTimer);
  _cdownTimer = setInterval(tickCountdown, 1000);
}

function tickCountdown(){
  var el = document.getElementById('countdown');
  if(!el) return;
  if(!_nextCycleAt){
    var phase = (_lastStatus.phase || '').toLowerCase();
    el.className = 'countdown';
    if(phase === 'scanning'){
      el.textContent = 'scanning...';
    } else if(!_lastStatus.running){
      el.textContent = '\u2014';
    } else {
      el.textContent = '\u2014';
    }
    return;
  }
  var diff = Math.round((new Date(_nextCycleAt) - Date.now()) / 1000);
  if(diff <= 0){
    el.className = 'countdown done';
    el.textContent = 'soon...';
  } else {
    el.className = 'countdown';
    var m = Math.floor(diff / 60);
    var s = diff % 60;
    el.textContent = (m > 0 ? m + 'm ' : '') + s + 's';
  }
}

// ─── bot actions ───
function act(a){
  var allBtns = [document.getElementById('btnStart'), document.getElementById('btnStop'), document.getElementById('btnRestart')];
  var fb = document.getElementById('actFeedback');
  allBtns.forEach(function(b){ if(b) b.disabled = true; });
  var label = a === 'start' ? 'Starting\u2026' : a === 'stop' ? 'Stopping\u2026' : 'Restarting\u2026';
  if(fb) fb.textContent = label;
  fetch('/api/' + a, {method:'POST'})
  .then(function(){
    setTimeout(function(){
      loadStatus();
      if(fb) fb.textContent = 'Done.';
      allBtns.forEach(function(b){ if(b) b.disabled = false; });
      setTimeout(function(){ if(fb) fb.textContent = ''; }, 3000);
    }, 4000);
    setTimeout(loadStatus, 9000);
  })
  .catch(function(e){
    allBtns.forEach(function(b){ if(b) b.disabled = false; });
    if(fb){ fb.textContent = 'Error: ' + e.message; fb.style.color = '#f85149'; }
  });
}

// ─── signals ───
var HDR = '<table><tr>'
  + '<th>Time</th><th>Symbol</th><th>Dir</th><th>Route</th>'
  + '<th>TFs</th><th>RSI</th><th>MACD Hist</th><th>ATR</th><th>Price</th>'
  + '</tr>';

function makeRow(s){
  var tc = 'tag t' + (s.direction === 'long' ? 'l' : 's');
  var rc = s.route === 'ETORO' ? 'tag te' : s.route === 'IBKR' ? 'tag ti' : 'tag tw';
  var ts = (s.timestamp||'').slice(0,19).replace('T',' ');
  var rsi  = s.rsi       != null ? (+s.rsi).toFixed(1)       : '\u2014';
  var macd = s.macd_hist != null ? (+s.macd_hist).toFixed(4) : '\u2014';
  var atr  = s.atr       != null ? (+s.atr).toFixed(2)       : '\u2014';
  var price = s.price    != null ? ('$' + (+s.price).toFixed(2)) : '\u2014';
  var tfs  = [s.tf_1d?'1D':'', s.tf_4h?'4H':'', s.tf_1h?'1H':'', s.tf_15m?'15m':''].filter(Boolean).join(' ');
  return '<tr>'
    + '<td style="color:#8b949e">' + ts + '</td>'
    + '<td><b>' + (s.symbol||'') + '</b></td>'
    + '<td><span class="' + tc + '">' + (s.direction||'').toUpperCase() + '</span></td>'
    + '<td><span class="' + rc + '">' + (s.route||'') + '</span></td>'
    + '<td><span style="font-size:12px;color:#58a6ff">' + (s.valid_count||0) + '/4</span>'
    + (tfs ? '<br><span style="font-size:10px;color:#6e7681">' + tfs + '</span>' : '') + '</td>'
    + '<td>' + rsi + '</td>'
    + '<td style="font-family:monospace">' + macd + '</td>'
    + '<td>' + atr + '</td>'
    + '<td><b>' + price + '</b></td>'
    + '</tr>';
}

function loadSig(){
  fetch('/api/signals?limit=10')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var w = document.getElementById('sigWrap');
    var sigs = d.signals || [];
    if(!sigs.length){
      w.innerHTML = '<div class="empty-state">No signals yet \u2014 bot is scanning in shadow mode</div>';
      return;
    }
    w.innerHTML = HDR + sigs.map(makeRow).join('') + '</table>';
  }).catch(function(){});
}

function loadAllSig(){
  fetch('/api/signals?limit=500')
  .then(function(r){ return r.json(); })
  .then(function(d){ _sigs = d.signals || []; renderSig(); })
  .catch(function(){});
}

function renderSig(){
  var s = (document.getElementById('sfilt').value||'').toLowerCase();
  var r = document.getElementById('rfilt').value;
  var d = document.getElementById('dfilt').value;
  var f = _sigs.filter(function(x){
    return (!s || (x.symbol||'').toLowerCase().indexOf(s) >= 0)
      && (!r || x.route === r)
      && (!d || x.direction === d);
  });
  setText('scount', f.length + ' signals');
  var w = document.getElementById('allSig');
  if(!f.length){ w.innerHTML = '<div class="empty-state">No signals match filter.</div>'; return; }
  w.innerHTML = HDR + f.map(makeRow).join('') + '</table>';
}

// ─── log coloring ───
function colorLine(l){
  if(!l) return '<span style="color:#21262d">&nbsp;</span>';
  var esc = l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  if(esc.indexOf('[SIGNAL]') >= 0)
    return '<span style="color:#3fb950;font-weight:700">' + esc + '</span>';
  if(esc.indexOf('ERROR') >= 0)
    return '<span style="color:#f85149;font-weight:600">' + esc + '</span>';
  if(esc.indexOf('WARNING') >= 0)
    return '<span style="color:#d29922">' + esc + '</span>';
  if(esc.indexOf('[STARTUP]') >= 0)
    return '<span style="color:#58a6ff;font-weight:600">' + esc + '</span>';
  if(esc.indexOf('[MAIN]') >= 0 && esc.indexOf('Cycle') >= 0)
    return '<span style="color:#58a6ff">' + esc + '</span>';
  if(esc.indexOf('[CYCLE]') >= 0)
    return '<span style="color:#79c0ff">' + esc + '</span>';
  if(esc.indexOf('ETORO') >= 0 || esc.indexOf('IBKR') >= 0)
    return '<span style="color:#3fb950">' + esc + '</span>';
  if(esc.indexOf('=====') >= 0 || esc.indexOf('SHADOW MODE') >= 0)
    return '<span style="color:#58a6ff;font-weight:700">' + esc + '</span>';
  return '<span style="color:#8b949e">' + esc + '</span>';
}

function setLF(f){
  _lf = f;
  ['all','sig','err','cyc'].forEach(function(x){
    var el = document.getElementById('lf-' + x);
    if(el) el.style.opacity = (x === f) ? '1' : '0.5';
  });
  loadFullLog();
}

function loadLog(){
  fetch('/api/logs?lines=80')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var el = document.getElementById('logbox');
    el.innerHTML = (d.lines||[]).map(colorLine).join('<br>');
    el.scrollTop = el.scrollHeight;
  }).catch(function(){});
}

function loadFullLog(){
  fetch('/api/logs?lines=600')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var lines = d.lines || [];
    if(_lf === 'sig') lines = lines.filter(function(l){ return l.indexOf('[SIGNAL]') >= 0 || l.indexOf('ETORO') >= 0 || l.indexOf('IBKR') >= 0; });
    if(_lf === 'err') lines = lines.filter(function(l){ return l.indexOf('ERROR') >= 0 || l.indexOf('WARNING') >= 0; });
    if(_lf === 'cyc') lines = lines.filter(function(l){ return l.indexOf('[CYCLE]') >= 0 || l.indexOf('[MAIN]') >= 0; });
    var el = document.getElementById('fullLog');
    el.innerHTML = lines.length ? lines.map(colorLine).join('<br>') : '<span style="color:#8b949e">No lines match this filter.</span>';
    el.scrollTop = el.scrollHeight;
  }).catch(function(){});
}

// ─── telegram (settings page) ───
function loadTg(){
  fetch('/api/telegram/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    // overview badge
    // (handled inside loadStatus via d.telegram)
    // settings page badge
    var badge = document.getElementById('tgBig');
    if(badge){
      if(d.ready)       { badge.style.background='#0d4a1a'; badge.style.color='#3fb950'; badge.textContent='enabled'; }
      else if(d.enabled){ badge.style.background='#4a2d00'; badge.style.color='#d29922'; badge.textContent='misconfigured'; }
      else              { badge.style.background='#21262d'; badge.style.color='#8b949e'; badge.textContent='disabled'; }
    }
  }).catch(function(){});
}

function loadTgSettings(){
  fetch('/api/telegram/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var big = document.getElementById('tgBig');
    var sb  = document.getElementById('tgStatusBig');
    if(d.ready){
      if(big){ big.style.background='#0d4a1a'; big.style.color='#3fb950'; big.textContent='enabled'; }
      if(sb){ sb.textContent='Telegram is configured and active.'; sb.style.color='#3fb950'; }
    } else if(d.enabled){
      if(big){ big.style.background='#2d1f00'; big.style.color='#d29922'; big.textContent='misconfigured'; }
      if(sb){ sb.textContent='Enabled but token or chat ID missing.'; sb.style.color='#d29922'; }
    } else {
      if(big){ big.style.background='#21262d'; big.style.color='#8b949e'; big.textContent='disabled'; }
      if(sb){ sb.textContent='Telegram disabled. Fill in settings below and save.'; sb.style.color='#8b949e'; }
    }
    fetch('/api/telegram/current')
    .then(function(r){ return r.json(); })
    .then(function(c){
      var en = document.getElementById('tgEnabled');
      var cd = document.getElementById('tgCooldown');
      if(en) en.value = c.enabled ? 'true' : 'false';
      if(cd) cd.value = c.cooldown || 14400;
    }).catch(function(){});
  }).catch(function(){});
}

function saveTgSettings(){
  var payload = {
    enabled:  document.getElementById('tgEnabled').value === 'true',
    token:    document.getElementById('tgToken').value,
    chat_id:  document.getElementById('tgChatId').value,
    cooldown: parseInt(document.getElementById('tgCooldown').value) || 14400
  };
  var msg = document.getElementById('tgSaveMsg');
  if(msg) msg.textContent = 'Saving...';
  fetch('/api/telegram/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(msg){ msg.textContent = d.ok ? 'Saved! Bot restarting...' : 'Error: ' + (d.error||'failed'); msg.style.color = d.ok ? '#3fb950' : '#f85149'; }
    setTimeout(function(){ loadTgSettings(); loadTg(); }, 2000);
  }).catch(function(e){ if(msg){ msg.textContent = 'Error: ' + e.message; msg.style.color = '#f85149'; } });
}

function sendTgTest(){
  var btn = document.getElementById('tgTestBig');
  if(btn){ btn.disabled = true; btn.textContent = 'Sending...'; }
  fetch('/api/telegram/test', {method:'POST'})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(btn){ btn.disabled = false; btn.textContent = '&#x1F4E4; Send Test'; }
    alert(d.ok ? ('Sent! ' + (d.message||'')) : ('Failed: ' + (d.message||d.error||'unknown')));
    loadTgSettings();
  }).catch(function(e){ if(btn){ btn.disabled = false; btn.textContent = '&#x1F4E4; Send Test'; } alert('Error: ' + e.message); });
}

function findChatId(){
  var el = document.getElementById('chatIdResult');
  if(el) el.textContent = 'Checking Telegram for recent messages...';
  fetch('/api/telegram/getupdates')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(!d.ok){ if(el) el.textContent = 'Error: ' + d.error; return; }
    if(!d.chats || !d.chats.length){ if(el) el.textContent = 'No messages found. Send a message to your bot first, then try again.'; return; }
    var c = d.chats[0];
    document.getElementById('tgChatId').value = c.chat_id;
    if(el){ el.textContent = 'Found: ' + c.chat_id + ' (' + c.first_name + ' ' + c.username + ') \u2014 auto-filled above'; el.style.color = '#3fb950'; }
  }).catch(function(e){ if(el) el.textContent = 'Error: ' + e.message; });
}

function savePw(){
  var pw = document.getElementById('newPw').value;
  if(!pw){ alert('Enter a password'); return; }
  fetch('/api/settings/password', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    var msg = document.getElementById('pwSaveMsg');
    if(msg){ msg.textContent = d.ok ? 'Saved!' : 'Error: ' + (d.error||'failed'); msg.style.color = d.ok ? '#3fb950' : '#f85149'; }
  }).catch(function(e){ alert('Error: ' + e.message); });
}

// ─── utils ───
function setText(id, val){
  var el = document.getElementById(id);
  if(el) el.textContent = val;
}

function fmtTime(iso){
  if(!iso) return '\u2014';
  try{
    var d = new Date(iso);
    if(isNaN(d)) return iso;
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'})
      + ' ' + d.toLocaleDateString([], {month:'short', day:'numeric'});
  }catch(e){ return iso; }
}

function fmtSecs(s){
  if(s < 60) return s + 's';
  if(s < 3600) return Math.round(s/60) + 'm';
  return Math.round(s/3600) + 'h';
}



// ─── backtest page ───
var _btPollTimer = null;

var BT_PRESETS = {
  mega:  'AAPL, MSFT, NVDA, GOOGL, AMZN',
  tech:  'AAPL, MSFT, NVDA, AMD, AVGO, CRM, ADBE, QCOM',
  mixed: 'AAPL, MSFT, NVDA, JPM, V, UNH, XOM, JNJ, WMT, NFLX',
};

function initBacktest(){
  // Load current strategy version
  fetch('/api/strategy')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var ver = (d.strategy || {}).version || 1;
    setText('btStratVer', 'v' + ver);
  }).catch(function(){});

  // Set default dates: 90 days back to today
  var today = new Date();
  var start = new Date(today); start.setDate(today.getDate() - 90);
  var el_end   = document.getElementById('btEnd');
  var el_start = document.getElementById('btStart');
  if(el_end)   el_end.value   = today.toISOString().slice(0,10);
  if(el_start) el_start.value = start.toISOString().slice(0,10);

  // Check if a result is already available
  fetch('/api/backtest/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(d.status === 'done') renderBtResults(d);
    else if(d.status === 'running') startBtPoll();
  }).catch(function(){});
}

function btPreset(key){
  var el = document.getElementById('btSymbols');
  if(el && BT_PRESETS[key]) el.value = BT_PRESETS[key];
}

function btDatePreset(days){
  var today = new Date();
  var start = new Date(today); start.setDate(today.getDate() - days);
  var el_e = document.getElementById('btEnd');
  var el_s = document.getElementById('btStart');
  if(el_e) el_e.value = today.toISOString().slice(0,10);
  if(el_s) el_s.value = start.toISOString().slice(0,10);
}

function cancelBacktest(){
  var btn = document.getElementById('btRunBtn');
  var cnc = document.getElementById('btCancelBtn');
  var msg = document.getElementById('btRunMsg');
  if(cnc) cnc.disabled = true;
  if(msg){ msg.textContent = 'Cancelling...'; msg.style.color = '#d29922'; }
  fetch('/api/backtest/cancel', {method:'POST'})
  .then(function(){ setTimeout(function(){
    if(btn) btn.disabled = false;
    if(cnc){ cnc.disabled=false; cnc.style.display='none'; }
    if(msg) msg.textContent = '';
  }, 1000); })
  .catch(function(){});
}

function runBacktest(){
  var rawSyms = (document.getElementById('btSymbols').value || '').trim();
  var start   = (document.getElementById('btStart').value  || '').trim();
  var end     = (document.getElementById('btEnd').value    || '').trim();
  var msg     = document.getElementById('btRunMsg');

  if(!rawSyms){ if(msg){ msg.textContent='Enter at least one symbol.'; msg.style.color='#f85149'; } return; }
  if(!start || !end){ if(msg){ msg.textContent='Set start and end dates.'; msg.style.color='#f85149'; } return; }
  // Warn if date range is too short for indicators
  var days = Math.round((new Date(end) - new Date(start)) / 86400000);
  if(days < 90 && msg){ msg.textContent = 'Note: short range (<90d). 1H/4H/15m data may be limited. Daily indicators need 60+ bars.'; msg.style.color = '#d29922'; }

  var symbols = rawSyms.split(',').map(function(s){ return s.trim().toUpperCase(); }).filter(Boolean);
  if(symbols.length > 10){ if(msg){ msg.textContent='Max 10 symbols per run.'; msg.style.color='#f85149'; } return; }
  // Basic ticker format check
  var badSyms = symbols.filter(function(s){ return !/^[A-Z]{1,5}(-[A-Z])?$/.test(s); });
  if(badSyms.length){ if(msg){ msg.textContent='Invalid ticker(s): '+badSyms.join(', ')+'. Use uppercase letters only (e.g. AAPL not APPL).'; msg.style.color='#f85149'; } return; }

  if(msg){ msg.textContent = 'Launching...'; msg.style.color = '#58a6ff'; }
  var btn = document.getElementById('btRunBtn');
  if(btn) btn.disabled = true;

  // Hide previous results
  var prog = document.getElementById('btProgress');
  var summ = document.getElementById('btSummarySection');
  var trad = document.getElementById('btTradesSection');
  if(prog) prog.style.display = 'block';
  if(summ) summ.style.display = 'none';
  if(trad) trad.style.display = 'none';
  var diagHide = document.getElementById('btDiagSection');
  if(diagHide) diagHide.style.display = 'none';
  ['btTFPanel','btEquitySection','btMonthlySection','btSymSection'].forEach(function(id){
    var el = document.getElementById(id); if(el) el.style.display='none';
  });
  var dsEl = document.getElementById('btDataStatus');
  if(dsEl) dsEl.innerHTML = '';

  fetch('/api/backtest/run', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({symbols: symbols, start_date: start, end_date: end})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(d.ok){
      if(msg){ msg.textContent = 'Running...'; msg.style.color = '#d29922'; }
      startBtPoll();
    } else {
      if(msg){ msg.textContent = 'Error: ' + (d.error||'failed'); msg.style.color = '#f85149'; }
      if(btn) btn.disabled = false;
    }
  }).catch(function(e){
    if(msg){ msg.textContent = 'Error: ' + e.message; msg.style.color = '#f85149'; }
    if(btn) btn.disabled = false;
  });
}

function startBtPoll(){
  if(_btPollTimer) clearInterval(_btPollTimer);
  _btPollTimer = setInterval(pollBtStatus, 1500);
}

function pollBtStatus(){
  fetch('/api/backtest/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var bar = document.getElementById('btProgressBar');
    var pmsg = document.getElementById('btProgressMsg');
    if(bar)  bar.style.width = (d.progress||0) + '%';
    if(pmsg) pmsg.textContent = d.progress_msg || '';

    var cancelBtn = document.getElementById('btCancelBtn');
    if(cancelBtn) cancelBtn.style.display = (d.status==='running') ? 'inline-block' : 'none';
    if(d.status === 'done' || d.status === 'error' || d.status === 'idle'){
      clearInterval(_btPollTimer);
      _btPollTimer = null;
      var btn = document.getElementById('btRunBtn');
      var msg = document.getElementById('btRunMsg');
      if(btn) btn.disabled = false;
      var prog = document.getElementById('btProgress');
      if(prog) prog.style.display = 'none';
      if(d.status === 'error'){
        if(msg){ msg.textContent = 'Error: ' + (d.error||'failed'); msg.style.color='#f85149'; }
      } else if(d.status === 'idle'){
        if(msg){ msg.textContent = 'Cancelled.'; msg.style.color='#8b949e'; }
      } else {
        if(msg){ msg.textContent = ''; }
        renderBtResults(d);
      }
    }
  }).catch(function(){});
}

function renderBtResults(d){
  var s = d.stats || {};
  var summ = document.getElementById('btSummarySection');
  var trad = document.getElementById('btTradesSection');
  if(summ) summ.style.display = 'block';
  if(trad) trad.style.display = 'block';
  // Call all enriched renderers
  renderTFPanel(d);
  renderEquityChart(d);
  renderMonthly(d);
  renderSymStats(d);
  renderExtraStats(d);
  renderMetaBanner(d);
  loadHistory();

  // Data status banner
  var diagAll = d.diagnostics || {};
  var allSyms = d.symbols || [];
  var noDataSyms = allSyms.filter(function(s){
    var st = diagAll[s]; if(!st) return true;
    return Object.keys(st.tf_coverage||{}).length === 0;
  });
  var dataMsg = document.getElementById('btDataStatus');
  if(dataMsg){
    if(noDataSyms.length === 0 && allSyms.length > 0){
      dataMsg.innerHTML = '&#x2714; Data loaded for all ' + allSyms.length + ' symbol(s)';
      dataMsg.style.color = '#3fb950';
    } else if(noDataSyms.length === allSyms.length){
      dataMsg.innerHTML = '&#x26A0; No data loaded for any symbol. Check Diagnostics panel below for fetch errors.';
      dataMsg.style.color = '#f85149';
    } else {
      dataMsg.innerHTML = '&#x26A0; Partial data: ' + (allSyms.length-noDataSyms.length) + '/' + allSyms.length + ' symbols loaded. Missing: ' + noDataSyms.join(', ');
      dataMsg.style.color = '#d29922';
    }
  }

  setText('bs_total',   s.total || 0);
  setText('bs_wr',      (s.win_rate||0) + '%');
  setText('bs_pf',      s.profit_factor != null ? s.profit_factor : 'n/a');
  setText('bs_dd',      (s.max_drawdown_pct||0) + '%');
  setText('bs_avg_ret', (s.avg_return_pct||0) + '%');
  setText('bs_avg_win', (s.avg_win_pct||0)    + '%');
  setText('bs_avg_los', (s.avg_loss_pct||0)   + '%');
  setText('bs_eq',      (s.final_equity||100));
  setText('bs_wlt',     (s.wins||0) + ' / ' + (s.losses||0) + ' / ' + (s.timeouts||0));
  setText('bs_ann_ret', s.annualised_return_pct!=null ? (s.annualised_return_pct>=0?'+':'')+s.annualised_return_pct+'%' : 'n/a');
  setText('bs_hold',    s.avg_hold_days!=null ? s.avg_hold_days+'d' : 'n/a');
  setText('bs_streak_w', s.max_consec_wins  || 0);
  setText('bs_streak_l', s.max_consec_losses || 0);

  // By confluence
  var cEl = document.getElementById('bs_by_conf');
  if(cEl){
    var bc = s.by_confluence || {};
    var html = '';
    Object.keys(bc).sort().forEach(function(k){
      var v = bc[k];
      html += '<div class="stat-item"><span class="stat-label">' + k + ' TFs</span>';
      html += '<span class="stat-value">' + v.total + ' trades &nbsp; WR: ' + v.win_rate + '%</span></div>';
    });
    cEl.innerHTML = html || '<div style="color:#6e7681;font-size:12px">No data</div>';
  }

  // By direction
  var dEl = document.getElementById('bs_by_dir');
  if(dEl){
    var bd = s.by_direction || {};
    var html = '';
    Object.keys(bd).forEach(function(k){
      var v = bd[k];
      var col = k === 'long' ? '#3fb950' : '#f85149';
      html += '<div class="stat-item"><span class="stat-label" style="color:' + col + '">' + k.toUpperCase() + '</span>';
      html += '<span class="stat-value">' + v.total + ' trades &nbsp; WR: ' + v.win_rate + '% &nbsp; Avg: ' + v.avg_ret + '%</span></div>';
    });
    dEl.innerHTML = html || '<div style="color:#6e7681;font-size:12px">No data</div>';
  }

  // By route
  var rEl = document.getElementById('bs_by_route');
  if(rEl){
    var br = s.by_route || {};
    var html = '';
    Object.keys(br).forEach(function(k){
      var v = br[k];
      html += '<div class="stat-item"><span class="stat-label">' + k + '</span>';
      html += '<span class="stat-value">' + v.total + ' trades &nbsp; WR: ' + v.win_rate + '%</span></div>';
    });
    rEl.innerHTML = html || '<div style="color:#6e7681;font-size:12px">No data</div>';
  }

  // Trade table
  var trades = d.trades || [];
  // Diagnostics panel — always visible after a completed run
  var btDiag = document.getElementById('btDiagSection');
  var btDiagContent = document.getElementById('btDiagContent');
  var diagData = d.diagnostics || {};
  var symList  = d.symbols || [];
  if(btDiag) btDiag.style.display = 'block';
  {
    var dhtml = '';
    // Strategy version + confluence
    var sv    = d.strategy_version || '-';
    var sconf = (d.strategy_confluence || {}).min_valid_tfs || '-';
    dhtml += '<div style="font-size:12px;color:#58a6ff;margin-bottom:10px">Strategy v' + sv + ' | Min confluence: ' + sconf + '/4 TFs</div>';
    // Per-symbol table
    dhtml += '<table><tr><th>Symbol</th><th>TF Coverage (bars)</th><th>Candidate signals</th><th>Rejection reasons</th><th>Trades</th></tr>';
    var trades_by_sym = {};
    (d.trades||[]).forEach(function(t){ trades_by_sym[t.symbol] = (trades_by_sym[t.symbol]||0)+1; });
    symList.forEach(function(sym){
      var sd    = diagData[sym] || {};
      var cov   = sd.tf_coverage  || {};
      var fst   = sd.fetch_status || {};
      var fir   = sd.tf_first     || {};
      var las   = sd.tf_last      || {};
      var ferr  = sd.fetch_error  || '';
      var allTfs = ['1D','4H','1H','15m'];
      // Build coverage + status per TF
      var covHtml = '';
      if(ferr && Object.keys(cov).length === 0){
        covHtml = '<span style="color:#f85149;font-weight:700">no data loaded</span>'
                + '<br><span style="font-size:10px;color:#f85149">' + ferr + '</span>';
      } else {
        covHtml = allTfs.map(function(k){
          if(!fst[k]) return '';
          var col   = fst[k]==='ok' ? '#3fb950' : '#f85149';
          var bars  = cov[k]  ? cov[k]+'b'  : '0b';
          var range = (fir[k] && las[k]) ? (' <span style="color:#6e7681">'+fir[k]+' → '+las[k]+'</span>') : '';
          return '<span style="color:'+col+'">'
               + '<b>' + k + '</b>:' + bars + ' [' + fst[k] + ']'
               + '</span>' + range;
        }).filter(Boolean).join('<br>');
        if(!covHtml) covHtml = '<span style="color:#f85149">no data loaded</span>';
      }
      var rej    = sd.rejected   || {};
      var rejStr = Object.keys(rej).map(function(k){ return k+'×'+rej[k]; }).join(', ')
                   || '<span style="color:#3fb950">none</span>';
      var cands  = sd.candidates || 0;
      var tcount = trades_by_sym[sym] || 0;
      dhtml += '<tr>';
      dhtml += '<td><b>' + sym + '</b></td>';
      dhtml += '<td style="font-size:11px;line-height:1.7">' + covHtml + '</td>';
      dhtml += '<td>' + cands + '</td>';
      dhtml += '<td style="font-size:11px;color:#d29922">' + rejStr + '</td>';
      dhtml += '<td><b>' + tcount + '</b></td>';
      dhtml += '</tr>';
    });
    dhtml += '</table>';
    // Explanation of common reasons
    dhtml += '<div style="margin-top:14px;font-size:11px;color:#6e7681">';
    dhtml += '<b>Rejection reason guide:</b> &nbsp;';
    dhtml += '<code>only_N_of_M_tfs</code> = signal scored on N TFs but needed M &nbsp;|&nbsp; ';
    dhtml += '<code>no_indicators</code> = insufficient bars for indicator computation &nbsp;|&nbsp; ';
    dhtml += 'Cooldown = same symbol/direction within 3 days (not counted above)';
    dhtml += '</div>';
    if(btDiagContent) btDiagContent.innerHTML = dhtml;
  }

  setText('btTradeCount', trades.length + ' trades');
  var wrap = document.getElementById('btTradeTable');
  if(wrap){
    if(!trades.length){
      wrap.innerHTML = '<div class="empty-state">No signals generated in this date range with current strategy settings.</div>';
    } else {
      var html = '<table><tr>'
        + '<th>Date</th><th>Symbol</th><th>Dir</th><th>Route</th>'
        + '<th>TFs</th><th>Entry</th><th>Stop</th><th>Target</th>'
        + '<th>RSI</th><th>ATR</th><th>Outcome</th><th>Return</th><th>Bars</th>'
        + '</tr>';
      trades.forEach(function(t){
        var oc = t.outcome === 'WIN' ? '#3fb950' : t.outcome === 'LOSS' ? '#f85149' : '#d29922';
        var dc = t.direction === 'long' ? '#3fb950' : '#f85149';
        var rc = t.return_pct >= 0 ? '#3fb950' : '#f85149';
        html += '<tr>';
        html += '<td style="color:#8b949e">' + t.date + '</td>';
        html += '<td><b>' + t.symbol + '</b></td>';
        html += '<td><span class="tag t' + (t.direction==='long'?'l':'s') + '">' + t.direction.toUpperCase() + '</span></td>';
        html += '<td><span class="tag t' + (t.route==='ETORO'?'e':'i') + '">' + t.route + '</span></td>';
        html += '<td style="font-size:11px;color:#58a6ff">' + (t.valid_count||0) + '/4<br><span style="color:#6e7681">' + (t.tfs_triggered||[]).join(' ') + '</span></td>';
        html += '<td>$' + (t.entry_price||0) + '</td>';
        html += '<td style="color:#f85149">$' + (t.stop_loss||0) + '</td>';
        html += '<td style="color:#3fb950">$' + (t.target_price||0) + '</td>';
        html += '<td>' + (t.rsi||0) + '</td>';
        html += '<td>' + (t.atr||0) + '</td>';
        html += '<td><span style="color:' + oc + ';font-weight:700">' + t.outcome + '</span></td>';
        html += '<td style="color:' + rc + '">' + (t.return_pct >= 0 ? '+' : '') + (t.return_pct||0) + '%</td>';
        html += '<td style="color:#6e7681">' + (t.bars_held||'-') + 'd</td>';
        html += '</tr>';
      });
      html += '</table>';
      wrap.innerHTML = html;
    }
  }
}


// ─── backtest: validation presets ───
var BT_VALID_PRESETS = {
  aapl1y:  { syms: 'AAPL',                               days: 365 },
  mega1y:  { syms: 'AAPL, MSFT, NVDA, GOOGL, AMZN',     days: 365 },
  mixed1y: { syms: 'AAPL, MSFT, NVDA, JPM, V, UNH, XOM, JNJ, WMT, NFLX', days: 365 },
  '90d15m':{ syms: 'AAPL, MSFT, NVDA',                  days: 90  },
};

function btValidPreset(key){
  var p = BT_VALID_PRESETS[key];
  if(!p) return;
  var el = document.getElementById('btSymbols');
  if(el) el.value = p.syms;
  btDatePreset(p.days);
  var msg = document.getElementById('btRunMsg');
  if(msg){
    var note = key==='90d15m'
      ? '90-day window: 15m data available for this range'
      : '1-year validation preset loaded';
    msg.textContent = note; msg.style.color = '#58a6ff';
    setTimeout(function(){ if(msg) msg.textContent=''; }, 3000);
  }
}

// ─── render TF availability panel ───
function renderTFPanel(d){
  var panel = document.getElementById('btTFPanel');
  var cont  = document.getElementById('btTFContent');
  var note  = document.getElementById('btTFNote');
  if(!panel || !cont) return;

  var meta = (d.meta || {});
  var avail = meta.tf_availability || {};
  var allTfs = ['1D','4H','1H','15m'];
  var symsTotal = (d.symbols||[]).length;

  if(!Object.keys(avail).length){ panel.style.display='none'; return; }
  panel.style.display = 'block';

  // 15m note
  var has15m = avail['15m'] && avail['15m'].syms_ok > 0;
  if(!has15m && note){
    note.textContent = '15m unavailable — Yahoo Finance only provides 15m data for the last 60 days. Use a ≤60 day range for 15m coverage.';
    note.style.color = '#d29922';
  } else if(note) { note.textContent = ''; }

  var html = '<table><tr><th>Timeframe</th><th>Symbols loaded</th><th>Max bars</th><th>Coverage</th><th>Limit note</th></tr>';
  allTfs.forEach(function(tf){
    var a = avail[tf] || {};
    var ok    = a.syms_ok || 0;
    var tot   = a.syms_total || symsTotal;
    var bars  = a.max_bars || 0;
    var col   = ok===tot && ok>0 ? '#3fb950' : ok>0 ? '#d29922' : '#f85149';
    var limit = {
      '1D':  'Up to 2 years',
      '4H':  'Up to 730 days (resampled from 1H)',
      '1H':  'Up to 730 days',
      '15m': 'Last 60 days only',
    }[tf] || '';
    var bar_col = ok>0 ? '#e6edf3' : '#6e7681';
    html += '<tr>';
    html += '<td><b>' + tf + '</b></td>';
    html += '<td style="color:'+col+'">' + ok + ' / ' + tot + '</td>';
    html += '<td style="color:'+bar_col+'">' + (bars||'—') + '</td>';
    var pct = tot>0 ? Math.round(ok/tot*100) : 0;
    html += '<td><div style="background:#21262d;border-radius:3px;height:6px;width:120px;display:inline-block">'
          + '<div style="background:'+col+';width:'+pct+'%;height:6px;border-radius:3px"></div></div>'
          + ' <span style="font-size:11px;color:'+col+'">' + pct + '%</span></td>';
    html += '<td style="font-size:11px;color:#6e7681">' + limit + '</td>';
    html += '</tr>';
  });
  html += '</table>';
  cont.innerHTML = html;
}

// ─── render equity curve chart ───
var _btChart = null;
function renderEquityChart(d){
  var sec = document.getElementById('btEquitySection');
  if(!sec) return;
  var eq = (d.stats||{}).equity_with_dates || [];
  if(!eq.length){ sec.style.display='none'; return; }
  sec.style.display = 'block';

  var labels = eq.map(function(p){ return p.d; });
  var values = eq.map(function(p){ return p.e; });

  var ctx = document.getElementById('btEquityChart');
  if(!ctx) return;

  if(_btChart){ try{ _btChart.destroy(); }catch(e){} _btChart=null; }

  var startVal = 100;
  var finalVal = values[values.length-1] || 100;
  var lineCol  = finalVal >= startVal ? '#3fb950' : '#f85149';

  _btChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        borderColor: lineCol,
        backgroundColor: lineCol + '22',
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#6e7681', maxTicksLimit: 8, font:{size:10} }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#6e7681', font:{size:10} }, grid: { color: '#21262d' } }
      }
    }
  });
}

// ─── monthly breakdown ───
function renderMonthly(d){
  var sec  = document.getElementById('btMonthlySection');
  var cont = document.getElementById('btMonthlyContent');
  if(!sec || !cont) return;
  var bm = (d.stats||{}).by_month || {};
  var months = Object.keys(bm).sort();
  if(!months.length){ sec.style.display='none'; return; }
  sec.style.display = 'block';
  var html = '<table><tr><th>Month</th><th>Trades</th><th>Win Rate</th><th>Avg Return</th><th>Bar</th></tr>';
  months.forEach(function(m){
    var v   = bm[m];
    var col = v.avg_ret >= 0 ? '#3fb950' : '#f85149';
    var wrCol = v.win_rate >= 50 ? '#3fb950' : '#f85149';
    var barW = Math.min(Math.abs(v.avg_ret) * 8, 80);
    html += '<tr>';
    html += '<td style="color:#8b949e">' + m + '</td>';
    html += '<td>' + v.total + '</td>';
    html += '<td style="color:'+wrCol+'">' + v.win_rate + '%</td>';
    html += '<td style="color:'+col+'">' + (v.avg_ret>=0?'+':'') + v.avg_ret + '%</td>';
    html += '<td><div style="width:'+barW+'px;height:8px;background:'+col+';border-radius:2px;display:inline-block"></div></td>';
    html += '</tr>';
  });
  html += '</table>';
  cont.innerHTML = html;
}

// ─── per-symbol stats ───
function renderSymStats(d){
  var sec  = document.getElementById('btSymSection');
  var cont = document.getElementById('btSymContent');
  if(!sec || !cont) return;
  var bs = (d.stats||{}).by_symbol || {};
  var syms = Object.keys(bs);
  if(syms.length <= 1){ sec.style.display='none'; return; }  // only show for multi-symbol
  sec.style.display = 'block';
  var html = '<table><tr><th>Symbol</th><th>Trades</th><th>Win Rate</th><th>Avg Return</th></tr>';
  syms.sort().forEach(function(sym){
    var v = bs[sym];
    var col = v.avg_ret >= 0 ? '#3fb950' : '#f85149';
    html += '<tr><td><b>'+sym+'</b></td><td>'+v.total+'</td>';
    html += '<td style="color:'+(v.win_rate>=50?'#3fb950':'#f85149')+'">'+v.win_rate+'%</td>';
    html += '<td style="color:'+col+'">'+(v.avg_ret>=0?'+':'')+v.avg_ret+'%</td></tr>';
  });
  html += '</table>';
  cont.innerHTML = html;
}

// ─── additional stats in returns card ───
function renderExtraStats(d){
  var s = d.stats || {};
  // Append extra stats to the returns card
  var map = {
    'bs_ann_ret': { label: 'Annualised return', val: (s.annualised_return_pct!=null ? (s.annualised_return_pct>0?'+':'')+s.annualised_return_pct+'%' : 'n/a') },
    'bs_hold':    { label: 'Avg hold (days)',   val: s.avg_hold_days != null ? s.avg_hold_days+'d' : 'n/a' },
    'bs_streak_w':{ label: 'Max consec. wins',  val: s.max_consec_wins  || 0 },
    'bs_streak_l':{ label: 'Max consec. losses',val: s.max_consec_losses || 0 },
  };
  Object.keys(map).forEach(function(id){
    var el = document.getElementById(id);
    if(el){
      el.textContent = map[id].val;
      if(id==='bs_ann_ret'){
        el.style.color = (s.annualised_return_pct||0) >= 0 ? '#3fb950' : '#f85149';
      }
    }
  });
}

// ─── run metadata banner ───
function renderMetaBanner(d){
  var el = document.getElementById('btMetaBanner');
  if(!el) return;
  var m = d.meta || {};
  var parts = [];
  if(m.strategy_version) parts.push('Strategy v' + m.strategy_version);
  if(m.confluence_min)   parts.push('Confluence ≥' + m.confluence_min + '/4');
  if(m.days_range)       parts.push(m.days_range + ' days');
  if(m.symbols_count)    parts.push(m.symbols_count + ' symbol(s)');
  if(m.run_timestamp)    parts.push('Run: ' + fmtTime(m.run_timestamp));
  if(m.data_source)      parts.push(m.data_source);
  el.textContent = parts.join('  ·  ');
}

// ─── summary JSON export ───
function exportSummaryJson(){
  fetch('/api/backtest/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(d.status !== 'done'){ alert('No completed run to export.'); return; }
    var summary = {
      meta:        d.meta || {},
      stats:       d.stats || {},
      diagnostics_summary: {},
    };
    // Compact diagnostics
    (d.symbols||[]).forEach(function(sym){
      var diag = (d.diagnostics||{})[sym] || {};
      summary.diagnostics_summary[sym] = {
        tf_coverage:  diag.tf_coverage  || {},
        fetch_status: diag.fetch_status || {},
        tf_first:     diag.tf_first     || {},
        tf_last:      diag.tf_last      || {},
        candidates:   diag.candidates   || 0,
        rejected:     diag.rejected     || {},
      };
    });
    var blob = new Blob([JSON.stringify(summary, null, 2)], {type:'application/json'});
    var a    = document.createElement('a');
    a.href   = URL.createObjectURL(blob);
    a.download = 'backtest_summary_' + (d.start_date||'') + '_to_' + (d.end_date||'') + '.json';
    a.click();
  }).catch(function(e){ alert('Export failed: ' + e.message); });
}

// ─── run history ───
function loadHistory(){
  fetch('/api/backtest/history')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var cont = document.getElementById('btHistoryContent');
    if(!cont) return;
    var runs = d.runs || [];
    if(!runs.length){ cont.innerHTML='<div class="empty-state">No runs recorded yet.</div>'; return; }
    var html = '<table><tr><th>Date</th><th>Symbols</th><th>Range</th><th>Trades</th><th>WR</th><th>PF</th><th>Drawdown</th><th>Ann. Return</th><th>Strategy</th></tr>';
    runs.forEach(function(r){
      var pf  = r.profit_factor != null ? r.profit_factor : 'n/a';
      var ar  = r.annualised_return_pct != null ? (r.annualised_return_pct>0?'+':'')+r.annualised_return_pct+'%' : 'n/a';
      var arC = (r.annualised_return_pct||0) >= 0 ? '#3fb950' : '#f85149';
      var wrC = (r.win_rate||0) >= 50 ? '#3fb950' : '#d29922';
      html += '<tr>';
      html += '<td style="color:#8b949e;font-size:11px">' + fmtTime(r.run_at) + '</td>';
      html += '<td style="font-size:11px">' + (r.symbols||[]).join(', ') + '</td>';
      html += '<td style="font-size:11px;color:#6e7681">' + (r.start_date||'') + ' / ' + (r.days_range||0) + 'd</td>';
      html += '<td>' + (r.total_trades||0) + '</td>';
      html += '<td style="color:'+wrC+'">' + (r.win_rate||0) + '%</td>';
      html += '<td>' + pf + '</td>';
      html += '<td style="color:#f85149">' + (r.max_drawdown_pct||0) + '%</td>';
      html += '<td style="color:'+arC+'">' + ar + '</td>';
      html += '<td style="font-size:11px;color:#58a6ff">v' + (r.strategy_version||1) + '</td>';
      html += '</tr>';
    });
    html += '</table>';
    cont.innerHTML = html;
  }).catch(function(){});
}


// ─── strategy page ───
var _stratData = {};

function loadStrategy(){
  fetch('/api/strategy')
  .then(function(r){ return r.json(); })
  .then(function(d){
    _stratData = d;
    var cfg = d.strategy || {};
    var ver = cfg.version || 1;
    var upd = cfg.updated_at ? fmtTime(cfg.updated_at) : 'defaults';
    var by  = cfg.updated_by || '';
    setText('stratBadge',   'v' + ver);
    setText('stratUpdated', 'Last saved: ' + upd + (by && by !== 'defaults' ? ' by ' + by : ''));

    // Timeframes
    var tfWrap = document.getElementById('tfToggles');
    if(tfWrap){
      var tfs = cfg.timeframes || {};
      var order = ['tf_1d','tf_4h','tf_1h','tf_15m'];
      var labels = {'tf_1d':'Daily (1D)','tf_4h':'4-Hour (4H)','tf_1h':'1-Hour (1H)','tf_15m':'15-Min (15m)'};
      var html = '';
      order.forEach(function(k){
        var en = (tfs[k] && tfs[k].enabled !== false) ? true : false;
        html += '<div class="stat-item"><span class="stat-label">' + (labels[k]||k) + '</span>';
        html += '<label style="display:flex;align-items:center;gap:8px;cursor:pointer">';
        html += '<input type="checkbox" id="tf_' + k + '" ' + (en?'checked':'') + ' style="width:16px;height:16px;cursor:pointer">';
        html += '<span style="font-size:12px;color:#8b949e">' + (en?'Enabled':'Disabled') + '</span>';
        html += '</label></div>';
      });
      tfWrap.innerHTML = html;
      // Update label on change
      order.forEach(function(k){
        var el = document.getElementById('tf_' + k);
        if(el) el.addEventListener('change', function(){
          var lbl = el.parentElement.querySelector('span');
          if(lbl) lbl.textContent = el.checked ? 'Enabled' : 'Disabled';
        });
      });
    }

    // Confluence
    setInput('s_min_valid_tfs', (cfg.confluence||{}).min_valid_tfs);

    // Long rules
    var lg = cfg.long || {};
    setInput('s_long_rsi_min',  lg.rsi_min);
    setInput('s_long_rsi_max',  lg.rsi_max);
    setInput('s_long_macd_gt',  lg.macd_hist_gt);
    setInput('s_long_ema_tol',  lg.ema_tolerance);
    setInput('s_long_vwap_min', lg.vwap_dev_min);
    setInput('s_long_vol_min',  lg.vol_ratio_min);

    // Short rules
    var sh = cfg.short || {};
    setInput('s_short_rsi_min',  sh.rsi_min);
    setInput('s_short_macd_lt',  sh.macd_hist_lt);
    setInput('s_short_ema_tol',  sh.ema_tolerance);
    setInput('s_short_vwap_max', sh.vwap_dev_max);
    setInput('s_short_vol_min',  sh.vol_ratio_min);

    // Risk
    var rk = cfg.risk || {};
    setInput('s_atr_stop',   rk.atr_stop_mult);
    setInput('s_atr_target', rk.atr_target_mult);

    // Routing
    var rt = cfg.routing || {};
    setInput('s_etoro_min', rt.etoro_min_tfs);
    setInput('s_ibkr_min',  rt.ibkr_min_tfs);

    // Audit trail
    renderAudit(d.audit || []);
  }).catch(function(e){ console.error('loadStrategy', e); });
}

function renderAudit(entries){
  var wrap = document.getElementById('auditWrap');
  if(!wrap) return;
  if(!entries.length){
    wrap.innerHTML = '<div class="empty-state">No changes recorded yet.</div>';
    return;
  }
  var html = '<table><tr><th>Time</th><th>Version</th><th>By</th><th>Confluence</th><th>Long RSI</th><th>Short RSI</th><th>ATR Stop/Target</th><th>Routing</th></tr>';
  entries.forEach(function(e){
    var ts   = e.ts   ? fmtTime(e.ts) : '-';
    var conf = (e.confluence||{}).min_valid_tfs || '-';
    var lrsi = ((e.long||{}).rsi_min||'-') + '-' + ((e.long||{}).rsi_max||'-');
    var srsi = (e.short||{}).rsi_min || '-';
    var atr  = ((e.risk||{}).atr_stop_mult||'-') + '/' + ((e.risk||{}).atr_target_mult||'-');
    var rt   = ((e.routing||{}).etoro_min_tfs||'-') + '/' + ((e.routing||{}).ibkr_min_tfs||'-');
    html += '<tr>';
    html += '<td style="color:#8b949e">' + ts + '</td>';
    html += '<td><span style="color:#58a6ff">v' + (e.version||'-') + '</span></td>';
    html += '<td>' + (e.by||'-') + '</td>';
    html += '<td>' + conf + '/4</td>';
    html += '<td>' + lrsi + '</td>';
    html += '<td>&gt;' + srsi + '</td>';
    html += '<td>' + atr + '</td>';
    html += '<td>' + rt + '</td>';
    html += '</tr>';
  });
  html += '</table>';
  wrap.innerHTML = html;
}

function saveStrategy(){
  var msg = document.getElementById('stratSaveMsg');
  if(msg){ msg.textContent = 'Validating...'; msg.style.color = '#8b949e'; }

  var tfs = {};
  ['tf_1d','tf_4h','tf_1h','tf_15m'].forEach(function(k){
    var el = document.getElementById('tf_' + k);
    var cur = (_stratData.strategy && _stratData.strategy.timeframes && _stratData.strategy.timeframes[k]) || {};
    tfs[k] = Object.assign({}, cur, {enabled: el ? el.checked : true});
  });

  var payload = {
    timeframes: tfs,
    confluence: { min_valid_tfs: intVal('s_min_valid_tfs', 3) },
    long: {
      rsi_min:       floatVal('s_long_rsi_min',  30),
      rsi_max:       floatVal('s_long_rsi_max',  75),
      macd_hist_gt:  floatVal('s_long_macd_gt',  0),
      ema_tolerance: floatVal('s_long_ema_tol',  0.005),
      vwap_dev_min:  floatVal('s_long_vwap_min', -0.015),
      vol_ratio_min: floatVal('s_long_vol_min',  0.6),
    },
    short: {
      rsi_min:       floatVal('s_short_rsi_min',  50),
      macd_hist_lt:  floatVal('s_short_macd_lt',  0),
      ema_tolerance: floatVal('s_short_ema_tol',  0.005),
      vwap_dev_max:  floatVal('s_short_vwap_max', 0.015),
      vol_ratio_min: floatVal('s_short_vol_min',  0.6),
    },
    risk: {
      atr_stop_mult:   floatVal('s_atr_stop',   2.0),
      atr_target_mult: floatVal('s_atr_target', 3.0),
    },
    routing: {
      etoro_min_tfs: intVal('s_etoro_min', 4),
      ibkr_min_tfs:  intVal('s_ibkr_min',  2),
    },
  };

  fetch('/api/strategy/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(d.ok){
      if(msg){ msg.textContent = 'Saved! Bot restarting...'; msg.style.color = '#3fb950'; }
      setTimeout(loadStrategy, 2500);
    } else {
      var errs = (d.errors||[d.error||'Unknown error']).join(' | ');
      if(msg){ msg.textContent = 'Error: ' + errs; msg.style.color = '#f85149'; }
    }
  }).catch(function(e){ if(msg){ msg.textContent = 'Error: ' + e.message; msg.style.color = '#f85149'; } });
}

function resetStrategy(){
  if(!confirm('Reset all strategy parameters to defaults? This cannot be undone.')) return;
  var msg = document.getElementById('stratSaveMsg');
  fetch('/api/strategy/reset', {method:'POST'})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(d.ok){
      if(msg){ msg.textContent = 'Reset to defaults. Bot restarting...'; msg.style.color = '#3fb950'; }
      setTimeout(loadStrategy, 2500);
    } else {
      if(msg){ msg.textContent = 'Error: ' + (d.error||'failed'); msg.style.color = '#f85149'; }
    }
  }).catch(function(e){ if(msg){ msg.textContent = 'Error: ' + e.message; msg.style.color = '#f85149'; } });
}

function setInput(id, val){
  var el = document.getElementById(id);
  if(el && val !== undefined && val !== null) el.value = val;
}

function floatVal(id, def){
  var el = document.getElementById(id);
  if(!el) return def;
  var v = parseFloat(el.value);
  return isNaN(v) ? def : v;
}

function intVal(id, def){
  var el = document.getElementById(id);
  if(!el) return def;
  var v = parseInt(el.value);
  return isNaN(v) ? def : v;
}

// init filter buttons visual state
document.addEventListener('DOMContentLoaded', function(){
  setLF('all');
});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

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


# ── Status ──────────────────────────────────────────────────────────────────

def _read_bot_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _db_counts() -> dict:
    counts = {'total': 0, 'ibkr': 0, 'etoro': 0}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        counts['total'] = conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        counts['ibkr']  = conn.execute("SELECT COUNT(*) FROM signals WHERE route='IBKR'").fetchone()[0]
        counts['etoro'] = conn.execute("SELECT COUNT(*) FROM signals WHERE route='ETORO'").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return counts


def _tg_status() -> dict:
    load_dotenv(BASE_DIR / '.env', override=True)
    enabled  = os.getenv('TELEGRAM_ENABLED', 'false').strip().lower() in ('true', '1', 'yes')
    has_tok  = bool(os.getenv('TELEGRAM_BOT_TOKEN', '').strip())
    has_cid  = bool(os.getenv('TELEGRAM_CHAT_ID', '').strip())
    ready    = enabled and has_tok and has_cid
    return {
        'ready':   ready,
        'enabled': enabled,
        'status':  'enabled' if ready else ('misconfigured' if enabled else 'disabled'),
    }


@app.route('/api/status')
@require_auth
def status():
    running = bool(
        subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True).stdout.strip()
    )
    state  = _read_bot_state()
    counts = _db_counts()
    tg     = _tg_status()

    # If pgrep says stopped but state file says running phase → likely crashed
    if not running and state.get('phase') in ('scanning', 'cooldown', 'starting'):
        state['phase'] = 'stopped'

    return jsonify({
        'running':               running,
        'phase':                 state.get('phase', 'stopped'),
        'mode':                  state.get('mode', 'shadow'),
        'cycle':                 state.get('cycle', 0),
        'focus_count':           state.get('focus_count'),
        'scan_interval_secs':    state.get('scan_interval_secs'),
        'uptime_started':        state.get('uptime_started'),
        'last_cycle_at':         state.get('last_cycle_at'),
        'last_cycle_signals':    state.get('last_cycle_signals'),
        'last_cycle_tfs':        state.get('last_cycle_tfs'),
        'last_cycle_tfs_list':   state.get('last_cycle_tfs_list', []),
        'last_cycle_symbols':    state.get('last_cycle_symbols'),
        'last_cycle_duration_s': state.get('last_cycle_duration_s'),
        'next_cycle_at':         state.get('next_cycle_at'),
        'counts':                counts,
        'telegram':              tg,
    })


# ── Signals ─────────────────────────────────────────────────────────────────

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


# ── Logs ─────────────────────────────────────────────────────────────────────

@app.route('/api/logs')
@require_auth
def logs():
    lines = min(int(request.args.get('lines', 100)), 600)
    try:
        with open(LOG_PATH) as f:
            all_lines = f.readlines()
        return jsonify({'lines': [l.rstrip() for l in all_lines[-lines:]]})
    except Exception:
        return jsonify({'lines': ['Log file not found']})


# ── Bot control ──────────────────────────────────────────────────────────────

def _run_bot():
    venv_python = BASE_DIR / 'venv' / 'bin' / 'python3'
    main_py     = BASE_DIR / 'main.py'
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(venv_python), str(main_py)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


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


# ── Telegram ─────────────────────────────────────────────────────────────────

@app.route('/api/telegram/status')
@require_auth
def telegram_status():
    tg = _tg_status()
    return jsonify(tg)


@app.route('/api/telegram/current')
@require_auth
def telegram_current():
    env = _read_env()
    return jsonify({
        'enabled':   env.get('TELEGRAM_ENABLED', 'false').lower() in ('true', '1', 'yes'),
        'cooldown':  int(env.get('TELEGRAM_COOLDOWN_SECS', '14400')),
        'has_token': bool(env.get('TELEGRAM_BOT_TOKEN', '').strip()),
        'has_chat_id': bool(env.get('TELEGRAM_CHAT_ID', '').strip()),
    })


@app.route('/api/telegram/save', methods=['POST'])
@require_auth
def telegram_save():
    data = request.get_json(silent=True) or {}
    try:
        updates = {
            'TELEGRAM_ENABLED':       'true' if data.get('enabled') else 'false',
            'TELEGRAM_COOLDOWN_SECS': str(int(data.get('cooldown', 14400))),
        }
        if data.get('token', '').strip():
            updates['TELEGRAM_BOT_TOKEN'] = data['token'].strip()
        if data.get('chat_id', '').strip():
            updates['TELEGRAM_CHAT_ID'] = data['chat_id'].strip()
        _write_env(updates)
        load_dotenv(BASE_DIR / '.env', override=True)

        def _restart():
            time.sleep(1)
            subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
            time.sleep(1)
            _run_bot()
        threading.Thread(target=_restart, daemon=True).start()

        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/telegram/test', methods=['POST'])
@require_auth
def telegram_test():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    try:
        load_dotenv(BASE_DIR / '.env', override=True)
        from bot.config   import load as _cfg
        from bot.notifier import send_test
        config = _cfg()
        ok, detail = send_test(config)
        return jsonify({'ok': ok, 'message': detail})
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Internal error: {str(e)}'}), 500


@app.route('/api/telegram/getupdates')
@require_auth
def telegram_getupdates():
    load_dotenv(BASE_DIR / '.env', override=True)
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        return jsonify({'ok': False, 'error': 'No bot token configured'})
    try:
        import requests as _req
        resp = _req.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=10)
        data = resp.json()
        if not data.get('ok'):
            return jsonify({'ok': False, 'error': data.get('description', f'HTTP {resp.status_code}')})
        chats = []
        for update in data.get('result', []):
            msg  = update.get('message') or update.get('channel_post') or {}
            chat = msg.get('chat', {})
            if chat:
                chats.append({'chat_id': chat.get('id'), 'type': chat.get('type'),
                               'username': chat.get('username', ''), 'first_name': chat.get('first_name', ''),
                               'text': msg.get('text', '')[:40]})
        seen, unique = set(), []
        for c in chats:
            if c['chat_id'] not in seen:
                seen.add(c['chat_id']); unique.append(c)
        return jsonify({'ok': True, 'chats': unique, 'count': len(data.get('result', []))})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── Settings ─────────────────────────────────────────────────────────────────

@app.route('/api/settings/password', methods=['POST'])
@require_auth
def save_password():
    data = request.get_json(silent=True) or {}
    pw = data.get('password', '').strip()
    if not pw:
        return jsonify({'ok': False, 'error': 'Password cannot be empty'}), 400
    try:
        _write_env({'DASHBOARD_PASSWORD': pw})
        load_dotenv(BASE_DIR / '.env', override=True)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500




# ── Backtest ──────────────────────────────────────────────────────────────────

@app.route('/api/backtest/run', methods=['POST'])
@require_auth
def backtest_run():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest import start_backtest, read_results
    data = request.get_json(silent=True) or {}
    symbols    = data.get('symbols', [])
    start_date = data.get('start_date', '')
    end_date   = data.get('end_date',   '')
    if not symbols or not start_date or not end_date:
        return jsonify({'ok': False, 'error': 'symbols, start_date and end_date required'}), 400
    if len(symbols) > 10:
        return jsonify({'ok': False, 'error': 'Max 10 symbols per run'}), 400
    # Check not already running
    cur = read_results()
    if cur.get('status') == 'running':
        return jsonify({'ok': False, 'error': 'A backtest is already running'}), 409
    start_backtest(symbols, start_date, end_date)
    return jsonify({'ok': True})


@app.route('/api/backtest/history')
@require_auth
def backtest_history():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest import read_history
    return jsonify({'runs': read_history()})


@app.route('/api/backtest/cancel', methods=['POST'])
@require_auth
def backtest_cancel():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest import cancel_backtest
    cancel_backtest()
    return jsonify({'ok': True})


@app.route('/api/backtest/status')
@require_auth
def backtest_status():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest import read_results
    return jsonify(read_results())


@app.route('/api/backtest/csv')
@require_auth
def backtest_csv():
    import sys, io, csv as _csv
    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest import read_results
    from flask import Response
    data   = read_results()
    trades = data.get('trades', [])
    if not trades:
        return Response('No results', mimetype='text/plain')
    keys = ['date','symbol','direction','route','valid_count',
            'tfs_triggered','entry_price','stop_loss','target_price',
            'outcome','return_pct','bars_held',
            'rsi','macd_hist','atr','bb_pos','vwap_dev','vol_ratio',
            'strategy_version']
    buf = io.StringIO()
    w   = _csv.DictWriter(buf, fieldnames=keys, extrasaction='ignore')
    w.writeheader()
    for t in trades:
        row = dict(t)
        row['tfs_triggered'] = ' '.join(t.get('tfs_triggered', []))
        w.writerow(row)
    filename = f"backtest_{data.get('start_date','')}_to_{data.get('end_date','')}.csv"
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


# ── Strategy ─────────────────────────────────────────────────────────────────

@app.route('/api/strategy')
@require_auth
def strategy_get():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.strategy import load as _load, get_audit, DEFAULTS
    return jsonify({
        'strategy': _load(),
        'defaults': DEFAULTS,
        'audit':    get_audit(20),
    })


@app.route('/api/strategy/save', methods=['POST'])
@require_auth
def strategy_save():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.strategy import save as _save
    data = request.get_json(silent=True) or {}
    errors = _save(data, updated_by='dashboard')
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 400
    # Restart bot so it picks up new thresholds
    def _restart():
        import time
        time.sleep(1)
        subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
        time.sleep(1)
        _run_bot()
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/strategy/reset', methods=['POST'])
@require_auth
def strategy_reset():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.strategy import reset as _reset
    _reset()
    def _restart():
        import time
        time.sleep(1)
        subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
        time.sleep(1)
        _run_bot()
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({'ok': True})


# ── .env helpers ─────────────────────────────────────────────────────────────

def _read_env() -> dict:
    env_path = BASE_DIR / '.env'
    result = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _write_env(updates: dict):
    env_path = BASE_DIR / '.env'
    existing, lines_with_comments = {}, []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith('#') or not stripped:
                lines_with_comments.append(line); continue
            if '=' in stripped:
                k, _, v = stripped.partition('=')
                existing[k.strip()] = (v.strip(), len(lines_with_comments))
                lines_with_comments.append(line)
    output_lines, keys_written = [], set()
    for line in lines_with_comments:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            output_lines.append(line); continue
        if '=' in stripped:
            k = stripped.partition('=')[0].strip()
            if k in updates:
                output_lines.append(f'{k}={updates[k]}'); keys_written.add(k)
            else:
                output_lines.append(line)
    for k, v in updates.items():
        if k not in keys_written:
            output_lines.append(f'{k}={v}')
    env_path.write_text('\n'.join(output_lines) + '\n')


if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', '8080'))
    app.run(host='0.0.0.0', port=port, debug=False)

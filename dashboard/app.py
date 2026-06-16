"""
dashboard/app.py
Flask dashboard for Algo Trader v1.
Reads bot_state.json, bot.log, and signals.db only — does not trade.
No JS backtick template literals.
"""
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

# M15.3.A — sys.path bootstrap for script-mode invocation.
# When this file is run directly (e.g. by systemd `ExecStart=python3
# /opt/algo-trader/dashboard/app.py`), Python only puts the script's
# directory on sys.path — not the repo root. That makes the
# `from dashboard.auth import ...` lines below fail with
# ModuleNotFoundError. When imported (tests, `-m`, `-c`), cwd is on
# sys.path and the imports work. To make BOTH paths work, prepend
# the repo root here, before any dashboard.* imports.
import sys
_M153A_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_M153A_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_M153A_REPO_ROOT))

from flask import Flask, request, jsonify, session, Response
from dotenv import load_dotenv

BASE_DIR   = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

# M15.3.A — auth/security hardening primitives.
from dashboard.auth import (
    verify_password as _m153a_verify_password,
    RateLimiter, LoginRateLimited,
    issue_csrf_token as _m153a_issue_csrf,
    rotate_csrf_token as _m153a_rotate_csrf,
    csrf_required,
    harden_app_config as _m153a_harden_app,
    rotate_session as _m153a_rotate_session,
    enforce_session_timeout as _m153a_enforce_timeout,
    is_secure_cookie_mode as _m153a_is_secure_mode,
    ensure_auth_events_schema as _m153a_ensure_schema,
    record_auth_event as _m153a_record_event,
    # M15.3.A.2 — TOTP second factor:
    totp_enabled as _m153a_totp_enabled,
    totp_verify_code as _m153a_totp_verify_code,
)
from dashboard.auth.passwords import password_configured as _m153a_pw_configured
from dashboard.auth.sessions import (
    DEFAULT_IDLE_MIN as _M153A_IDLE_MIN,
    DEFAULT_MAX_HOUR as _M153A_MAX_HOUR,
)
from dashboard.auth.csrf import get_csrf_token as _m153a_get_csrf
from dashboard.auth.trusted_proxy import (
    resolve_client_ip as _m153a_resolve_client_ip,
)

_m153a_log = logging.getLogger("dashboard.m153a")
_m153a_log.setLevel(logging.INFO)

app = Flask(__name__)

# M15.3.A — Stable secret key.
# Old behaviour (pre-M15.3.A): app.secret_key was derived from
# DASHBOARD_PASSWORD, which invalidated all sessions when the password
# changed AND made the secret guessable when the password was the
# default 'changeme'. The new policy:
#   1. If DASHBOARD_SECRET_KEY env is set, use it (preferred).
#   2. Else fall back to a password-derived key with a loud warning.
# The fallback exists strictly to avoid locking the operator out
# during the first M15.3.A deploy — tools/set_dashboard_password.py
# generates and writes a real secret on first run.
_secret_env = os.getenv('DASHBOARD_SECRET_KEY', '').strip()
if _secret_env:
    app.secret_key = _secret_env
    _m153a_log.info("Using DASHBOARD_SECRET_KEY from env (stable across password rotations).")
else:
    # ISSUE-013: in production (DASHBOARD_ENV=production) refuse to start
    # without an explicit secret — a password-derived / default session key
    # is unsafe for production session signing. Dev/local keeps the
    # transitional fallback with a loud warning.
    if os.getenv('DASHBOARD_ENV', '').strip().lower() == 'production':
        raise RuntimeError(
            "DASHBOARD_SECRET_KEY must be set when DASHBOARD_ENV=production. "
            "Refusing to start with a password-derived or default session "
            "key in production. Run `python tools/set_dashboard_password.py` "
            "to write a stable random secret to .env. "
            "See docs/M15_3_A_dashboard_auth.md §4."
        )
    # Transitional fallback (dev/local only). Loud one-time warning at startup.
    _fallback_pw = os.getenv('DASHBOARD_PASSWORD', 'changeme')
    app.secret_key = _fallback_pw + '_algo_session'
    _m153a_log.warning(
        "DASHBOARD_SECRET_KEY not set — falling back to password-derived "
        "secret key (transitional behaviour, dev/local only). Run "
        "`python tools/set_dashboard_password.py` to write a stable "
        "secret to .env. See docs/M15_3_A_dashboard_auth.md §4."
    )

# M15.3.A — Cookie hardening (env-gated Secure flag per correction #2).
_m153a_cookie_diag = _m153a_harden_app(app, logger=_m153a_log)
_m153a_log.info(
    "M15.3.A cookie config: httponly=%s samesite=%s secure=%s idle_min=%s max_hour=%s",
    _m153a_cookie_diag["httponly"], _m153a_cookie_diag["samesite"],
    _m153a_cookie_diag["secure"], _m153a_cookie_diag["idle_min"],
    _m153a_cookie_diag["max_hour"],
)

# M15.3.A — Bind-host warning (soft cutover per Q-A.3 / correction #3).
# Default remains 0.0.0.0 during transition. If the operator hasn't
# explicitly acknowledged plaintext exposure AND hasn't switched to
# 127.0.0.1 + Caddy, log a warning at startup. Loud, not blocking.
_m153a_bind_host = os.getenv('DASHBOARD_BIND_HOST', '0.0.0.0').strip() or '0.0.0.0'
_m153a_accept_plaintext = os.getenv('DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE', '').strip().lower() in ('true', '1', 'yes')
if _m153a_bind_host == '0.0.0.0' and not _m153a_is_secure_mode() and not _m153a_accept_plaintext:
    _m153a_log.warning(
        "DASHBOARD_BIND_HOST=0.0.0.0 and no HTTPS mode — dashboard is "
        "exposed on plaintext HTTP to any reachable network. Either: "
        "(a) set DASHBOARD_BIND_HOST=127.0.0.1 and front with Caddy/TLS "
        "(see docs/M15_3_A_dashboard_auth.md §3), or "
        "(b) set DASHBOARD_ACCEPT_PLAINTEXT_EXPOSURE=yes to silence "
        "this warning if you've explicitly accepted the risk during "
        "the transition window."
    )

# M15.3.A — Password-configured warning.
if not _m153a_pw_configured():
    _m153a_log.error(
        "Dashboard has no real password configured. Set DASHBOARD_PASSWORD_HASH "
        "via `python tools/set_dashboard_password.py` (preferred) or "
        "DASHBOARD_PASSWORD in .env (transitional fallback). The default "
        "'changeme' is REJECTED for login — the dashboard is currently "
        "unreachable until a password is configured."
    )

# M15.3.A — Module-level rate-limiter for /api/login.
_m153a_login_limiter = RateLimiter()

LOG_PATH   = BASE_DIR / 'logs' / 'bot.log'
DB_PATH    = BASE_DIR / 'data' / 'signals.db'
STATE_PATH = BASE_DIR / 'data' / 'bot_state.json'


# M15.3.A — Ensure auth_events table exists at startup.
def _m153a_ensure_auth_schema_once() -> None:
    try:
        c = sqlite3.connect(str(DB_PATH))
        try:
            _m153a_ensure_schema(c)
        finally:
            c.close()
    except Exception as e:
        _m153a_log.error("auth_events schema bootstrap failed: %s", e)
_m153a_ensure_auth_schema_once()


def _m153a_client_ip() -> str:
    """Resolve the real client IP for rate-limiting + audit logging.

    P0-1 fix (M1-M16 audit, 2026-06-05): `X-Forwarded-For` is now
    honoured ONLY when `request.remote_addr` is in the env-configured
    `DASHBOARD_TRUSTED_PROXIES` allowlist (default 127.0.0.1,::1 —
    the Caddy-on-same-host deployment). Previously XFF was honoured
    from any caller, letting an attacker rotate spoofed XFF on every
    request to bypass the login rate-limiter and corrupt audit IPs.

    Also: when XFF is honoured, we use the LAST entry, not the
    first. The last entry is the hop immediately before our trusted
    proxy — i.e. the actual client. Earlier entries were
    attacker-supplied when they were added and remain untrustworthy
    even when the final hop is trusted.

    Returns "" only if no IP is derivable from any source — never
    raises. The pure helper `dashboard.auth.trusted_proxy
    .resolve_client_ip()` carries the implementation so it is
    testable without a Flask request context.
    """
    return _m153a_resolve_client_ip(
        remote_addr=request.remote_addr,
        xff_header=request.headers.get('X-Forwarded-For', ''),
    )


def _m153a_audit(kind: str, *, success: bool,
                  extras: dict | None = None) -> None:
    """Write one auth_events row, swallowing DB errors so audit
    never blocks the request. The errors are logged."""
    try:
        c = sqlite3.connect(str(DB_PATH))
        try:
            _m153a_record_event(
                c,
                kind=kind,
                client_ip=_m153a_client_ip(),
                user_agent=request.headers.get('User-Agent', ''),
                session_id=request.cookies.get(app.config.get('SESSION_COOKIE_NAME', 'session'), ''),
                success=success,
                extras=extras,
            )
        finally:
            c.close()
    except Exception as e:
        _m153a_log.error("auth_events insert failed (kind=%s): %s", kind, e)


# M15.3.A — Per-request session timeout enforcement.
@app.before_request
def _m153a_enforce_session():
    if request.endpoint is None:
        return None
    # Only enforce on authenticated sessions; unauthed flows (e.g.
    # /api/login) handle their own checks. We still call this on
    # every request so that an expired auth marker is cleared
    # PROMPTLY rather than at the next protected endpoint.
    if session.get('authed'):
        valid = _m153a_enforce_timeout(
            session,
            idle_min=_M153A_IDLE_MIN,
            max_hour=_M153A_MAX_HOUR,
        )
        if not valid:
            _m153a_audit('session_expired', success=False)
    return None


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
  <input type="text" id="totp" inputmode="numeric" pattern="[0-9]{6}" maxlength="6"
         autocomplete="one-time-code" placeholder="Authenticator code (if 2FA enabled)"
         style="margin-top:10px;letter-spacing:4px;text-align:center;font-family:monospace"
         onkeydown="if(event.key==='Enter')doLogin()">
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
    <a onclick="go('sentiment')" id="n-sentiment">Sentiment</a>
    <a onclick="go('settings')"  id="n-settings">Settings</a>
    <a onclick="go('risk');loadRisk()" id="n-risk">Risk</a>
    <a onclick="go('riskauth');loadRiskAuthority()" id="n-riskauth">Risk Authority</a>
    <a onclick="go('recovery');loadRecovery()" id="n-recovery" style="color:#f0883e">Recovery</a>
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
      <div class="stat-item">
        <span class="stat-label">Data provider</span>
        <span class="stat-value" id="sysProviderName" style="color:#58a6ff">&mdash;</span>
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
      <button class="btn gy-btn" id="btResetBtn" style="font-size:12px;padding:6px 12px" onclick="resetBacktest()" title="Force-clear stuck/stale running state">&#x21BA; Reset</button>
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

  <!-- Partial/cancelled/timeout warning banner -->
  <div id="btPartialWarn" style="display:none;background:#2d1f00;color:#d29922;border:1px solid #d29922;border-radius:8px;padding:10px 16px;font-size:13px;margin-bottom:12px"></div>

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
        <div class="ct" style="margin-top:16px">By Timeframe</div>
        <div id="bs_by_tf"></div>
        <div class="ct" style="margin-top:16px">By TF Combination</div>
        <div id="bs_by_tf_combo"></div>
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

  <!-- Benchmark Comparison -->
  <div id="btBenchmarkSection" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">
      Strategy vs Benchmark
      <span id="btBmLabel" style="font-size:10px;color:#6e7681;font-weight:400"></span>
    </div>
    <div class="g2" style="margin-bottom:0">
      <div>
        <div class="section-title" style="margin-bottom:10px;color:#58a6ff">Your Strategy</div>
        <div class="stat-item"><span class="stat-label">Final equity</span><span class="stat-value" id="bm_strat_eq">-</span></div>
        <div class="stat-item"><span class="stat-label">Total return</span><span class="stat-value" id="bm_strat_ret">-</span></div>
        <div class="stat-item"><span class="stat-label">Annualised return</span><span class="stat-value" id="bm_strat_ann">-</span></div>
        <div class="stat-item"><span class="stat-label">Max drawdown</span><span class="stat-value" id="bm_strat_dd">-</span></div>
      </div>
      <div>
        <div class="section-title" style="margin-bottom:10px;color:#8b949e" id="bm_sym_label">Benchmark</div>
        <div class="stat-item"><span class="stat-label">Final equity</span><span class="stat-value" id="bm_bm_eq">-</span></div>
        <div class="stat-item"><span class="stat-label">Total return</span><span class="stat-value" id="bm_bm_ret">-</span></div>
        <div class="stat-item"><span class="stat-label">Annualised return</span><span class="stat-value" id="bm_bm_ann">-</span></div>
        <div class="stat-item"><span class="stat-label">Max drawdown</span><span class="stat-value" id="bm_bm_dd">-</span></div>
      </div>
    </div>
    <div style="margin-top:16px;padding:12px;border-radius:8px;text-align:center" id="bm_verdict_box">
      <span id="bm_verdict" style="font-size:16px;font-weight:700"></span>
    </div>
    <canvas id="btBenchmarkChart" style="width:100%;max-height:220px;margin-top:16px"></canvas>
  </div>

  <!-- Trade Scatter Plot -->
  <div id="btScatterSection" style="display:none;margin-bottom:18px" class="card">
    <div class="ct">
      Trade Scatter
      <span style="font-size:10px;color:#6e7681;font-weight:400">RSI at entry vs return%</span>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:10px">
      <button class="btn gy-btn" style="font-size:11px;padding:4px 10px" onclick="setScatterX('rsi')">RSI</button>
      <button class="btn gy-btn" style="font-size:11px;padding:4px 10px" onclick="setScatterX('atr')">ATR</button>
      <button class="btn gy-btn" style="font-size:11px;padding:4px 10px" onclick="setScatterX('confluence')">Confluence</button>
    </div>
    <canvas id="btScatterChart" style="width:100%;max-height:240px"></canvas>
    <div id="btScatterNote" style="font-size:11px;color:#6e7681;margin-top:8px;text-align:center"></div>
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
// ─── M15.3.A: CSRF auto-attach ───
// Every state-changing fetch (POST/PUT/PATCH/DELETE) automatically
// gets the X-CSRF-Token header attached. /api/login is exempt
// because no session/token exists yet. Existing fetch(...) call
// sites do not need to change — we wrap window.fetch once here.
window._csrfToken = null;
(function() {
  var _origFetch = window.fetch;
  window.fetch = function(url, opts) {
    opts = opts || {};
    var method = (opts.method || 'GET').toUpperCase();
    var stateChanging = ['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) !== -1;
    var isLogin = (typeof url === 'string') && url.indexOf('/api/login') !== -1;
    if (stateChanging && !isLogin) {
      opts.headers = opts.headers || {};
      // Don't clobber an explicitly-set header.
      if (!opts.headers['X-CSRF-Token'] && !opts.headers['x-csrf-token']) {
        opts.headers['X-CSRF-Token'] = window._csrfToken || '';
      }
    }
    return _origFetch.call(window, url, opts);
  };
})();
// On page load, if a session is already authed (from a prior login),
// fetch the current CSRF token. The /api/auth/csrf endpoint returns
// 401 if not authed — silently OK, login flow will populate it then.
(function() {
  fetch('/api/auth/csrf', {method: 'GET', credentials: 'same-origin'})
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(d) {
      if (d && d.csrf_token) { window._csrfToken = d.csrf_token; }
    })
    .catch(function() {});
})();

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
  // M15.3.A.2 — TOTP code is optional from the client's perspective.
  // Server ignores it when TOTP is disabled, requires it when enabled.
  var totpEl = document.getElementById('totp');
  var totp = totpEl ? totpEl.value.trim() : '';
  var body = {password: pw};
  if (totp) { body.totp_code = totp; }
  document.getElementById('lerr').textContent = 'Checking...';
  fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
  .then(function(r){ return r.json().then(function(d){ return {ok: r.ok, status: r.status, body: d}; }); })
  .then(function(res){
    var d = res.body || {};
    if(d.ok){
      // M15.3.A — capture the CSRF token so subsequent state-changing
      // requests can attach the X-CSRF-Token header.
      if (d.csrf_token) { window._csrfToken = d.csrf_token; }
      document.getElementById('loginWrap').style.display = 'none';
      document.getElementById('appWrap').style.display   = 'block';
      boot();
    } else if (res.status === 429 && d.error === 'rate_limited') {
      document.getElementById('lerr').textContent = 'Too many failed attempts. Retry in ' + (d.retry_after_sec || '?') + 's.';
    } else if (res.status === 503 && d.error === 'no_password_configured') {
      document.getElementById('lerr').textContent = 'Dashboard has no password configured. Run tools/set_dashboard_password.py on the server.';
    } else if (res.status === 401 && d.error === 'totp_required') {
      // M15.3.A.2 — password verified but TOTP code missing.
      document.getElementById('lerr').textContent = 'Authenticator code required.';
      if (totpEl) { totpEl.focus(); totpEl.style.outline = '2px solid #d29922'; }
    } else {
      document.getElementById('lerr').textContent = 'Incorrect password or authenticator code';
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
  if(p === 'sentiment') loadSentimentConfig();
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
    if(d.status === 'done' || d.status === 'partial' ||
       d.status === 'timeout' || d.status === 'no_data')
      renderBtResults(d);
    else if(d.status === 'running') startBtPoll();
    // cancelled/idle/error: show nothing, ready for new run
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

function resetBacktest(){
  var btn = document.getElementById('btResetBtn');
  var msg = document.getElementById('btRunMsg');
  if(btn) btn.disabled = true;
  if(msg){ msg.textContent = 'Resetting...'; msg.style.color = '#d29922'; }
  fetch('/api/backtest/reset', {method:'POST'})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(btn) btn.disabled = false;
    if(msg){ msg.textContent = 'Reset. Ready for new run.'; msg.style.color = '#3fb950'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 3000);
    // Clear any stale progress display
    var prog = document.getElementById('btProgress');
    if(prog) prog.style.display = 'none';
    var runBtn = document.getElementById('btRunBtn');
    if(runBtn) runBtn.disabled = false;
    var cancelBtn = document.getElementById('btCancelBtn');
    if(cancelBtn) cancelBtn.style.display = 'none';
    if(_btPollTimer){ clearInterval(_btPollTimer); _btPollTimer = null; }
  }).catch(function(e){
    if(btn) btn.disabled = false;
    if(msg){ msg.textContent = 'Reset failed: '+e.message; msg.style.color='#f85149'; }
  });
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
  ['btTFPanel','btEquitySection','btBenchmarkSection','btScatterSection','btMonthlySection','btSymSection'].forEach(function(id){
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
    var TERMINAL = ['done','cancelled','no_data','error','idle'];
    if(TERMINAL.indexOf(d.status) !== -1){
      clearInterval(_btPollTimer);
      _btPollTimer = null;
      var btn = document.getElementById('btRunBtn');
      var msg = document.getElementById('btRunMsg');
      if(btn) btn.disabled = false;
      var prog = document.getElementById('btProgress');
      if(prog) prog.style.display = 'none';
      if(d.status === 'error'){
        if(msg){ msg.textContent = 'Error: ' + (d.error||'failed'); msg.style.color='#f85149'; }
      } else if(d.status === 'no_data'){
        if(msg){ msg.textContent = 'No data loaded — Yahoo may be rate limiting. Check Diagnostics.'; msg.style.color='#f85149'; }
        renderBtResults(d);
      } else if(d.status === 'idle' || d.status === 'cancelled'){
        if(msg){ msg.textContent = 'Cancelled.'; msg.style.color='#8b949e'; }
      } else if(d.status === 'timeout'){
        if(msg){ msg.textContent = 'Timed out — partial results shown.'; msg.style.color='#d29922'; }
        renderBtResults(d);
      } else if(d.status === 'partial'){
        if(msg){ msg.textContent = 'Partial run — not all symbols completed.'; msg.style.color='#d29922'; }
        renderBtResults(d);
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
  renderEquityWithBenchmark(d);
  renderBenchmark(d);
  renderScatter(d);
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
    var cov = st.tf_coverage || {};
    // Symbol has no data if tf_coverage is empty OR all values are 0
    return Object.keys(cov).length === 0 ||
           !Object.keys(cov).some(function(k){ return cov[k] > 0; });
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

  // By timeframe
  var tfBreakEl = document.getElementById('bs_by_tf');
  if(tfBreakEl){
    var btf = s.by_timeframe || {};
    var tfHtml = '';
    ['1D','4H','1H','15m'].forEach(function(tf){
      if(!btf[tf]) return;
      var v = btf[tf];
      var wc = v.win_rate >= 50 ? '#3fb950' : '#d29922';
      var rc = v.avg_ret  >= 0  ? '#3fb950' : '#f85149';
      tfHtml += '<div class="stat-item"><span class="stat-label">' + tf + '</span>';
      tfHtml += '<span class="stat-value">' + v.trades + ' trades &nbsp; ';
      tfHtml += '<span style="color:'+wc+'">' + v.win_rate + '% WR</span> &nbsp; ';
      tfHtml += '<span style="color:'+rc+'">' + (v.avg_ret>=0?'+':'') + v.avg_ret + '%</span>';
      tfHtml += '</span></div>';
    });
    tfBreakEl.innerHTML = tfHtml || '<div style="color:#6e7681;font-size:12px">No data</div>';
  }

  // By TF combination
  var comboEl = document.getElementById('bs_by_tf_combo');
  if(comboEl){
    var bco = s.by_tf_combo || {};
    var ckeys = Object.keys(bco).sort(function(a,b){ return bco[b].total-bco[a].total; });
    var cHtml = '';
    ckeys.forEach(function(k){
      var v = bco[k];
      var wc = v.win_rate >= 50 ? '#3fb950' : '#d29922';
      var rc = v.avg_ret  >= 0  ? '#3fb950' : '#f85149';
      cHtml += '<div class="stat-item"><span class="stat-label" style="font-size:11px">'+k+'</span>';
      cHtml += '<span class="stat-value">'+v.total+' trades &nbsp; ';
      cHtml += '<span style="color:'+wc+'">'+v.win_rate+'% WR</span> &nbsp; ';
      cHtml += '<span style="color:'+rc+'">'+(v.avg_ret>=0?'+':'')+v.avg_ret+'%</span>';
      cHtml += '</span></div>';
    });
    comboEl.innerHTML = cHtml || '<div style="color:#6e7681;font-size:12px">No data</div>';
  }

  // Partial/cancelled/timeout warning
  var warnEl = document.getElementById('btPartialWarn');
  if(warnEl){
    var runSt = d.status || 'done';
    if(runSt === 'partial' || runSt === 'cancelled' || runSt === 'timeout'){
      var symsComp = (d.meta && d.meta.symbols_completed) || 0;
      var symsAll  = (d.meta && d.meta.symbols_count) || (d.symbols||[]).length || 0;
      var rsn  = d.stop_reason ? (' Reason: ' + d.stop_reason + '.') : '';
      warnEl.innerHTML = '\u26A0 <b>' + runSt.toUpperCase() + '</b> run \u2014 '
        + symsComp + '/' + symsAll + ' symbols completed.' + rsn
        + ' Results below are partial only.';
      warnEl.style.display = 'block';
    } else {
      warnEl.style.display = 'none';
    }
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
  // Also show in backtest meta line if present
  var provEl = document.getElementById('sysProviderName');
  if(provEl && m.data_source) provEl.textContent = m.data_source;
  // Fetch provider info once if element not yet populated
  if(provEl && (!provEl.textContent || provEl.textContent === '—')){
    fetch('/api/provider')
    .then(function(r){ return r.json(); })
    .then(function(p){ if(provEl) provEl.textContent = p.name || '—'; })
    .catch(function(){});
  }
  el.textContent = parts.join('  ·  ');
}

// ─── summary JSON export ───
function exportSummaryJson(){
  fetch('/api/backtest/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var exportable = ['done','cancelled','no_data'];
    if(exportable.indexOf(d.status) === -1){
      alert('No exportable results. Run a backtest first.'); return;
    }
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

// ─── sentiment test ───
function loadSentimentConfig(){
  fetch('/api/sentiment/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var el = document.getElementById('sentConfigBody');
    if(!el) return;
    el.innerHTML =
      '<div class="stat-item"><span class="stat-label">Mode</span><span class="stat-value" style="color:#58a6ff">' + (d.mode||'off') + '</span></div>' +
      '<div class="stat-item"><span class="stat-label">Active provider</span><span class="stat-value">' + (d.provider||'disabled') + '</span></div>' +
      '<div class="stat-item"><span class="stat-label">Enabled</span><span class="stat-value" style="color:' + (d.enabled?'#3fb950':'#f85149') + '">' + (d.enabled?'Yes':'No (mode=off)') + '</span></div>' +
      '<div class="stat-item"><span class="stat-label">Threshold</span><span class="stat-value">±' + (d.threshold||0.15) + '</span></div>';
  }).catch(function(){});
}

function runSentimentTest(){
  var sym  = (document.getElementById('sentSymbol').value||'AAPL').trim().toUpperCase();
  var src  = document.getElementById('sentSource').value;
  var btn  = document.getElementById('sentRunBtn');
  var msg  = document.getElementById('sentMsg');
  var card = document.getElementById('sentResultCard');
  var body = document.getElementById('sentResultBody');
  if(!sym){ if(msg) msg.textContent='Enter a symbol.'; return; }
  if(btn) btn.disabled = true;
  if(msg){ msg.textContent='Fetching from ' + src + '...'; msg.style.color='#d29922'; }
  if(card) card.style.display='none';
  var fl   = document.getElementById('sentForceLive') && document.getElementById('sentForceLive').checked ? '&force_live=1' : '';
  fetch('/api/sentiment/test?symbol=' + encodeURIComponent(sym) + '&source=' + encodeURIComponent(src) + fl)
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(btn) btn.disabled = false;
    if(card) card.style.display='block';
    var ok   = d.fetch_success === true;
    var scol = ok ? '#3fb950' : '#f85149';
    var lbl  = d.label||'unavailable';
    var lcol = lbl==='bullish'?'#3fb950': lbl==='bearish'?'#f85149':'#d29922';
    if(msg){
      msg.textContent = ok
        ? ('Done — ' + d.article_count + ' articles, score=' + (d.score!=null?d.score:'n/a') + ', label=' + lbl)
        : ('Fetch failed: ' + (d.error_class||d.status||'unknown'));
      msg.style.color = ok ? '#3fb950' : '#f85149';
    }
    var rows = [
      ['Symbol',          '<b>' + d.symbol + '</b>'],
      ['Source requested',d.source_requested],
      ['Source used',     d.source_used],
      ['Mode configured', d.mode_configured],
      ['Threshold',       '±' + d.threshold],
      ['Cache used',      '<span style="color:'+(d.cache_used?'#d29922':'#3fb950')+';font-weight:700">' + (d.cache_used?'YES (cached result)':'NO (live fetch)') + '</span>'],
      ['Force live',      d.force_live ? '<span style="color:#58a6ff">YES</span>' : 'No'],
      ['Fetch attempted', d.fetch_attempted ? 'Yes' : 'No'],
      ['Fetch success',   '<span style="color:'+scol+';font-weight:700">' + (ok?'YES':'NO') + '</span>'],
      ['Article count',   d.article_count||0],
      ['Score',           d.score!=null ? '<b style="color:'+lcol+'">' + d.score + '</b>' : '<span style="color:#6e7681">null</span>'],
      ['Label',           '<span style="color:'+lcol+';font-weight:700">' + lbl + '</span>'],
      ['Status',          '<code>' + (d.status||'?') + '</code>'],
      ['Error class',     d.error_class ? '<span style="color:#f85149">' + d.error_class + '</span>' : '<span style="color:#3fb950">none</span>'],
      ['Error detail',    d.error ? '<span style="color:#f85149;font-size:11px">' + d.error.slice(0,200) + '</span>' : '—'],
      ['Item keys (debug)', d.item_keys_debug ? '<code style="font-size:11px">' + JSON.stringify(d.item_keys_debug) + '</code>' : null],
      ['Headlines',       d.headlines&&d.headlines.length ? '<ul style="margin:0;padding-left:16px;font-size:11px;color:#8b949e">' + d.headlines.map(function(h){return '<li>'+h+'</li>';}).join('')+'</ul>' : '—'],
      ['Elapsed',         (d.elapsed_s||0) + 's'],
      ['Tested at',       d.tested_at||'—'],
    ];
    body.innerHTML = '<table style="width:100%;font-size:13px;border-collapse:collapse">' +
      rows.filter(function(r){ return r[1] !== null && r[1] !== undefined; }).map(function(r){
        return '<tr><td style="color:#8b949e;padding:4px 8px 4px 0;vertical-align:top;white-space:nowrap;min-width:160px">'+r[0]+'</td><td style="padding:4px 0">'+r[1]+'</td></tr>';
      }).join('') + '</table>';
  }).catch(function(e){
    if(btn) btn.disabled = false;
    if(msg){ msg.textContent='Request failed: '+e.message; msg.style.color='#f85149'; }
  });
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



// ─── benchmark + scatter chart globals ───
var _btBmChart     = null;
var _btScatChart   = null;
var _scatterX      = 'rsi';   // current scatter x-axis
var _lastTrades    = [];       // cached for scatter re-render

// ─── update equity chart to show benchmark overlay ───
function renderEquityWithBenchmark(d){
  var sec = document.getElementById('btEquitySection');
  if(!sec) return;
  var eq = (d.stats||{}).equity_with_dates || [];
  if(!eq.length){ sec.style.display='none'; return; }
  sec.style.display = 'block';

  var ctx = document.getElementById('btEquityChart');
  if(!ctx) return;
  if(_btChart){ try{ _btChart.destroy(); }catch(e){} _btChart=null; }

  var labels = eq.map(function(p){ return p.d; });
  var strat  = eq.map(function(p){ return p.e; });
  var finalVal = strat[strat.length-1] || 100;
  var lineCol  = finalVal >= 100 ? '#3fb950' : '#f85149';

  var datasets = [{
    label: 'Strategy',
    data:  strat,
    borderColor: lineCol,
    backgroundColor: lineCol + '22',
    borderWidth: 2,
    pointRadius: 0,
    fill: true,
    tension: 0.3,
  }];

  // Add benchmark overlay if available
  var bm = d.benchmark || {};
  var bmEq = bm.equity_with_dates || [];
  if(bmEq.length){
    // Align benchmark to strategy labels
    var bmMap = {};
    bmEq.forEach(function(p){ bmMap[p.d] = p.e; });
    var bmData = labels.map(function(dt){
      // Find nearest benchmark date
      return bmMap[dt] || null;
    });
    datasets.push({
      label: bm.label || 'Benchmark',
      data: bmData,
      borderColor: '#8b949e',
      borderDash: [4,3],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      tension: 0.3,
      spanGaps: true,
    });
  }

  _btChart = new Chart(ctx, {
    type: 'line',
    data: { labels: labels, datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: datasets.length > 1,
          labels: { color: '#8b949e', font: { size: 11 } }
        }
      },
      scales: {
        x: { ticks: { color: '#6e7681', maxTicksLimit: 8, font:{size:10} }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#6e7681', font:{size:10} }, grid: { color: '#21262d' } }
      }
    }
  });
}

// ─── benchmark comparison panel ───
function renderBenchmark(d){
  var sec = document.getElementById('btBenchmarkSection');
  if(!sec) return;
  var bm  = d.benchmark || {};
  var s   = d.stats     || {};

  if(!bm.symbol){ sec.style.display='none'; return; }
  sec.style.display = 'block';

  // Label
  var lbl = document.getElementById('btBmLabel');
  var symLbl = document.getElementById('bm_sym_label');
  if(lbl) lbl.textContent = '— ' + (bm.label || bm.symbol);
  if(symLbl) symLbl.textContent = bm.label || bm.symbol;

  // Strategy column
  var stratRet = s.annualised_return_pct != null ? s.annualised_return_pct : 0;
  var stratTotal = s.final_equity != null ? round2(s.final_equity - 100) : 0;
  setText('bm_strat_eq',  (s.final_equity||100).toFixed(2));
  setColText('bm_strat_ret', (stratTotal>=0?'+':'')+stratTotal.toFixed(2)+'%', stratTotal>=0);
  setColText('bm_strat_ann', (stratRet>=0?'+':'')+stratRet+'%', stratRet>=0);
  setText('bm_strat_dd',  (s.max_drawdown_pct||0)+'%');

  // Benchmark column
  var bmRet = bm.annualised_pct != null ? bm.annualised_pct : 0;
  var bmTotal = bm.return_pct != null ? bm.return_pct : 0;
  setText('bm_bm_eq',  (bm.final_equity||100).toFixed(2));
  setColText('bm_bm_ret', (bmTotal>=0?'+':'')+bmTotal.toFixed(2)+'%', bmTotal>=0);
  setColText('bm_bm_ann', (bmRet>=0?'+':'')+bmRet+'%', bmRet>=0);
  setText('bm_bm_dd', (bm.max_drawdown_pct||0)+'%');

  // Verdict
  var diff    = (d.outperformance_pct != null) ? d.outperformance_pct : (stratRet - bmRet);
  var vBox    = document.getElementById('bm_verdict_box');
  var vEl     = document.getElementById('bm_verdict');
  var beat    = diff >= 0;
  if(vBox) vBox.style.background = beat ? '#0d4a1a' : '#4a0d0d';
  if(vEl){
    var sign  = diff >= 0 ? '+' : '';
    var label = beat ? '&#x25B2; Outperforms benchmark' : '&#x25BC; Underperforms benchmark';
    vEl.innerHTML = label + ' by <span style="color:'+(beat?'#3fb950':'#f85149')+'">' + sign + diff.toFixed(2) + '% ann.</span>';
    vEl.style.color = beat ? '#3fb950' : '#f85149';
  }
}

// ─── scatter plot ───
function setScatterX(axis){
  _scatterX = axis;
  if(_lastTrades.length) renderScatterFromTrades(_lastTrades);
}

function renderScatter(d){
  _lastTrades = d.trades || [];
  renderScatterFromTrades(_lastTrades);
}

function renderScatterFromTrades(trades){
  var sec = document.getElementById('btScatterSection');
  if(!sec) return;
  if(!trades.length){ sec.style.display='none'; return; }
  sec.style.display = 'block';

  var ctx = document.getElementById('btScatterChart');
  if(!ctx) return;
  if(_btScatChart){ try{ _btScatChart.destroy(); }catch(e){} _btScatChart=null; }

  var axisX = _scatterX;
  var xLabel = axisX==='rsi' ? 'RSI at Entry' : axisX==='atr' ? 'ATR at Entry' : 'Confluence (TFs)';

  var wins  = [];
  var losses= [];
  var tos   = [];
  trades.forEach(function(t){
    var xVal = axisX==='rsi' ? t.rsi : axisX==='atr' ? t.atr : t.valid_count;
    if(xVal==null || t.return_pct==null) return;
    var pt = { x: xVal, y: t.return_pct };
    if(t.outcome==='WIN')         wins.push(pt);
    else if(t.outcome==='LOSS')   losses.push(pt);
    else                          tos.push(pt);
  });

  // Note text
  var noteEl = document.getElementById('btScatterNote');
  if(noteEl){
    var corr = _pearson(trades.map(function(t){
      return axisX==='rsi'?t.rsi : axisX==='atr'?t.atr : t.valid_count;
    }), trades.map(function(t){ return t.return_pct; }));
    noteEl.textContent = xLabel + ' vs Return  |  Pearson r = ' + (corr||'n/a');
  }

  _btScatChart = new Chart(ctx, {
    type: 'scatter',
    data: {
      datasets: [
        { label:'WIN',     data: wins,   backgroundColor:'#3fb95088', pointRadius:5 },
        { label:'LOSS',    data: losses, backgroundColor:'#f8514988', pointRadius:5 },
        { label:'TIMEOUT', data: tos,    backgroundColor:'#d2992288', pointRadius:5 },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color:'#8b949e', font:{size:11} } },
        tooltip: {
          callbacks: {
            label: function(ctx){
              return ctx.dataset.label+': x='+ctx.parsed.x.toFixed(2)+' ret='+ctx.parsed.y.toFixed(2)+'%';
            }
          }
        }
      },
      scales: {
        x: { title: { display:true, text:xLabel, color:'#8b949e', font:{size:11} },
             ticks:{ color:'#6e7681', font:{size:10} }, grid:{color:'#21262d'} },
        y: { title: { display:true, text:'Return %', color:'#8b949e', font:{size:11} },
             ticks:{ color:'#6e7681', font:{size:10} }, grid:{color:'#21262d'},
             afterDataLimits: function(scale){
               scale.min = Math.min(scale.min, -1);
               scale.max = Math.max(scale.max, 1);
             }
           }
      }
    }
  });
}

function _pearson(xs, ys){
  var n = xs.length;
  if(n<2) return null;
  xs = xs.filter(function(x){ return x!=null; });
  ys = ys.filter(function(y){ return y!=null; });
  n = Math.min(xs.length, ys.length);
  if(n<2) return null;
  var mx=0,my=0;
  for(var i=0;i<n;i++){ mx+=xs[i]; my+=ys[i]; }
  mx/=n; my/=n;
  var num=0,dx2=0,dy2=0;
  for(var i=0;i<n;i++){
    var dx=xs[i]-mx, dy=ys[i]-my;
    num+=dx*dy; dx2+=dx*dx; dy2+=dy*dy;
  }
  var denom=Math.sqrt(dx2*dy2);
  return denom===0 ? null : (num/denom).toFixed(3);
}

function setColText(id, val, positive){
  var el=document.getElementById(id);
  if(!el) return;
  el.textContent = val;
  el.style.color = positive ? '#3fb950' : '#f85149';
}

function round2(v){ return Math.round(v*100)/100; }


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

<!-- ════════════════ SENTIMENT ════════════════ -->
<div id="sentiment" class="page">

  <div class="card" style="margin-bottom:18px">
    <div class="ct">Sentiment Test — On-Demand Source Verification</div>
    <div style="font-size:12px;color:#8b949e;margin-bottom:14px">
      Test any sentiment source directly. Shows real fetch result — success or exact failure reason.
      Does not wait for a scan cycle.
    </div>
    <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:14px">
      <div>
        <label style="font-size:12px;color:#8b949e;display:block;margin-bottom:4px">Symbol</label>
        <input id="sentSymbol" type="text" value="AAPL" maxlength="10"
          style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 10px;border-radius:6px;font-size:13px;width:100px;text-transform:uppercase">
      </div>
      <div>
        <label style="font-size:12px;color:#8b949e;display:block;margin-bottom:4px">Source</label>
        <select id="sentSource"
          style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:7px 10px;border-radius:6px;font-size:13px">
          <option value="yfinance_news">yfinance_news</option>
          <option value="google_news">google_news</option>
          <option value="alphavantage_news">alphavantage_news (needs ALPHAVANTAGE_KEY)</option>
        </select>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <button class="btn gn" onclick="runSentimentTest()" id="sentRunBtn"
          style="padding:8px 18px;font-size:13px">&#x25BA; Run Test</button>
        <label style="font-size:12px;color:#8b949e;display:flex;align-items:center;gap:5px;cursor:pointer">
          <input type="checkbox" id="sentForceLive" style="cursor:pointer">
          Force live (bypass cache)
        </label>
      </div>
    </div>
    <div id="sentMsg" style="font-size:12px;min-height:16px"></div>
  </div>

  <div id="sentResultCard" class="card" style="display:none;margin-bottom:18px">
    <div class="ct">Result</div>
    <div id="sentResultBody"></div>
  </div>

  <div class="card">
    <div class="ct">Active Configuration</div>
    <div id="sentConfigBody" style="font-size:13px;color:#8b949e">Loading...</div>
  </div>

</div>
<!-- END SENTIMENT PAGE -->

<!-- RISK PAGE -->
<div id="risk" class="page">
<h2 style="color:#e6edf3;margin:0 0 16px">Portfolio Risk</h2>

<!-- Status Banner -->
<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
  <div id="riskBanner" style="flex:1;padding:12px 16px;border-radius:6px;font-weight:600;font-size:15px">Loading...</div>
  <button onclick="loadRisk()" style="padding:8px 14px;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;cursor:pointer;font-size:13px">&#x21bb; Refresh</button>
</div>

<!-- M15.1 Gateway Watchdog Panel -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">&#x1F50C; Gateway Watchdog</div>
  <div id="gwBanner" style="padding:8px 12px;border-radius:5px;font-weight:600;font-size:13px;margin-bottom:10px;background:#21262d;color:#8b949e">Loading...</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;font-size:12px">
    <div><span style="color:#8b949e">State:</span> <span id="gwState" style="color:#e6edf3;font-weight:600">&mdash;</span></div>
    <div><span style="color:#8b949e">service_running:</span> <span id="gwService">&mdash;</span></div>
    <div><span style="color:#8b949e">tcp_ok:</span> <span id="gwTcp">&mdash;</span></div>
    <div><span style="color:#8b949e">api_ok:</span> <span id="gwApi">&mdash;</span></div>
    <div><span style="color:#8b949e">API latency:</span> <span id="gwLatency">&mdash;</span></div>
    <div><span style="color:#8b949e">Last success:</span> <span id="gwLastSuccess">&mdash;</span></div>
    <div><span style="color:#8b949e">Last probe:</span> <span id="gwLastProbe">&mdash;</span></div>
    <div><span style="color:#8b949e">Probe age (s):</span> <span id="gwAge" style="font-weight:600">&mdash;</span></div>
    <div><span style="color:#8b949e">Failure count:</span> <span id="gwFailCount">&mdash;</span></div>
    <div><span style="color:#8b949e">Degraded:</span> <span id="gwDegraded">&mdash;</span></div>
    <div><span style="color:#8b949e">Manual action:</span> <span id="gwManual">&mdash;</span></div>
    <div><span style="color:#8b949e">Mode:</span> <span id="gwMode">&mdash;</span></div>
    <div><span style="color:#8b949e">Broker:</span> <span id="gwBroker">&mdash;</span></div>
  </div>
  <div style="margin-top:12px">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:4px">Last 10 events</div>
    <div id="gwEvents" style="font-size:11px;color:#8b949e;max-height:160px;overflow-y:auto">Loading...</div>
  </div>
</div>

<!-- Summary Cards Row -->
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:16px">
  <div class="card" style="padding:14px">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:4px">Kill Switch</div>
    <div id="rKillSwitch" style="font-size:18px;font-weight:700">—</div>
  </div>
  <div class="card" style="padding:14px">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:4px">Open Trades</div>
    <div id="rOpenTrades" style="font-size:18px;font-weight:700">—</div>
  </div>
  <div class="card" style="padding:14px">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:4px">Daily P&amp;L</div>
    <div id="rDailyPnl" style="font-size:18px;font-weight:700">—</div>
  </div>
  <div class="card" style="padding:14px">
    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:4px">Loss Streak</div>
    <div id="rLossStreak" style="font-size:18px;font-weight:700">—</div>
  </div>
</div>

<!-- Daily Loss Block Status -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">Daily Loss Block</div>
  <div id="rDailyLossBlock" style="font-size:13px;color:#8b949e">Loading...</div>
</div>

<!-- P&L Availability Note -->
<div class="card" style="margin-bottom:16px;border-left:3px solid #d29922">
  <div class="ct">Daily P&amp;L Availability</div>
  <div id="rPnlNote" style="font-size:13px;color:#8b949e">Loading...</div>
</div>

<!-- Latest Snapshot -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">Latest Portfolio Snapshot</div>
  <div id="rSnapshot" style="font-size:13px;color:#8b949e">Loading...</div>
</div>

<!-- Recent Risk Rejections -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">Recent Risk Rejections</div>
  <div id="rRejections" style="font-size:13px;color:#8b949e">Loading...</div>
</div>

<!-- M14 Config Editor -->
<div class="card">
  <div class="ct">Portfolio Risk Settings</div>
  <div style="color:#8b949e;font-size:12px;margin-bottom:12px">
    Changes take effect on next signal evaluation. Only RISK_* keys are editable here. Secrets and broker keys are never shown.
  </div>
  <div id="rConfigForm" style="font-size:13px">Loading...</div>
  <div id="rConfigMsg" style="margin-top:8px;font-size:13px"></div>
  <button onclick="saveRiskConfig()" style="margin-top:12px;padding:8px 18px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px">Save Settings</button>
</div>

<!-- M13.4A — Broker Allocation + Budget Controls -->
<div class="card" style="margin-top:16px">
  <div class="ct">&#x1F4B0; Broker Allocation &amp; Budget Controls (M13.4A)</div>
  <div style="color:#8b949e;font-size:12px;margin-bottom:14px;line-height:1.5">
    Configure how much capital each broker is allowed to auto-trade and which brokers are eligible.
    The M13.5 live writer (future) will consult these settings before any order is sent.
    <b>Server-side validation is the gate.</b> No live trading is wired yet. <code>etoro_real</code> is not selectable in M13.4A.
  </div>
  <div id="baMsg" style="margin-bottom:10px;font-size:13px"></div>
  <div id="baForm" style="font-size:13px">Loading...</div>
  <div style="display:flex;gap:8px;margin-top:14px">
    <button onclick="saveBrokerAllocation()" style="padding:8px 18px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px">Save Allocation Policy</button>
    <button onclick="loadBrokerAllocation()" style="padding:8px 14px;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;cursor:pointer;font-size:13px">&#x21bb; Reload</button>
  </div>
</div>

</div><!-- end risk page -->

<div id="riskauth" class="page">
<h2 style="color:#e6edf3;margin:0 0 16px">Risk Authority (M14)</h2>
<div style="margin-bottom:12px;padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:6px;font-size:12px;color:#8b949e">
  Read-only view of the M14 Risk Authority engine state. The dashboard cannot
  modify authority, override decisions, place orders, or contact a broker
  from this tab. Live writes still go through the operator-only CLI
  (<code>tools/etoro_live_write.py</code>).
</div>

<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
  <div id="raBanner" style="flex:1;padding:12px 16px;border-radius:6px;font-weight:600;font-size:14px;background:#21262d;color:#8b949e">Loading...</div>
  <button onclick="loadRiskAuthority()" style="padding:8px 14px;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;cursor:pointer;font-size:13px">&#x21bb; Refresh</button>
</div>

<!-- Combined snapshot summary -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">&#x1F4CA; Latest Risk Snapshot</div>
  <div id="raSnapshotMeta" style="font-size:12px;color:#8b949e;margin-bottom:10px">Loading...</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;font-size:13px">
    <div><span style="color:#8b949e">Combined capital deployed:</span> <span id="raCombinedCap" style="font-weight:600">&mdash;</span></div>
    <div><span style="color:#8b949e">Combined open positions:</span> <span id="raCombinedPos">&mdash;</span></div>
    <div><span style="color:#8b949e">Combined daily loss:</span> <span id="raCombinedLoss">&mdash;</span></div>
    <div><span style="color:#8b949e">Any PnL unknown:</span> <span id="raAnyPnlUnknown">&mdash;</span></div>
    <div><span style="color:#8b949e">Any exposure unknown:</span> <span id="raAnyExpUnknown">&mdash;</span></div>
    <div><span style="color:#8b949e">Trading day (UTC):</span> <span id="raTradingDay">&mdash;</span></div>
  </div>
</div>

<!-- Per-scope state cards -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">&#x1F4CD; Per-Scope State</div>
  <div id="raScopes" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px">Loading...</div>
</div>

<!-- Authority view -->
<div class="card" style="margin-bottom:16px">
  <div class="ct">&#x1F510; Authority &amp; Governor (read-only)</div>
  <div style="font-size:11px;color:#8b949e;margin-bottom:8px">
    Latest authority recorded per scope. Manual reset is design-only in M14.G — there is no button.
  </div>
  <div id="raAuthority" style="font-size:12px">Loading...</div>
</div>

<!-- Recent decisions -->
<div class="card">
  <div class="ct">&#x1F4DC; Latest Risk Decisions</div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;font-size:12px">
    <span style="color:#8b949e">Limit:</span>
    <select id="raDecLimit" onchange="loadRiskAuthority()" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px">
      <option value="10">10</option><option value="20" selected>20</option>
      <option value="50">50</option><option value="100">100</option>
    </select>
    <span style="color:#8b949e">Scope:</span>
    <select id="raDecScope" onchange="loadRiskAuthority()" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px">
      <option value="">all</option>
      <option value="ibkr_live">ibkr_live</option>
      <option value="ibkr_paper">ibkr_paper</option>
      <option value="etoro_real">etoro_real</option>
      <option value="etoro_paper">etoro_paper</option>
    </select>
    <span id="raDecCount" style="color:#8b949e;margin-left:auto">&mdash;</span>
  </div>
  <div id="raDecisions" style="font-size:12px">Loading...</div>
</div>

</div><!-- end riskauth page -->

<!-- ════════════════ M15.3.B — Recovery (manual_reset) ════════════════ -->
<div id="recovery" class="page">
  <h2 style="color:#e6edf3;margin:0 0 16px">Recovery — Operator manual_reset</h2>
  <div class="card" style="border-color:#f0883e">
    <div class="ct" style="color:#f0883e">&#9888;&#65039; M15.3.B — Operator-only authority recovery</div>
    <p style="color:#8b949e;font-size:13px;margin:8px 0 14px">
      Clears the M13.4A allocation-policy <strong>kill switches</strong> (global + per-broker).
      Use this when the M14 Risk Authority Engine is locked down by a kill_switch and you've
      reviewed the situation and want to restore engine authority. This action does
      <strong>not</strong> place orders, cancel orders, modify positions, or restart services
      &mdash; it only clears the safety locks so the engine can resume normal operation
      under its existing gating logic.
    </p>
    <p style="color:#8b949e;font-size:12px;margin:0 0 14px">
      Note: this is <em>different</em> from the file-based emergency kill switch
      (<code>data/kill_switch.json</code>) that you toggle elsewhere &mdash; that one
      is unchanged by this action.
    </p>
    <div id="mrBanner" style="padding:10px;border-radius:4px;background:#161b22;margin-bottom:14px;font-size:13px">
      Click <strong>Load current state</strong> to see kill_switch status and obtain a 60-second preview token.
    </div>
    <button id="mrLoad" onclick="loadRecovery()"
            style="padding:8px 16px;background:#30363d;color:#e6edf3;border:1px solid #444c56;border-radius:4px;cursor:pointer">
      Load current state
    </button>
    <div id="mrPreviewBlock" style="display:none;margin-top:18px">
      <h3 style="color:#e6edf3;margin:0 0 8px;font-size:14px">Current kill_switch state</h3>
      <div id="mrState" style="font-family:monospace;font-size:13px;background:#0d1117;padding:10px;border-radius:4px;border:1px solid #30363d;margin-bottom:14px"></div>
      <h3 style="color:#e6edf3;margin:0 0 8px;font-size:14px">Operator reason (10&ndash;500 chars, required)</h3>
      <p style="color:#8b949e;font-size:11px;margin:0 0 6px">
        This reason is recorded in the audit log. Do not paste passwords, TOTP codes, secrets, API keys, or broker credentials.
      </p>
      <textarea id="mrReason" rows="3" maxlength="500"
                style="width:100%;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:8px;font-family:monospace;font-size:12px"
                placeholder="e.g. 'M13.5.A test left global.kill_switch=true; cleared after verifying broker state.'"></textarea>
      <h3 style="color:#e6edf3;margin:14px 0 8px;font-size:14px">Type <code style="color:#f0883e">RESET</code> to confirm</h3>
      <input id="mrConfirm" type="text" autocomplete="off"
              style="width:160px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:8px;font-family:monospace"
              placeholder="RESET" />
      <h3 style="color:#e6edf3;margin:14px 0 8px;font-size:14px">Current 6-digit authenticator code</h3>
      <p style="color:#8b949e;font-size:11px;margin:0 0 6px">
        Required at reset time. If you just logged in with a code, your authenticator may need
        ~30 seconds to generate a new one before this will accept it.
      </p>
      <input id="mrTotp" type="text" autocomplete="off" maxlength="6"
              style="width:120px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:8px;font-family:monospace;font-size:16px;letter-spacing:4px"
              placeholder="000000" />
      <div style="margin-top:18px;display:flex;gap:10px;align-items:center">
        <button id="mrExecute" onclick="executeRecovery()"
                style="padding:10px 24px;background:#da3633;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold">
          Clear kill switches
        </button>
        <span id="mrCountdown" style="color:#8b949e;font-size:12px"></span>
      </div>
    </div>
    <div id="mrResult" style="margin-top:18px;display:none"></div>
  </div>

  <!-- ── M15.3.C — Audit Export (compliance) ─────────────────────────────── -->
  <div class="card" style="border-color:#3b82f6;margin-top:14px">
    <div class="ct" style="color:#3b82f6">Audit Export (M15.3.C)</div>
    <p style="color:#8b949e;font-size:13px;margin:8px 0 14px">
      Compliance-friendly export of the M15.3 audit trail: all
      <code>auth_events</code> rows (login / TOTP / manual_reset) plus
      <code>risk_decisions</code> rows with <code>source='manual_reset'</code>.
      Read-only — no broker, order, or trading-state writes. The export call
      itself is meta-audited as <code>audit_export_request</code>.
    </p>
    <div style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
      <div>
        <div style="color:#8b949e;font-size:11px;margin-bottom:2px">From (UTC, inclusive)</div>
        <input id="aeFrom" type="date"
                style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px;font-family:monospace" />
      </div>
      <div>
        <div style="color:#8b949e;font-size:11px;margin-bottom:2px">To (UTC, inclusive)</div>
        <input id="aeTo" type="date"
                style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px;font-family:monospace" />
      </div>
      <div>
        <div style="color:#8b949e;font-size:11px;margin-bottom:2px">Format</div>
        <select id="aeFormat"
                style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px;font-family:monospace">
          <option value="jsonl">JSONL (high-fidelity, programmatic)</option>
          <option value="csv">CSV (ZIP — opens in Excel)</option>
        </select>
      </div>
      <button onclick="downloadAuditExport()"
              style="padding:8px 18px;background:#1f6feb;color:#fff;border:none;border-radius:4px;cursor:pointer;font-weight:bold">
        Download export
      </button>
    </div>
    <div id="aeResult" style="margin-top:12px;font-size:12px;color:#8b949e"></div>
  </div>

</div>
<!-- end recovery page -->

<script>
// ── M15.3.C — Audit Export ───────────────────────────────────────────────────
function downloadAuditExport(){
  var from = (document.getElementById('aeFrom').value || '').trim();
  var to   = (document.getElementById('aeTo').value   || '').trim();
  var fmt  = (document.getElementById('aeFormat').value || 'jsonl').trim();
  var res  = document.getElementById('aeResult');
  var qs   = new URLSearchParams();
  qs.set('format', fmt);
  if(from) qs.set('from', from);
  if(to)   qs.set('to',   to);
  res.style.color = '#8b949e';
  res.textContent = 'Building export...';
  fetch('/api/audit-export?' + qs.toString(), {credentials:'include'})
    .then(function(r){
      if(r.status !== 200){
        return r.json().then(function(d){
          res.style.color = '#f85149';
          var msg = 'Export failed: ' + (d.error || 'http_' + r.status);
          if(d.error === 'row_cap_exceeded'){
            msg += ' (max ' + d.max_rows + ' rows; got ' + JSON.stringify(d.row_counts) + ' — narrow your date range)';
          } else if(d.error === 'redaction_violation'){
            msg += ' (defence-in-depth refusal; export_id=' + d.export_id + ', labels=' + JSON.stringify(d.violation_labels) + '). This indicates a bug in audit-row writing upstream.';
          } else if(d.error === 'rate_limited'){
            msg += ' (retry in ' + (d.retry_after_sec || '?') + 's)';
          }
          res.textContent = msg;
        });
      }
      var exportId = r.headers.get('X-Export-Id') || 'unknown';
      var sha      = r.headers.get('X-Export-Sha256') || '';
      var cd       = r.headers.get('Content-Disposition') || '';
      var match    = /filename="([^"]+)"/.exec(cd);
      var filename = match ? match[1] : ('audit_export.' + (fmt === 'csv' ? 'zip' : 'jsonl'));
      return r.blob().then(function(blob){
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        URL.revokeObjectURL(url);
        res.style.color = '#7ee787';
        res.textContent = 'Downloaded ' + filename + '   (export_id=' + exportId + ', sha256=' + sha.substring(0, 16) + '...)';
      });
    })
    .catch(function(e){
      res.style.color = '#f85149';
      res.textContent = 'Network error: ' + String(e);
    });
}
</script>

<!-- end M15.3.C export controls -->

<script>
// ── M15.3.B — Recovery (manual_reset) ────────────────────────────────────────
// GET /api/manual-reset/preview to fetch current kill_switch state + a
// 60-second preview token. Then POST /api/manual-reset with the token,
// the confirm string "RESET", the operator reason, and a fresh TOTP code.
var _mrPreviewToken = null;
var _mrTokenExpiresAt = 0;
var _mrCountdownTimer = null;

function _mrEscapeHtml(s){
  if(s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function _mrRenderState(state){
  if(!state || typeof state !== 'object') return '<em>(no state)</em>';
  var keys = Object.keys(state).sort();
  if(keys.length === 0) return '<em>(no kill_switch flags present in policy)</em>';
  var rows = keys.map(function(k){
    var v = state[k];
    var label = v === true
      ? '<span style="color:#f85149">true (locked)</span>'
      : '<span style="color:#2ea043">false</span>';
    return '<div>' + _mrEscapeHtml(k) + '.kill_switch = ' + label + '</div>';
  });
  return rows.join('');
}

function _mrStartCountdown(){
  var el = document.getElementById('mrCountdown');
  if(!el) return;
  if(_mrCountdownTimer){ clearInterval(_mrCountdownTimer); }
  _mrCountdownTimer = setInterval(function(){
    var remaining = Math.max(0, Math.floor((_mrTokenExpiresAt - Date.now()) / 1000));
    if(remaining <= 0){
      el.textContent = 'Preview token expired. Click "Load current state" again.';
      el.style.color = '#f85149';
      clearInterval(_mrCountdownTimer);
      _mrCountdownTimer = null;
      _mrPreviewToken = null;
    } else {
      el.textContent = 'Preview token: ' + remaining + 's remaining';
      el.style.color = remaining < 15 ? '#f0883e' : '#8b949e';
    }
  }, 1000);
}

function loadRecovery(){
  var banner = document.getElementById('mrBanner');
  banner.textContent = 'Loading...';
  banner.style.background = '#161b22';
  fetch('/api/manual-reset/preview', {credentials:'include'})
    .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
    .then(function(pair){
      var status = pair[0], d = pair[1];
      if(status !== 200 || !d.ok){
        banner.textContent = 'Preview failed: ' + _mrEscapeHtml(d.error || 'unknown');
        banner.style.background = '#3d1d1d';
        banner.style.color = '#f85149';
        return;
      }
      _mrPreviewToken = d.preview_token;
      _mrTokenExpiresAt = Date.now() + (d.preview_token_ttl_seconds * 1000);
      document.getElementById('mrState').innerHTML = _mrRenderState(d.kill_switch_state);
      document.getElementById('mrPreviewBlock').style.display = 'block';
      document.getElementById('mrResult').style.display = 'none';
      banner.textContent = 'Preview loaded. You have 60 seconds to confirm and execute.';
      banner.style.background = '#1d3d1d';
      banner.style.color = '#2ea043';
      _mrStartCountdown();
    })
    .catch(function(e){
      banner.textContent = 'Network error: ' + _mrEscapeHtml(String(e));
      banner.style.background = '#3d1d1d';
      banner.style.color = '#f85149';
    });
}

function executeRecovery(){
  var resultEl = document.getElementById('mrResult');
  var confirm = document.getElementById('mrConfirm').value;
  var reason = document.getElementById('mrReason').value;
  var totp = document.getElementById('mrTotp').value;
  if(!_mrPreviewToken){
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div style="padding:10px;background:#3d1d1d;color:#f85149;border-radius:4px">No preview token. Click "Load current state" first.</div>';
    return;
  }
  if(confirm !== 'RESET'){
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div style="padding:10px;background:#3d1d1d;color:#f85149;border-radius:4px">Type RESET exactly to confirm.</div>';
    return;
  }
  if(!reason || reason.trim().length < 10){
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div style="padding:10px;background:#3d1d1d;color:#f85149;border-radius:4px">Reason must be at least 10 characters.</div>';
    return;
  }
  if(!totp || totp.length !== 6 || !/^\d{6}$/.test(totp)){
    resultEl.style.display = 'block';
    resultEl.innerHTML = '<div style="padding:10px;background:#3d1d1d;color:#f85149;border-radius:4px">Enter your current 6-digit authenticator code.</div>';
    return;
  }
  var csrfToken = (typeof getCsrfToken === 'function') ? getCsrfToken() : '';
  fetch('/api/manual-reset', {
    method: 'POST',
    headers: {'Content-Type':'application/json', 'X-CSRF-Token': csrfToken || ''},
    credentials:'include',
    body: JSON.stringify({
      confirm: confirm,
      preview_token: _mrPreviewToken,
      reason: reason,
      totp_code: totp
    })
  })
    .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
    .then(function(pair){
      var status = pair[0], d = pair[1];
      resultEl.style.display = 'block';
      if(status === 200 && d.ok){
        var sw = d.switches_cleared || [];
        var noop = !!d.noop;
        var msg = noop
          ? '<strong>No-op success.</strong> All kill switches were already cleared. Audit rows written.'
          : '<strong>Success.</strong> Cleared ' + sw.length + ' kill switch(es): ' + _mrEscapeHtml(sw.join(', ')) + '. The M14 engine will re-evaluate authority on its next cycle.';
        resultEl.innerHTML = '<div style="padding:12px;background:#1d3d1d;color:#7ee787;border-radius:4px">' + msg +
          '<div style="margin-top:6px;font-size:11px;color:#8b949e">audit auth_event_id=' + (d.audit && d.audit.auth_event_id) + ', decision_id=' + _mrEscapeHtml(d.audit && d.audit.decision_id) + '</div></div>';
        // Reset the form; force operator to reload preview before next action.
        _mrPreviewToken = null;
        document.getElementById('mrPreviewBlock').style.display = 'none';
        if(_mrCountdownTimer){ clearInterval(_mrCountdownTimer); _mrCountdownTimer = null; }
        document.getElementById('mrCountdown').textContent = '';
        document.getElementById('mrConfirm').value = '';
        document.getElementById('mrReason').value = '';
        document.getElementById('mrTotp').value = '';
      } else {
        var errLabel = d.error || ('http_' + status);
        var hint = d.hint || '';
        var msg;
        if(errLabel === 'totp_invalid' && hint === 'recently_used'){
          msg = '<strong>Invalid authenticator code.</strong> This code was recently used. Wait ~30 seconds for your authenticator to generate a new one.';
        } else if(errLabel === 'totp_invalid'){
          msg = '<strong>Invalid authenticator code.</strong>';
        } else if(errLabel === 'rate_limited'){
          msg = '<strong>Rate-limited.</strong> Retry in ' + (d.retry_after_sec || '?') + ' seconds.';
        } else if(errLabel === 'preview_token_invalid' || errLabel === 'preview_token_missing'){
          msg = '<strong>Preview token expired or invalid.</strong> Click "Load current state" to get a new one.';
          _mrPreviewToken = null;
        } else {
          msg = '<strong>Failed:</strong> ' + _mrEscapeHtml(errLabel);
        }
        resultEl.innerHTML = '<div style="padding:12px;background:#3d1d1d;color:#f85149;border-radius:4px">' + msg + '</div>';
      }
    })
    .catch(function(e){
      resultEl.style.display = 'block';
      resultEl.innerHTML = '<div style="padding:12px;background:#3d1d1d;color:#f85149;border-radius:4px">Network error: ' + _mrEscapeHtml(String(e)) + '</div>';
    });
}

</script>

<script>
// ── Risk page ────────────────────────────────────────────────────────────────
var _riskConfigData = {};

// M15.1 — Gateway watchdog panel loader (uses fetch, NOT undefined api()).
function loadGateway(){
  fetch('/api/gateway/state')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(!d || d.error){
      var b=document.getElementById('gwBanner');
      if(b){ b.textContent='Gateway state error: '+(d&&d.error?d.error:'unknown');
             b.style.background='#9e6a03'; b.style.color='#fff'; }
      return;
    }
    var s = d.state || {};
    var events = d.events || [];

    var banner = document.getElementById('gwBanner');
    var state = s.state || 'unknown';
    banner.textContent = 'STATE: ' + state.toUpperCase();
    var bg = '#21262d', fg = '#8b949e';
    if(state === 'api_up_healthy'){ bg = '#1a7f37'; fg = '#fff'; }
    else if(state === 'service_down' || state === 'service_running_tcp_down'){ bg = '#da3633'; fg = '#fff'; }
    else if(state === 'tcp_up_api_down'){ bg = '#9e6a03'; fg = '#fff'; }
    banner.style.background = bg; banner.style.color = fg;

    function setEl(id, v){
      var el = document.getElementById(id);
      if(el) el.textContent = (v === null || v === undefined) ? '\u2014' : v;
    }
    setEl('gwState', s.state);
    setEl('gwService', s.service_running);
    setEl('gwTcp', s.tcp_ok);
    setEl('gwApi', s.api_ok);
    setEl('gwLatency', (s.api_latency_ms !== null && s.api_latency_ms !== undefined) ? s.api_latency_ms + ' ms' : '\u2014');
    setEl('gwLastSuccess', s.last_success_ts);
    setEl('gwLastProbe', s.last_probe_ts);
    // Highlight stale probes (red text if probe_age > 3*interval, ~3 min default)
    var ageEl = document.getElementById('gwAge');
    if(ageEl){
      var age = s.probe_age_seconds;
      ageEl.textContent = (age === null || age === undefined) ? '\u2014' : age;
      ageEl.style.color = (age !== null && age > 180) ? '#da3633' : '#e6edf3';
    }
    setEl('gwFailCount', s.failure_count);
    setEl('gwDegraded', s.degraded);
    setEl('gwManual', s.manual_action_required);
    setEl('gwMode', s.mode);
    setEl('gwBroker', s.broker_mode);

    // Events table (last 10)
    var evEl = document.getElementById('gwEvents');
    if(evEl){
      if(!events.length){
        evEl.textContent = 'No events recorded yet.';
      } else {
        var rows = events.slice(0,10).map(function(e){
          var ts = (e.ts || '').substring(0,19).replace('T',' ');
          var trans = (e.status_before||'') + ' \u2192 ' + (e.status_after||'');
          return '<tr style="border-bottom:1px solid #21262d">' +
            '<td style="padding:3px 6px;color:#8b949e">'+ts+'</td>' +
            '<td style="padding:3px 6px">'+e.event_type+'</td>' +
            '<td style="padding:3px 6px;color:#8b949e">'+e.broker_mode+'</td>' +
            '<td style="padding:3px 6px;color:#8b949e">'+trans+'</td></tr>';
        }).join('');
        evEl.innerHTML = '<table style="border-collapse:collapse;width:100%;font-size:11px">' +
          '<tr style="border-bottom:1px solid #30363d"><th style="padding:3px 6px;text-align:left;color:#8b949e">ts</th>' +
          '<th style="padding:3px 6px;text-align:left;color:#8b949e">event</th>' +
          '<th style="padding:3px 6px;text-align:left;color:#8b949e">broker</th>' +
          '<th style="padding:3px 6px;text-align:left;color:#8b949e">transition</th></tr>' + rows + '</table>';
      }
    }
  }).catch(function(err){
    var b=document.getElementById('gwBanner');
    if(b){ b.textContent='Gateway load error: '+err;
           b.style.background='#da3633'; b.style.color='#fff'; }
  });
}

function loadRisk(){
  loadGateway();
  // Load state
  fetch('/api/portfolio-risk/state')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if(!d || d.error){
      var b=document.getElementById('riskBanner');
      b.textContent='Error loading risk state: '+(d&&d.error?d.error:'not authenticated');
      b.style.background='#da3633'; b.style.color='#fff'; return;
    }
    var snap = d.latest_snapshot || {};
    var daily = d.daily_state || {};
    var ks = d.kill_switch || {};

    // Status banner
    var status = (snap.risk_status || 'ok').toUpperCase();
    var bannerEl = document.getElementById('riskBanner');
    bannerEl.textContent = 'Risk Status: ' + status;
    bannerEl.style.background = status==='BLOCKED'?'#da3633':status==='WARNING'?'#9e6a03':'#1a7f37';
    bannerEl.style.color = '#fff';

    // Kill switch
    var ksActive = ks.active;
    document.getElementById('rKillSwitch').innerHTML =
      ksActive ? '<span style="color:#da3633">ACTIVE</span>' : '<span style="color:#3fb950">INACTIVE</span>';

    // Open trades
    document.getElementById('rOpenTrades').textContent =
      (snap.open_trade_count !== undefined ? snap.open_trade_count : '—') +
      ' / ' + (d.latest_snapshot ? JSON.parse(snap.policy_json||'{}').max_open_trades||'?' : '?');

    // Daily P&L
    var pnlAvail = daily.daily_pnl_available;
    document.getElementById('rDailyPnl').innerHTML = pnlAvail
      ? (daily.realised_pnl_pct||0).toFixed(2)+'%'
      : '<span style="color:#8b949e">Unavailable</span>';

    // Loss streak
    document.getElementById('rLossStreak').textContent =
      d.persistent_state && d.persistent_state.consecutive_losses
        ? d.persistent_state.consecutive_losses : '0';

    // Daily loss block
    var blockEl = document.getElementById('rDailyLossBlock');
    if(daily.daily_loss_block_active){
      blockEl.innerHTML = '<span style="color:#da3633;font-weight:600">BLOCKED</span> — daily loss limit exceeded. Alert sent: '+(daily.daily_loss_alert_sent?'yes':'no');
    } else {
      blockEl.innerHTML = '<span style="color:#3fb950">No block active</span>';
    }

    // P&L availability note
    document.getElementById('rPnlNote').innerHTML = pnlAvail
      ? 'Source: ' + (daily.daily_pnl_source||'unknown')
      : '<strong>Daily P&amp;L is currently unavailable.</strong> signal_outcomes has no qty/position_size column. '
       +'The system honestly returns daily_pnl_available=false and follows unavailable-data rules. '
       +'Real P&amp;L enforcement will activate when outcome linkage is complete in a future milestone.';

    // Latest snapshot detail
    var snapEl = document.getElementById('rSnapshot');
    if(snap && snap.id){
      var sym = '—', sec = '—';
      try{ var se = JSON.parse(snap.symbol_exposures_json||'{}'); sym = se.symbol ? se.symbol+': '+se.pct+'%' : '—'; }catch(e){}
      try{ var sece = JSON.parse(snap.sector_exposures_json||'{}'); sec = sece.sector ? sece.sector+': '+sece.pct+'%' : '—'; }catch(e){}
      snapEl.innerHTML =
        '<table style="border-collapse:collapse;width:100%">' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Cycle</td><td style="padding:3px 8px">'+snap.cycle_id+'</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Broker</td><td style="padding:3px 8px">'+snap.broker+'</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Portfolio</td><td style="padding:3px 8px">$'+(snap.portfolio_value||0).toLocaleString()+' ('+snap.portfolio_value_source+')</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Open Trades</td><td style="padding:3px 8px">'+snap.open_trade_count+'</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Symbol Exposure</td><td style="padding:3px 8px">'+sym+'</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Sector Exposure</td><td style="padding:3px 8px">'+sec+'</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Cooldown Until</td><td style="padding:3px 8px">'+(snap.cooldown_until||'none')+'</td></tr>' +
        '<tr><td style="padding:3px 8px;color:#8b949e">Recorded</td><td style="padding:3px 8px">'+snap.created_at+'</td></tr>' +
        '</table>';
    } else { snapEl.textContent = 'No snapshot yet.'; }
  }).catch(function(e){
    var b=document.getElementById('riskBanner');
    b.textContent='Risk API error: '+e;
    b.style.background='#da3633'; b.style.color='#fff';
  });

  // Load rejections
  fetch('/api/portfolio-risk/rejections')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var el = document.getElementById('rRejections');
    if(!d || !d.length){ el.textContent = 'No risk rejections recorded.'; return; }
    var rows = d.slice(0,10).map(function(r){
      var checks = '';
      try{ checks = JSON.stringify(JSON.parse(r.risk_checks||'{}'), null, 1).substring(0,200); }catch(e){}
      return '<tr style="border-bottom:1px solid #30363d">' +
        '<td style="padding:4px 8px;color:#8b949e">'+r.timestamp.substring(0,16)+'</td>' +
        '<td style="padding:4px 8px;font-weight:600">'+r.symbol+'</td>' +
        '<td style="padding:4px 8px;color:#da3633">'+r.rejection_reason+'</td>' +
        '<td style="padding:4px 8px;font-size:11px;color:#8b949e;max-width:300px;white-space:pre-wrap">'+checks+'</td>' +
        '</tr>';
    }).join('');
    el.innerHTML = '<table style="border-collapse:collapse;width:100%;font-size:12px">' +
      '<tr style="border-bottom:1px solid #30363d"><th style="padding:4px 8px;text-align:left;color:#8b949e">Time</th>' +
      '<th style="padding:4px 8px;text-align:left">Symbol</th>' +
      '<th style="padding:4px 8px;text-align:left">Reason</th>' +
      '<th style="padding:4px 8px;text-align:left">Checks</th></tr>' + rows + '</table>';
  }).catch(function(){
    document.getElementById('rRejections').textContent = 'Could not load rejections.';
  });

  // Load config
  fetch('/api/portfolio-risk/config')
  .then(function(r){ return r.json(); })
  .then(function(d){
    _riskConfigData = d;
    var el = document.getElementById('rConfigForm');
    if(!d){ el.textContent = 'Could not load config.'; return; }
    var html = '<table style="border-collapse:collapse;width:100%">';
    Object.keys(d).sort().forEach(function(k){
      var cfg = d[k];
      var val = cfg.value !== undefined ? cfg.value : '';
      var srcBadge = cfg.source === 'env'
        ? '<span style="font-size:10px;background:#1f4e2e;color:#3fb950;padding:1px 5px;border-radius:3px;margin-left:4px">env</span>'
        : '<span style="font-size:10px;background:#1a2035;color:#79c0ff;padding:1px 5px;border-radius:3px;margin-left:4px">default</span>';
      var inputHtml;
      if(cfg.type === 'bool'){
        inputHtml = '<label style="display:flex;align-items:center;gap:6px;cursor:pointer">'
          + '<input type="checkbox" id="cfg_'+k+'" '+(val==='true'?'checked':'')+' style="width:16px;height:16px">'
          + '<span style="color:#8b949e;font-size:12px">'+(val==='true'?'true':'false')+'</span></label>';
      } else {
        var placeholder = cfg.optional ? 'blank = disabled' : cfg.default;
        inputHtml = '<input type="number" id="cfg_'+k+'" value="'+val+'" step="any" '
          +(cfg.min!==null?'min="'+cfg.min+'"':'')+' '
          +(cfg.max!==null?'max="'+cfg.max+'"':'')+
          ' placeholder="'+placeholder+'"'
          +' style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:4px 8px;border-radius:4px;width:160px">';
      }
      var rangeHint = (cfg.min!==null && cfg.max!==null)
        ? '<span style="color:#484f58;font-size:11px">'+cfg.min+' – '+cfg.max+'</span>' : '';
      html += '<tr style="border-bottom:1px solid #21262d">' +
        '<td style="padding:6px 8px;font-size:12px;white-space:nowrap">'
          + '<span style="color:#e6edf3">'+k+'</span>'+srcBadge+'</td>' +
        '<td style="padding:6px 8px">'+inputHtml+'</td>' +
        '<td style="padding:6px 8px">'+rangeHint+'</td>' +
        '</tr>';
    });
    html += '</table><div style="margin-top:8px;font-size:11px;color:#484f58">'
      + '<span style="background:#1f4e2e;color:#3fb950;padding:1px 5px;border-radius:3px">env</span> = explicitly set &nbsp;'
      + '<span style="background:#1a2035;color:#79c0ff;padding:1px 5px;border-radius:3px">default</span> = using built-in default</div>';
    el.innerHTML = html;
  }).catch(function(){
    document.getElementById('rConfigForm').textContent = 'Could not load config.';
  });
}

function saveRiskConfig(){
  var payload = {};
  Object.keys(_riskConfigData).forEach(function(k){
    var cfg = _riskConfigData[k];
    var el = document.getElementById('cfg_'+k);
    if(!el) return;
    payload[k] = cfg.type==='bool' ? (el.checked?'true':'false') : el.value;
  });
  var msgEl = document.getElementById('rConfigMsg');
  msgEl.textContent = 'Saving...';
  fetch('/api/portfolio-risk/config',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    credentials:'include',
    body:JSON.stringify(payload)
  }).then(function(r){return r.json();}).then(function(d){
    if(d.errors){
      msgEl.style.color='#da3633';
      msgEl.textContent = 'Errors: '+JSON.stringify(d.errors);
    } else {
      msgEl.style.color='#3fb950';
      msgEl.textContent = 'Saved. '+d.note;
      loadRisk();
    }
  }).catch(function(e){
    msgEl.style.color='#da3633';
    msgEl.textContent = 'Save failed: '+e;
  });
}

// ── M13.4A — Broker Allocation + Budget Controls ────────────────────────────
// M13.4A.1 — UX polish: card layout, status badges, effective summary,
// helper text, money-formatted labels, kill-switch emphasis, zero-cap warning.
// Policy logic, persistence, validation, and DOM IDs are unchanged.
var _baPolicy = null;

function _baEscape(s){
  return String(s===undefined||s===null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function _baFmtMoney(v){
  var n = Number(v);
  if(!isFinite(n)) return '$0.00';
  return '$' + n.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
}

function _baInput(id, value, attrs){
  attrs = attrs || '';
  return '<input id="'+id+'" value="'+_baEscape(value)+'" '+attrs+
         ' style="width:100%;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 8px;font-family:inherit;font-size:13px">';
}
function _baMoneyInput(id, value){
  return '<div style="display:flex;align-items:center;gap:0;border:1px solid #30363d;border-radius:4px;background:#0d1117;overflow:hidden">'+
    '<span style="padding:6px 8px;color:#8b949e;background:#161b22;border-right:1px solid #30363d;font-size:13px">$</span>'+
    '<input id="'+id+'" value="'+_baEscape(value)+'" type="number" min="0" step="0.01" '+
    'style="flex:1;background:transparent;color:#e6edf3;border:0;padding:6px 8px;font-family:inherit;font-size:13px;outline:none">'+
    '</div>';
}
function _baCheckbox(id, checked){
  return '<label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer">'+
    '<input type="checkbox" id="'+id+'"'+(checked?' checked':'')+' style="width:16px;height:16px;cursor:pointer"> '+
    '<span style="color:#8b949e;font-size:12px">enabled</span></label>';
}
function _baKillCheckbox(id, checked){
  // Visually prominent kill-switch toggle.
  var color = checked ? '#f85149' : '#8b949e';
  var weight = checked ? '700' : '500';
  return '<label style="display:inline-flex;align-items:center;gap:10px;cursor:pointer;padding:6px 10px;border:1px solid '+
         (checked?'#da3633':'#30363d')+';border-radius:6px;background:'+
         (checked?'rgba(248,81,73,0.10)':'transparent')+'">'+
    '<input type="checkbox" id="'+id+'"'+(checked?' checked':'')+' style="width:16px;height:16px;cursor:pointer;accent-color:#da3633"> '+
    '<span style="color:'+color+';font-size:12px;font-weight:'+weight+';letter-spacing:0.5px">'+
      (checked?'\u26A0 KILL SWITCH ACTIVE':'kill switch')+
    '</span></label>';
}
function _baRow(label, html, hint){
  var hintHtml = hint ? '<div style="color:#6e7681;font-size:11px;margin-top:2px;line-height:1.4">'+hint+'</div>' : '';
  return '<div style="display:grid;grid-template-columns:230px 1fr;gap:12px;align-items:start;margin-bottom:10px">'+
    '<div><div style="color:#c9d1d9;font-size:12px;font-weight:500">'+label+'</div>'+hintHtml+'</div>'+
    '<div>'+html+'</div></div>';
}

// Status badge: ENABLED / DISABLED / KILL SWITCH ACTIVE.
function _baBadge(state){
  // state: 'enabled' | 'disabled' | 'kill'
  var cfg = {
    enabled:  {label:'ENABLED',            bg:'rgba(63,185,80,0.15)',  bd:'#3fb950', fg:'#3fb950'},
    disabled: {label:'DISABLED',           bg:'rgba(139,148,158,0.15)',bd:'#484f58', fg:'#8b949e'},
    kill:     {label:'\u26A0 KILL SWITCH ACTIVE', bg:'rgba(248,81,73,0.15)', bd:'#da3633', fg:'#f85149'}
  };
  var c = cfg[state] || cfg.disabled;
  return '<span style="display:inline-block;padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:0.6px;'+
         'border:1px solid '+c.bd+';border-radius:999px;background:'+c.bg+';color:'+c.fg+'">'+c.label+'</span>';
}

function _baEffectiveState(block, isGlobal, globalState){
  // Returns 'enabled' | 'disabled' | 'kill'.
  if(block && block.kill_switch) return 'kill';
  if(isGlobal){
    return block && block.auto_trading_enabled ? 'enabled' : 'disabled';
  }
  // For a broker: effective enabled requires global enabled AND broker enabled.
  if(globalState !== 'enabled') return 'disabled';
  return block && block.auto_trading_enabled ? 'enabled' : 'disabled';
}

function _baCard(title, badgeHtml, bodyHtml, accent){
  accent = accent || '#30363d';
  return '<div style="border:1px solid '+accent+';border-radius:8px;margin-bottom:14px;background:#0d1117;overflow:hidden">'+
    '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 14px;border-bottom:1px solid '+accent+';background:#161b22">'+
      '<div style="font-size:12px;font-weight:700;color:#e6edf3;letter-spacing:0.8px;text-transform:uppercase">'+title+'</div>'+
      '<div>'+(badgeHtml||'')+'</div>'+
    '</div>'+
    '<div style="padding:14px">'+bodyHtml+'</div>'+
    '</div>';
}

function _baSummary(states){
  // states: {global:'enabled'|'disabled'|'kill', ibkr:..., etoro:...}
  function row(label, s){
    return '<div style="display:flex;align-items:center;gap:10px">'+
      '<span style="min-width:64px;color:#8b949e;font-size:12px;font-weight:600">'+label+'</span>'+
      _baBadge(s)+'</div>';
  }
  return '<div style="border:1px solid #30363d;border-radius:8px;background:#161b22;padding:12px 14px;margin-bottom:14px">'+
    '<div style="font-size:11px;font-weight:600;color:#8b949e;letter-spacing:0.8px;text-transform:uppercase;margin-bottom:10px">Effective Status</div>'+
    '<div style="display:flex;flex-wrap:wrap;gap:18px">'+
      row('Global', states.global)+
      row('IBKR',   states.ibkr)+
      row('eToro',  states.etoro)+
    '</div></div>';
}

function _baZeroCapsWarning(g, ibkr, etoro){
  var allZero =
    Number(g.max_auto_trading_capital||0) === 0 &&
    Number(ibkr.max_auto_trading_capital||0) === 0 &&
    Number(etoro.max_auto_trading_capital||0) === 0;
  if(!allZero) return '';
  return '<div style="border:1px solid #d29922;background:rgba(210,153,34,0.10);color:#e3b341;'+
    'padding:10px 12px;border-radius:6px;font-size:12px;margin-bottom:14px;line-height:1.5">'+
    '<b>\u26A0 All capital caps are $0.00.</b> Even with auto-trading enabled, no broker can place '+
    'a trade until at least one positive cap is configured.</div>';
}

function renderBrokerAllocation(p){
  var g = p['global'] || {};
  var ibkr = p.ibkr || {};
  var etoro = p.etoro || {};
  var routing = p.routing || {};
  var allowed = (routing.allowed_brokers || []).slice();
  // Defensive: never show etoro_real in the UI.
  allowed = allowed.filter(function(b){ return b !== 'etoro_real'; });
  var defBroker = routing.default_broker || 'paper';

  // Effective states.
  var gState = _baEffectiveState(g, true);
  var ibkrState  = _baEffectiveState(ibkr,  false, gState);
  var etoroState = _baEffectiveState(etoro, false, gState);

  // Global card body.
  var globalBody =
    _baRow('Auto-trading enabled',
           _baCheckbox('ba_g_auto', !!g.auto_trading_enabled),
           'Master toggle. When off, no broker is allowed to auto-trade.') +
    _baRow('Kill switch',
           _baKillCheckbox('ba_g_kill', !!g.kill_switch),
           'Emergency stop for ALL brokers. Overrides every per-broker setting.') +
    _baRow('Max auto-trading capital',
           _baMoneyInput('ba_g_cap', g.max_auto_trading_capital),
           'Hard ceiling across all brokers. Per-broker capital cannot exceed this when greater than $0.');

  function brokerBody(prefix, b){
    return _baRow('Auto-trading enabled',
                  _baCheckbox(prefix+'_auto', !!b.auto_trading_enabled),
                  'Allow the M13.5 live writer to send orders via this broker.') +
           _baRow('Kill switch',
                  _baKillCheckbox(prefix+'_kill', !!b.kill_switch),
                  'Emergency stop for this broker only.') +
           _baRow('Max auto-trading capital',
                  _baMoneyInput(prefix+'_cap', b.max_auto_trading_capital),
                  'Total capital this broker is allowed to deploy.') +
           _baRow('Max single trade amount',
                  _baMoneyInput(prefix+'_single', b.max_single_trade_amount),
                  'Per-order cap. Must be \u2264 max auto-trading capital.') +
           _baRow('Max daily loss',
                  _baMoneyInput(prefix+'_loss', b.max_daily_loss),
                  'Daily realised loss limit before this broker is paused.') +
           _baRow('Max open positions',
                  _baInput(prefix+'_pos', b.max_open_positions, 'type="number" min="0" step="1"'),
                  'Hard cap on simultaneous open positions for this broker.');
  }

  // Routing: editable controls only over the M13.4A whitelist (minus etoro_real).
  var routeOptions = ['paper','ibkr_paper','ibkr_live','etoro_paper'];
  var allowedChecks = routeOptions.map(function(b){
    var checked = allowed.indexOf(b) !== -1;
    return '<label style="display:inline-flex;align-items:center;gap:6px;margin:0 14px 6px 0;padding:4px 8px;'+
      'border:1px solid '+(checked?'#3fb950':'#30363d')+';border-radius:6px;'+
      'background:'+(checked?'rgba(63,185,80,0.08)':'transparent')+';font-size:12px;color:#e6edf3;cursor:pointer">'+
      '<input type="checkbox" data-ba-allowed="'+b+'"'+(checked?' checked':'')+'> '+
      '<code style="font-size:11px;color:#e6edf3">'+b+'</code></label>';
  }).join('');
  var defSelect = '<select id="ba_r_default" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 8px;font-family:inherit;font-size:13px">'+
    routeOptions.map(function(b){
      return '<option value="'+b+'"'+(defBroker===b?' selected':'')+'>'+b+'</option>';
    }).join('') + '</select>';

  var routeOverrides = routing.route_overrides || {};
  function overrideSelect(name, current){
    return '<select id="ba_r_ov_'+name+'" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 8px;font-family:inherit;font-size:13px">'+
      routeOptions.map(function(b){
        return '<option value="'+b+'"'+(current===b?' selected':'')+'>'+b+'</option>';
      }).join('') + '</select>';
  }

  var routingBody =
    _baRow('Allowed brokers', '<div>'+allowedChecks+'</div>',
           'Brokers the policy will let the live writer touch. <code>etoro_real</code> is intentionally not selectable in M13.4A.') +
    _baRow('Default broker', defSelect,
           'Fallback broker when a signal has no route override. Must be in Allowed brokers.') +
    _baRow('Route override: IBKR \u2192', overrideSelect('IBKR', routeOverrides.IBKR || 'ibkr_live'),
           'Where signals tagged IBKR are sent.') +
    _baRow('Route override: ETORO \u2192', overrideSelect('ETORO', routeOverrides.ETORO || 'etoro_paper'),
           'Where signals tagged ETORO are sent.') +
    _baRow('eToro live enabled',
           '<span style="display:inline-block;padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:0.6px;'+
           'border:1px solid #da3633;border-radius:999px;background:rgba(248,81,73,0.10);color:#f85149">FALSE \u2014 LOCKED IN M13.4A</span>',
           'Will unlock in M13.5 with explicit approval. <code>etoro_live_enabled=true</code> is rejected by the server until then.');

  function accentFor(state){
    return state === 'kill' ? '#da3633' : '#30363d';
  }

  var html =
    _baSummary({global:gState, ibkr:ibkrState, etoro:etoroState}) +
    _baZeroCapsWarning(g, ibkr, etoro) +
    _baCard('Global',  _baBadge(gState),     globalBody,           accentFor(gState)) +
    _baCard('IBKR',    _baBadge(ibkrState),  brokerBody('ba_i', ibkr),  accentFor(ibkrState)) +
    _baCard('eToro',   _baBadge(etoroState), brokerBody('ba_e', etoro), accentFor(etoroState)) +
    _baCard('Routing', '', routingBody, '#30363d');
  document.getElementById('baForm').innerHTML = html;
}

function loadBrokerAllocation(){
  var msg = document.getElementById('baMsg');
  msg.textContent = '';
  fetch('/api/broker-allocation', {credentials:'include'})
    .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, body:d}; }); })
    .then(function(o){
      if(!o.ok || o.body.error){
        msg.style.color = '#da3633';
        msg.textContent = 'Load failed: ' + (o.body.error || 'unknown');
        return;
      }
      _baPolicy = o.body.policy;
      renderBrokerAllocation(_baPolicy);
    })
    .catch(function(e){
      msg.style.color = '#da3633';
      msg.textContent = 'Load failed: ' + e;
    });
}

function _baReadForm(){
  function val(id){ var el = document.getElementById(id); return el ? el.value : ''; }
  function chk(id){ var el = document.getElementById(id); return !!(el && el.checked); }
  function num(id){ var v = val(id); return v === '' ? 0 : Number(v); }
  function int_(id){ var v = val(id); return v === '' ? 0 : parseInt(v, 10); }

  var allowed = [];
  document.querySelectorAll('input[data-ba-allowed]').forEach(function(el){
    if(el.checked) allowed.push(el.getAttribute('data-ba-allowed'));
  });

  return {
    version: 1,
    'global': {
      auto_trading_enabled: chk('ba_g_auto'),
      max_auto_trading_capital: num('ba_g_cap'),
      kill_switch: chk('ba_g_kill')
    },
    ibkr: {
      auto_trading_enabled: chk('ba_i_auto'),
      max_auto_trading_capital: num('ba_i_cap'),
      max_single_trade_amount: num('ba_i_single'),
      max_daily_loss: num('ba_i_loss'),
      max_open_positions: int_('ba_i_pos'),
      kill_switch: chk('ba_i_kill')
    },
    etoro: {
      auto_trading_enabled: chk('ba_e_auto'),
      max_auto_trading_capital: num('ba_e_cap'),
      max_single_trade_amount: num('ba_e_single'),
      max_daily_loss: num('ba_e_loss'),
      max_open_positions: int_('ba_e_pos'),
      kill_switch: chk('ba_e_kill')
    },
    routing: {
      default_broker: val('ba_r_default') || 'paper',
      route_overrides: {
        IBKR: val('ba_r_ov_IBKR') || 'ibkr_live',
        ETORO: val('ba_r_ov_ETORO') || 'etoro_paper'
      },
      allowed_brokers: allowed,
      etoro_live_enabled: false
    }
  };
}

function saveBrokerAllocation(){
  var msg = document.getElementById('baMsg');
  msg.style.color = '#8b949e';
  msg.textContent = 'Saving...';
  var payload = _baReadForm();
  fetch('/api/broker-allocation', {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, body:d}; }); })
    .then(function(o){
      if(!o.ok){
        msg.style.color = '#da3633';
        var errs = (o.body && o.body.errors) ? o.body.errors : [{msg: o.body && o.body.error || 'unknown'}];
        msg.innerHTML = 'Save rejected: <code style="font-size:11px">'+
          errs.map(function(e){ return (e.path||'')+': '+(e.code||'')+' '+(e.msg||''); }).join(' | ')+'</code>';
        return;
      }
      msg.style.color = '#3fb950';
      msg.textContent = 'Saved. Reloading to confirm persistence...';
      // Reload from server to confirm persistence round-trip.
      loadBrokerAllocation();
    })
    .catch(function(e){
      msg.style.color = '#da3633';
      msg.textContent = 'Save failed: ' + e;
    });
}

// Wire into existing loadRisk() flow without modifying its body.
var _origLoadRisk = (typeof loadRisk === 'function') ? loadRisk : null;
loadRisk = function(){
  if(_origLoadRisk) _origLoadRisk();
  loadBrokerAllocation();
};

// loadRisk() called directly from nav onclick — no go() override needed

// ── M14.G Risk Authority tab (read-only) ────────────────────────────────────
// All four endpoints are GET-only; never POST/DELETE/PUT/PATCH from this
// tab. No dashboard live-write surface, no manual_reset action.
function _raFmtUsd(v){
  if(v === null || v === undefined) return '<span style="color:#f85149">unknown</span>';
  var n = Number(v);
  if(!isFinite(n)) return '<span style="color:#f85149">NaN</span>';
  return '$' + n.toFixed(2);
}
function _raFmtInt(v){
  if(v === null || v === undefined) return '<span style="color:#f85149">unknown</span>';
  return String(v);
}
function _raFmtBool(v){
  if(v === true) return '<span style="color:#f85149">yes</span>';
  if(v === false) return '<span style="color:#2ea043">no</span>';
  return '&mdash;';
}
function _raStatusBadge(label, known, isZero){
  // Known-zero must look different from unknown. Plan says: never collapse.
  if(label === 'absent') return '<span style="background:#3b1f2b;color:#f85149;padding:2px 6px;border-radius:3px;font-size:11px">absent (unknown)</span>';
  if(!known) return '<span style="background:#3b1f2b;color:#f85149;padding:2px 6px;border-radius:3px;font-size:11px">unknown</span>';
  if(isZero) return '<span style="background:#0f2e1a;color:#2ea043;padding:2px 6px;border-radius:3px;font-size:11px">'+label+' (known-zero)</span>';
  return '<span style="background:#1f3b25;color:#3fb950;padding:2px 6px;border-radius:3px;font-size:11px">'+label+'</span>';
}
function _raWarningBadges(warnings){
  if(!warnings || warnings.length === 0)
    return '<span style="color:#2ea043;font-size:11px">none</span>';
  return warnings.map(function(w){
    return '<span style="background:#3b2a1f;color:#f0883e;padding:1px 5px;border-radius:3px;font-size:10px;margin-right:4px">'+w+'</span>';
  }).join('');
}
function _raEscape(s){
  if(s === null || s === undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function loadRiskAuthority(){
  document.getElementById('raBanner').textContent = 'Loading Risk Authority state...';

  // 1. Snapshot
  fetch('/api/risk-authority/snapshot/latest').then(function(r){return r.json();}).then(function(s){
    if(s.snapshot_id === null || s.snapshot_id === undefined){
      document.getElementById('raSnapshotMeta').innerHTML = '<span style="color:#f0883e">No risk_snapshots row yet — engine has not been invoked.</span>';
      document.getElementById('raCombinedCap').textContent = '—';
      document.getElementById('raCombinedPos').textContent = '—';
      document.getElementById('raCombinedLoss').textContent = '—';
      document.getElementById('raAnyPnlUnknown').textContent = '—';
      document.getElementById('raAnyExpUnknown').textContent = '—';
      document.getElementById('raTradingDay').textContent = '—';
      document.getElementById('raBanner').innerHTML = '<span style="color:#f0883e">No Risk Authority decisions recorded yet</span>';
      return;
    }
    document.getElementById('raSnapshotMeta').innerHTML =
      'snapshot_id=' + s.snapshot_id +
      ' &middot; taken_at=' + _raEscape(s.taken_at) +
      ' &middot; policy_version=' + _raEscape(s.policy_version) +
      ' &middot; source=' + _raEscape(s.source);
    var c = s.combined || {};
    document.getElementById('raCombinedCap').innerHTML  = _raFmtUsd(c.combined_capital_deployed);
    document.getElementById('raCombinedPos').innerHTML  = _raFmtInt(c.combined_open_positions);
    document.getElementById('raCombinedLoss').innerHTML = _raFmtUsd(c.combined_realised_daily_loss);
    document.getElementById('raAnyPnlUnknown').innerHTML = _raFmtBool(c.any_pnl_unknown);
    document.getElementById('raAnyExpUnknown').innerHTML = _raFmtBool(c.any_exposure_unknown);
    document.getElementById('raTradingDay').textContent = s.trading_day_utc || '—';
    var banner = (c.any_pnl_unknown || c.any_exposure_unknown)
      ? '<span style="color:#f85149">Snapshot shows unknown state — fail-closed in effect</span>'
      : '<span style="color:#2ea043">Snapshot clean</span>';
    document.getElementById('raBanner').innerHTML = banner;
  }).catch(function(e){
    document.getElementById('raBanner').innerHTML = '<span style="color:#f85149">Error loading snapshot: '+_raEscape(e)+'</span>';
  });

  // 2. Per-scope state
  fetch('/api/risk-authority/scopes').then(function(r){return r.json();}).then(function(s){
    var scopes = s.scopes || {};
    var html = '';
    ['ibkr_live','ibkr_paper','etoro_real','etoro_paper'].forEach(function(scope){
      var sv = scopes[scope] || {};
      html += '<div style="padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:5px">';
      html += '<div style="font-weight:700;color:#e6edf3;margin-bottom:6px">'+_raEscape(scope)+'</div>';
      html += '<div style="font-size:11px;line-height:1.6">';
      html += '<div><span style="color:#8b949e">PnL:</span> ' + _raStatusBadge(sv.pnl_status, sv.pnl_known, sv.pnl_known_zero) + '</div>';
      html += '<div><span style="color:#8b949e">Realised loss:</span> ' + _raFmtUsd(sv.realised_daily_loss) + '</div>';
      html += '<div><span style="color:#8b949e">Exposure:</span> ' + _raStatusBadge(sv.exposure_status, sv.exposure_known, sv.exposure_known_zero) + '</div>';
      html += '<div><span style="color:#8b949e">Open positions:</span> ' + _raFmtInt(sv.open_positions) + '</div>';
      html += '<div><span style="color:#8b949e">Capital deployed:</span> ' + _raFmtUsd(sv.capital_deployed) + '</div>';
      html += '<div><span style="color:#8b949e">Drawdown:</span> ' + (sv.drawdown_from_peak ? (Number(sv.drawdown_from_peak)*100).toFixed(2)+'%' : '0.00%') + '</div>';
      html += '<div><span style="color:#8b949e">Fresh reads:</span> ' + _raFmtInt(sv.exposure_fresh_reads_count) + '</div>';
      html += '<div style="margin-top:5px"><span style="color:#8b949e">Warnings:</span> ' + _raWarningBadges(sv.warnings) + '</div>';
      html += '</div></div>';
    });
    document.getElementById('raScopes').innerHTML = html;
  }).catch(function(e){
    document.getElementById('raScopes').innerHTML = '<span style="color:#f85149">Error: '+_raEscape(e)+'</span>';
  });

  // 3. Authority view (read-only)
  fetch('/api/risk-authority/authority').then(function(r){return r.json();}).then(function(s){
    var scopes = s.scopes || {};
    var html = '<table style="width:100%;border-collapse:collapse;font-size:11px"><thead><tr style="color:#8b949e;border-bottom:1px solid #30363d"><th style="text-align:left;padding:6px">scope</th><th style="text-align:left;padding:6px">latest authority</th><th style="text-align:left;padding:6px">latest result</th><th style="text-align:left;padding:6px">downgrade reason</th><th style="text-align:left;padding:6px">manual reset required?</th><th style="text-align:left;padding:6px">last decision</th></tr></thead><tbody>';
    ['ibkr_live','ibkr_paper','etoro_real','etoro_paper'].forEach(function(scope){
      var a = scopes[scope] || {};
      var authBadge = a.latest_authority_after
        ? '<span style="background:#1f3b25;color:#3fb950;padding:1px 5px;border-radius:3px">'+_raEscape(a.latest_authority_after)+'</span>'
        : '<span style="color:#8b949e">no decisions yet</span>';
      var manual = a.manual_reset_would_be_required
        ? '<span style="color:#f85149;font-weight:600">yes (operator action required)</span>'
        : '<span style="color:#2ea043">no</span>';
      html += '<tr style="border-bottom:1px solid #21262d">';
      html += '<td style="padding:6px;color:#e6edf3">'+_raEscape(scope)+'</td>';
      html += '<td style="padding:6px">'+authBadge+'</td>';
      html += '<td style="padding:6px;color:#8b949e">'+_raEscape(a.latest_result || '—')+'</td>';
      html += '<td style="padding:6px;color:#8b949e">'+_raEscape(a.latest_downgrade_reason || '—')+'</td>';
      html += '<td style="padding:6px">'+manual+'</td>';
      html += '<td style="padding:6px;color:#8b949e;font-size:10px">'+_raEscape(a.latest_taken_at || '—')+'</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    document.getElementById('raAuthority').innerHTML = html;
  }).catch(function(e){
    document.getElementById('raAuthority').innerHTML = '<span style="color:#f85149">Error: '+_raEscape(e)+'</span>';
  });

  // 4. Latest decisions
  var limit = document.getElementById('raDecLimit').value;
  var scopeFilter = document.getElementById('raDecScope').value;
  var url = '/api/risk-authority/decisions?limit=' + encodeURIComponent(limit);
  if(scopeFilter) url += '&scope=' + encodeURIComponent(scopeFilter);
  fetch(url).then(function(r){return r.json();}).then(function(s){
    document.getElementById('raDecCount').textContent =
      'showing ' + (s.decisions ? s.decisions.length : 0) + ' of ' + (s.total_count || 0) + ' total';
    var html = '<table style="width:100%;border-collapse:collapse;font-size:10px"><thead><tr style="color:#8b949e;border-bottom:1px solid #30363d">';
    html += '<th style="text-align:left;padding:4px 6px">taken_at</th>';
    html += '<th style="text-align:left;padding:4px 6px">scope</th>';
    html += '<th style="text-align:left;padding:4px 6px">action</th>';
    html += '<th style="text-align:left;padding:4px 6px">result</th>';
    html += '<th style="text-align:left;padding:4px 6px">auth before&rarr;after</th>';
    html += '<th style="text-align:left;padding:4px 6px">reason codes</th>';
    html += '<th style="text-align:left;padding:4px 6px">snapshot</th>';
    html += '</tr></thead><tbody>';
    (s.decisions || []).forEach(function(d){
      var resCol = d.result === 'allow' ? '#3fb950' : '#f85149';
      html += '<tr style="border-bottom:1px solid #21262d">';
      html += '<td style="padding:4px 6px;color:#8b949e">'+_raEscape(d.taken_at)+'</td>';
      html += '<td style="padding:4px 6px;color:#e6edf3">'+_raEscape(d.broker_scope)+'</td>';
      html += '<td style="padding:4px 6px;color:#8b949e">'+_raEscape(d.requested_action)+'</td>';
      html += '<td style="padding:4px 6px;color:'+resCol+';font-weight:600">'+_raEscape(d.result)+'</td>';
      html += '<td style="padding:4px 6px;color:#8b949e">'+_raEscape(d.authority_before)+' &rarr; '+_raEscape(d.authority_after)+'</td>';
      var rc = Array.isArray(d.reason_codes) ? d.reason_codes.join(', ') : '';
      html += '<td style="padding:4px 6px;color:#f0883e">'+_raEscape(rc)+'</td>';
      html += '<td style="padding:4px 6px;color:#8b949e">'+_raEscape(d.snapshot_id || '—')+'</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    if(!s.decisions || s.decisions.length === 0){
      html = '<span style="color:#8b949e">No decisions recorded yet.</span>';
    }
    document.getElementById('raDecisions').innerHTML = html;
  }).catch(function(e){
    document.getElementById('raDecisions').innerHTML = '<span style="color:#f85149">Error: '+_raEscape(e)+'</span>';
  });
}
</script>

<!-- END RISK PAGE -->
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
    """M15.3.A — bcrypt + plaintext fallback, rate-limit, audit.

    CSRF-EXEMPT (per Q-A.7): no session exists yet to embed the token in.
    Protected by rate-limit + bcrypt's natural ~250ms verify time.

    Response shape on success: {ok: True, csrf_token: "..."}
    The client (browser JS) stores the csrf_token and attaches it to
    every subsequent state-changing request as X-CSRF-Token header.
    """
    client_ip = _m153a_client_ip()

    # 1. Rate-limit check — short-circuit before any password compare.
    try:
        _m153a_login_limiter.check_locked(client_ip)
    except LoginRateLimited as e:
        _m153a_audit('login_locked', success=False, extras={
            'retry_after_sec': e.retry_after_sec,
            'policy': _m153a_login_limiter.policy(),
        })
        return jsonify({'ok': False,
                         'error': 'rate_limited',
                         'retry_after_sec': e.retry_after_sec}), 429

    # 2. Defensive: if no password is configured (default 'changeme'
    # only), refuse all logins. Better than accepting 'changeme' from
    # the network.
    if not _m153a_pw_configured():
        _m153a_audit('login_unconfigured', success=False)
        return jsonify({'ok': False, 'error': 'no_password_configured'}), 503

    # 3. Verify password.
    data = request.get_json(silent=True) or {}
    provided = data.get('password', '')
    matched, info = _m153a_verify_password(provided if isinstance(provided, str) else '')

    if not matched:
        _m153a_login_limiter.record_failure(client_ip)
        _m153a_audit('login_failure', success=False, extras={
            'path': info.get('path', 'none'),
            'failure_count': _m153a_login_limiter.failure_count(client_ip),
        })
        return jsonify({'ok': False}), 401

    # 3b. M15.3.A.2 — TOTP second factor (only if DASHBOARD_TOTP_SECRET set).
    # When TOTP is unset/empty, this block is a complete no-op — preserving
    # M15.3.A password-only behaviour byte-for-byte (regression-tested in
    # test_m15_3_a_2_totp.py group G2).
    if _m153a_totp_enabled():
        provided_code = data.get('totp_code', '')
        if not isinstance(provided_code, str):
            provided_code = ''
        provided_code = provided_code.strip()
        if not provided_code:
            # Q-A.3 / Correction 3: only return totp_required AFTER password
            # has verified. This is a small password-validity oracle (see
            # the M15.3.A.2 runbook §12). Mitigation: rate-limiter still
            # caps probes at 5 / 15 min via the per-IP counter. Approved
            # trade-off for UX clarity. Counter is NOT incremented for this
            # path — the operator legitimately forgot to enter the code.
            _m153a_audit('totp_required_not_provided', success=False)
            return jsonify({'ok': False, 'error': 'totp_required'}), 401
        ok_t, info_t = _m153a_totp_verify_code(provided_code)
        if not ok_t:
            # Generic 401 — do NOT leak whether code was wrong/expired/
            # replay/format-invalid. Counter IS incremented (same per-IP
            # bucket as wrong-password) per Correction 3.
            _m153a_login_limiter.record_failure(client_ip)
            _m153a_audit('totp_failure', success=False, extras={
                # extras_json contract: NEVER include the code, the secret,
                # or the URI. Only the reason classifier (already a
                # closed-set string from totp.verify_code).
                'reason': info_t.get('reason', 'unknown'),
                'failure_count': _m153a_login_limiter.failure_count(client_ip),
            })
            return jsonify({'ok': False}), 401
        # TOTP success — record audit but do not yet finalize login
        # (still need to rotate session etc.).
        _m153a_audit('totp_success', success=True, extras={
            'window': info_t.get('window'),
        })

    # 4. Success — rotate session, issue CSRF, audit.
    _m153a_login_limiter.record_success(client_ip)
    _m153a_rotate_session(session, client_ip=client_ip)
    new_csrf = _m153a_issue_csrf(session)
    audit_extras = {'path': info.get('path', 'none')}
    if info.get('warning'):
        audit_extras['warning'] = info['warning']
    _m153a_audit('login_success', success=True, extras=audit_extras)
    return jsonify({'ok': True, 'csrf_token': new_csrf})


@app.route('/api/logout', methods=['POST'])
@require_auth
@csrf_required
def logout():
    """M15.3.A — logout requires auth + CSRF (per Q-A.7).

    A CSRF-free logout could be abused to log the operator out via a
    malicious page; the CSRF requirement makes that impossible
    cross-origin."""
    _m153a_audit('logout', success=True)
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/auth/csrf', methods=['GET'])
@require_auth
def auth_csrf():
    """M15.3.A — return the current session's CSRF token.

    Used by the dashboard JS on page load (after determining the
    operator is already logged in from a prior session) to repopulate
    window._csrfToken without forcing a re-login."""
    tok = _m153a_get_csrf(session)
    if not tok:
        # Session is authed but has no token (legacy session from
        # before M15.3.A deploy). Issue one now.
        tok = _m153a_issue_csrf(session)
    return jsonify({'csrf_token': tok})


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
@csrf_required
def start():
    _run_bot()
    return jsonify({'ok': True})


@app.route('/api/stop', methods=['POST'])
@require_auth
@csrf_required
def stop():
    subprocess.run(['pkill', '-f', 'main.py'], capture_output=True)
    return jsonify({'ok': True})


@app.route('/api/restart', methods=['POST'])
@require_auth
@csrf_required
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
@csrf_required
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
@csrf_required
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
@csrf_required
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




@app.route('/api/backtest/reset', methods=['POST'])
@require_auth
@csrf_required
def backtest_reset():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.backtest_job import reset_job
    reset_job()
    return jsonify({'ok': True, 'message': 'Backtest state reset'})


@app.route('/api/backtest/cancel', methods=['POST'])
@require_auth
@csrf_required
def backtest_cancel():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.backtest_job import cancel_job
    cancel_job()
    return jsonify({'ok': True})


@app.route('/api/backtest/run', methods=['POST'])
@require_auth
@csrf_required
def backtest_run():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.backtest_job import start_job, is_running
    data = request.get_json(silent=True) or {}
    symbols    = data.get('symbols', [])
    start_date = data.get('start_date', '')
    end_date   = data.get('end_date',   '')
    if not symbols or not start_date or not end_date:
        return jsonify({'ok': False, 'error': 'symbols, start_date and end_date required'}), 400
    if len(symbols) > 10:
        return jsonify({'ok': False, 'error': 'Max 10 symbols per run'}), 400
    if is_running():
        return jsonify({'ok': False, 'error': 'A backtest is already running'}), 409
    try:
        start_job(symbols, start_date, end_date)
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 409
    return jsonify({'ok': True})


@app.route('/api/backtest/status')
@require_auth
def backtest_status():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.backtest_job import get_status
    return jsonify(get_status())


@app.route('/api/backtest/history')
@require_auth
def backtest_history():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.backtest_v2 import REPORTS_DIR
    import json
    history_path = BASE_DIR / 'data' / 'backtest_history.json'
    try:
        runs = json.loads(history_path.read_text()) if history_path.exists() else []
    except Exception:
        runs = []
    return jsonify({'runs': runs})


@app.route('/api/backtest/csv')
@require_auth
def backtest_csv():
    import sys, io, csv as _csv
    sys.path.insert(0, str(BASE_DIR))
    from bot.backtest_job import get_status
    from flask import Response
    data   = get_status()
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


# ── Provider ─────────────────────────────────────────────────────────────────

@app.route('/api/provider')
@require_auth
def provider_info():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.providers import get_provider_name
    from bot.providers import get_provider
    try:
        p = get_provider()
        return jsonify({
            'name':         p.name,
            'capabilities': p.capabilities,
        })
    except Exception as e:
        return jsonify({'name': get_provider_name(), 'error': str(e)})


# ── Sentiment ────────────────────────────────────────────────────────────────

@app.route('/api/sentiment/status')
@require_auth
def sentiment_status():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.sentiment import get_sentiment_mode, get_sentiment_provider, get_sentiment_threshold
    mode = get_sentiment_mode()
    prov = get_sentiment_provider()
    return jsonify({
        'mode':      mode,
        'provider':  prov.name,
        'enabled':   mode != 'off',
        'threshold': get_sentiment_threshold(),
    })


@app.route('/api/sentiment/test')
@require_auth
def sentiment_test():
    """On-demand sentiment test for a single symbol and source.
    GET /api/sentiment/test?symbol=AAPL&source=yfinance_news
    """
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.sentiment import get_sentiment_mode, get_sentiment_threshold
    import os
    symbol     = request.args.get('symbol', 'AAPL').upper().strip()
    source     = request.args.get('source', 'yfinance_news').lower().strip()
    force_live = request.args.get('force_live', '0') in ('1', 'true', 'yes')
    mode       = get_sentiment_mode()
    thresh     = get_sentiment_threshold()
    try:
        if source == 'yfinance_news':
            from bot.sentiment.news_provider import YFinanceNewsProvider
            provider = YFinanceNewsProvider()
        elif source == 'google_news':
            from bot.sentiment.news_provider import GoogleNewsProvider
            provider = GoogleNewsProvider()
        elif source == 'alphavantage_news':
            av_key = os.getenv('ALPHAVANTAGE_KEY', '').strip()
            if not av_key:
                return jsonify({'error': 'ALPHAVANTAGE_KEY not set in .env'}), 400
            from bot.sentiment.news_provider import AlphaVantageNewsProvider
            provider = AlphaVantageNewsProvider(av_key)
        elif source == 'disabled':
            from bot.sentiment.disabled_provider import DisabledProvider
            provider = DisabledProvider()
        else:
            return jsonify({'error': f'Unknown source: {source}. Use: yfinance_news, google_news, alphavantage_news'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    import time as _time
    t0     = _time.monotonic()
    result = provider.get_sentiment(symbol, force_live=force_live)
    elapsed = round(_time.monotonic() - t0, 2)
    raw = result.raw or {}
    return jsonify({
        'symbol':           symbol,
        'source_requested': source,
        'source_used':      result.source,
        'mode_configured':  mode,
        'threshold':        thresh,
        'force_live':       force_live,
        'cache_used':       raw.get('cache_used', False),
        'fetch_attempted':  raw.get('fetch_attempted', True),
        'fetch_success':    raw.get('fetch_success', result.status == 'ok'),
        'article_count':    raw.get('article_count', 0),
        'item_keys_debug':  raw.get('item_keys', None),
        'score':            result.score,
        'label':            result.label,
        'status':           result.status,
        'error':            raw.get('error'),
        'error_class':      raw.get('error_class'),
        'headlines':        raw.get('headlines', []),
        'elapsed_s':        elapsed,
        'tested_at':        raw.get('fetched_at', ''),
    })


# ── Execution / Flywheel ────────────────────────────────────────────────────────

@app.route('/api/execution/intents')
@require_auth
def execution_intents():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.flywheel import recent_intents
    limit = int(request.args.get('limit', 20))
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    rows = recent_intents(conn, limit)
    return jsonify(rows)


@app.route('/api/execution/candidates')
@require_auth
def execution_candidates():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.flywheel import recent_candidates
    limit = int(request.args.get('limit', 100))
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    rows = recent_candidates(conn, limit)
    return jsonify(rows)


@app.route('/api/execution/status')
@require_auth
def execution_status():
    import sys, os; sys.path.insert(0, str(BASE_DIR))
    from bot.brokers import get_broker_name, get_broker
    from bot.risk import RiskManager
    rm   = RiskManager()
    name = get_broker_name()
    result = {
        'broker':          name,
        'is_live':         False,
        'max_position_pct':rm.max_position_pct,
        'max_open':        rm.max_open,
        'portfolio_size':  rm.portfolio_size,
        'allow_duplicates':rm.allow_duplicates,
    }
    # Include IBKR connection status if broker is ibkr_paper
    if name == 'ibkr_paper':
        try:
            broker = get_broker()
            result['ibkr'] = broker.connection_status()
        except Exception as e:
            result['ibkr'] = {'connected': False, 'error': str(e)}
    return jsonify(result)


# ── M14 Portfolio Risk ───────────────────────────────────────────────────────

_M14_RISK_KEYS = {
    'RISK_MAX_DAILY_LOSS_PCT':         ('float', 0.1,  20.0),
    'RISK_MAX_DAILY_LOSS_USD':         ('float', 0.0,  999999.0),
    'RISK_REQUIRE_DAILY_PNL_FOR_LIVE': ('bool',  None, None),
    'RISK_ALLOW_DAILY_LOSS_OVERRIDE':  ('bool',  None, None),
    'RISK_MAX_SYMBOL_EXPOSURE_PCT':    ('float', 0.1,  100.0),
    'RISK_MAX_SECTOR_EXPOSURE_PCT':    ('float', 0.1,  100.0),
    'RISK_REQUIRE_SECTOR_FOR_LIVE':    ('bool',  None, None),
    'RISK_LOSS_STREAK_LIMIT':          ('int',   1,    20),
    'RISK_LOSS_STREAK_COOLDOWN_MINS':  ('int',   1,    10080),
    'RISK_REQUIRE_OUTCOMES_FOR_LIVE':  ('bool',  None, None),
    'RISK_MAX_OPEN_POSITIONS':         ('int',   1,    100),
    'RISK_MAX_POSITION_PCT':           ('float', 0.1,  10.0),
    'RISK_PORTFOLIO_SIZE':             ('float', 1000, 99999999),
}


@app.route('/api/portfolio-risk/state')
@require_auth
def portfolio_risk_state():
    import sys, sqlite3
    sys.path.insert(0, str(BASE_DIR))
    try:
        conn = sqlite3.connect(str(DB_PATH))
        from bot.flywheel import get_daily_state, get_persistent_state
        daily = get_daily_state(conn)
        persistent = get_persistent_state(conn)
        cur = conn.execute('SELECT * FROM portfolio_risk_snapshots ORDER BY id DESC LIMIT 1')
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        snap = dict(zip(cols, row)) if row else {}
        conn.close()
        from bot.kill_switch import get_kill_switch_state
        return jsonify({'daily_state': daily, 'persistent_state': persistent,
                        'latest_snapshot': snap, 'kill_switch': get_kill_switch_state()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio-risk/snapshots')
@require_auth
def portfolio_risk_snapshots():
    import sys, sqlite3
    sys.path.insert(0, str(BASE_DIR))
    limit = min(int(request.args.get('limit', 20)), 100)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            'SELECT * FROM portfolio_risk_snapshots ORDER BY id DESC LIMIT ?', (limit,))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        conn.close()
        return jsonify([dict(zip(cols, r)) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio-risk/rejections')
@require_auth
def portfolio_risk_rejections():
    import sys, sqlite3
    sys.path.insert(0, str(BASE_DIR))
    limit = min(int(request.args.get('limit', 20)), 100)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT id,symbol,rejection_reason,risk_checks,timestamp "
            "FROM execution_intents WHERE status='risk_rejected' "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'symbol': r[1], 'rejection_reason': r[2],
                         'risk_checks': r[3], 'timestamp': r[4]} for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio-risk/config', methods=['GET'])
@require_auth
def portfolio_risk_config_get():
    import os
    # Defaults mirror PortfolioRiskPolicy.__init__ so UI shows effective values
    _defaults = {
        'RISK_MAX_DAILY_LOSS_PCT':         '3.0',
        'RISK_MAX_DAILY_LOSS_USD':         '',          # optional — blank = disabled
        'RISK_REQUIRE_DAILY_PNL_FOR_LIVE': 'true',
        'RISK_ALLOW_DAILY_LOSS_OVERRIDE':  'false',
        'RISK_MAX_SYMBOL_EXPOSURE_PCT':    '10.0',
        'RISK_MAX_SECTOR_EXPOSURE_PCT':    '30.0',
        'RISK_REQUIRE_SECTOR_FOR_LIVE':    'true',
        'RISK_LOSS_STREAK_LIMIT':          '3',
        'RISK_LOSS_STREAK_COOLDOWN_MINS':  '60',
        'RISK_REQUIRE_OUTCOMES_FOR_LIVE':  'false',
        'RISK_MAX_OPEN_POSITIONS':         '10',
        'RISK_MAX_POSITION_PCT':           '2.0',
        'RISK_PORTFOLIO_SIZE':             '100000',
    }
    result = {}
    for k, (t, lo, hi) in _M14_RISK_KEYS.items():
        raw = os.getenv(k)           # None if not set in env
        default = _defaults.get(k, '')
        effective = raw if raw is not None else default
        result[k] = {
            'value':     effective,
            'default':   default,
            'source':    'env' if raw is not None else 'default',
            'optional':  k == 'RISK_MAX_DAILY_LOSS_USD',
            'type':      t,
            'min':       lo,
            'max':       hi,
        }
    return jsonify(result)


@app.route('/api/portfolio-risk/config', methods=['POST'])
@require_auth
@csrf_required
def portfolio_risk_config_set():
    import os, shutil
    changes = request.json or {}
    errors = {}
    applied = {}
    _optional_keys = {'RISK_MAX_DAILY_LOSS_USD'}
    for key, raw_val in changes.items():
        if key not in _M14_RISK_KEYS:
            errors[key] = 'not in whitelist'
            continue
        typ, lo, hi = _M14_RISK_KEYS[key]
        # Allow blank only for optional keys — blank clears the env var
        if str(raw_val).strip() == '':
            if key in _optional_keys:
                applied[key] = ''  # will delete from env
            else:
                errors[key] = 'required field cannot be blank'
            continue
        try:
            if typ == 'bool':
                val_str = 'true' if str(raw_val).lower() in ('true','1','yes') else 'false'
            elif typ == 'int':
                v = int(raw_val)
                if lo is not None and v < lo: raise ValueError('below min ' + str(lo))
                if hi is not None and v > hi: raise ValueError('above max ' + str(hi))
                val_str = str(v)
            else:
                v = float(raw_val)
                if lo is not None and v < lo: raise ValueError('below min ' + str(lo))
                if hi is not None and v > hi: raise ValueError('above max ' + str(hi))
                val_str = str(v)
            applied[key] = val_str
        except Exception as e:
            errors[key] = str(e)
    if errors:
        return jsonify({'errors': errors}), 400
    env_path = BASE_DIR / '.env'
    if env_path.exists():
        shutil.copy(env_path, str(env_path) + '.bak')
    try:
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        env_dict = {}
        for line in lines:
            if '=' in line and not line.strip().startswith('#'):
                k, _, v = line.partition('=')
                env_dict[k.strip()] = v
        for k, v in applied.items():
            env_dict[k] = v
            os.environ[k] = v
        joined = '\n'.join(kk + '=' + vv for kk, vv in env_dict.items()) + '\n'
        env_path.write_text(joined)
        app.logger.info('[M14_CONFIG] Applied: %s', list(applied.keys()))
        return jsonify({'applied': applied, 'note': 'takes effect next signal evaluation'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── M13.4A Broker Allocation + Budget Controls ──────────────────────────────

@app.route('/api/broker-allocation', methods=['GET'])
@require_auth
def broker_allocation_get():
    import sqlite3 as _sql
    from bot.broker_allocation import load_policy
    try:
        conn = _sql.connect(str(DB_PATH))
        policy = load_policy(conn)
        conn.close()
        return jsonify({'policy': policy})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/broker-allocation', methods=['POST'])
@require_auth
@csrf_required
def broker_allocation_set():
    import sqlite3 as _sql
    from bot.broker_allocation import validate_policy, save_policy
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            'ok': False,
            'errors': [{'path': '$', 'code': 'type_error',
                        'msg': 'request body must be a JSON object'}]
        }), 400
    result = validate_policy(payload)
    if not result.ok:
        return jsonify({'ok': False, 'errors': result.errors}), 400
    try:
        conn = _sql.connect(str(DB_PATH))
        save_policy(conn, payload)
        conn.close()
        return jsonify({'ok': True, 'note': 'policy persisted'})
    except ValueError as e:
        # Validation already passed, but save_policy re-validates defensively.
        return jsonify({
            'ok': False,
            'errors': [{'path': '$', 'code': 'save_rejected', 'msg': str(e)}]
        }), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── M14.G Risk Authority — READ-ONLY dashboard surface ───────────────────────
# Four endpoints expose M14.B–F state for operator visibility. No DB writes,
# no broker calls, no live-write paths, no authority editing. All routes are
# GET-only; Flask returns 405 on POST/DELETE/PUT/PATCH by default.

@app.route('/api/risk-authority/decisions', methods=['GET'])
@require_auth
def risk_authority_decisions():
    import sqlite3 as _sql
    from bot.risk_authority.dashboard_read import (
        list_recent_decisions, DECISIONS_DEFAULT_LIMIT, DECISIONS_MAX_LIMIT,
    )
    try:
        raw_limit = request.args.get('limit', str(DECISIONS_DEFAULT_LIMIT))
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = DECISIONS_DEFAULT_LIMIT
        scope = request.args.get('scope')
        if scope is not None and scope.strip() == "":
            scope = None
        conn = _sql.connect(str(DB_PATH))
        try:
            result = list_recent_decisions(conn, limit=limit, scope=scope)
        finally:
            conn.close()
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk-authority/scopes', methods=['GET'])
@require_auth
def risk_authority_scopes():
    import sqlite3 as _sql
    from bot.risk_authority.dashboard_read import get_scope_status
    try:
        conn = _sql.connect(str(DB_PATH))
        try:
            result = get_scope_status(conn)
        finally:
            conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk-authority/snapshot/latest', methods=['GET'])
@require_auth
def risk_authority_snapshot_latest():
    import sqlite3 as _sql
    from bot.risk_authority.dashboard_read import get_latest_snapshot
    try:
        conn = _sql.connect(str(DB_PATH))
        try:
            result = get_latest_snapshot(conn)
        finally:
            conn.close()
        if result is None:
            return jsonify({'snapshot_id': None,
                            'message': 'no risk_snapshots row yet'}), 200
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk-authority/authority', methods=['GET'])
@require_auth
def risk_authority_authority():
    import sqlite3 as _sql
    from bot.risk_authority.dashboard_read import get_authority_view
    try:
        conn = _sql.connect(str(DB_PATH))
        try:
            result = get_authority_view(conn)
        finally:
            conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── M15.0 Production process visibility — READ-ONLY ─────────────────────────
# Reports the canonical M15.0 service map and the real systemd state for each
# unit. No mutations; no live-write surface; no manual_reset. Separate from
# /api/health so external monitoring contracts (M15.2) are unaffected.

# Canonical service map. If install.sh has been run, both services should
# report active=active. If not yet installed, both should report not-found
# — that's the legacy nohup-managed state and is the operator's signal that
# M15.0 hasn't been applied on this host.
_M15_0_SERVICES = (
    ('algo-trader.service',           'main.py',          'bot/scanner main loop'),
    ('algo-trader-dashboard.service', 'dashboard/app.py', 'this Flask dashboard'),
)


def _systemctl_state(unit):
    """Return (active_state, enabled_state) for a unit. Both are strings;
    'not-found' if systemctl doesn't know the unit; 'unavailable' if
    systemctl itself can't be invoked (no systemd, no PATH, etc.)."""
    import subprocess
    try:
        active = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or 'not-found'
    except (FileNotFoundError, subprocess.SubprocessError):
        return ('unavailable', 'unavailable')
    try:
        enabled = subprocess.run(
            ['systemctl', 'is-enabled', unit],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or 'not-found'
    except (FileNotFoundError, subprocess.SubprocessError):
        enabled = 'unavailable'
    return (active, enabled)


def _process_owner_cgroup(pattern):
    """Return the cgroup path of the first process matching `pattern`, or
    None if no such process is running. Read-only /proc inspection."""
    import re, os as _os
    try:
        for pid_str in _os.listdir('/proc'):
            if not pid_str.isdigit():
                continue
            try:
                with open(f'/proc/{pid_str}/cmdline', 'rb') as fh:
                    cmdline = fh.read().replace(b'\x00', b' ').decode(
                        'utf-8', errors='replace')
            except (OSError, IOError):
                continue
            if re.search(pattern, cmdline):
                try:
                    with open(f'/proc/{pid_str}/cgroup', 'r') as fh:
                        cg = fh.read().strip().split('\n')[0]
                except (OSError, IOError):
                    cg = None
                return {'pid': int(pid_str), 'cgroup': cg}
    except (OSError, IOError):
        pass
    return None


@app.route('/api/system/services', methods=['GET'])
@require_auth
def system_services():
    """Read-only view of M15.0 canonical service map + actual systemd state.

    Returns:
      {
        "services": [
          {"unit", "script", "description",
           "active", "enabled",
           "process": {"pid", "cgroup"} | null,
           "managed_by": "systemd" | "session" | "unknown"},
          ...
        ],
        "m15_0_installed": bool,   # true iff both canonical units exist + active
        "as_of_utc": str,
      }
    """
    from datetime import datetime, timezone
    try:
        out = []
        all_active = True
        for unit, script_path, desc in _M15_0_SERVICES:
            active, enabled = _systemctl_state(unit)
            proc = _process_owner_cgroup(rf'python[0-9.]*\s.*{script_path}')
            if proc and proc.get('cgroup') and unit in proc['cgroup']:
                managed = 'systemd'
            elif proc and proc.get('cgroup') and 'user.slice' in proc['cgroup']:
                managed = 'session'
            elif proc:
                managed = 'unknown'
            else:
                managed = 'not-running'
            if active != 'active':
                all_active = False
            out.append({
                'unit':        unit,
                'script':      script_path,
                'description': desc,
                'active':      active,
                'enabled':     enabled,
                'process':     proc,
                'managed_by':  managed,
            })
        return jsonify({
            'services':         out,
            'm15_0_installed':  all_active,
            'as_of_utc': datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── M15.1 Gateway Watchdog ──────────────────────────────────────────────────

@app.route('/api/gateway/state')
@require_auth
def gateway_state():
    """M15.1 — current watchdog state + last 20 events."""
    import sys; sys.path.insert(0, str(BASE_DIR))
    try:
        from bot.flywheel import read_gateway_state, read_gateway_events
        state = read_gateway_state(db_path=str(DB_PATH))
        events = read_gateway_events(limit=20, db_path=str(DB_PATH))
        return jsonify({'state': state, 'events': events})
    except Exception as e:
        app.logger.exception('gateway_state failed')
        return jsonify({'error': str(e)}), 500


# ── M15.4 IB Gateway truth layer — READ-ONLY point-in-time view ─────────────
# Distinct from /api/gateway/state (M15.1 historical events from DB).
# This endpoint reads systemd + ports + IBC config + log tail + journalctl
# at request time and returns a single classified status. No DB writes,
# no systemctl mutations, no IB API call, no broker construction. The
# existing M15.1 endpoint is preserved unchanged.

@app.route('/api/gateway/health', methods=['GET'])
@require_auth
def gateway_health():
    import sys; sys.path.insert(0, str(BASE_DIR))
    try:
        from bot.gateway_health import assemble_health
        return jsonify(assemble_health())
    except Exception as e:
        app.logger.exception('gateway_health failed')
        return jsonify({'error': str(e)}), 500



# ── M15.2 Health endpoint (external monitoring) ─────────────────────────────
# Public endpoint (no session-cookie auth) so external monitors can reach it.
# Optional bearer-token protection via HEALTH_ENDPOINT_AUTH_TOKEN env var.
# - No token configured  -> minimal payload to everyone (logs WARN once)
# - Token configured, no Authorization header -> minimal payload
# - Token configured, wrong Authorization     -> 401
# - Token configured, correct Authorization   -> full payload
# This endpoint NEVER touches signals.db. All state is read from
# data/heartbeat.json (atomic writes) and data/kill_switch.json.
import hmac as _hmac

_health_unauth_warned = False


def _resolve_health_token():
    return os.environ.get('HEALTH_ENDPOINT_AUTH_TOKEN', '').strip()


def _is_valid_bearer(header_value, expected_token):
    if not header_value or not expected_token:
        return False
    if not header_value.startswith('Bearer '):
        return False
    presented = header_value[len('Bearer '):].strip()
    # Constant-time comparison
    return _hmac.compare_digest(presented, expected_token)


def _seconds_since(iso_ts):
    if not iso_ts:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        dt = _dt.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return int((_dt.now(_tz.utc) - dt).total_seconds())
    except Exception:
        return None


@app.route('/api/health')
def api_health():
    """M15.2 — external monitoring endpoint.

    Status synthesis (first match wins):
      1. heartbeat file missing OR mtime age > HEARTBEAT_STALE_SEC
                                                  -> critical / 503
      2. heartbeat says db_writable=False         -> critical / 503
      3. scan_started > scan_completed AND
         scan_started_age > 2 * scan_interval     -> critical / 503 (scan_wedged)
      4. scan_completed_age > scan_interval * SCAN_STALE_MULTIPLIER
                                                  -> critical / 503
      5. watchdog state != api_up_healthy OR
         kill_switch_active                       -> degraded  / 200
      6. otherwise                                -> ok        / 200
    """
    global _health_unauth_warned

    import sys
    sys.path.insert(0, str(BASE_DIR))
    from bot.heartbeat import read_heartbeat, resolve_heartbeat_path

    # ── Auth handling ────────────────────────────────────────────────────
    expected_token = _resolve_health_token()
    presented_header = request.headers.get('Authorization', '')
    authed = False
    if expected_token:
        if presented_header:
            if not _is_valid_bearer(presented_header, expected_token):
                return jsonify({'error': 'unauthorized'}), 401
            authed = True
        # No header sent → minimal payload (no rejection)
    else:
        if not _health_unauth_warned:
            app.logger.warning(
                '[HEALTH] HEALTH_ENDPOINT_AUTH_TOKEN not configured — '
                '/api/health is serving minimal payload to all callers'
            )
            _health_unauth_warned = True

    # ── Inputs ───────────────────────────────────────────────────────────
    hb_stale_sec = int(os.environ.get('HEARTBEAT_STALE_SEC', '90'))
    scan_stale_mult = float(os.environ.get('SCAN_STALE_MULTIPLIER', '3'))

    hb_path = resolve_heartbeat_path()
    hb = read_heartbeat(hb_path)

    # mtime is the trustworthy liveness signal (cannot be spoofed by the
    # JSON contents). We cross-check against last_heartbeat_ts inside.
    mtime_age = None
    if hb_path.exists():
        try:
            from datetime import datetime as _dt, timezone as _tz
            mtime_age = int((_dt.now(_tz.utc).timestamp()
                             - hb_path.stat().st_mtime))
        except OSError:
            mtime_age = None

    # Cross-check mtime vs JSON ts (tamper signal)
    tamper_warning = None
    if hb and hb.get('last_heartbeat_ts') and mtime_age is not None:
        json_age = _seconds_since(hb['last_heartbeat_ts'])
        if json_age is not None and abs(json_age - mtime_age) > 5:
            tamper_warning = (
                f'heartbeat_ts_mtime_mismatch '
                f'json_age={json_age}s mtime_age={mtime_age}s'
            )
            app.logger.warning('[HEALTH] %s', tamper_warning)

    # Prefer mtime for staleness (trustworthy)
    hb_age_sec = mtime_age if mtime_age is not None else (
        _seconds_since(hb.get('last_heartbeat_ts')) if hb else None
    )

    scan_interval_sec = (hb or {}).get('scan_interval_sec') or 0
    scan_started_age = _seconds_since((hb or {}).get('last_scan_started_ts'))
    scan_completed_age = _seconds_since((hb or {}).get('last_scan_completed_ts'))
    db_writable = bool((hb or {}).get('db_writable', False))

    # Watchdog state — read from heartbeat file ONLY. /api/health must
    # NEVER open signals.db (zero lock contention with the trading scan
    # loop). The heartbeat thread is the one component that touches
    # signals.db read-only and writes the summary into heartbeat.json.
    gw_summary = (hb or {}).get('gateway') or {}
    gw_state_name = gw_summary.get('state') or 'unknown'

    # Kill switch (file-based — does not touch signals.db)
    kill_switch_active = False
    try:
        from bot.kill_switch import is_kill_switch_active
        kill_switch_active = bool(is_kill_switch_active())
    except Exception:
        pass

    # ── Status synthesis ────────────────────────────────────────────────
    status = 'ok'
    http_code = 200
    reason = None

    if hb is None:
        status, http_code, reason = 'critical', 503, 'heartbeat_missing'
    elif hb_age_sec is None or hb_age_sec > hb_stale_sec:
        status, http_code, reason = 'critical', 503, 'heartbeat_stale'
    elif not db_writable:
        status, http_code, reason = 'critical', 503, 'db_unwritable'
    elif (scan_started_age is not None
          and (scan_completed_age is None or scan_started_age < scan_completed_age)
          and scan_interval_sec > 0
          and scan_started_age > 2 * scan_interval_sec):
        # scan_started timestamp is fresher than scan_completed AND
        # scan_started itself is far in the past → loop wedged mid-scan
        status, http_code, reason = 'critical', 503, 'scan_wedged'
    elif (scan_completed_age is not None and scan_interval_sec > 0
          and scan_completed_age > scan_stale_mult * scan_interval_sec):
        status, http_code, reason = 'critical', 503, 'scan_stale'
    elif kill_switch_active:
        status, http_code, reason = 'degraded', 200, 'kill_switch_active'
    elif gw_state_name not in ('api_up_healthy', 'unknown', ''):
        # 'unknown' = watchdog not yet probed (e.g. paper broker without
        # watchdog or first few seconds after boot). Don't flag as degraded.
        status, http_code, reason = 'degraded', 200, 'gateway_degraded'

    # ── Payload ─────────────────────────────────────────────────────────
    from datetime import datetime as _dt, timezone as _tz
    minimal = {
        'status': status,
        'http_code': http_code,
        'checked_at': _dt.now(_tz.utc).isoformat(timespec='seconds'),
        'heartbeat_age_sec': hb_age_sec,
        'scan_age_sec': scan_completed_age,
        'gateway_state': gw_state_name,
        'reason_code': reason,
    }
    if not authed:
        return jsonify(minimal), http_code

    full = dict(minimal)
    full.update({
        'heartbeat': {
            'age_sec': hb_age_sec,
            'mtime_age_sec': mtime_age,
            'stale_threshold_sec': hb_stale_sec,
            'fresh': (hb_age_sec is not None and hb_age_sec <= hb_stale_sec),
            'last_heartbeat_ts': (hb or {}).get('last_heartbeat_ts'),
            'interval_sec': (hb or {}).get('heartbeat_interval_sec'),
        },
        'scan': {
            'started_age_sec': scan_started_age,
            'completed_age_sec': scan_completed_age,
            'last_scan_started_ts': (hb or {}).get('last_scan_started_ts'),
            'last_scan_completed_ts': (hb or {}).get('last_scan_completed_ts'),
            'interval_sec': scan_interval_sec,
            'stale_multiplier': scan_stale_mult,
        },
        'db_writable': db_writable,
        'db_writable_checked_at': (hb or {}).get('db_writable_checked_at'),
        'gateway': gw_summary,
        'kill_switch_active': kill_switch_active,
        'pid': (hb or {}).get('pid'),
        'process_started_at': (hb or {}).get('process_started_at'),
        'warnings': [w for w in [tamper_warning] if w],
    })
    return jsonify(full), http_code


# ── Kill Switch ─────────────────────────────────────────────────────────────

@app.route('/api/kill-switch/state')
@require_auth
def kill_switch_state():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.kill_switch import get_kill_switch_state
    return jsonify(get_kill_switch_state())


@app.route('/api/kill-switch/activate', methods=['POST'])
@require_auth
@csrf_required
def kill_switch_activate():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.kill_switch import activate_kill_switch
    reason = request.json.get('reason', 'Dashboard activation') if request.json else 'Dashboard activation'
    return jsonify(activate_kill_switch(reason))


@app.route('/api/kill-switch/deactivate', methods=['POST'])
@require_auth
@csrf_required
def kill_switch_deactivate():
    import sys; sys.path.insert(0, str(BASE_DIR))
    from bot.kill_switch import deactivate_kill_switch
    reason = request.json.get('reason', 'Dashboard deactivation') if request.json else 'Dashboard deactivation'
    return jsonify(deactivate_kill_switch(reason))


# ── M15.3.B — manual_reset operator flow ─────────────────────────────────────
#
# Two endpoints implementing the operator-initiated reset of the M13.4A
# allocation-policy kill switches (NOT bot/kill_switch.py — that file-based
# emergency-stop is unchanged). When the M14 Risk Authority Engine is
# locked down by `policy.global.kill_switch=true` or per-broker kill_switches,
# the only recovery path is for the operator to clear those flags. Until
# M15.3.B, the operator did this by hand-editing the M13.4A allocation JSON.
# M15.3.B formalises it with: dedicated rate limiter, fresh step-up TOTP,
# preview-then-execute pattern, dual audit (auth_events + risk_decisions),
# and an explicit 10-500 char operator-reason field for compliance.
#
# Hard constraints (per M15.3.B pre-code checklist approval 2026-06-04):
#   * No broker orders/writes/live-trading. AST-enforced in tests.
#   * No scanner/strategy changes.
#   * No M14 engine/governor/snapshot/preflight code changes.
#   * No eToro/IBKR changes.
#   * No service restarts (DB state action only).
#
# Operator-approved corrections (C1..C4):
#   C1: TOTP error UX — only 'recently_used' hint is exposed; everything
#       else returns the generic {ok:false, error:'totp_invalid'}.
#   C2: Idempotent — empty switches_cleared still writes the audit rows.
#   C3: Reason field 10-500 chars; UI helper text deters secret-pasting.
#   C4: Design intent — manual_reset doesn't trade, but its purpose IS
#       to let the engine resume trading after the operator clears locks.


# Module-level singleton, parallel to _m153a_login_limiter.
_m153b_reset_limiter = None


def _m153b_get_limiter():
    """Lazy-init the manual_reset rate limiter. Tests can replace via
    `dashboard.app._m153b_reset_limiter = <fake>`."""
    global _m153b_reset_limiter
    if _m153b_reset_limiter is None:
        from dashboard.auth.manual_reset import make_manual_reset_limiter
        _m153b_reset_limiter = make_manual_reset_limiter()
    return _m153b_reset_limiter


def _m153b_session_hash() -> str:
    """Stable per-session binding key for the manual_reset preview token.

    Uses a per-session nonce stored INSIDE the Flask session payload
    (`_mr_session_key`). This survives Flask re-signing the session
    cookie between requests (which would otherwise change the raw
    cookie value, breaking a cookie-hash binding). The nonce is
    generated lazily on first call within a given Flask session and
    persisted by Flask's normal session mechanism on the response.

    The returned value is the sha256 of the nonce; the raw nonce never
    appears outside the encrypted session cookie. Audit rows continue
    to use `hash_session_id(raw_cookie)` for cross-row correlation,
    which is a separate concern.
    """
    import secrets as _secrets
    import hashlib
    if "_mr_session_key" not in session:
        session["_mr_session_key"] = _secrets.token_urlsafe(32)
    return hashlib.sha256(
        session["_mr_session_key"].encode("utf-8")).hexdigest()


@app.route('/api/manual-reset/preview', methods=['GET'])
@require_auth
def m153b_manual_reset_preview():
    """Return current kill_switch state + a short-lived preview token.

    Read-only. Issues a 60-second single-use token bound to the
    current session. The token is required by POST /api/manual-reset.
    """
    from dashboard.auth.manual_reset import (
        read_kill_switch_state, get_preview_token_store,
        make_preview_extras,
    )
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            state = read_kill_switch_state(conn)
        finally:
            conn.close()
    except Exception as e:
        log.error("manual_reset_preview: db read failed: %s", e)
        return jsonify({'ok': False, 'error': 'db_error'}), 500

    sess_hash = _m153b_session_hash()
    token = get_preview_token_store().issue(sess_hash)
    _m153a_audit('manual_reset_preview', success=True,
                  extras=make_preview_extras(
                      kill_switch_state=state, token_issued=True))
    return jsonify({
        'ok': True,
        'kill_switch_state': state,
        'preview_token': token,
        'preview_token_ttl_seconds': 60,
    })


@app.route('/api/manual-reset', methods=['POST'])
@require_auth
@csrf_required
def m153b_manual_reset_execute():
    """Execute the operator manual_reset: clear all M13.4A kill switches.

    Validation order (early-rejects are cheaper than late-rejects):
      1. JSON body parses
      2. Rate limit check
      3. confirm == "RESET"
      4. preview_token valid + bound to session
      5. reason 10-500 chars
      6. Step-up TOTP (last — most expensive, runs pyotp + replay cache)

    Every POST writes a `manual_reset_attempt` row first (regardless of
    outcome). On validation failure, also writes `manual_reset_failure`
    with the reason code. On success, an atomic transaction writes:
      * the updated M13.4A allocation policy (kill_switches cleared)
      * a risk_decisions row with source='manual_reset'
      * a manual_reset_success auth_events row
    """
    from dashboard.auth.manual_reset import (
        validate_confirm, validate_reason, verify_step_up_totp,
        get_preview_token_store, execute_atomic_reset,
        make_attempt_extras, make_failure_extras,
    )
    from dashboard.auth.rate_limit import LoginRateLimited

    client_ip = _m153a_client_ip()
    cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
    raw_sid = request.cookies.get(cookie_name, '') or ''
    sess_hash = _m153b_session_hash()

    # 1. Parse JSON body.
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    confirm_value = body.get('confirm')
    preview_token = body.get('preview_token')
    reason_text = body.get('reason')
    totp_code = body.get('totp_code')

    confirm_ok = validate_confirm(confirm_value)
    has_preview_token = isinstance(preview_token, str) and bool(preview_token)
    has_totp = isinstance(totp_code, str) and bool(totp_code)
    has_reason = isinstance(reason_text, str) and bool(reason_text.strip())

    # ALWAYS write the attempt row first.
    _m153a_audit('manual_reset_attempt', success=True,
                  extras=make_attempt_extras(
                      has_csrf=True,  # csrf_required decorator already passed
                      has_preview_token=has_preview_token,
                      has_totp=has_totp,
                      has_reason=has_reason,
                      confirm_ok=confirm_ok))

    # 2. Rate limit check.
    limiter = _m153b_get_limiter()
    try:
        limiter.check_locked(client_ip)
    except LoginRateLimited as e:
        _m153a_audit('manual_reset_failure', success=False,
                      extras=make_failure_extras(
                          reason_code='rate_limited',
                          extra={'retry_after_sec': e.retry_after_sec,
                                  'policy': limiter.policy()}))
        return jsonify({'ok': False, 'error': 'rate_limited',
                         'retry_after_sec': e.retry_after_sec}), 429

    def _fail(reason_code: str, status: int = 400,
              api_error: str = None, extra_extras: dict = None,
              count_against_limit: bool = True):
        """Write a manual_reset_failure row + return error JSON.

        count_against_limit=True bumps the rate-limit counter (used for
        attempts that pass auth+CSRF but fail validation — those are
        what we want to limit; an attacker shouldn't get unlimited
        retries on the confirm/TOTP fields)."""
        if count_against_limit:
            limiter.record_failure(client_ip)
        _m153a_audit('manual_reset_failure', success=False,
                      extras=make_failure_extras(reason_code=reason_code,
                                                  extra=extra_extras))
        payload = {'ok': False, 'error': api_error or 'invalid_request'}
        if reason_code == 'totp_invalid' and extra_extras and \
                extra_extras.get('totp_hint') == 'recently_used':
            payload['hint'] = 'recently_used'
        return jsonify(payload), status

    # 3. Confirm string.
    if not confirm_ok:
        return _fail('confirm_invalid', status=400,
                      api_error='confirm_invalid')

    # 4. Preview token (session-bound, single-use).
    if not has_preview_token:
        return _fail('preview_token_missing', status=400,
                      api_error='preview_token_missing')
    token_ok = get_preview_token_store().consume(sess_hash, preview_token)
    if not token_ok:
        return _fail('preview_token_invalid', status=400,
                      api_error='preview_token_invalid')

    # 5. Reason.
    reason_ok, reason_err = validate_reason(reason_text)
    if not reason_ok:
        return _fail(reason_err, status=400,
                      api_error='reason_invalid')

    # 6. Step-up TOTP (last; most expensive).
    if not has_totp:
        return _fail('totp_missing', status=401,
                      api_error='totp_invalid')
    totp_ok, totp_hint = verify_step_up_totp(totp_code)
    if not totp_ok:
        extra = {'totp_hint': totp_hint} if totp_hint == 'recently_used' else None
        return _fail('totp_invalid', status=401,
                      api_error='totp_invalid', extra_extras=extra)

    # All validation passed. Run the atomic write.
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            actor = 'operator'  # short identifier, no secret material
            result = execute_atomic_reset(
                conn,
                actor=actor,
                reason_text=reason_text.strip(),
                client_ip=client_ip,
                user_agent=request.headers.get('User-Agent', ''),
                session_id=raw_sid,
            )
        finally:
            conn.close()
    except Exception as e:
        _m153a_log.exception("manual_reset_execute: atomic write failed")
        # Don't bump the rate limit on db errors — operator shouldn't be
        # locked out by a transient infra problem.
        _m153a_audit('manual_reset_failure', success=False,
                      extras=make_failure_extras(
                          reason_code='db_error',
                          extra={'exception_type': type(e).__name__}))
        return jsonify({'ok': False, 'error': 'db_error'}), 500

    return jsonify({
        'ok': True,
        'before_state':     result['before_state'],
        'after_state':      result['after_state'],
        'switches_cleared': result['switches_cleared'],
        'noop':             result['noop'],
        'audit': {
            'auth_event_id': result['auth_event_id'],
            'decision_id':   result['decision_id'],
        },
    })


# ── M15.3.C — audit export endpoint ─────────────────────────────────────────
#
# GET /api/audit-export?format=jsonl|csv&from=YYYY-MM-DD&to=YYYY-MM-DD
#
# Compliance-friendly export of:
#   * auth_events       — all rows in the date range (M15.3.A login/session +
#                         M15.3.A.2 TOTP + M15.3.B manual_reset audit history)
#   * risk_decisions    — rows with source='manual_reset' ONLY (the M14-side
#                         half of M15.3.B's dual-audit). Other risk_decisions
#                         (source IN 'auto','manual','reconciled') are
#                         EXCLUDED per Q-C.1 — operational/risk-engine
#                         audit, separate from operator/security audit.
#
# This endpoint is READ-ONLY with respect to all trading/account state.
# The ONLY write it performs is a single `audit_export_request` row in
# `auth_events` — the meta-audit-of-the-audit (success=1 or success=0).
#
# Per Q-C.7 operator approval: GET is acceptable for this milestone even
# though it writes the meta-audit row, because the primary action is
# download/export of already-visible-to-the-operator data. No CSRF token
# required (GET has no cross-site-attack surface that mutates state).
#
# Per Q-C.8 operator approval: no step-up TOTP for this milestone.
# Conscious decision — exported data is already visible to the
# authenticated operator via existing dashboard views; the export is a
# convenience aggregation, not new access. If broader trading/risk
# exports are exposed later, step-up TOTP should be reconsidered.

_m153c_export_limiter = None


def _m153c_get_export_limiter():
    """Lazy-init the audit-export rate limiter."""
    global _m153c_export_limiter
    if _m153c_export_limiter is None:
        from dashboard.auth.audit_export import make_export_limiter
        _m153c_export_limiter = make_export_limiter()
    return _m153c_export_limiter


@app.route('/api/audit-export', methods=['GET'])
@require_auth
def m153c_audit_export():
    """Stream-spool an audit export. Returns a file download.

    Validation order (each failure writes a manual_reset_failure-style
    audit_export_request row with success=0):
      1. Rate limit
      2. format param ∈ {jsonl, csv}
      3. from/to date params parse + valid range
      4. Row-count cap (100,000)
      5. Build body (spooled to memory bytes for SHA-256)
      6. Redaction scan (fail-fast — do NOT silent-strip)
      7. Write audit_export_request row with success=1, export_id linked
      8. Return file response
    """
    import sqlite3 as _sqlite3
    from dashboard.auth import audit_export as _ae

    client_ip = _m153a_client_ip()
    limiter = _m153c_get_export_limiter()

    # Helper to write the audit_export_request meta-audit row.
    def _write_meta(*, success, export_id=None, fmt=None,
                     from_iso=None, to_iso=None,
                     row_counts=None, reason=None,
                     redaction_violations=None):
        extras = {
            "export_id":     export_id,
            "format":        fmt,
            "from_iso":      from_iso,
            "to_iso":        to_iso,
            "row_counts":    row_counts,
        }
        if reason is not None:
            extras["reason"] = reason
        if redaction_violations is not None:
            # Labels only — never the actual secret values.
            extras["redaction_violations"] = list(redaction_violations)
        # Drop None values for cleaner extras.
        extras = {k: v for k, v in extras.items() if v is not None}
        _m153a_audit('audit_export_request', success=success, extras=extras)

    # 1. Rate limit — counts EVERY authenticated attempt that reaches
    #    this endpoint (success or failure). Per Q-C.8 M15.3.C re-spec
    #    2026-06-05: a compliance export endpoint must bound total
    #    volume per IP, not just failed-credential probes. The shared
    #    M15.3.A/B RateLimiter is unchanged; this uses an M15.3.C-local
    #    ExportAttemptLimiter (see dashboard/auth/audit_export.py).
    allowed, retry_after = limiter.check_and_record(client_ip)
    if not allowed:
        _write_meta(success=False, reason='rate_limited')
        return jsonify({'ok': False, 'error': 'rate_limited',
                         'retry_after_sec': retry_after}), 429

    # 2. Format param.
    fmt = (request.args.get('format') or _ae.DEFAULT_FORMAT).lower()
    if fmt not in _ae.SUPPORTED_FORMATS:
        _write_meta(success=False, fmt=fmt, reason='format_invalid')
        return jsonify({'ok': False, 'error': 'format_invalid',
                         'supported': list(_ae.SUPPORTED_FORMATS)}), 400

    # 3. Date range.
    from_str = request.args.get('from') or None
    to_str   = request.args.get('to')   or None
    ok, derr, from_iso, to_iso = _ae.validate_date_range(from_str, to_str)
    if not ok:
        _write_meta(success=False, fmt=fmt, reason=derr)
        return jsonify({'ok': False, 'error': derr}), 400

    # 4. Row-count cap.
    conn = _sqlite3.connect(str(DB_PATH))
    try:
        n_auth, n_rd = _ae.count_export_rows(
            conn, from_iso=from_iso, to_iso=to_iso)
    finally:
        conn.close()
    total = n_auth + n_rd
    if total > _ae.MAX_EXPORT_ROWS:
        _write_meta(success=False, fmt=fmt,
                     from_iso=from_iso, to_iso=to_iso,
                     row_counts={'auth_events': n_auth,
                                  'risk_decisions_manual_reset': n_rd},
                     reason='row_cap_exceeded')
        return jsonify({
            'ok': False,
            'error': 'row_cap_exceeded',
            'hint': 'narrow your date range',
            'max_rows': _ae.MAX_EXPORT_ROWS,
            'row_counts': {'auth_events': n_auth,
                            'risk_decisions_manual_reset': n_rd},
        }), 400

    # 5. Build body (spooled to bytes).
    export_id = _ae._new_export_id()
    generated_at = _ae._now_utc_iso()
    conn = _sqlite3.connect(str(DB_PATH))
    try:
        if fmt == 'jsonl':
            body_bytes, manifest = _ae.build_jsonl_export(
                conn, from_iso=from_iso, to_iso=to_iso,
                export_id=export_id, generated_at_utc=generated_at)
            content_type = 'application/x-ndjson'
        else:
            body_bytes, manifest = _ae.build_csv_zip_export(
                conn, from_iso=from_iso, to_iso=to_iso,
                export_id=export_id, generated_at_utc=generated_at)
            content_type = 'application/zip'
    except Exception:
        _m153a_log.exception("audit_export: build failed")
        _write_meta(success=False, export_id=export_id, fmt=fmt,
                     from_iso=from_iso, to_iso=to_iso,
                     row_counts={'auth_events': n_auth,
                                  'risk_decisions_manual_reset': n_rd},
                     reason='build_failed')
        return jsonify({'ok': False, 'error': 'build_failed'}), 500
    finally:
        conn.close()

    # 6. Redaction scan — fail-fast.
    clean, violations = _ae.scan_for_secrets(body_bytes)
    if not clean:
        _m153a_log.error(
            "audit_export %s: redaction_violation labels=%r — refusing to "
            "return export (no secret values logged or returned)",
            export_id, violations)
        # Fail-fast per Q-C.5: do NOT return the body. Meta-audit the
        # violation with labels-only (no secret values).
        _write_meta(success=False, export_id=export_id, fmt=fmt,
                     from_iso=from_iso, to_iso=to_iso,
                     row_counts={'auth_events': n_auth,
                                  'risk_decisions_manual_reset': n_rd},
                     reason='redaction_violation',
                     redaction_violations=violations)
        return jsonify({
            'ok': False,
            'error': 'redaction_violation',
            'export_id': export_id,
            # Labels-only — never secret values.
            'violation_labels': violations,
            'hint': ('the export was refused because secret-pattern '
                      'substrings were detected in audit data; this '
                      'is defence-in-depth and indicates a bug in '
                      'audit-row writing somewhere upstream'),
        }), 500

    # 7. Meta-audit success row (linked by export_id).
    _write_meta(success=True, export_id=export_id, fmt=fmt,
                 from_iso=from_iso, to_iso=to_iso,
                 row_counts={'auth_events': n_auth,
                              'risk_decisions_manual_reset': n_rd})

    # 8. File download response.
    filename = _ae.make_download_filename(fmt, generated_at)
    resp = Response(body_bytes, status=200, mimetype=content_type)
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="{filename}"')
    resp.headers['X-Export-Id'] = export_id
    resp.headers['X-Export-Sha256'] = manifest['_sha256_payload']
    return resp


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
@csrf_required
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
@csrf_required
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


# ─── M16 historical-data read-only endpoints ─────────────────────────────────
# Reads bot.data.store (which reads data/historical.db + Parquet only).
# No writes. No provider calls. CSRF not needed (GET). Same @require_auth
# as the rest of the dashboard. M16 refresh is operator-CLI only — no
# POST endpoint per D-ε.

@app.route('/api/historical/status')
@require_auth
def m16_historical_status():
    """Returns a 4-key summary for the Observability card."""
    try:
        from bot.historical import schema as _hist_schema
        from bot.historical import store as _hist_store
    except ImportError as e:
        return jsonify({'ok': False, 'error': f'm16 not available: {e}'}), 503

    db_path = _hist_schema.default_db_path()
    if not db_path.exists():
        return jsonify({
            'ok': True,
            'available': False,
            'message': 'historical store not yet initialised',
            'provider': 'yfinance',
            'last_refresh': None,
            'totals': {'symbols_covered': 0, 'timeframes_covered': 0,
                        'coverage_rows': 0},
            'oldest_stale_symbol': None,
            'quality_errors_24h': 0,
        })

    conn = _hist_schema.open_db(db_path)
    try:
        # M16.A.fix-3: idempotent migration BEFORE any SELECT that
        # references v2-shaped columns (symbols_rate_limited). Without
        # this, the endpoint would raise OperationalError ("no such
        # column") when the dashboard is the first thing to touch a
        # pre-v2 historical.db. Matches the cmd_status fix in fix-2.
        _hist_schema.apply_schema(conn)

        last_row = conn.execute(
            "SELECT run_id, started_at_utc, finished_at_utc, mode, status, "
            "       provider, symbols_ok, symbols_no_data, symbols_failed, "
            "       symbols_rate_limited, bars_written, duration_sec, "
            "       rate_limit_count "
            "FROM historical_refresh_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        last_refresh = None
        if last_row:
            last_refresh = {
                'run_id':      last_row[0],
                'started_at':  last_row[1],
                'finished_at': last_row[2],
                'mode':        last_row[3],
                'status':      last_row[4],
                'provider':    last_row[5],
                'symbols_ok':  last_row[6],
                'symbols_no_data': last_row[7],
                'symbols_failed':  last_row[8],
                'symbols_rate_limited': last_row[9],
                'bars_written':    last_row[10],
                'duration_sec':    last_row[11],
                'rate_limit_count': last_row[12],
            }

        n_syms = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM historical_coverage").fetchone()[0]
        n_tfs = conn.execute(
            "SELECT COUNT(DISTINCT timeframe) FROM historical_coverage").fetchone()[0]
        n_cov = conn.execute(
            "SELECT COUNT(*) FROM historical_coverage").fetchone()[0]

        oldest_stale = conn.execute(
            "SELECT symbol, timeframe, last_ts_utc FROM historical_coverage "
            "WHERE freshness_status='stale' "
            "ORDER BY last_ts_utc ASC LIMIT 1").fetchone()
        oldest = None
        if oldest_stale:
            oldest = {'symbol': oldest_stale[0], 'timeframe': oldest_stale[1],
                       'last_ts_utc': oldest_stale[2]}

        since = (datetime.now(timezone.utc) - __import__('datetime').timedelta(
            hours=24)).isoformat()
        errs_24h = conn.execute(
            "SELECT COUNT(*) FROM historical_quality_events "
            "WHERE severity='error' AND created_at_utc >= ?",
            (since,)).fetchone()[0]

        provider = (last_refresh or {}).get('provider', 'yfinance')

        return jsonify({
            'ok': True,
            'available': True,
            'provider': provider,
            'last_refresh': last_refresh,
            'totals': {'symbols_covered': n_syms, 'timeframes_covered': n_tfs,
                        'coverage_rows': n_cov},
            'oldest_stale_symbol': oldest,
            'quality_errors_24h': int(errs_24h),
        })
    finally:
        conn.close()


@app.route('/api/historical/coverage')
@require_auth
def m16_historical_coverage():
    """Per-symbol coverage across all timeframes."""
    try:
        from bot.historical import store as _hist_store
    except ImportError as e:
        return jsonify({'ok': False, 'error': f'm16 not available: {e}'}), 503

    sym = (request.args.get('symbol') or '').strip()
    if not sym:
        # No symbol → list all (capped).
        try:
            symbols = _hist_store.list_symbols()[:200]
        except Exception as e:  # noqa: BLE001
            return jsonify({'ok': False, 'error': str(e)}), 500
        out = []
        for s in symbols:
            cov = _hist_store.get_coverage(s)
            if cov:
                out.append({'symbol': s, 'timeframes': cov})
        return jsonify({'ok': True, 'count': len(out), 'rows': out})

    cov = _hist_store.get_coverage(sym)
    return jsonify({'ok': True, 'symbol': sym.upper(),
                     'timeframes': cov if cov else []})


@app.route('/api/historical/quality-events')
@require_auth
def m16_historical_quality_events():
    """Recent quality events list, newest first."""
    try:
        from bot.historical import store as _hist_store
    except ImportError as e:
        return jsonify({'ok': False, 'error': f'm16 not available: {e}'}), 503

    sym = request.args.get('symbol')
    tf = request.args.get('timeframe')
    severity = request.args.get('severity')
    since = request.args.get('since')
    try:
        limit = min(int(request.args.get('limit') or 100), 500)
    except ValueError:
        limit = 100

    events = _hist_store.list_quality_events(
        symbol=sym, timeframe=tf, severity=severity,
        since_utc=since, limit=limit)
    return jsonify({'ok': True, 'count': len(events), 'events': events})


if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', '8080'))
    # M15.3.A.cutover — bind to _m153a_bind_host (env-controlled via
    # DASHBOARD_BIND_HOST), NOT a hardcoded '0.0.0.0'. The previous
    # hardcoded value ignored the env var and left the dashboard
    # listening on every interface even after the operator set
    # DASHBOARD_BIND_HOST=127.0.0.1 for the Caddy/TLS cutover.
    app.run(host=_m153a_bind_host, port=port, debug=False)
